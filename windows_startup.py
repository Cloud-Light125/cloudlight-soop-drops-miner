from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from .constants import APP_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = APP_NAME


def _quote(path: str | Path) -> str:
    return f'"{str(Path(path).resolve())}"'


def get_auto_start_command(
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    entry_path: str | Path | None = None,
) -> str:
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable_path = Path(executable or sys.executable).resolve()
    if frozen:
        return f"{_quote(executable_path)} --tray"

    pythonw = executable_path.with_name("pythonw.exe")
    if not pythonw.is_file():
        pythonw = executable_path
    entry = Path(entry_path or (Path(__file__).resolve().parent / "entry.py")).resolve()
    return f"{_quote(pythonw)} {_quote(entry)} --tray"


def _read_auto_start_value() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None


def is_auto_start_enabled() -> bool:
    return _read_auto_start_value() is not None


def enable_auto_start() -> None:
    if sys.platform != "win32":
        raise RuntimeError("开机自启仅支持 Windows")
    import winreg

    command = get_auto_start_command()
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)


def disable_auto_start() -> None:
    if sys.platform != "win32":
        raise RuntimeError("开机自启仅支持 Windows")
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, STARTUP_VALUE_NAME)
    except FileNotFoundError:
        return


def sync_auto_start_setting(enabled: bool | None = None) -> bool:
    if sys.platform != "win32":
        return False
    actual = is_auto_start_enabled()
    if enabled is True:
        enable_auto_start()
        return True
    if enabled is False:
        disable_auto_start()
        return False
    if actual and _read_auto_start_value() != get_auto_start_command():
        enable_auto_start()
    return actual


def reconcile_auto_start_state(settings):
    """Registry is authoritative; refresh stale commands when the value exists."""
    if sys.platform != "win32":
        return replace(settings, auto_start_enabled=False)
    actual = sync_auto_start_setting(None)
    return replace(settings, auto_start_enabled=actual)


def apply_auto_start_setting(settings, enabled: bool, *, settings_path=None):
    """Apply registry state first, then persist; roll registry back on save failure."""
    from .config import SETTINGS_PATH, save_settings

    target_path = settings_path or SETTINGS_PATH
    previous = bool(settings.auto_start_enabled)
    if enabled:
        enable_auto_start()
    else:
        disable_auto_start()
    try:
        return save_settings(replace(settings, auto_start_enabled=enabled), target_path)
    except Exception:
        try:
            if previous:
                enable_auto_start()
            else:
                disable_auto_start()
        except Exception:
            pass
        raise
