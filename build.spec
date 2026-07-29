# -*- mode: python ; coding: utf-8 -*-
# 在当前仓库根目录执行: pyinstaller build.spec --noconfirm --clean

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve()
SOURCE_NAMES = [
    "__init__.py",
    "__main__.py",
    "auth.py",
    "center.py",
    "channel.py",
    "config.py",
    "constants.py",
    "drops.py",
    "gui.py",
    "ui_components.py",
    "ui_state.py",
    "ui_theme.py",
    "miner.py",
    "models.py",
    "modern_gui.py",
    "multi_miner.py",
    "network.py",
    "settings_dialog.py",
    "single_instance.py",
    "stream.py",
    "systray.py",
    "watch.py",
    "windows_startup.py",
    "soop.png",
]

a = Analysis(
    [str(ROOT / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / name), ".") for name in SOURCE_NAMES] + collect_data_files("customtkinter"),
    hiddenimports=[
        "aiohttp",
        "aiohttp.web",
        "multidict",
        "yarl",
        "frozenlist",
        "aiosignal",
        "attrs",
        "idna",
        "tkinter",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "tkinter.ttk",
        "customtkinter",
        "_tkinter",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CloudLight_SOOP_Drops_Miner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "soop.png"),
)
