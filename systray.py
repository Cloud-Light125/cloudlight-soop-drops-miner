"""Windows 系统托盘（无额外依赖）。"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable

from .constants import DATA_DIR, WINDOW_TITLE

# 跨进程请求显示主窗口（单实例二次启动时写入）
_SHOW_FLAG = DATA_DIR / ".show_window"

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
IDI_APPLICATION = 32512
MF_STRING = 0x0000
MF_SEPARATOR = 0x0000800
TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTALIGN = 0x0080
TPM_RETURNCMD = 0x0100
ID_SHOW = 1001
ID_EXIT = 1002

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_ERROR_CLASS_ALREADY_EXISTS = 1410
_configured = False


def _configure_win32_api() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    """64 位下为 user32/kernel32 声明 argtypes，避免句柄溢出。"""
    global _configured
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if _configured:
        return user32, kernel32

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND

    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT

    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadIconW.restype = wintypes.HICON

    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None

    _configured = True
    return user32, kernel32


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


class WinSystray:
    """在独立消息线程中维护托盘图标。"""

    def __init__(
        self,
        *,
        tip: str,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self._tip = tip[:127]
        self._on_show = on_show
        self._on_exit = on_exit
        self._thread = threading.Thread(target=self._message_loop, name="Systray", daemon=True)
        self._ready = threading.Event()
        self._hwnd: int = 0
        self._added = False
        self._wndproc_ref = None

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def stop(self) -> None:
        if sys.platform != "win32" or not self._hwnd:
            return
        user32 = ctypes.windll.user32
        user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    def _message_loop(self) -> None:
        user32, kernel32 = _configure_win32_api()
        shell32 = ctypes.windll.shell32

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        def _wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONDBLCLK:
                    self._invoke(self._on_show)
                elif lparam == WM_RBUTTONUP:
                    self._popup_menu(hwnd)
            elif msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == ID_SHOW:
                    self._invoke(self._on_show)
                elif cmd == ID_EXIT:
                    self._invoke(self._on_exit)
            elif msg == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(_wnd_proc)
        class_name = "SoopDropsMinerTrayWnd"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if atom == 0:
            err = ctypes.windll.kernel32.GetLastError()
            if err != _ERROR_CLASS_ALREADY_EXISTS:
                self._ready.set()
                return

        self._hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "SoopDropsMinerTray",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            wc.hInstance,
            None,
        )
        if not self._hwnd:
            self._ready.set()
            return

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = user32.LoadIconW(None, IDI_APPLICATION)
        nid.szTip = self._tip
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._added = True
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _remove_icon(self) -> None:
        if not self._added or not self._hwnd:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self._added = False

    def _popup_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示窗口")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出")
        pos = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(
            menu,
            TPM_BOTTOMALIGN | TPM_RIGHTALIGN | TPM_RETURNCMD,
            pos.x,
            pos.y,
            0,
            hwnd,
            None,
        )
        user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)
        user32.DestroyMenu(menu)

    @staticmethod
    def _invoke(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            pass


def create_systray(
    *,
    tip: str,
    on_show: Callable[[], None],
    on_exit: Callable[[], None],
) -> WinSystray | None:
    if sys.platform != "win32":
        return None
    tray = WinSystray(tip=tip, on_show=on_show, on_exit=on_exit)
    tray.start()
    return tray


def request_show_main_window() -> None:
    """供单实例逻辑调用：请求已有进程恢复主窗口。"""
    try:
        _SHOW_FLAG.write_text("1", encoding="utf-8")
    except OSError:
        pass


def consume_show_request() -> bool:
    try:
        if _SHOW_FLAG.is_file():
            _SHOW_FLAG.unlink(missing_ok=True)
            return True
    except OSError:
        pass
    return False
