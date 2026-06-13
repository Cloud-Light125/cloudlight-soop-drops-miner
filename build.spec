# -*- mode: python ; coding: utf-8 -*-
# 在项目根目录执行: pyinstaller soop_miner/build.spec --noconfirm

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "soop_miner" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "soop_miner",
        "soop_miner.__main__",
        "soop_miner.gui",
        "soop_miner.miner",
        "soop_miner.multi_miner",
        "soop_miner.channel",
        "soop_miner.auth",
        "soop_miner.drops",
        "soop_miner.center",
        "soop_miner.watch",
        "soop_miner.models",
        "soop_miner.constants",
        "soop_miner.single_instance",
        "soop_miner.systray",
        "aiohttp",
        "aiohttp.web",
        "multidict",
        "yarl",
        "frozenlist",
        "aiosignal",
        "attrs",
        "idna",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.client",
        "websockets.legacy.protocol",
        "websockets.asyncio",
        "websockets.asyncio.client",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["cookies"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SOOP_Drops_Miner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
