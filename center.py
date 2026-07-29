from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
import time
from collections import defaultdict
from typing import Any, Callable

import aiohttp

from .auth import cookie_header, userid_from_cookies
from .constants import HTTP_CONNECT_TIMEOUT, LIVE_API, PLAY_ORIGIN, USER_AGENT

logger = logging.getLogger("SoopDropsMiner.center")

LIVE_FORM = (
    "bid={bjid}&type=live&pwd=&player_type=html5&stream_type=common"
    "&quality=HD&mode=landing&from_api=0&is_revive=false"
)
BRIDGE_WS = "wss://bridge.sooplive.com/Websocket/{bjid}"
STREAM_ASSIGN = "https://livestream-manager.sooplive.com/broad_stream_assign.html"

GW_CLIENT_HTML5 = 41
CC_CLIENT_HTML5 = 30
BRIDGE_ACTIVITY_TIMEOUT = 65.0
KEEPALIVE_INTERVAL = 20.0


class BridgeClosedError(ConnectionError):
    pass


class BridgeSession:
    """Account-local Bridge connection with queued SVC message dispatch."""

    def __init__(
        self,
        cookies: dict[str, str],
        bjid: str,
        *,
        on_disconnect: Callable[[BaseException], Any] | None = None,
        activity_timeout: float = BRIDGE_ACTIVITY_TIMEOUT,
    ):
        self.cookies = dict(cookies)
        self.bjid = bjid
        self.uid = userid_from_cookies(cookies)
        self.guid = secrets.token_hex(16).upper()
        self._ws: Any = None
        self._channel: dict[str, Any] | None = None
        self._gw_ticket = ""
        self._center_auth = ""
        self._aid = ""
        self._stream_base = ""
        self._receive_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._queues: dict[str, asyncio.Queue[dict[str, Any] | BaseException]] = defaultdict(asyncio.Queue)
        self._connect_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._on_disconnect = on_disconnect
        self._disconnect_notified = False
        self._closing = False
        self._connection_error: BaseException | None = None
        self._last_activity = 0.0
        self._activity_timeout = activity_timeout

    @property
    def is_connected(self) -> bool:
        ws = self._ws
        receive = self._receive_task
        keepalive = self._keepalive_task
        return bool(
            ws is not None
            and not getattr(ws, "closed", True)
            and receive is not None
            and not receive.done()
            and keepalive is not None
            and not keepalive.done()
            and self._connection_error is None
            and self._last_activity > 0
            and time.monotonic() - self._last_activity <= self._activity_timeout
        )

    @property
    def seconds_since_last_activity(self) -> float | None:
        if self._last_activity <= 0:
            return None
        return max(0.0, time.monotonic() - self._last_activity)

    async def _fetch_channel(self, session: Any) -> dict[str, Any]:
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

    async def _send(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or getattr(ws, "closed", True):
            raise BridgeClosedError("Bridge WebSocket 已关闭")
        await ws.send_json(msg)
        self._last_activity = time.monotonic()

    async def _notify_disconnect(self, exc: BaseException) -> None:
        if self._disconnect_notified or self._closing:
            return
        self._disconnect_notified = True
        callback = self._on_disconnect
        if callback is not None:
            try:
                result = callback(exc)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Bridge 断开回调失败", exc_info=True)

    async def _invalidate(self, exc: BaseException) -> None:
        if self._connection_error is None:
            self._connection_error = exc
        for queue in list(self._queues.values()):
            queue.put_nowait(self._connection_error)
        ws = self._ws
        if ws is not None and not getattr(ws, "closed", True):
            try:
                await ws.close()
            except Exception:
                pass
        await self._notify_disconnect(self._connection_error)

    async def _receive_loop(self) -> None:
        try:
            while not self._closing:
                ws = self._ws
                if ws is None:
                    raise BridgeClosedError("Bridge WebSocket 不存在")
                msg = await ws.receive()
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw = msg.data
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        payload = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        logger.debug("忽略无法解析的 Bridge 消息: %r", str(raw)[:200])
                        continue
                    if not isinstance(payload, dict):
                        logger.debug("忽略非对象 Bridge 消息")
                        continue
                    self._last_activity = time.monotonic()
                    svc = str(payload.get("SVC") or "")
                    if not svc:
                        logger.debug("Bridge 未知消息（无 SVC）: %s", str(payload)[:200])
                        continue
                    if svc not in {"FLASH_LOGIN", "CERTTICKETEX", "JOINCH_COMMON", "GETCHINFOEX", "KEEPALIVE"}:
                        logger.debug("Bridge 未知 SVC=%s（已缓存）", svc)
                    self._queues[svc].put_nowait(payload)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                    raise BridgeClosedError("Bridge 被远端关闭")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise BridgeClosedError(f"Bridge 接收错误: {ws.exception()}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._invalidate(exc)

    async def _wait_for(self, svc: str, *, timeout: float = 15.0) -> dict[str, Any]:
        if self._connection_error is not None:
            raise BridgeClosedError(str(self._connection_error)) from self._connection_error
        queue = self._queues[svc]
        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"等待 {svc} 超时") from exc
        if isinstance(item, BaseException):
            raise BridgeClosedError(str(item)) from item
        if item.get("RESULT", 0) < 0:
            raise RuntimeError(f"{svc} 失败: {item}")
        return item

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

    async def fetch_stream_url(self, session: Any, *, allow_media_probe: bool = False) -> str | None:
        """Debug-only stream assignment helper; never used by the miner loop."""
        if not allow_media_probe:
            logger.debug("低流量策略已阻止流分配/HLS URL 探测")
            return None
        if self._channel is None:
            return None
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
        return base

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closing:
                await self._send({"SVC": "KEEPALIVE", "RESULT": 0, "DATA": {}})
                await asyncio.sleep(KEEPALIVE_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._invalidate(BridgeClosedError(f"Bridge Keepalive 失败: {exc}"))

    async def connect(self, session: Any) -> None:
        async with self._connect_lock:
            if self.is_connected:
                return
            await self.close()
            self._closing = False
            self._disconnect_notified = False
            self._connection_error = None
            self._queues = defaultdict(asyncio.Queue)
            self._channel = await self._fetch_channel(session)
            url = BRIDGE_WS.format(bjid=self.bjid)
            try:
                self._ws = await session.ws_connect(
                    url,
                    protocols=["bridge"],
                    timeout=HTTP_CONNECT_TIMEOUT,
                    heartbeat=None,
                    autoping=True,
                )
                self._last_activity = time.monotonic()
                self._receive_task = asyncio.create_task(
                    self._receive_loop(), name=f"bridge-recv-{self.uid}-{self.bjid}"
                )
                await self._init_gateway()
                await self._join_broad()
                self._keepalive_task = asyncio.create_task(
                    self._keepalive_loop(), name=f"bridge-keepalive-{self.uid}-{self.bjid}"
                )
            except Exception:
                await self.close()
                raise
            logger.info("已加入直播间 center → %s (broadNo=%s)", self.bjid, self._channel.get("BNO"))

    @property
    def broad_no(self) -> str | None:
        return str(self._channel.get("BNO") or "") if self._channel else None

    @property
    def center_ip(self) -> str | None:
        return str(self._channel.get("CTIP") or "") if self._channel else None

    @property
    def center_port(self) -> str | None:
        return str(self._channel.get("CTPT") or "") if self._channel else None

    async def close(self) -> None:
        async with self._close_lock:
            if self._closing and self._ws is None and self._receive_task is None and self._keepalive_task is None:
                return
            self._closing = True
            error = self._connection_error or BridgeClosedError("Bridge 已关闭")
            self._connection_error = error
            for queue in list(self._queues.values()):
                queue.put_nowait(error)
            current = asyncio.current_task()
            tasks = [self._keepalive_task, self._receive_task]
            self._keepalive_task = None
            self._receive_task = None
            for task in tasks:
                if task is not None and task is not current and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in tasks if task is not None and task is not current),
                return_exceptions=True,
            )
            ws, self._ws = self._ws, None
            if ws is not None and not getattr(ws, "closed", True):
                try:
                    await ws.close()
                except Exception:
                    pass
