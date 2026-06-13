"""Windows 单实例：重复启动时激活已有窗口，而非再开一个新进程。"""
from __future__ import annotations

import sys

from .constants import APP_NAME, WINDOW_TITLE

# 进程存活期间保持 mutex 句柄，避免被 GC 释放后允许多开
_mutex_handle: int | None = None

_MUTEX_NAME = "Global\\SOOP_Drops_Miner_SingleInstance"
_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9
_SW_SHOW = 5
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2


def ensure_single_instance_or_exit() -> bool:
    """
    若已有 GUI 实例在运行，则将其窗口置前并返回 False（当前进程应退出）。
    若为本实例首次启动，返回 True。
    非 Windows 平台始终返回 True。
    """
    if sys.platform != "win32":
        return True

    if not _try_acquire_mutex():
        _activate_existing_window()
        return False
    return True


def _try_acquire_mutex() -> bool:
    global _mutex_handle
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not handle:
        return True
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def _activate_existing_window() -> None:
    import ctypes
    from ctypes import wintypes

    from .systray import request_show_main_window

    request_show_main_window()

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        hwnd = _find_window_by_title(user32, APP_NAME)

    if not hwnd:
        return

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, _SW_SHOW)

    # 短暂置顶再取消，提高被系统带到前台的成功率
    user32.SetWindowPos(
        hwnd,
        _HWND_TOPMOST,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE | _SWP_NOSIZE,
    )
    user32.SetWindowPos(
        hwnd,
        _HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE | _SWP_NOSIZE,
    )

    foreground = user32.GetForegroundWindow()
    if foreground != hwnd:
        fg_thread = user32.GetWindowThreadProcessId(foreground, None)
        current_thread = kernel32.GetCurrentThreadId()
        attached = False
        if fg_thread and fg_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, fg_thread, True))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, fg_thread, False)


def _find_window_by_title(user32: object, title_part: str) -> int:
    import ctypes
    from ctypes import wintypes

    found: list[int] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_part in buf.value:
            found.append(hwnd)
            return False
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_callback)
    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else 0
