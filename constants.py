from __future__ import annotations

import sys
from pathlib import Path

SEP = "\x06"

LOGIN_URL = "https://login.sooplive.co.kr/app/LoginAction.php"
DROPS_ORIGIN = "https://drops.sooplive.com"
DROPS_API = f"{DROPS_ORIGIN}/api"
GATHER_URL = "https://exlogcollector.sooplive.com/gather"
PLAY_ORIGIN = "https://play.sooplive.com"
LIVE_API = "https://live.sooplive.co.kr/afreeca/player_live_api.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

def _resolve_data_dir() -> Path:
    # 打包后 cookies 保存在 exe 同目录，不会打进安装包
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DATA_DIR = _resolve_data_dir()
COOKIES_PATH = DATA_DIR / "cookies.json"

APP_NAME = "SOOP Drops Miner"
WINDOW_TITLE = APP_NAME

HEARTBEAT_INTERVAL = 5.0
MISSION_POLL_INTERVAL = 60.0
INVENTORY_POLL_INTERVAL = 120.0
