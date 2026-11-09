# -*- coding: utf-8 -*-
"""
任务栏股价展示（无浮窗）
======================
两条通道，均由 Windows 任务栏原生承载：

1. 任务栏按钮进度条（ITaskbarList3）
   - 用当日涨跌幅映射进度宽度，涨为红色、跌为绿色，最小化后依然可见；
   - 窗口未合并时配合窗口标题可直接读出价格。
2. 通知区域图标（Shell_NotifyIcon）
   - 常驻任务栏右下角，图标底色随涨跌变化，鼠标悬停显示
     「名称 代码 现价 涨跌幅」；左键点击恢复主窗口，右键取消监控。

全部使用 ctypes 调用系统 API，无第三方依赖。
"""

import ctypes
import ctypes.wintypes as w
import threading
import time
import uuid

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

WS_POPUP = 0x80000000
WM_DESTROY, WM_QUIT, WM_CLOSE, WM_NULL = 0x0002, 0x0012, 0x0010, 0x0000
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
PM_REMOVE = 0x0001

# 弹出菜单
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4

TBPF_NOPROGRESS, TBPF_NORMAL, TBPF_ERROR, TBPF_PAUSED = 0x0, 0x2, 0x4, 0x8


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @staticmethod
    def from_str(text):
        u = uuid.UUID(text)
        g = GUID()
        g.Data1 = u.time_low
        g.Data2 = u.time_mid
        g.Data3 = u.time_hi_version
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
                ("uFlags", w.UINT), ("uCallbackMessage", w.UINT), ("hIcon", w.HICON),
                ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
                ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", w.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


# ---------------- 签名 ----------------
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
user32.PeekMessageW.restype = w.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.IsWindow.argtypes = [w.HWND]
user32.IsWindow.restype = w.BOOL
user32.DestroyWindow.argtypes = [w.HWND]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.LoadCursorW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = w.HANDLE
user32.GetDC.argtypes = [w.HWND]
user32.GetDC.restype = w.HDC
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.DestroyIcon.argtypes = [w.HICON]
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HINSTANCE

shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = w.BOOL

gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.DeleteDC.argtypes = [w.HDC]
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.CreateSolidBrush.argtypes = [w.DWORD]
gdi32.CreateSolidBrush.restype = w.HBRUSH
gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
gdi32.CreateDIBSection.restype = w.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = w.HBITMAP
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = w.HICON
gdi32.RoundRect.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.Polygon.argtypes = [w.HDC, ctypes.POINTER(w.POINT), ctypes.c_int]
gdi32.SetBkMode.argtypes = [w.HDC, ctypes.c_int]

user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.CreatePopupMenu.restype = w.HMENU
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.TrackPopupMenu.restype = w.BOOL
user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, w.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [w.HMENU]


def rgb(r, g, b):
    return r | (g << 8) | (b << 16)


C_UP = rgb(214, 62, 62)
C_DOWN = rgb(28, 160, 90)
C_FLAT = rgb(110, 122, 138)
WHITE = rgb(255, 255, 255)


def make_icon(pct):
    """绘制 32x32 托盘图标：红/绿底 + 白色涨跌箭头；pct=None 为灰色待机图标。"""
    size = 32
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = size
    bi.bmiHeader.biHeight = -size
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(screen, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    old = gdi32.SelectObject(mem, color)

    if pct is None:
        color_ref = C_FLAT
    else:
        color_ref = C_UP if pct > 0 else C_DOWN if pct < 0 else C_FLAT
    brush = gdi32.CreateSolidBrush(color_ref)
    ob = gdi32.SelectObject(mem, brush)
    gdi32.RoundRect(mem, 0, 0, size, size, 10, 10)

    # 箭头（待机态不绘制）
    if pct is None:
        pts = None
    elif pct >= 0:
        pts = [(16, 6), (25, 18), (19, 18), (19, 26), (13, 26), (13, 18), (7, 18)]
    else:
        pts = [(16, 26), (25, 14), (19, 14), (19, 6), (13, 6), (13, 14), (7, 14)]
    if pts:
        arr = (w.POINT * len(pts))(*[w.POINT(x, y) for x, y in pts])
        wb = gdi32.CreateSolidBrush(WHITE)
        gdi32.SelectObject(mem, wb)
        gdi32.Polygon(mem, arr, len(pts))
        gdi32.DeleteObject(wb)

    gdi32.SelectObject(mem, ob)
    gdi32.DeleteObject(brush)
    gdi32.SelectObject(mem, old)

    info = ICONINFO()
    info.fIcon = True
    info.hbmMask = mask
    info.hbmColor = color
    hicon = user32.CreateIconIndirect(ctypes.byref(info))

    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    return hicon


class TrayTicker:
    """通知区域图标（常驻）：锁定时显示股价，未锁定时显示待机图标；
    右键弹出菜单（打开主窗口 / 取消锁定 / 退出）。"""

    def __init__(self, on_click=None, on_menu=None, menu_provider=None):
        self._on_click = on_click
        self._on_menu = on_menu
        self._menu_provider = menu_provider
        self._lock = threading.Lock()
        self._text = None          # (title, tip)
        self._pct = 0.0
        self._hwnd = None
        self._icon = None
        self._thread = None
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._alive = threading.Event()

    # ------- 对外 -------
    @property
    def healthy(self):
        return self._alive.is_set()

    def show(self, name, code, price, pct):
        with self._lock:
            self._text = (name or "--", "%s %s  %.2f  %+.2f%%" %
                          (name or "", code or "", price or 0, pct or 0))
            self._pct = pct or 0.0
        self._ensure_started()

    def show_idle(self, tip="StockDesk · 点击打开主窗口，右键更多操作"):
        """未锁定时的待机图标（灰色方块）。"""
        with self._lock:
            self._text = ("StockDesk", tip)
            self._pct = None
        self._ensure_started()

    def show_tip(self, tip, pct=0.0):
        """直接设置悬停提示（支持多行 \n）与图标涨跌配色。"""
        with self._lock:
            self._text = ("StockDesk", tip)
            self._pct = pct
        self._ensure_started()

    def hide(self):
        """移除图标（取消锁定）。"""
        hwnd = self._hwnd
        if hwnd:
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = hwnd
            data.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        with self._lock:
            self._text = None

    def stop(self):
        self.hide()
        if self._hwnd:
            user32.PostMessageW(self._hwnd, 0x0010, 0, 0)

    # ------- 内部 -------
    def _ensure_started(self):
        with self._start_lock:
            t = self._thread
            if t and t.is_alive():
                self._ready.wait(2.0)      # 线程仍在运行（可能尚未就绪），不重复启动
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="tray-ticker", daemon=True)
            self._thread.start()
        self._ready.wait(2.0)

    def _run(self):
        try:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = "StockDeskTrayWnd"
            self._wndproc_ref = WNDPROC(self._wndproc)

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
            wc.lpszClassName = cls_name
            user32.RegisterClassW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0, cls_name, "StockDeskTray", WS_POPUP,
                0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                self._ready.set()
                return
            self._hwnd = hwnd
            self._alive.set()
            self._ready.set()

            msg = w.MSG()
            while True:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        self._alive.clear()
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                if not user32.IsWindow(hwnd):
                    break
                with self._lock:
                    text = self._text
                    pct = self._pct
                if text:
                    self._update(text, pct)
                time.sleep(0.3)
            self._alive.clear()
        except Exception:
            self._alive.clear()
            try:
                import os, traceback
                log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "tray_error.log")
                with open(log, "a", encoding="utf-8") as f:
                    f.write("---- %s ----\n" % time.strftime("%F %T"))
                    traceback.print_exc(file=f)
            except Exception:
                pass

    def _update(self, text, pct):
        """增量更新图标与提示文本。"""
        title, tip = text
        key = (title, tip, pct)
        if getattr(self, "_last", None) == key:
            return
        self._last = key

        new_icon = make_icon(pct)
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = new_icon
        data.szTip = tip[:127]

        if self._icon is None:
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        else:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
            user32.DestroyIcon(self._icon)
        self._icon = new_icon

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._fire(self._on_click)
                elif lparam == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if msg == WM_DESTROY:
                user32.PostMessageW(hwnd, WM_QUIT, 0, 0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        """右键弹出菜单：由 menu_provider 提供条目，on_menu 处理选择。"""
        items = (self._menu_provider() if self._menu_provider
                 else [("exit", "退出")])
        if not items:
            return
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)
        menu = user32.CreatePopupMenu()
        for action, label in items:
            if action is None:                       # 分隔线
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, 0, int(action), label)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
            pt.x, pt.y, 0, self._hwnd, None)
        user32.DestroyMenu(menu)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        if cmd:
            self._fire(lambda: self._on_menu(int(cmd)))

    @staticmethod
    def _fire(cb):
        if cb:
            try:
                cb()
            except Exception:
                pass


class TaskbarProgress:
    """任务栏按钮进度条：用涨跌幅表示幅度，红涨绿跌。"""

    CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
    IID = "{EA1AFB91-9E28-4B86-90E9-C8557ABC8EC0}"

    def __init__(self):
        self._ptr = None
        self._failed = False
        self._lock = threading.Lock()

    def _ensure(self):
        if self._ptr or self._failed:
            return self._ptr is not None
        try:
            ole32.CoInitialize(None)
            clsid, iid = GUID.from_str(self.CLSID), GUID.from_str(self.IID)
            ptr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                        ctypes.byref(iid), ctypes.byref(ptr))
            if hr != 0 or not ptr:
                self._failed = True
                return False
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
            if init(ptr) != 0:
                self._failed = True
                return False
            self._ptr = ptr
            self._vtbl = vtbl
            return True
        except Exception:
            self._failed = True
            return False

    def _call(self, index, argtypes, *args):
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(self._vtbl[index])
        return fn(self._ptr, *args)

    def set(self, hwnd, pct):
        if not hwnd:
            return
        with self._lock:
            if not self._ensure():
                return
            try:
                # 涨跌幅 -10% ~ +10% 映射到 0 ~ 100，0% 为 50
                ratio = max(-10.0, min(10.0, float(pct or 0))) / 10.0
                value = int(round((ratio + 1) * 5000))
                state = TBPF_ERROR if pct > 0 else TBPF_NORMAL if pct < 0 else TBPF_PAUSED
                self._call(10, [w.HWND, ctypes.c_int], hwnd, state)
                self._call(9, [w.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong],
                           hwnd, value, 10000)
            except Exception:
                pass

    def clear(self, hwnd):
        if not hwnd or not self._ptr:
            return
        try:
            self._call(10, [w.HWND, ctypes.c_int], hwnd, TBPF_NOPROGRESS)
        except Exception:
            pass
