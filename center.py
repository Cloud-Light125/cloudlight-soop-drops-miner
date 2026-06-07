from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

import aiohttp

from .auth import apply_cookies, cookie_header, userid_from_cookies
from .constants import LIVE_API, PLAY_ORIGIN, USER_AGENT

logger = logging.getLogger("SoopDropsMiner.center")

LIVE_FORM = (
    "bid={bjid}&type=live&pwd=&player_type=html5&stream_type=common"
    "&quality=HD&mode=landing&from_api=0&is_revive=false"
)
BRIDGE_WS = "wss://bridge.sooplive.com/Websocket/{bjid}"
STREAM_ASSIGN = "https://livestream-manager.sooplive.com/broad_stream_assign.html"

GW_CLIENT_HTML5 = 41
CC_CLIENT_HTML5 = 30


class BridgeSession:
    """通过 bridge WebSocket 加入直播间（Drops 进度依赖此会话）。"""

    def __init__(self, cookies: dict[str, str], bjid: str):
        self.cookies = cookies
        self.bjid = bjid
        self.uid = userid_from_cookies(cookies)
        self.guid = secrets.token_hex(16).upper()
        self._ws: Any = None
        self._channel: dict[str, Any] | None = None
        self._gw_ticket = ""
        self._center_auth = ""
        self._aid = ""
        self._stream_base = ""
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def _fetch_channel(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header(self.cookies),
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{PLAY_ORIGIN}/{self.bjid}",
        }
        async with session.post(
            f"{LIVE_API}?bjid={self.bjid}",
            data=LIVE_FORM.format(bjid=self.bjid),
            headers=headers,
        ) as resp:
            data = await resp.json(content_type=None)
        channel = data.get("CHANNEL") or {}
        if channel.get("RESULT") != 1:
            raise RuntimeError(f"player_live_api 失败: {channel.get('MSG', data)}")
        return channel

    async def _recv_json(self, timeout: float = 12.0) -> dict[str, Any]:
        assert self._ws is not None
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw)

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(msg))

    async def _wait_for(self, svc: str, *, timeout: float = 15.0) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            msg = await self._recv_json(timeout=max(1.0, deadline - asyncio.get_event_loop().time()))
            if msg.get("SVC") == svc:
                if msg.get("RESULT", 0) < 0:
                    raise RuntimeError(f"{svc} 失败: {msg}")
                return msg
        raise TimeoutError(f"等待 {svc} 超时")

    async def _init_gateway(self) -> None:
        assert self._channel is not None
        ch = self._channel
        await self._send(
            {
                "SVC": "INIT_GW",
                "RESULT": 0,
                "DATA": {
                    "gate_ip": ch["GWIP"],
                    "gate_port": int(ch["GWPT"]),
                    "center_ip": ch["CTIP"],
                    "center_port": int(ch["CTPT"]),
                    "broadno": int(ch["BNO"]),
                    "cookie": self.cookies.get("AuthTicket", ""),
                    "guid": self.guid,
                    "BJID": ch["BJID"],
                    "category": ch.get("CATE", ""),
                    "cli_type": GW_CLIENT_HTML5,
                    "cc_cli_type": CC_CLIENT_HTML5,
                    "passwd": "",
                    "fanticket": "",
                    "addinfo": f"uid={self.uid}",
                    "JOINLOG": "",
                    "update_info": 0,
                },
            }
        )
        await self._wait_for("FLASH_LOGIN")
        ticket = await self._wait_for("CERTTICKETEX")
        data = ticket.get("DATA") or {}
        self._gw_ticket = str(data.get("pcTicket") or "")
        self._center_auth = str(data.get("pcAppendDat") or "")

    async def _join_broad(self) -> None:
        assert self._channel is not None
        ch = self._channel
        await self._send(
            {
                "SVC": "INIT_BROAD",
                "RESULT": 0,
                "DATA": {
                    "center_ip": ch["CTIP"],
                    "center_port": int(ch["CTPT"]),
                    "passwd": "",
                    "JOINLOG": "",
                    "cli_type": GW_CLIENT_HTML5,
                    "cc_cli_type": CC_CLIENT_HTML5,
                    "QUALITY": "HD",
                    "guid": self.guid,
                    "gw_ticket": self._gw_ticket,
                    "append_data": self._center_auth,
                },
            }
        )
        await self._wait_for("JOINCH_COMMON")
        info = await self._wait_for("GETCHINFOEX", timeout=20.0)
        data = info.get("DATA") or {}
        if data.get("acBjId"):
            logger.debug("已加入频道 %s broadNo=%s", data.get("acBjId"), data.get("uiBroadNo"))

    async def _fetch_aid(self, session: aiohttp.ClientSession) -> None:
        assert self._channel is not None
        ch = self._channel
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header(self.cookies),
            "Referer": f"{PLAY_ORIGIN}/{self.bjid}",
        }
        async with session.post(
            f"{LIVE_API}?bjid={self.bjid}",
            data=LIVE_FORM.format(bjid=self.bjid),
            headers=headers,
        ) as resp:
            data = await resp.json(content_type=None)
        channel = (data or {}).get("CHANNEL") or {}
        aid = channel.get("AID")
        if aid:
            self._aid = str(aid)

    async def fetch_stream_url(self, session: aiohttp.ClientSession) -> str | None:
        return await self._fetch_stream_url(session)

    async def _fetch_stream_url(self, session: aiohttp.ClientSession) -> str | None:
        assert self._channel is not None
        import random

        bno = self._channel["BNO"]
        params = {
            "return_type": self._channel.get("CDN") or "gcp_cdn",
            "use_cors": "true",
            "cors_origin_url": "play.sooplive.com",
            "broad_key": f"{bno}-common-sd-hls",
            "player_mode": "embed",
            "time": str(int(random.random() * 1e10)),
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header(self.cookies),
            "Referer": f"{PLAY_ORIGIN}/{self.bjid}",
        }
        async with session.get(STREAM_ASSIGN, params=params, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if str(data.get("result")) != "1":
            return None
        base = str(data.get("view_url") or "")
        if not base:
            return None
        self._stream_base = base
        if self._aid and "aid=" not in base:
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}aid={self._aid}"
        return base

    async def _keepalive_loop(self) -> None:
        while self._ws is not None:
            try:
                await self._send({"SVC": "KEEPALIVE", "RESULT": 0, "DATA": {}})
            except Exception:
                break
            await asyncio.sleep(20)

    async def connect(self, session: aiohttp.ClientSession) -> str | None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("需要安装 websockets: pip install websockets") from exc

        self._channel = await self._fetch_channel(session)
        url = BRIDGE_WS.format(bjid=self.bjid)
        self._ws = await websockets.connect(
            url,
            subprotocols=["bridge"],
            open_timeout=15,
            ping_interval=None,
        )
        await self._init_gateway()
        await self._join_broad()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info("已加入直播间 center → %s (broadNo=%s)", self.bjid, self._channel.get("BNO"))
        return None

    @property
    def broad_no(self) -> str | None:
        if self._channel:
            return str(self._channel.get("BNO") or "")
        return None

    @property
    def center_ip(self) -> str | None:
        if self._channel:
            return str(self._channel.get("CTIP") or "")
        return None

    @property
    def center_port(self) -> str | None:
        if self._channel:
            return str(self._channel.get("CTPT") or "")
        return None

    async def close(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
