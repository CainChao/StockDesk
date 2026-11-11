# -*- coding: utf-8 -*-
"""
StockDesk 数据服务
==================
职责：
  1. 以纯标准库实现本地 HTTP 服务（无第三方依赖）；
  2. 代理并归一化三家公开行情数据源，规避浏览器跨域与 GBK 编码问题；
  3. 提供静态资源与 API 路由，供桌面窗口（Edge App 模式）加载。

数据源：
  - 腾讯财经  qt.gtimg.cn / web.ifzq.gtimg.cn   : 实时报价、分时、日 K
  - 东方财富  datacenter.eastmoney.com          : 公司概况、主要财务指标、主营构成
"""

import ctypes
import gzip
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

try:
    import overlay as taskbar
except Exception:  # 非 Windows 环境降级
    taskbar = None
try:
    import tray as trayapi
except Exception:  # 非 Windows 环境降级
    trayapi = None

if getattr(sys, "frozen", False):
    # PyInstaller 打包运行：资源（static）在解包目录，数据文件与 exe 同目录持久化
    RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR
STATIC_DIR = os.path.join(RESOURCE_DIR, "static")
APP_VERSION = "2.3.1"                # 版本号随功能更新，供 /api/health 与启动清理判断
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# ---------------------------------------------------------------- 基础设施

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, producer):
    """带 TTL 的进程内缓存，降低对上游接口的压力。"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def fetch(url, referer=None, timeout=12):
    """抓取文本，自动处理 gzip 与 GBK/UTF-8 双编码。"""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def fetch_json(url, referer=None, timeout=12):
    text = fetch(url, referer=referer, timeout=timeout).strip()
    # 部分接口返回 jsonp 包裹
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


class ApiError(Exception):
    pass


def to_num(value, default=None):
    try:
        if value is None or value == "" or value == "-":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 代码归一化

def normalize(code):
    """将用户输入归一为 (tencent_code, secucode, em_secid)。"""
    s = (code or "").strip().lower().replace(" ", "")
    s = re.sub(r"^(?:sh|sz|bj)(?=\d{6})", lambda m: m.group(0), s)
    m = re.match(r"^(sh|sz|bj)?(\d{6})$", s)
    if not m:
        raise ApiError("股票代码格式不正确，请输入 6 位数字，如 600519 / 000001 / 300750")
    prefix, digits = m.group(1), m.group(2)
    if not prefix:
        if digits[0] in "69":
            prefix = "sh"
        elif digits[0] in "03":
            prefix = "sz"
        elif digits[0] in "48":
            prefix = "bj"
        else:
            prefix = "sh"
    tencent = prefix + digits
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[prefix]
    secucode = "%s.%s" % (digits, suffix)
    em_secid = ("1." if prefix == "sh" else "0.") + digits
    return tencent, secucode, em_secid


# ---------------------------------------------------------------- 实时报价

def _vol_plain(v):
    """成交量紧凑展示：3.6亿 / 6820万 / 123（单位：手）。"""
    v = v or 0.0
    if v >= 1e8:
        return "%.1f亿" % (v / 1e8)
    if v >= 1e4:
        return "%.0f万" % (v / 1e4)
    return "%d" % int(round(v))


def _m5_vol_by_day(tencent):
    """取最近 5 分钟 K 线，按交易日聚合：{yyyyMMdd: [(HHMM, 成交量), ...]}（时间升序）。"""
    url = ("https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=%s,m5,,320" % tencent)
    data = json.loads(fetch(url))["data"][tencent]
    days = {}
    for b in (data.get("m5") or []):
        try:
            t = str(b[0])
            if len(t) != 12:
                continue
            days.setdefault(t[:8], []).append((t[8:], to_num(b[5], 0) or 0.0))
        except Exception:
            continue
    for k in days:
        days[k].sort()
    return days


def _index_vol_yoy(tencent):
    """指数成交量同比（%）：(今日此时点累计量 - 昨日同时点累计量) / 今日此时点累计量。

    "同一时点"按 5 分钟 K 线收盘时刻对齐：只比较双方都已走完的相同时刻窗口，
    避免盘中拿半天量去比昨天全天量。上游异常或数据不足返回 None。缓存 30 秒。
    """
    def build():
        try:
            days = _m5_vol_by_day(tencent)
        except Exception:
            return None
        if len(days) < 2:
            return None
        keys = sorted(days)                 # 升序，keys[-1] 为最近交易日
        today_key, prev_key = keys[-1], keys[-2]
        today_bars = list(days[today_key])
        # 最后一根 5 分钟 K 线可能仍在生成（标签时刻晚于当前钟点），剔除后再对比
        now = datetime.now()
        if today_key == now.strftime("%Y%m%d") and today_bars and \
                today_bars[-1][0] > now.strftime("%H%M"):
            today_bars = today_bars[:-1]
        if not today_bars:
            return None
        cutoff = today_bars[-1][0]          # 今日已走完的最后一个时刻
        cur = sum(v for _, v in today_bars)
        prev = sum(v for hm, v in days[prev_key] if hm <= cutoff)
        if cur <= 0 or prev <= 0:
            return None
        return round((cur - prev) / cur * 100, 1)

    return cached("volcmp:%s" % tencent, 30, build)


def api_quote(code):
    tencent, secucode, _ = normalize(code)
    # 输入带显式市场前缀（如 sh000300 沪深300）时，回传完整代码，
    # 避免前端回填 6 位裸代码后被再次推断成错误市场（000300 会被当成 sz）
    explicit = bool(re.match(r"^\s*(sh|sz|bj)", (code or "").strip(), re.I))

    def quote_of(tc):
        text = fetch("https://qt.gtimg.cn/q=" + tc)
        m = re.search(r'="([^"]*)"', text)
        if not m or len(m.group(1).split("~")) < 50:
            raise ApiError("未获取到该代码的行情，请确认代码是否存在")
        f = m.group(1).split("~")
        digits = tc[2:]
        sfx = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[tc[:2]]
        # 指数代码特征：沪市 000xxx、深市 399xxx、北交所 899xxx
        is_index = ((tc[:2] == "sh" and digits.startswith("000"))
                    or (tc[:2] == "sz" and digits.startswith("399"))
                    or (tc[:2] == "bj" and digits.startswith("899")))
        vol = to_num(f[6], 0)          # 今日累计成交量（手）
        return {
            # 显式前缀或跨市场回退时回传完整代码，前端回填后才能继续正确取分时/K线
            "code": tc if (explicit or tc != tencent) else f[2],
            "name": f[1],
            "is_index": is_index,
            "vol_yoy": _index_vol_yoy(tc) if is_index else None,
            "market": {"sh": "上证", "sz": "深证", "bj": "北证"}[tc[:2]],
            "price": to_num(f[3], 0) or 0,
            "prev_close": to_num(f[4], 0) or 0,
            "change": to_num(f[31], 0) or 0,
            "pct": to_num(f[32], 0) or 0,
            "open": to_num(f[5], 0),
            "high": to_num(f[33], 0),
            "low": to_num(f[34], 0),
            "volume": vol,                      # 手
            "amount": to_num(f[37], 0),         # 万元
            "turnover": to_num(f[38], 0),       # 换手率 %
            "pe": to_num(f[39]),
            "amplitude": to_num(f[43], 0),      # 振幅 %
            "float_cap": to_num(f[44], 0),      # 流通市值 亿
            "total_cap": to_num(f[45], 0),      # 总市值 亿
            "pb": to_num(f[46]),
            "vol_ratio": to_num(f[49]) if len(f) > 49 else None,
            "time": f[30] if len(f) > 30 else "",
            "suspended": (to_num(f[3], 0) or 0) == 0,
            "secucode": "%s.%s" % (digits, sfx),
        }

    def build():
        try:
            return quote_of(tencent)
        except ApiError:
            if explicit:
                raise
            # 裸 6 位代码推断错市场时回退（如 000300 实为沪市沪深300，而非深市股票）
            alt = ("sz" if tencent.startswith("sh") else "sh") + tencent[2:]
            return quote_of(alt)

    return cached("quote:" + tencent, 5, build)


# ---------------------------------------------------------------- 指数行情

# 常用指数（顺序即前端快捷条展示顺序）
INDEX_LIST = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000016", "上证50"),
    ("bj899050", "北证50"),
]


def api_indices():
    """批量获取常用指数行情，一次上游请求即可。"""
    def build():
        text = fetch("https://qt.gtimg.cn/q=" + ",".join(c for c, _ in INDEX_LIST))
        items = []
        for code, fallback_name in INDEX_LIST:
            m = re.search(r'v_%s="([^"]*)"' % code, text)
            if not m:
                continue
            f = m.group(1).split("~")
            if len(f) < 33:
                continue
            items.append({"code": code, "name": f[1] or fallback_name,
                          "price": to_num(f[3], 0), "change": to_num(f[31], 0),
                          "pct": to_num(f[32], 0)})
        if not items:
            raise ApiError("指数行情暂不可用")
        return {"items": items}

    return cached("indices", 10, build)


# ---------------------------------------------------------------- 分时数据

def api_minute(code):
    tencent, _, _ = normalize(code)

    def build():
        quote = api_quote(code)
        payload = fetch_json("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + tencent)
        node = (payload.get("data") or {}).get(tencent) or {}
        body = node.get("data") or {}
        rows = body.get("data") or []
        if not rows:
            raise ApiError("该代码暂无分时数据（可能停牌或未开盘）")
        times, prices, avgs, vols = [], [], [], []
        prev_vol, prev_amt = 0.0, 0.0
        for row in rows:
            parts = row.split()
            if len(parts) < 3:
                continue
            hhmm, price = parts[0], to_num(parts[1], 0)
            cum_vol = to_num(parts[2], 0)
            cum_amt = to_num(parts[3], 0) if len(parts) > 3 else None
            vol = max(cum_vol - prev_vol, 0)
            if cum_amt is not None:
                avg = cum_amt / (cum_vol * 100) if cum_vol > 0 else price
                prev_amt = cum_amt
            else:
                avg = price if not avgs else (avgs[-1] * prev_vol + price * vol) / max(prev_vol + vol, 1)
            prev_vol = cum_vol
            times.append("%s:%s" % (hhmm[:2], hhmm[2:]))
            prices.append(round(price, 2))
            avgs.append(round(avg, 3))
            vols.append(round(vol, 0))
        return {
            "code": tencent,
            "name": quote["name"],
            "date": body.get("date") or "",
            "prev_close": quote["prev_close"],
            "items": list(zip(times, prices, avgs, vols)),
        }

    return cached("minute:" + tencent, 20, build)


# ---------------------------------------------------------------- 日 K 数据

def api_kline(code, days=320):
    tencent, _, _ = normalize(code)
    days = max(30, min(int(days), 1200))

    def build():
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               "?param=%s,day,,,%d,qfq" % (tencent, days))
        payload = fetch_json(url)
        node = (payload.get("data") or {}).get(tencent) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if node.get("qfqday") is None and not rows:
            rows = node.get("day") or []
        bars = []
        for r in rows:
            if len(r) < 6:
                continue
            bars.append({
                "date": r[0],
                "open": to_num(r[1], 0),
                "close": to_num(r[2], 0),
                "high": to_num(r[3], 0),
                "low": to_num(r[4], 0),
                "volume": to_num(r[5], 0),
            })
        if not bars:
            raise ApiError("该代码暂无日线数据")
        return {"code": tencent, "bars": bars}

    return cached("kline:%s:%d" % (tencent, days), 60, build)


# ---------------------------------------------------------------- 公司概况

PROFILE_LABELS = [
    ("ORG_NAME", "公司全称"), ("EM2016", "所属行业"), ("INDUSTRYCSRC1", "证监会行业"),
    ("SECURITY_TYPE", "上市板块"), ("TRADE_MARKET", "交易市场"), ("CHAIRMAN", "董事长"),
    ("PRESIDENT", "总经理"), ("SECRETARY", "董秘"), ("LEGAL_PERSON", "法人代表"),
    ("REG_CAPITAL", "注册资本"), ("EMP_NUM", "员工人数"), ("ORG_WEB", "公司网址"),
    ("ORG_TEL", "联系电话"), ("ADDRESS", "办公地址"), ("REG_ADDRESS", "注册地址"),
    ("ACCOUNTFIRM_NAME", "会计师事务所"), ("LAW_FIRM", "法律顾问"),
]


def api_profile(code):
    tencent, secucode, _ = normalize(code)

    def build():
        url = ("https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
               "?code=" + tencent.upper())
        payload = fetch_json(url, referer="https://emweb.securities.eastmoney.com/")
        jbzl = (payload.get("jbzl") or [{}])[0]
        fxxg = (payload.get("fxxg") or [{}])[0]
        rows = []
        for key, label in PROFILE_LABELS:
            val = jbzl.get(key)
            if val in (None, "", "-"):
                continue
            if isinstance(val, float):
                val = "{:,.2f}".format(val)
            rows.append({"label": label, "value": str(val)})
        if fxxg.get("LISTING_DATE"):
            rows.append({"label": "上市日期", "value": str(fxxg["LISTING_DATE"])[:10]})
        if fxxg.get("FOUND_DATE"):
            rows.append({"label": "成立日期", "value": str(fxxg["FOUND_DATE"])[:10]})
        if fxxg.get("ISSUE_PRICE"):
            rows.append({"label": "发行价", "value": "%.2f 元" % to_num(fxxg["ISSUE_PRICE"], 0)})
        text = jbzl.get("ORG_PROFILE") or ""
        if text:
            text = re.sub(r"\s+", " ", str(text)).strip()
        return {
            "name": jbzl.get("SECURITY_NAME_ABBR") or "",
            "rows": rows,
            "profile": text,
            "business": re.sub(r"\s+", " ", str(jbzl.get("BUSINESS_SCOPE") or "")).strip(),
        }

    return cached("profile:" + secucode, 3600, build)


# ---------------------------------------------------------------- 财务指标

FIN_FIELDS = [
    ("TOTALOPERATEREVE", "营业总收入", 1e8, "亿元"),
    ("PARENTNETPROFIT", "归母净利润", 1e8, "亿元"),
    ("TOTALOPERATEREVETZ", "营收同比", 1, "%"),
    ("PARENTNETPROFITTZ", "净利同比", 1, "%"),
    ("ROEJQ", "净资产收益率", 1, "%"),
    ("XSMLL", "销售毛利率", 1, "%"),
    ("XSJLL", "销售净利率", 1, "%"),
    ("ZCFZL", "资产负债率", 1, "%"),
    ("EPSJB", "每股收益", 1, "元"),
    ("BPS", "每股净资产", 1, "元"),
    ("MGJYXJJE", "每股经营现金流", 1, "元"),
    ("LD", "流动比率", 1, ""),
]


def api_finance(code):
    _, secucode, _ = normalize(code)

    def build():
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
               "?reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL"
               "&filter=(SECUCODE%3D%22" + urllib.parse.quote(secucode) + "%22)"
               "&pageNumber=1&pageSize=12&sortTypes=-1&sortColumns=REPORT_DATE"
               "&source=HSF10&client=PC")
        payload = fetch_json(url, referer="https://emweb.securities.eastmoney.com/")
        result = payload.get("result") or {}
        data = result.get("data") or []
        if not data:
            raise ApiError("该代码暂无财务数据（ETF / 指数 / 新股可能无财务披露）")
        periods = []
        for item in data:
            metrics = {}
            for key, label, scale, unit in FIN_FIELDS:
                val = to_num(item.get(key))
                metrics[key] = None if val is None else round(val / scale, 2)
            periods.append({
                "period": item.get("REPORT_DATE_NAME") or str(item.get("REPORT_DATE"))[:10],
                "report_date": str(item.get("REPORT_DATE"))[:10],
                "notice_date": str(item.get("NOTICE_DATE") or "")[:10],
                "metrics": metrics,
            })
        latest = periods[0]
        summary = []
        for key, label, _, unit in FIN_FIELDS[:8]:
            summary.append({
                "key": key, "label": label, "unit": unit,
                "value": latest["metrics"].get(key),
            })
        return {"secucode": secucode, "latest_period": latest["period"],
                "periods": periods, "summary": summary}

    return cached("finance:" + secucode, 1800, build)


# ---------------------------------------------------------------- 主营构成

def api_mainop(code):
    _, secucode, _ = normalize(code)

    def build():
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
               "?reportName=RPT_F10_FN_MAINOP"
               "&columns=SECUCODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,MAIN_BUSINESS_INCOME,MBI_RATIO"
               "&filter=(SECUCODE%3D%22" + urllib.parse.quote(secucode) + "%22)"
               "&pageNumber=1&pageSize=30&sortTypes=-1&sortColumns=REPORT_DATE"
               "&source=HSF10&client=PC")
        payload = fetch_json(url, referer="https://emweb.securities.eastmoney.com/")
        data = ((payload.get("result") or {}).get("data")) or []
        if not data:
            return {"date": "", "by_product": [], "by_region": []}
        latest = max(str(x.get("REPORT_DATE"))[:10] for x in data)
        by_product, by_region = [], []
        for item in data:
            if str(item.get("REPORT_DATE"))[:10] != latest:
                continue
            income = to_num(item.get("MAIN_BUSINESS_INCOME"), 0) / 1e8
            ratio = to_num(item.get("MBI_RATIO"), 0) * 100
            row = {"name": item.get("ITEM_NAME"), "income": round(income, 2),
                   "ratio": round(ratio, 2)}
            if str(item.get("MAINOP_TYPE")) == "2":
                by_product.append(row)
            elif str(item.get("MAINOP_TYPE")) == "3":
                by_region.append(row)
        by_product.sort(key=lambda r: -r["income"])
        by_region.sort(key=lambda r: -r["income"])
        return {"date": latest, "by_product": by_product[:8], "by_region": by_region[:8]}

    return cached("mainop:" + secucode, 3600, build)


# ---------------------------------------------------------------- 名称搜索

def api_suggest(q):
    q = (q or "").strip()
    if not q:
        return {"items": []}

    def build():
        url = ("https://smartbox.gtimg.cn/s3/?v=2&q=" + urllib.parse.quote(q) +
               "&t=all&c=1&s=1&p=1&_var=vs_")
        text = fetch(url)
        m = re.search(r'="([^"]*)"', text)
        items = []
        if m:
            for entry in m.group(1).split("^"):
                parts = entry.split("~")
                if len(parts) >= 3 and parts[1].isdigit() and len(parts[1]) == 6:
                    name = parts[2]
                    if "\\u" in name:  # 上游返回 unicode 转义
                        try:
                            name = name.encode("latin-1", "ignore").decode("unicode_escape")
                        except Exception:
                            pass
                    mk = parts[0].lower()
                    full = mk + parts[1] if mk in ("sh", "sz", "bj") else parts[1]
                    kind = parts[4] if len(parts) > 4 else ""
                    items.append({"code": parts[1], "name": name,
                                  "market": mk, "full": full,
                                  "is_index": kind == "ZS"})
        return {"items": items[:8]}

    return cached("suggest:" + q, 600, build)


# ---------------------------------------------------------------- 持仓监控

WATCH_FILE = os.path.join(BASE_DIR, "watchlist.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

# ---------------------------------------------------------------- 开机自启

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "StockDesk"


def _load_setting(key, default=None):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


def _save_setting(key, value):
    try:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[key] = value
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _autostart_cmd():
    """注册表 Run 值：exe 以 --tray-only 启动（只常驻托盘，不打开界面）。"""
    if getattr(sys, "frozen", False):
        return '"%s" --tray-only' % sys.executable
    return '"%s" "%s" --tray-only' % (sys.executable,
                                      os.path.join(BASE_DIR, "server.py"))


def _autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _AUTOSTART_NAME)
        return True
    except Exception:
        return False


def _set_autostart(on):
    """写入/删除 HKCU Run 键（无需管理员权限）。返回设置后的实际状态。"""
    try:
        import winreg
        if on:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _autostart_cmd())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, _AUTOSTART_NAME)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return _autostart_enabled()


def ensure_autostart():
    """首次运行（exe 模式）默认开启开机自启；用户通过托盘菜单关闭后不再自动打开。"""
    if not getattr(sys, "frozen", False):
        return
    pref = _load_setting("autostart")
    if pref is None:                       # 首次运行：默认开启并记录偏好
        _set_autostart(True)
        _save_setting("autostart", True)


class WatchState:
    """持仓监控列表。每项：{"code", "shares", "cost", "quote"}"""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []
        self.updated = 0.0

    def find(self, code):
        for it in self.items:
            if _same_code(it["code"], code):
                return it
        return None


def _code_key(code):
    """代码比较键：(市场前缀, 6 位数字)。兼容 sh000300 / 000300 两种写法。"""
    s = (code or "").strip().lower()
    m = re.match(r"^(sh|sz|bj)?(\d{6})$", s)
    if m:
        return (m.group(1) or "", m.group(2))
    return ("", re.sub(r"\D", "", s)[-6:])


def _same_code(a, b):
    pa, da = _code_key(a)
    pb, db = _code_key(b)
    return bool(da) and da == db and (not pa or not pb or pa == pb)


WATCH = WatchState()
TICKER = (trayapi.TrayTicker(on_click=lambda: _tray_open(),
                             on_menu=lambda a: _tray_menu(a),
                             menu_provider=lambda: _tray_menu_items())
          if trayapi else None)
PROGRESS = trayapi.TaskbarProgress() if trayapi else None
BASE_TITLE = "StockDesk · 股票分析终端"
APP_ISOLATED = False
APP_URL = None         # 主窗口地址，托盘"打开主窗口"时使用


def _load_watch():
    """启动时恢复上次的自选/持仓（watchlist.json）。

    兼容两种存储格式：裸 6 位（601288）与带市场前缀（sh000300 / sh601288）。
    指数必须有前缀（000300 会被误判为深市），统一归一化为 tencent 全码保存。
    """
    try:
        with open(WATCH_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for it in (raw if isinstance(raw, list) else []):
            code = str(it.get("code") or "")
            try:
                canonical, _, _ = normalize(code)     # 校验并补全市场前缀
            except ApiError:
                continue
            shares = max(0.0, to_num(it.get("shares"), 0) or 0)
            cost = max(0.0, to_num(it.get("cost"), 0) or 0)
            WATCH.items.append({"code": canonical, "shares": shares,
                                "cost": cost, "quote": None})
    except Exception:
        pass


def _save_watch():
    try:
        data = [{"code": i["code"], "shares": i["shares"], "cost": i["cost"]}
                for i in WATCH.items]
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _money(v):
    """紧凑金额：+1,234 / -2.5万 / +1.20亿。"""
    v = v or 0.0
    sign = "+" if v > 0 else "-" if v < 0 else ""
    a = abs(v)
    if a >= 100000000:
        return "%s%.2f亿" % (sign, a / 100000000)
    if a >= 10000:
        return "%s%.1f万" % (sign, a / 10000)
    return "%s%s" % (sign, format(int(round(a)), ","))


def _item_view(it):
    """单条持仓 → 展示字段（今日盈亏按昨收，持仓盈亏按成本价）。"""
    q = it.get("quote") or {}
    price = q.get("price") or 0.0
    prev = q.get("prev_close") or 0.0
    shares = it.get("shares") or 0.0
    cost = it.get("cost") or 0.0
    today = (price - prev) * shares if (prev and shares) else 0.0
    hold = (price - cost) * shares if (cost and shares) else 0.0
    hold_pct = (price / cost - 1) * 100 if (cost and price) else 0.0
    return {
        "code": it["code"],
        "name": q.get("name") or it["code"],
        "is_index": bool(q.get("is_index")),
        "volume": q.get("volume") or 0.0,
        "vol_yoy": q.get("vol_yoy"),
        "price": price,
        "pct": q.get("pct") or 0.0,
        "prev_close": prev,
        "shares": shares,
        "cost": cost,
        "today_pnl": round(today, 2),
        "hold_pnl": round(hold, 2),
        "hold_pct": round(hold_pct, 2),
        "market_value": round(price * shares, 2),
    }


def _watch_view():
    with WATCH.lock:
        items = [_item_view(i) for i in WATCH.items]
        updated = WATCH.updated
    mv = sum(i["market_value"] for i in items)
    today = sum(i["today_pnl"] for i in items)
    hold = sum(i["hold_pnl"] for i in items)
    base = mv - today                     # 昨日市值
    if base:
        pct = today / base * 100
    else:
        # 纯自选（含指数，未填股数）时按等权平均涨跌幅，托盘图标/标题才有意义
        vals = [i["pct"] for i in items if i["pct"]]
        pct = sum(vals) / len(vals) if vals else 0.0
    return {
        "items": items,
        "totals": {"market_value": round(mv, 2), "today_pnl": round(today, 2),
                   "hold_pnl": round(hold, 2), "pct": round(pct, 2)},
        "updated": updated,
    }


def _tray_tip(items, totals):
    """托盘悬停提示，每行带现价：
    个股：`名称 现价 涨跌幅 单日收益`（无持仓则省略收益）
    指数：`名称 现价 涨跌 成交量 同比y%`（涨跌为点数，负数自带 -；同比同理）。上限 127 字符。"""
    body = []
    for it in items:
        price = it.get("price") or 0.0
        name = it["name"][:8]
        px = ("%.2f" % price) if price else "--"
        if it.get("is_index") and not it["shares"]:
            chg = price - (it.get("prev_close") or price)
            line = "%s %s %+.2f %s" % (name, px, chg, _vol_plain(it.get("volume")))
            yoy = it.get("vol_yoy")
            if yoy is not None:
                line += " 同比%.1f%%" % yoy
        else:
            line = "%s %s %+.2f%%" % (name, px, it["pct"])
            if it["shares"]:
                line += " %s" % _pnl_plain(it["today_pnl"])
        body.append(line)

    def build(shown):
        parts = body[:shown]
        if shown < len(body):
            parts.append("…等%d只" % len(body))
        return "\n".join(parts)

    tip, n = build(len(body)), len(body)
    while len(tip) > 127 and n > 0:
        n -= 1
        tip = build(n)
    return tip[:127]


def _pnl_plain(v):
    """收益金额：正数不带 + 号，负数带 - 号（如 468 / -1400）。"""
    v = v or 0.0
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 100000000:
        return "%s%.2f亿" % (sign, a / 100000000)
    if a >= 10000:
        return "%s%.1f万" % (sign, a / 10000)
    return "%s%s" % (sign, format(int(round(a)), ","))


def _refresh_watch():
    """刷新全部持仓行情，并推送托盘图标/提示/窗口标题/进度条。"""
    with WATCH.lock:
        codes = [i["code"] for i in WATCH.items]
    quotes = {}
    for code in codes:
        try:
            quotes[code] = api_quote(code)
        except Exception:
            pass
    with WATCH.lock:
        for it in WATCH.items:
            q = quotes.get(it["code"])
            if q:
                it["quote"] = q
        WATCH.updated = time.time()

    data = _watch_view()
    items, totals = data["items"], data["totals"]
    if TICKER:
        if items:
            TICKER.show_tip(_tray_tip(items, totals), totals["pct"])
        else:
            TICKER.show_idle()
    if taskbar:
        if items:
            taskbar.set_app_title("自选%d只 %+.2f%% 今日%s · StockDesk" %
                                  (len(items), totals["pct"], _money(totals["today_pnl"])))
        else:
            taskbar.set_app_title(BASE_TITLE)
        if PROGRESS:
            hwnd = taskbar.find_app_window()
            if hwnd:
                PROGRESS.set(hwnd, totals["pct"]) if items else PROGRESS.clear(hwnd)
    return data


def is_trading_time():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (915 <= hm <= 1135) or (1255 <= hm <= 1505)


def watch_monitor():
    """持仓存在时持续刷新行情；无持仓则保持待机图标常驻。"""
    while True:
        try:
            with WATCH.lock:
                has = bool(WATCH.items)
                age = time.time() - WATCH.updated
            if has:
                if TICKER and not TICKER.healthy:
                    _refresh_watch()
                interval = 5 if is_trading_time() else 60
                if age >= interval:
                    _refresh_watch()
            else:
                if TICKER and not TICKER.healthy:
                    TICKER.show_idle()
        except Exception:
            pass
        time.sleep(1.0)


def watch_add(code, shares="0", cost="0"):
    """加入/更新自选。shares>0 时同时记录持仓并计算盈亏；shares=0 仅监控涨跌。"""
    quote = api_quote(code)                     # 先校验代码有效性
    shares = max(0.0, to_num(shares, 0) or 0)
    cost = max(0.0, to_num(cost, 0) or 0)
    with WATCH.lock:
        it = WATCH.find(quote["code"])
        if it is None:
            it = {"code": quote["code"], "shares": 0.0, "cost": 0.0, "quote": quote}
            WATCH.items.append(it)
        it["shares"] = shares
        it["cost"] = cost
        it["quote"] = quote
        WATCH.updated = time.time()
    _save_watch()
    return _refresh_watch()


def watch_remove(code):
    with WATCH.lock:
        WATCH.items[:] = [i for i in WATCH.items if not _same_code(i["code"], code)]
        WATCH.updated = time.time()
    _save_watch()
    return _refresh_watch()


def watch_clear():
    with WATCH.lock:
        WATCH.items[:] = []
        WATCH.updated = time.time()
    _save_watch()
    return _refresh_watch()


def api_watch(q=None):
    q = q or {}
    action = (q.get("action", [""])[0] or "").lower()
    if action == "add":
        return watch_add(q.get("code", [""])[0],
                         q.get("shares", ["0"])[0],
                         q.get("cost", ["0"])[0])
    if action == "remove":
        return watch_remove(q.get("code", [""])[0])
    if action == "clear":
        return watch_clear()
    return _watch_view()


# 兼容旧接口（旧页面/旧书签仍可用）
def api_pin(code=None, visible=None):
    if code:
        return watch_add(code)
    return _watch_view()


def api_unpin():
    return watch_clear()


# ------------------------------------------------ 托盘菜单（右键）

def _tray_menu_items():
    """根据当前状态生成托盘右键菜单。条目 (动作ID, 文本)，None 为分隔线。"""
    items = []
    alive = bool(taskbar and taskbar.app_alive())
    if not alive:
        items.append((1, "打开主窗口"))
    with WATCH.lock:
        n = len(WATCH.items)
    if n:
        items.append((2, "清空自选（%d 只）" % n))
    items.append((4, "开机自启：%s" % ("已开启" if _autostart_enabled() else "未开启")))
    items.append((None, None))
    items.append((3, "退出"))
    return items


_last_open = 0.0          # 上次开窗时间（冷却用）
_open_lock = threading.Lock()


def _tray_open():
    """托盘左键 / 菜单"打开主窗口"：窗口存在则恢复，否则重新打开（冷却由 open_window 统一控制）。"""
    if taskbar and taskbar.app_alive():
        taskbar.restore_app_window()
        return
    if APP_URL:
        open_window(APP_URL)


def _tray_menu(action):
    if action == 1:
        _tray_open()
    elif action == 2:
        watch_clear()
    elif action == 3:
        _tray_exit()
    elif action == 4:
        on = not _autostart_enabled()
        _set_autostart(on)
        _save_setting("autostart", on)   # 记录偏好：关闭后重启不再自动打开


def _tray_exit():
    """托盘菜单"退出"：移除图标、关闭主窗口、结束服务进程。"""
    def _do():
        try:
            if TICKER:
                TICKER.stop()
            if taskbar:
                taskbar.close_app_window()
        except Exception:
            pass
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_do, name="app-exit", daemon=True).start()


# ---------------------------------------------------------------- HTTP 服务

def _flag(value):
    if value is None or value == "":
        return None
    return 1 if str(value).lower() in ("1", "true", "yes", "on") else 0


ROUTES = {
    "/api/health": lambda q: {"app": "StockDesk", "version": APP_VERSION},
    "/api/quote": lambda q: api_quote(q.get("code", [""])[0]),
    "/api/indices": lambda q: api_indices(),
    "/api/minute": lambda q: api_minute(q.get("code", [""])[0]),
    "/api/kline": lambda q: api_kline(q.get("code", [""])[0], q.get("days", ["320"])[0]),
    "/api/profile": lambda q: api_profile(q.get("code", [""])[0]),
    "/api/finance": lambda q: api_finance(q.get("code", [""])[0]),
    "/api/mainop": lambda q: api_mainop(q.get("code", [""])[0]),
    "/api/suggest": lambda q: api_suggest(q.get("q", [""])[0]),
    "/api/pin": lambda q: api_pin(code=q.get("code", [None])[0] or None,
                                  visible=_flag(q.get("visible", [None])[0])),
    "/api/unpin": lambda q: api_unpin(),
    "/api/watch": lambda q: api_watch(q),
}

POST_PATHS = {"/api/pin", "/api/unpin", "/api/overlay", "/api/watch"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, fmt, *args):  # 静默日志，避免控制台噪音
        pass

    def end_headers(self):
        # 界面资源永不缓存：升级后浏览器总是加载最新页面
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ROUTES:
            self._serve_api(parsed.path, urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_index()
            return
        return super().do_GET()

    def _serve_index(self):
        """返回主页：把页面内的 __APP_VERSION__ 占位符替换为服务端实际版本，
        保证版本自检单一数据源，杜绝"页面常量落后于服务端版本"导致的无限刷新。"""
        try:
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                body = f.read().replace(b"__APP_VERSION__", APP_VERSION.encode("utf-8"))
        except Exception:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = "/api/overlay" if parsed.path == "/api/overlay" else parsed.path
        if path in POST_PATHS:
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/api/overlay":
                path = "/api/pin"
            self._serve_api(path, query)
            return
        self.send_error(404)

    def _serve_api(self, path, query):
        try:
            data = ROUTES[path](query)
            body = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
            status = 200
        except ApiError as exc:
            body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
            status = 200
        except Exception as exc:  # 网络异常、上游变更等
            body = json.dumps({"ok": False, "error": "数据获取失败：%s" % exc},
                              ensure_ascii=False).encode("utf-8")
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def pick_port(start=8765):
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def kill_previous_instance():
    """清理默认端口上的旧实例：确认是本应用后结束其进程，保证新启动总是最新代码。"""
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=8).stdout
    except Exception:
        return
    pids = set()
    for line in out.splitlines():
        if ":8765 " in line and "LISTENING" in line:
            try:
                pids.add(int(line.split()[-1]))
            except (ValueError, IndexError):
                pass
    me = os.getpid()
    for pid in pids:
        if pid == me:
            continue
        # 先探测是否为本应用（避免误杀占用端口的其他程序）
        is_ours = False
        try:
            req = urllib.request.Request("http://127.0.0.1:8765/api/health")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                is_ours = json.loads(resp.read().decode("utf-8")).get("app") == "StockDesk"
        except Exception:
            is_ours = False
        if not is_ours:
            # 旧版本没有 /api/health：按进程名识别（本应用 exe 或其 Python 后备）
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV"],
                    capture_output=True, text=True, timeout=8).stdout
                image = out.strip().splitlines()[-1].split('","')[0].strip('"')
                is_ours = image.lower() in ("stockdesk.exe", "pythonw.exe", "python.exe")
            except Exception:
                is_ours = False
        if is_ours:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8)
            print("已结束旧实例 PID %d" % pid, flush=True)
    if pids:
        time.sleep(0.8)                   # 等端口释放


def close_stale_app_windows():
    """关闭残留的旧应用窗口（Edge App 窗口标题含 StockDesk），避免聚焦到旧页面。"""
    user32 = ctypes.WinDLL("user32") if sys.platform == "win32" else None
    if not user32:
        return
    from ctypes import wintypes as w
    mine = os.getpid()
    targets = []

    def _cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(200)
            user32.GetWindowTextW(hwnd, buf, 200)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if ("StockDesk" in buf.value
                    and cls.value.startswith("Chrome_WidgetWin")):
                pid = w.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != mine:
                    targets.append(hwnd)
        return True

    CMPFUNC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    cb_ref = CMPFUNC(_cb)                 # 保活引用，防止回调对象被 GC 引发崩溃
    user32.EnumWindows(cb_ref, 0)
    for hwnd in targets:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE，温和关闭
    if targets:
        print("已关闭 %d 个残留窗口" % len(targets), flush=True)


def open_window(url):
    """以无地址栏的应用窗口打开，获得原生桌面程序观感。

    加锁 + 4 秒冷却：Edge 窗口从发起启动到真正出现约需 1~3 秒，
    期间 app_alive() 仍为 False，托盘快速连点/双击会重复 Popen 导致开多个窗口；
    冷却期内所有重复调用直接忽略（启动开窗与托盘开窗统一走这里）。
    """
    global _last_open, APP_ISOLATED
    with _open_lock:
        if time.monotonic() - _last_open < 4:
            return                      # 冷却期内：窗口正在打开，忽略重复调用
        _last_open = time.monotonic()
        edge_paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    profile = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "StockDesk", "winprofile")
    for exe in edge_paths:
        if os.path.exists(exe):
            try:
                import subprocess
                subprocess.Popen([exe, "--app=" + url,
                                  "--window-size=1440,960",
                                  "--user-data-dir=" + profile,
                                  "--no-first-run",
                                  "--disable-http-cache",   # 禁用 HTTP 磁盘缓存，杜绝旧页面
                                  "--disable-features=msEdgeSidebarV2"],
                                 close_fds=True)
                APP_ISOLATED = True
                return
            except Exception:
                continue
    webbrowser.open(url)


def main():
    global APP_ISOLATED, APP_URL
    port = None
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg == "--isolated":
            APP_ISOLATED = True
    if not port:
        kill_previous_instance()          # 一次点击总是得到最新版本
        port = pick_port()
    # URL 带版本号+启动时间戳：Edge 按完整 URL 取缓存，每次启动 URL 不同，
    # 保证应用窗口永远加载本实例的最新页面，不会命中旧缓存
    APP_URL = url = ("http://127.0.0.1:%d/?v=%s&b=%d"
                     % (port, APP_VERSION, int(time.time())))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    _load_watch()                        # 恢复上次持仓
    threading.Thread(target=watch_monitor, name="watch-monitor", daemon=True).start()
    print("StockDesk v%s 已启动：%s" % (APP_VERSION, url), flush=True)
    if TICKER:
        if WATCH.items:
            _refresh_watch()
        else:
            TICKER.show_idle()           # 启动即常驻任务栏
    ensure_autostart()                   # 首次运行默认开启开机自启
    # --no-window：开发/测试用；--tray-only：开机自启用，仅常驻托盘不打开界面
    if "--no-window" not in sys.argv and "--tray-only" not in sys.argv:
        close_stale_app_windows()        # 先关掉旧窗口，Edge 才不会聚焦旧页面
        threading.Timer(1.0, lambda: open_window(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
