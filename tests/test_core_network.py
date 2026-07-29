from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
from yarl import URL

from soop_miner.center import BridgeClosedError, BridgeSession
from soop_miner.config import AppConfig, load_config, save_config, validate_proxy_url
from soop_miner.drops import ClaimStatus, DropsClient
from soop_miner.miner import SoopMiner
from soop_miner.models import InventoryItem, LiveChannel
from soop_miner.network import AccountNetworkContext, AccountSession, NetworkStats
from soop_miner.stream import head_latest_segment
from soop_miner.watch import WatchHeartbeat


def run(coro):
    return asyncio.run(coro)


def test_two_accounts_have_independent_session_and_cookie_jar() -> None:
    async def scenario() -> None:
        a = AccountNetworkContext("A", {"AuthTicket": "ticket-a", "BbsTicket": "A"})
        b = AccountNetworkContext("B", {"AuthTicket": "ticket-b", "BbsTicket": "B"})
        sa = await a.open()
        sb = await b.open()
        try:
            assert sa is not sb
            assert sa.raw_session is not sb.raw_session
            assert sa.cookie_jar is not sb.cookie_jar
            ca = sa.cookie_jar.filter_cookies(URL("https://drops.sooplive.com/"))
            cb = sb.cookie_jar.filter_cookies(URL("https://drops.sooplive.com/"))
            assert ca["AuthTicket"].value == "ticket-a"
            assert cb["AuthTicket"].value == "ticket-b"
            assert "ticket-a" not in cb.output()
            assert "ticket-b" not in ca.output()
        finally:
            await a.close()
            await b.close()
            await a.close()
            await b.close()
        assert sa.closed and sb.closed

    run(scenario())


class FakeResponse:
    def __init__(self, status: int, body: str, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class HeartbeatSession:
    def __init__(self, response: FakeResponse):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


def _heartbeat() -> WatchHeartbeat:
    return WatchHeartbeat("account", LiveChannel("bj", "BJ", "123", True))


def test_heartbeat_http_200_with_failed_body_is_failure() -> None:
    heartbeat = _heartbeat()
    ok = run(heartbeat.send(HeartbeatSession(FakeResponse(200, '{"nRet":-1}'))))
    assert not ok
    assert heartbeat.consecutive_failures == 1
    assert not heartbeat.connection_healthy


def test_heartbeat_real_success_structure_is_success() -> None:
    heartbeat = _heartbeat()
    ok = run(heartbeat.send(HeartbeatSession(FakeResponse(200, '{"nRet":0}'))))
    assert ok
    assert heartbeat.consecutive_failures == 0
    assert heartbeat.last_success_time is not None
    assert heartbeat.connection_healthy


class FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send_json(self, data) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(data)

    async def receive(self, timeout=None):
        return await self.incoming.get()

    async def close(self) -> bool:
        self.closed = True
        return True

    def exception(self):
        return None

    def feed(self, svc: str, **extra) -> None:
        data = {"SVC": svc, "RESULT": 0, **extra}
        self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(data), ""))

    def disconnect(self) -> None:
        self.closed = True
        self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, ""))


async def _forever() -> None:
    await asyncio.Event().wait()


async def _bridge_with_receiver() -> tuple[BridgeSession, FakeWS]:
    bridge = BridgeSession({"BbsTicket": "A"}, "bj")
    ws = FakeWS()
    bridge._ws = ws
    bridge._last_activity = asyncio.get_running_loop().time()
    bridge._receive_task = asyncio.create_task(bridge._receive_loop())
    bridge._keepalive_task = asyncio.create_task(_forever())
    return bridge, ws


def test_bridge_message_order_is_cached_and_unknown_does_not_block() -> None:
    async def scenario() -> None:
        bridge, ws = await _bridge_with_receiver()
        try:
            ws.feed("CERTTICKETEX", DATA={"pcTicket": "t"})
            ws.feed("UNKNOWN_SVC")
            ws.feed("FLASH_LOGIN")
            flash = await bridge._wait_for("FLASH_LOGIN")
            ticket = await bridge._wait_for("CERTTICKETEX")
            assert flash["SVC"] == "FLASH_LOGIN"
            assert ticket["DATA"]["pcTicket"] == "t"

            ws.feed("GETCHINFOEX", DATA={"uiBroadNo": 123})
            ws.feed("JOINCH_COMMON")
            joined = await bridge._wait_for("JOINCH_COMMON")
            info = await bridge._wait_for("GETCHINFOEX")
            assert joined["SVC"] == "JOINCH_COMMON"
            assert info["DATA"]["uiBroadNo"] == 123
        finally:
            await bridge.close()
            await bridge.close()

    run(scenario())


def test_bridge_waiter_gets_explicit_disconnect_error() -> None:
    async def scenario() -> None:
        bridge, ws = await _bridge_with_receiver()
        waiter = asyncio.create_task(bridge._wait_for("FLASH_LOGIN", timeout=1))
        await asyncio.sleep(0)
        ws.disconnect()
        try:
            await waiter
            raise AssertionError("waiter should fail")
        except BridgeClosedError:
            pass
        finally:
            await bridge.close()

    run(scenario())


def test_bridge_health_checks_closed_ws_and_tasks() -> None:
    async def scenario() -> None:
        bridge, ws = await _bridge_with_receiver()
        assert bridge.is_connected
        ws.closed = True
        assert not bridge.is_connected
        ws.closed = False
        bridge._keepalive_task.cancel()
        await asyncio.gather(bridge._keepalive_task, return_exceptions=True)
        assert not bridge.is_connected
        await bridge.close()

    run(scenario())


class RawTransport:
    def __init__(self):
        self.http_kwargs = None
        self.ws_kwargs = None
        self.closed = False

    async def request(self, method, url, **kwargs):
        self.http_kwargs = kwargs
        return SimpleNamespace()

    async def ws_connect(self, url, **kwargs):
        self.ws_kwargs = kwargs
        return FakeWS()

    async def close(self):
        self.closed = True


class FailingProxyTransport(RawTransport):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def request(self, method, url, **kwargs):
        self.calls += 1
        raise aiohttp.ClientConnectionError("proxy down")


def test_proxy_disabled_is_direct_and_enabled_covers_http_and_wss() -> None:
    async def scenario() -> None:
        raw = RawTransport()
        direct = AccountSession(raw, AppConfig(proxy_enabled=False), NetworkStats())
        await direct.request("GET", "https://example.test")
        assert "proxy" not in raw.http_kwargs

        proxied = AccountSession(
            raw,
            AppConfig(proxy_enabled=True, proxy_url="http://user:pass@127.0.0.1:7897"),
            NetworkStats(),
        )
        await proxied.request("GET", "https://example.test")
        assert raw.http_kwargs["proxy"] == "http://user:pass@127.0.0.1:7897"
        ws = await proxied.ws_connect("wss://bridge.sooplive.com/Websocket/bj")
        assert raw.ws_kwargs["proxy"] == "http://user:pass@127.0.0.1:7897"
        await ws.close()

    run(scenario())


def test_proxy_failure_does_not_silently_fallback_by_default() -> None:
    async def scenario() -> None:
        raw = FailingProxyTransport()
        session = AccountSession(
            raw,
            AppConfig(proxy_enabled=True, proxy_url="http://127.0.0.1:7897"),
            NetworkStats(),
        )
        try:
            await session.request("GET", "https://example.test")
            raise AssertionError("proxy failure should be reported")
        except aiohttp.ClientConnectionError as exc:
            assert "未回退直连" in str(exc)
        assert raw.calls == 1

    run(scenario())


def test_config_round_trip_and_invalid_values_fall_back(tmp_path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(
        proxy_enabled=True,
        proxy_url="http://user:pass@127.0.0.1:7897",
        auto_claim_enabled=True,
        mission_poll_interval=120,
    )
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.proxy_url == config.proxy_url
    assert loaded.auto_claim_enabled
    assert loaded.mission_poll_interval == 120
    assert loaded.effective_mission_poll_interval == 120
    assert loaded.effective_inventory_poll_interval >= 300
    assert validate_proxy_url("https://127.0.0.1:8443")

    path.write_text('{"proxy_enabled":true,"proxy_url":"socks5://127.0.0.1:1",'
                    '"mission_poll_interval":-1}', encoding="utf-8")
    fallback = load_config(path)
    assert not fallback.proxy_enabled
    assert fallback.proxy_url == ""
    assert fallback.mission_poll_interval == 90


class FakeDrops(DropsClient):
    def __init__(self, inventories):
        self.inventories = list(inventories)
        self.claim_calls = 0

    async def get_inventory(self, **kwargs):
        if len(self.inventories) > 1:
            return self.inventories.pop(0)
        return self.inventories[0]

    async def claim_item(self, item_code_idx):
        self.claim_calls += 1
        return {"result": 1, "data": {"itemCode": "SECRET-CODE"}}


def _item(claimed: bool) -> InventoryItem:
    return InventoryItem("item-1", "Reward", claimed=claimed)


def test_claim_only_succeeds_after_inventory_state_changes() -> None:
    client = FakeDrops([[_item(False)], [_item(True)]])
    result = run(client.claim_and_verify("item-1"))
    assert result.status == ClaimStatus.CLAIMED
    assert result.success
    assert client.claim_calls == 1

    unchanged = FakeDrops([[_item(False)], [_item(False)], [_item(False)]])
    result = run(unchanged.claim_and_verify("item-1", max_attempts=2))
    assert result.status == ClaimStatus.UNCONFIRMED
    assert not result.success


def test_auto_claim_disabled_never_calls_claim_endpoint() -> None:
    async def scenario() -> None:
        miner = SoopMiner(
            {"BbsTicket": "A"},
            app_config=AppConfig(auto_claim_enabled=False),
        )
        fake = FakeDrops([[_item(False)]])
        miner._drops = fake
        await miner._try_claim()
        assert fake.claim_calls == 0

    run(scenario())


def test_low_bandwidth_mode_never_requests_media_url() -> None:
    class FailSession:
        def get(self, *args, **kwargs):
            raise AssertionError("media request must not be made")

        def head(self, *args, **kwargs):
            raise AssertionError("media request must not be made")

    assert not run(
        head_latest_segment(
            FailSession(),
            "https://cdn.example/live/master.m3u8",
            bjid="bj",
        )
    )
