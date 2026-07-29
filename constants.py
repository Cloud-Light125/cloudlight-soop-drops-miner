from __future__ import annotations

import sys
from pathlib import Path

SEP = "\x06"

LOGIN_URL = "https://login.sooplive.co.kr/app/LoginAction.php"
DROPS_ORIGIN = "https://drops.sooplive.com"
DROPS_MISSION_URL = f"{DROPS_ORIGIN}/mission"
DROPS_EVENT_URL = f"{DROPS_ORIGIN}/event"
DROPS_INVENTORY_URL = f"{DROPS_ORIGIN}/inventory"
DROPS_API = f"{DROPS_ORIGIN}/api"
GATHER_URL = "https://exlogcollector.sooplive.com/gather"
PLAY_ORIGIN = "https://play.sooplive.com"
LIVE_API = "https://live.sooplive.co.kr/afreeca/player_live_api.php"
SEARCH_API = "https://sch.sooplive.com/api.php"
DROPS_HASHTAG = "드롭스"
DROPS_HASHTAG_SEARCH_REFERER = (
    "https://www.sooplive.com/search"
    "?hash=hashtag&tagname=%EB%93%9C%EB%A1%AD%EC%8A%A4"
    "&hashtype=live&stype=hash&acttype=live&location=drops"
)

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
ACCOUNTS_DIR = DATA_DIR / "accounts"
COOKIES_PATH = DATA_DIR / "cookies.json"  # 旧版单账号，启动时自动迁移


def resource_path(name: str) -> Path:
    """返回源码目录或 PyInstaller 临时解包目录中的只读资源。"""
    bundled_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundled_root) if bundled_root else Path(__file__).resolve().parent
    return root / name

APP_NAME = "CloudLight SOOP Drops Miner"
VERSION = "1.0"
AUTHOR = "cloudlight"
AUTHOR_BY = f"by {AUTHOR}"
PAGE_SUBTITLE = "SOOP 掉宝多账号管理工具"
GITHUB_REPOSITORY_URL = "https://github.com/yundan125/cloudlight-soop-drops-miner"
LICENSE_NAME = "未声明独立许可证（保留原仓库声明）"
DEFAULT_CHANNEL_BJID = "owesports"
WINDOW_TITLE = f"{APP_NAME} {VERSION}"
APP_ICON_PATH = resource_path("soop.png")
DISCLAIMER_ACCEPTED_PATH = DATA_DIR / ".disclaimer_accepted"

DISCLAIMER_TEXT = """CloudLight SOOP Drops Miner 免责说明

1. 本软件为第三方辅助工具，与 SOOP Live / AfreecaTV 官方无任何关联，亦未获官方授权或认可。

2. 使用本软件可能违反平台服务条款。账号受限、封号、奖励失效等风险由使用者自行承担。

3. 本软件按「现状」提供，不对挂机成功率、掉宝进度、奖励领取结果作任何保证。

4. 请妥善保管账号密码与本地登录信息，勿向他人泄露。因泄露或不当使用造成的损失，开发者不承担责任。

5. 仅限个人学习与交流使用。向他人分发本软件时，请一并告知以上条款。

使用本软件即表示您已阅读并理解上述内容。"""

HEARTBEAT_INTERVAL = 5.0
MISSION_POLL_INTERVAL = 60.0
INVENTORY_POLL_INTERVAL = 120.0

# aiohttp 默认无超时，网络异常时可能长时间挂起；gather 心跳超时会导致整账号挂机退出
HTTP_CONNECT_TIMEOUT = 15.0
HTTP_TOTAL_TIMEOUT = 30.0
NETWORK_RECOVER_COOLDOWN = 30.0
