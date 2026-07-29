"""Windows 系统托盘（无额外依赖）。"""
from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
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
MF_GRAYED = 0x0001
MF_SEPARATOR = 0x0000800
TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTALIGN = 0x0080
TPM_RETURNCMD = 0x0100
ID_SHOW = 1001
ID_START_ALL = 1002
ID_STOP_ALL = 1003
ID_EXIT = 1004

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_ERROR_CLASS_ALREADY_EXISTS = 1410
_configured = False


@dataclass(frozen=True, slots=True)
class TrayMenuState:
    account_count: int = 0
    running_count: int = 0
    proxy_enabled: bool = False
    low_bandwidth_mode: bool = True
    busy: bool = False

    @property
    def can_start(self) -> bool:
        return self.account_count > 0 and self.running_count == 0 and not self.busy

    @property
    def can_stop(self) -> bool:
        return self.running_count > 0 or self.busy


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

    user32.CreatePopupMenu.argtypes = []
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPCWSTR,
    ]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

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
        on_start_all: Callable[[], None],
        on_stop_all: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self._tip = tip[:127]
        self._on_show = on_show
        self._on_start_all = on_start_all
        self._on_stop_all = on_stop_all
        self._on_exit = on_exit
        self._thread = threading.Thread(target=self._message_loop, name="Systray", daemon=True)
        self._ready = threading.Event()
        self._hwnd: int = 0
        self._added = False
        self._wndproc_ref = None
        self._state = TrayMenuState()
        self._state_lock = threading.Lock()

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
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=3.0)

    def update_state(self, state: TrayMenuState) -> None:
        with self._state_lock:
            self._state = state

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
                elif cmd == ID_START_ALL:
                    self._invoke(self._on_start_all)
                elif cmd == ID_STOP_ALL:
                    self._invoke(self._on_stop_all)
                elif cmd == ID_EXIT:
                    self._invoke(self._on_exit)
            elif msg == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(_wnd_proc)
        class_name = "CloudLightSoopDropsMinerTrayWnd"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = kernel32.GetModuleHandleW(None)
        app_icon = user32.LoadIconW(wc.hInstance, ctypes.c_void_p(1))
        if not app_icon:
            app_icon = user32.LoadIconW(None, IDI_APPLICATION)
        wc.hIcon = app_icon
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
            "CloudLightSoopDropsMinerTray",
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
        nid.hIcon = app_icon
        nid.szTip = self._tip
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._added = True
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = 0

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
        with self._state_lock:
            state = self._state
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示主窗口")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(
            menu,
            MF_STRING if state.can_start else MF_STRING | MF_GRAYED,
            ID_START_ALL,
            "开始全部账号",
        )
        user32.AppendMenuW(
            menu,
            MF_STRING if state.can_stop else MF_STRING | MF_GRAYED,
            ID_STOP_ALL,
            "停止全部账号",
        )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, f"运行中：{state.running_count} 个账号")
        user32.AppendMenuW(
            menu,
            MF_STRING | MF_GRAYED,
            0,
            f"代理：{'已启用' if state.proxy_enabled else '未启用'}",
        )
        user32.AppendMenuW(
            menu,
            MF_STRING | MF_GRAYED,
            0,
            f"低流量模式：{'已启用' if state.low_bandwidth_mode else '未启用'}",
        )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出程序")
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
        if cmd:
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
    on_start_all: Callable[[], None],
    on_stop_all: Callable[[], None],
    on_exit: Callable[[], None],
) -> WinSystray | None:
    if sys.platform != "win32":
        return None
    tray = WinSystray(
        tip=tip,
        on_show=on_show,
        on_start_all=on_start_all,
        on_stop_all=on_stop_all,
        on_exit=on_exit,
    )
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
