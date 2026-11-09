# -*- coding: utf-8 -*-
"""
主窗口工具（Win32 / ctypes）
==========================
仅负责定位浏览器应用窗口、恢复窗口与改写窗口标题。
任务栏股价展示见 tray.py（通知区域图标 + 任务栏进度条），不使用悬浮窗口。
"""

import ctypes
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsWindowVisible.restype = w.BOOL
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]


def find_app_window(keyword="StockDesk"):
    """定位浏览器应用窗口（Edge/Chrome App 模式）。最小化窗口同样视为存在。"""
    found = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def _cb(hwnd, _lp):
        title = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(hwnd, title, 300)
        if keyword in title.value:
            cls = ctypes.create_unicode_buffer(120)
            user32.GetClassNameW(hwnd, cls, 120)
            if cls.value.startswith(("Chrome_WidgetWin", "ApplicationFrameWindow")):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def app_alive(keyword="StockDesk"):
    """应用窗口是否仍然存在（关闭窗口后返回 False）。"""
    return find_app_window(keyword) is not None


def restore_app_window(keyword="StockDesk"):
    hwnd = find_app_window(keyword)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    return False


def close_app_window(keyword="StockDesk"):
    """请求关闭应用窗口（托盘"退出"时调用）。"""
    hwnd = find_app_window(keyword)
    if hwnd:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return True
    return False


def set_app_title(text, keyword="StockDesk"):
    """把实时价格写入窗口标题：任务栏按钮 / 悬停预览 / Alt+Tab 均可读到。"""
    hwnd = find_app_window(keyword)
    if hwnd:
        try:
            user32.SetWindowTextW(hwnd, text)
            return True
        except Exception:
            return False
    return False
