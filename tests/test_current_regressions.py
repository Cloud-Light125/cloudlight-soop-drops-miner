from __future__ import annotations

import asyncio

from soop_miner.channel import format_channel_drops_label
from soop_miner.miner import MinerState
from soop_miner.models import DropItem, LiveChannel, Mission
from soop_miner.modern_gui import ModernSoopGui
from soop_miner.ui_state import CallbackMailbox, LatestStateMailbox, friendly_account_status
from soop_miner.watch import HeartbeatStatus, WatchHeartbeat


def _mission(idx: str = "owwc", *, end_date: str = "2999-12-31") -> Mission:
    channel = LiveChannel("owesports", "Overwatch Esports", "123", True)
    return Mission(
        drops_idx=idx,
        title="OWWC GROUP STAGE",
        start_date="2026-01-01",
        end_date=end_date,
        ingame_give=True,
        live=True,
        give_con="term",
        drops_type="mission",
        filter="progress",
        dp_flag=None,
        category_name="Overwatch",
        category_no="1",
        guide_str=None,
        channels=[channel],
        items=[DropItem("1h reward", 60, 2, 3, False, {})],
        raw={},
    )


class _Widget:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def configure(self, **kwargs) -> None:
        self.values.update(kwargs)


class _Row:
    def __init__(self) -> None:
        self.selected = False

    def set_selected(self, selected: bool) -> None:
        self.selected = selected


class _DetailWidget:
    def __init__(self, text: str = "—") -> None:
        self.text = text
        self.visible = False

    def cget(self, name: str):
        assert name == "text"
        return self.text

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self.text = kwargs["text"]

    def grid(self) -> None:
        self.visible = True

    def grid_remove(self) -> None:
        self.visible = False


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body: str) -> None:
        self.body = body

    async def text(self) -> str:
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _HeartbeatSession:
    def __init__(self, body: str) -> None:
        self.response = _Response(body)

    def post(self, *args, **kwargs):
        return self.response


def test_channel_drops_label_uses_real_missions() -> None:
    mission = _mission()
    assert format_channel_drops_label(mission.channels[0], [mission]) == "固定"


def test_select_account_is_single_source_for_highlight_detail_and_start_button() -> None:
    gui = ModernSoopGui.__new__(ModernSoopGui)
    gui._selected_uid = None
    gui._states = {"new-account": MinerState(uid="new-account")}
    gui._pages = {"accounts": object()}
    gui._account_rows = {"new-account": _Row(), "old-account": _Row()}
    gui._cached_missions = []
    gui._cached_channels = []
    gui._channels_loaded = False
    gui._manager = None
    gui._starting = False
    gui._stopping = False
    gui._account_starting_uids = set()
    gui._account_stopping_uids = set()
    gui._account_start_btn = _Widget()
    gui._account_stop_btn = _Widget()
    shown: list[MinerState | None] = []
    gui._show_detail = shown.append
    gui._refresh_header = lambda: None

    gui._select_account("new-account")

    assert gui._selected_uid == "new-account"
    assert gui._account_rows["new-account"].selected
    assert not gui._account_rows["old-account"].selected
    assert shown[-1].uid == "new-account"
    assert gui._account_start_btn.values["state"] == "normal"


def test_callback_failure_does_not_block_following_callback_or_reschedule() -> None:
    class Root:
        scheduled = False

        def after(self, _interval, _callback):
            self.scheduled = True
            return "after-id"

    gui = ModernSoopGui.__new__(ModernSoopGui)
    gui.root = Root()
    gui._callback_mailbox = CallbackMailbox()
    gui._state_mailbox = LatestStateMailbox()
    gui._state_poll_id = "old"
    gui._quitting = False
    gui._in_tray = False
    called: list[str] = []

    def broken() -> None:
        raise TypeError("render failed")

    gui._callback_mailbox.submit(broken)
    gui._callback_mailbox.submit(lambda: called.append("second"))
    gui._drain_ui_mailboxes()

    assert called == ["second"]
    assert gui.root.scheduled


def test_channel_and_inventory_render_failures_always_restore_loading() -> None:
    gui = ModernSoopGui.__new__(ModernSoopGui)
    gui._pages = {"channels": object(), "inventory": object()}
    gui._channel_loading = True
    gui._inventory_loading = True
    gui._channels_loaded = False
    gui._inventory_loaded = False
    gui._cached_channels = []
    gui._channel_refresh_btn = _Widget()
    gui._inventory_refresh_btn = _Widget()
    gui._channel_hint = _Widget()
    gui._sync_channel_page = lambda: (_ for _ in ()).throw(TypeError("channel render"))
    gui._set_inventory = lambda _items: (_ for _ in ()).throw(TypeError("inventory render"))
    gui._append_log = lambda *_args, **_kwargs: None
    gui._schedule_channel_refresh = lambda: None

    gui._finish_channels([LiveChannel("owesports", "OW", "123", True)], None, True)
    gui._finish_inventory([], None)

    assert not gui._channel_loading
    assert not gui._inventory_loading
    assert gui._channel_refresh_btn.values == {"state": "normal", "text": "刷新直播间"}
    assert gui._inventory_refresh_btn.values == {"state": "normal", "text": "刷新背包"}


def test_state_snapshot_is_cached_and_expired_mission_is_not_current() -> None:
    gui = ModernSoopGui.__new__(ModernSoopGui)
    gui._states = {}
    gui._selected_uid = "account"
    gui._pages = {}
    gui._cached_missions = []
    gui._cached_channels = []
    gui._channels_loaded = False
    gui._all_inventory = []
    gui._latest_account_ui = {}
    gui._refresh_account_action_buttons = lambda: None
    gui._refresh_header = lambda: None
    active = _mission("active")
    expired = _mission("expired", end_date="2026-07-31")
    state = MinerState(uid="account", running=True, missions=[expired, active])

    gui._apply_state(state)

    assert gui._states["account"] is state
    assert [mission.drops_idx for mission in gui._cached_missions] == ["active"]


def test_heartbeat_13342_and_13343_are_unknown_without_failures() -> None:
    async def scenario() -> None:
        heartbeat = WatchHeartbeat("account", LiveChannel("owesports", "OW", "123", True))
        first = await heartbeat.send(_HeartbeatSession('{"nRet":1,"szMsg":"13342"}'))
        second = await heartbeat.send(_HeartbeatSession('{"nRet":1,"szMsg":"13343"}'))
        assert first is HeartbeatStatus.UNKNOWN
        assert second is HeartbeatStatus.UNKNOWN
        assert heartbeat.consecutive_failures == 0
        assert heartbeat.connection_healthy

    asyncio.run(scenario())


def test_friendly_account_status_accepts_current_and_legacy_call_styles() -> None:
    assert friendly_account_status("挂机中", True) == "正在累计掉宝进度"
    assert friendly_account_status("未知运行状态", running=True) == "正在运行"
    assert friendly_account_status("未知停止状态") == "未启动"


def test_show_detail_populates_running_account_fields() -> None:
    gui = ModernSoopGui.__new__(ModernSoopGui)
    gui._pages = {"accounts": object()}
    gui._manager = None
    gui._detail_grid = _DetailWidget()
    gui._detail_hint = _DetailWidget()
    detail_keys = (
        "uid", "status", "channel", "mission", "progress", "connection",
        "connection_activity", "watch", "last_watch", "failures", "upload",
        "download", "total", "source",
    )
    gui._detail_values = {key: _DetailWidget() for key in detail_keys}
    state = MinerState(
        uid="cloudlight12",
        running=True,
        status="挂机中",
        channel_id="owesports",
        channel_nick="Overwatch Esports",
        bridge_connected=True,
        missions=[_mission()],
        heartbeat_last_success="2026-08-20T15:00:00+00:00",
        network_uploaded=1024,
        network_downloaded=2048,
        network_upload_bps=8000,
        network_download_bps=16000,
    )

    gui._show_detail(state)

    for key in (
        "uid", "status", "channel", "mission", "progress", "connection",
        "watch", "last_watch", "failures", "upload", "download", "total",
    ):
        assert gui._detail_values[key].text != "—"
    assert gui._detail_values["uid"].text == "cloudlight12"
    assert gui._detail_values["status"].text == "正在累计掉宝进度"
    assert gui._detail_values["channel"].text == "Overwatch Esports"
    assert gui._detail_values["mission"].text == "OWWC GROUP STAGE"
