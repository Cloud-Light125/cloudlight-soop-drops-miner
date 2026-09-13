from __future__ import annotations

import json
import re
import stat
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from .constants import ACCOUNTS_DIR, COOKIES_PATH, LOGIN_URL, USER_AGENT

if TYPE_CHECKING:
    from .config import AppConfig

COOKIE_NAMES = (
    "AbroadChk",
    "AbroadVod",
    "AuthTicket",
    "BbsTicket",
    "RDB",
    "UserTicket",
    "_au",
    "_au3rd",
    "_ausa",
    "_ausb",
    "isBbs",
)


def cookies_path_for(userid: str) -> Path:
    return ACCOUNTS_DIR / userid / "cookies.json"


def migrate_legacy_cookies() -> str | None:
    """将旧版 cookies.json 迁移到 accounts/<userid>/。"""
    if not COOKIES_PATH.is_file():
        return None
    try:
        data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    cookies = {str(k): str(v) for k, v in data.items()}
    uid = userid_from_cookies(cookies)
    dest = cookies_path_for(uid)
    if not dest.is_file():
        save_cookies(cookies, uid)
    # The legacy file is only a migration source. Keeping it makes a later
    # list_accounts() call recreate an account that the user just deleted.
    try:
        COOKIES_PATH.unlink()
    except OSError:
        pass
    return uid


def save_cookies(cookies: dict[str, str], userid: str | None = None) -> str:
    uid = userid or userid_from_cookies(cookies)
    path = cookies_path_for(uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    return uid


def load_cookies(userid: str | None = None) -> dict[str, str] | None:
    migrate_legacy_cookies()
    if userid:
        path = cookies_path_for(userid)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
        return None

    accounts = list_accounts()
    if accounts:
        return load_cookies(accounts[0])
    if COOKIES_PATH.is_file():
        data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    return None


def load_all_cookies() -> dict[str, dict[str, str]]:
    migrate_legacy_cookies()
    result: dict[str, dict[str, str]] = {}
    for uid in list_accounts():
        cookies = load_cookies(uid)
        if cookies:
            result[uid] = cookies
    return result


def list_accounts() -> list[str]:
    migrate_legacy_cookies()
    if not ACCOUNTS_DIR.is_dir():
        return []
    return sorted(
        p.name
        for p in ACCOUNTS_DIR.iterdir()
        if p.is_dir() and cookies_path_for(p.name).is_file()
    )


def remove_account(userid: str) -> bool:
    path = ACCOUNTS_DIR / userid
    if not path.is_dir():
        return False

    def remove_readonly(func, target, _exc_info) -> None:
        try:
            Path(target).chmod(stat.S_IWRITE)
        except OSError:
            pass
        func(target)

    try:
        shutil.rmtree(path, onerror=remove_readonly)
    except OSError:
        return False
    return not path.exists() and not cookies_path_for(userid).exists()


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def login(
    userid: str,
    password: str,
    *,
    config: AppConfig | None = None,
) -> dict[str, str]:
    from .config import AppConfig
    from .network import AccountNetworkContext

    context = AccountNetworkContext(userid, {}, config or AppConfig())
    session = await context.open()
    try:
        async with session.post(
            LOGIN_URL,
            data={
                "szWork": "login",
                "szType": "json",
                "szUid": userid,
                "szPassword": password,
            },
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"登录失败 (HTTP {resp.status}): {body[:200]}")
            if '"RESULT":1' not in body and '"RESULT": 1' not in body:
                raise RuntimeError(f"登录被拒绝: {body[:200]}")

            cookies: dict[str, str] = {name: morsel.value for name, morsel in resp.cookies.items()}
            if not cookies.get("AuthTicket") and not cookies.get("BbsTicket"):
                for cookie in session.cookie_jar:
                    if cookie.key in COOKIE_NAMES or cookie.key in cookies:
                        cookies[cookie.key] = cookie.value
    finally:
        await context.close()

    if "AuthTicket" not in cookies and "BbsTicket" not in cookies:
        raise RuntimeError("登录未返回有效 Ticket Cookie")

    save_cookies(cookies, userid)
    return cookies


def userid_from_cookies(cookies: dict[str, str]) -> str:
    ticket = cookies.get("BbsTicket", "")
    if ticket:
        return ticket
    user_ticket = cookies.get("UserTicket", "")
    match = re.search(r"uid=([^&]+)", user_ticket)
    return match.group(1) if match else "unknown"


def apply_cookies(session: aiohttp.ClientSession, cookies: dict[str, str]) -> None:
    jar = session.cookie_jar
    for domain in (".sooplive.com", ".sooplive.co.kr", "drops.sooplive.com"):
        jar.update_cookies(cookies, response_url=URL(f"https://{domain.lstrip('.')}/"))
