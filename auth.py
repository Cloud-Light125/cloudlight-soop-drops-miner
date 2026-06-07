from __future__ import annotations

import json
import re
from typing import Any

import aiohttp
from yarl import URL

from .constants import COOKIES_PATH, LOGIN_URL, USER_AGENT

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


def save_cookies(cookies: dict[str, str]) -> None:
    COOKIES_PATH.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def load_cookies() -> dict[str, str] | None:
    if not COOKIES_PATH.is_file():
        return None
    data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data:
        return {str(k): str(v) for k, v in data.items()}
    return None


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def login(userid: str, password: str) -> dict[str, str]:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
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

            # 多个 Set-Cookie 时 headers.get 只能拿到第一个，需从 resp.cookies 读取
            cookies: dict[str, str] = {name: morsel.value for name, morsel in resp.cookies.items()}
            if not cookies.get("AuthTicket") and not cookies.get("BbsTicket"):
                for cookie in session.cookie_jar:
                    if cookie.key in COOKIE_NAMES or cookie.key in cookies:
                        cookies[cookie.key] = cookie.value

    if "AuthTicket" not in cookies and "BbsTicket" not in cookies:
        raise RuntimeError("登录未返回有效 Ticket Cookie")

    save_cookies(cookies)
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
