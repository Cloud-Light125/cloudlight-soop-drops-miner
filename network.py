from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Generic, TypeVar
from urllib.parse import urlsplit

import aiohttp

from .auth import apply_cookies
from .config import AppConfig
from .constants import DROPS_ORIGIN, HTTP_CONNECT_TIMEOUT, HTTP_TOTAL_TIMEOUT, LIVE_API, LOGIN_URL, USER_AGENT

logger = logging.getLogger("SoopDropsMiner.network")

T = TypeVar("T")


class ProxyRouteError(aiohttp.ClientConnectionError):
    """The explicitly configured proxy route failed and direct fallback is disabled."""


@dataclass(slots=True)
class TrafficBucket:
    uploaded: int = 0
    downloaded: int = 0


@dataclass(slots=True)
class NetworkStats:
    """Application-layer byte estimates; transport/TLS overhead is not included."""

    uploaded: int = 0
    downloaded: int = 0
    by_type: dict[str, TrafficBucket] = field(default_factory=dict)
    _recent: deque[tuple[float, int, int]] = field(default_factory=deque, repr=False)

    def record(self, request_type: str, *, uploaded: int = 0, downloaded: int = 0) -> None:
        uploaded = max(0, int(uploaded))
        downloaded = max(0, int(downloaded))
        self.uploaded += uploaded
        self.downloaded += downloaded
        bucket = self.by_type.setdefault(request_type, TrafficBucket())
        bucket.uploaded += uploaded
        bucket.downloaded += downloaded
        now = time.monotonic()
        self._recent.append((now, uploaded, downloaded))
        self._trim(now)

    def _trim(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.monotonic()) - 60.0
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    @property
    def last_minute_bytes(self) -> int:
        self._trim()
        return sum(up + down for _, up, down in self._recent)

    @property
    def last_minute_uploaded(self) -> int:
        self._trim()
        return sum(up for _, up, _ in self._recent)

    @property
    def last_minute_downloaded(self) -> int:
        self._trim()
        return sum(down for _, _, down in self._recent)

    @property
    def last_minute_upload_bps(self) -> float:
        return self.last_minute_uploaded * 8 / 60.0

    @property
    def last_minute_download_bps(self) -> float:
        return self.last_minute_downloaded * 8 / 60.0

    @property
    def last_minute_bps(self) -> float:
        return self.last_minute_bytes * 8 / 60.0

    @property
    def total_bytes(self) -> int:
        return self.uploaded + self.downloaded


class _AsyncContext(Generic[T]):
    def __init__(self, awaitable: Awaitable[T]):
        self._awaitable = awaitable
        self._value: T | None = None

    def __await__(self):
        return self._awaitable.__await__()

    async def __aenter__(self) -> T:
        self._value = await self._awaitable
        return self._value

    async def __aexit__(self, exc_type, exc, tb) -> None:
        value = self._value
        if isinstance(value, aiohttp.ClientResponse):
            value.release()
            await value.wait_for_close()
        elif value is not None and hasattr(value, "close"):
            await value.close()


class AccountWebSocket:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse, stats: NetworkStats):
        self._ws = ws
        self._stats = stats

    @property
    def closed(self) -> bool:
        return self._ws.closed

    async def send_json(self, data: Any) -> None:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        await self._ws.send_str(raw.decode("utf-8"))
        self._stats.record("websocket", uploaded=len(raw))

    async def receive(self, timeout: float | None = None) -> aiohttp.WSMessage:
        msg = await self._ws.receive(timeout=timeout)
        data = msg.data
        size = len(data) if isinstance(data, bytes) else len(str(data).encode("utf-8"))
        self._stats.record("websocket", downloaded=size)
        return msg

    async def close(self, *args: Any, **kwargs: Any) -> bool:
        return await self._ws.close(*args, **kwargs)

    def exception(self) -> BaseException | None:
        return self._ws.exception()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)


def classify_request(url: str) -> str:
    lower = url.lower()
    if "exlogcollector" in lower or "/gather" in lower:
        return "heartbeat"
    if "bridge.sooplive.com" in lower:
        return "websocket"
    if "get_drops_mission" in lower or "get_drops_event" in lower or "get_drops_enable" in lower:
        return "mission"
    if "get_drops_list" in lower or "get_drops_use_info" in lower or "/inventory" in lower:
        return "inventory"
    if "player_live_api" in lower or "sch.sooplive" in lower:
        return "selection"
    return "other"


class AccountSession:
    """Per-account aiohttp session facade that always applies the configured proxy."""

    def __init__(self, raw: aiohttp.ClientSession, config: AppConfig, stats: NetworkStats):
        self._raw = raw
        self._config = config
        self._stats = stats

    @property
    def raw_session(self) -> aiohttp.ClientSession:
        return self._raw

    @property
    def cookie_jar(self) -> aiohttp.CookieJar:
        return self._raw.cookie_jar

    @property
    def closed(self) -> bool:
        return self._raw.closed

    @property
    def proxy_url(self) -> str | None:
        return self._config.effective_proxy_url

    async def close(self) -> None:
        await self._raw.close()

    async def _request(self, method: str, url: str, **kwargs: Any) -> aiohttp.ClientResponse:
        proxy = self.proxy_url
        if proxy:
            kwargs["proxy"] = proxy
        try:
            return await self._raw.request(method, url, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if not proxy or not self._config.proxy_fallback_direct:
                if proxy:
                    raise ProxyRouteError(
                        f"代理请求失败，未回退直连: {urlsplit(url).netloc}: {exc}"
                    ) from exc
                raise
            logger.warning("代理请求失败，按配置回退直连: %s", urlsplit(url).netloc)
            kwargs.pop("proxy", None)
            return await self._raw.request(method, url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> _AsyncContext[aiohttp.ClientResponse]:
        return _AsyncContext(self._request(method, url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> _AsyncContext[aiohttp.ClientResponse]:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _AsyncContext[aiohttp.ClientResponse]:
        return self.request("POST", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> _AsyncContext[aiohttp.ClientResponse]:
        return self.request("HEAD", url, **kwargs)

    async def _ws_connect(self, url: str, **kwargs: Any) -> AccountWebSocket:
        proxy = self.proxy_url
        if proxy:
            kwargs["proxy"] = proxy
        try:
            ws = await self._raw.ws_connect(url, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if not proxy or not self._config.proxy_fallback_direct:
                if proxy:
                    raise ProxyRouteError(
                        f"代理 WebSocket 失败，未回退直连: {urlsplit(url).netloc}: {exc}"
                    ) from exc
                raise
            logger.warning("代理 WebSocket 失败，按配置回退直连: %s", urlsplit(url).netloc)
            kwargs.pop("proxy", None)
            ws = await self._raw.ws_connect(url, **kwargs)
        return AccountWebSocket(ws, self._stats)

    def ws_connect(self, url: str, **kwargs: Any) -> _AsyncContext[AccountWebSocket]:
        return _AsyncContext(self._ws_connect(url, **kwargs))


class AccountNetworkContext:
    """Owns one account's CookieJar, HTTP/WSS session, proxy and traffic stats."""

    def __init__(self, uid: str, cookies: dict[str, str], config: AppConfig | None = None):
        self.uid = uid
        self.cookies = dict(cookies)
        self.config = config or AppConfig()
        self.stats = NetworkStats()
        self._raw: aiohttp.ClientSession | None = None
        self.session: AccountSession | None = None

    async def open(self) -> AccountSession:
        if self.session is not None and not self.session.closed:
            return self.session
        timeout = aiohttp.ClientTimeout(total=HTTP_TOTAL_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)
        trace = aiohttp.TraceConfig()

        async def on_start(_session, ctx, params) -> None:
            ctx.kind = classify_request(str(params.url))
            header_size = sum(len(str(k)) + len(str(v)) + 4 for k, v in params.headers.items())
            self.stats.record(ctx.kind, uploaded=len(params.method) + len(str(params.url)) + header_size)

        async def on_chunk_sent(_session, ctx, params) -> None:
            self.stats.record(getattr(ctx, "kind", "other"), uploaded=len(params.chunk))

        async def on_chunk_received(_session, ctx, params) -> None:
            self.stats.record(getattr(ctx, "kind", "other"), downloaded=len(params.chunk))

        trace.on_request_start.append(on_start)
        trace.on_request_chunk_sent.append(on_chunk_sent)
        trace.on_response_chunk_received.append(on_chunk_received)
        jar = aiohttp.CookieJar()
        self._raw = aiohttp.ClientSession(
            cookie_jar=jar,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            trace_configs=[trace],
            trust_env=False,
        )
        apply_cookies(self._raw, self.cookies)
        self.session = AccountSession(self._raw, self.config, self.stats)
        return self.session

    async def recreate(self) -> AccountSession:
        await self.close()
        return await self.open()

    async def close(self) -> None:
        session, self.session = self.session, None
        self._raw = None
        if session is not None and not session.closed:
            await session.close()

    async def __aenter__(self) -> "AccountNetworkContext":
        await self.open()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


@dataclass(slots=True)
class ProxyTestResult:
    target: str
    ok: bool
    detail: str
    elapsed_ms: int = 0
    complete: bool = True


async def test_proxy_connectivity(
    config: AppConfig,
    *,
    bjid: str = "owesports",
) -> list[ProxyTestResult]:
    """Test the configured route without silently changing proxy policy."""
    from .center import BRIDGE_WS, LIVE_FORM

    context = AccountNetworkContext("proxy-test", {}, config)
    session = await context.open()
    checks: list[tuple[str, str, str, dict[str, Any]]] = [
        ("SOOP 登录域名", "GET", LOGIN_URL, {}),
        ("Drops API", "GET", DROPS_ORIGIN, {}),
        ("SOOP Live API", "POST", f"{LIVE_API}?bjid={bjid}", {"data": LIVE_FORM.format(bjid=bjid)}),
    ]
    results: list[ProxyTestResult] = []
    try:
        for name, method, url, kwargs in checks:
            started = time.monotonic()
            try:
                async with session.request(method, url, **kwargs) as resp:
                    await resp.read()
                    results.append(
                        ProxyTestResult(
                            name,
                            resp.status < 500,
                            f"HTTP {resp.status}",
                            int((time.monotonic() - started) * 1000),
                        )
                    )
            except Exception as exc:
                results.append(
                    ProxyTestResult(
                        name,
                        False,
                        str(exc),
                        int((time.monotonic() - started) * 1000),
                    )
                )
        started = time.monotonic()
        try:
            ws = await session.ws_connect(
                BRIDGE_WS.format(bjid=bjid),
                protocols=["bridge"],
                timeout=HTTP_CONNECT_TIMEOUT,
                heartbeat=None,
                autoping=True,
            )
            await ws.close()
            results.append(
                ProxyTestResult(
                    "Bridge WebSocket",
                    True,
                    "代理链路可达；完整 Bridge 鉴权未测试",
                    int((time.monotonic() - started) * 1000),
                    complete=False,
                )
            )
        except Exception as exc:
            results.append(
                ProxyTestResult(
                    "Bridge WebSocket",
                    False,
                    str(exc),
                    int((time.monotonic() - started) * 1000),
                    complete=False,
                )
            )
    finally:
        await context.close()
    return results
