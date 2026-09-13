from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from soop_miner import auth
from soop_miner.channel import filter_missions_by_priority
from soop_miner.config import AppConfig
from soop_miner.miner import SoopMiner
from soop_miner.models import DropEvent, DropItem, LiveChannel, Mission


def _mission(idx: str, *, active: bool = True) -> Mission:
    return Mission(
        drops_idx=idx,
        title=f"Mission {idx}",
        start_date="2026-01-01",
        end_date="2099-12-31" if active else "2020-01-01",
        ingame_give=True,
        live=active,
        give_con="term",
        drops_type="mission",
        filter="progress",
        dp_flag=None,
        category_name="Drops",
        category_no="1",
        guide_str=None,
        channels=[],
        items=[DropItem(f"Reward {idx}", 60, 1, 1, False, {})],
        raw={},
    )


class _SequenceDrops:
    def __init__(self, missions: list[list[Mission]], events: list[list[DropEvent]] | None = None) -> None:
        self._missions = list(missions)
        self._events = list(events or [[]])
        self.mission_calls = 0
        self.event_calls = 0
        self.inventory_calls: list[bool] = []

    async def get_missions(self) -> list[Mission]:
        self.mission_calls += 1
        return self._missions.pop(0) if len(self._missions) > 1 else self._missions[0]

    async def get_inventory(self, *, with_codes: bool) -> list:
        self.inventory_calls.append(with_codes)
        return []

    async def get_progress_events(self) -> list[DropEvent]:
        self.event_calls += 1
        return self._events.pop(0) if len(self._events) > 1 else self._events[0]


def _event(idx: str, *, active: bool = True) -> DropEvent:
    return DropEvent(
        drops_idx=idx,
        title=f"Activity {idx}",
        filter="progress" if active else "ended",
        give_con="term",
        dup_flag=False,
        live=active,
        acct_conn=True,
        start_date="2026-01-01",
        end_date="2099-12-31" if active else "2020-01-01",
        raw={"cateName": "Overwatch"},
    )


def test_force_refresh_fetches_new_missions_inventory_and_channels() -> None:
    async def scenario() -> None:
        old = _mission("old")
        old_ended = _mission("old", active=False)
        new = _mission("new")
        drops = _SequenceDrops(
            [[old], [old_ended, new]],
            [[_event("old")], [_event("old"), _event("new")]],
        )
        callbacks = []
        miner = SoopMiner(
            {"BbsTicket": "account"},
            on_state=callbacks.append,
            app_config=AppConfig(),
        )
        miner._running = True
        miner._session = SimpleNamespace(closed=False)
        miner._drops = drops

        async def no_channel_sync() -> None:
            return None

        async def refresh_channels() -> None:
            miner._available_channels = [LiveChannel("live", "Live", "1", True)]

        miner._sync_channel = no_channel_sync
        miner._refresh_available_channels = refresh_channels

        first = await miner.force_refresh()
        second = await miner.force_refresh()

        assert drops.mission_calls == 2
        assert drops.event_calls == 2
        assert drops.inventory_calls == [True, True]
        assert [mission.drops_idx for mission in first.missions] == ["old"]
        assert {mission.drops_idx for mission in second.missions} == {"old", "new"}
        assert not next(mission for mission in second.missions if mission.drops_idx == "old").is_event_active
        assert next(mission for mission in second.missions if mission.drops_idx == "new").is_event_active
        assert {activity.drops_idx for activity in second.events} == {"old", "new"}
        assert [channel.user_id for channel in second.available_channels] == ["live"]
        assert callbacks and {mission.drops_idx for mission in callbacks[-1].missions} == {"old", "new"}

    asyncio.run(scenario())


def test_force_refresh_failure_restores_previous_snapshot() -> None:
    async def scenario() -> None:
        old = _mission("old")
        miner = SoopMiner({"BbsTicket": "account"}, app_config=AppConfig())
        miner._running = True
        miner._session = SimpleNamespace(closed=False)
        miner._missions = [old]
        miner._events = [_event("old")]
        miner._inventory = []
        miner._available_channels = [LiveChannel("old-live", "Old", "1", True)]

        async def mutate_then_fail() -> None:
            miner._missions = [_mission("new")]
            miner._events = [_event("new")]
            raise ConnectionError("SOOP API unavailable")

        miner._refresh_missions = mutate_then_fail

        try:
            await miner.force_refresh()
        except ConnectionError as exc:
            assert str(exc) == "SOOP API unavailable"
        else:
            raise AssertionError("force_refresh should propagate the remote failure")

        assert [mission.drops_idx for mission in miner._missions] == ["old"]
        assert [activity.drops_idx for activity in miner._events] == ["old"]
        assert [channel.user_id for channel in miner._available_channels] == ["old-live"]

    asyncio.run(scenario())


def test_mission_poll_updates_state_when_new_activity_appears() -> None:
    async def scenario() -> None:
        old = _mission("old")
        new = _mission("new")
        miner = SoopMiner({"BbsTicket": "account"}, app_config=AppConfig())
        miner._running = True
        miner._app_config = SimpleNamespace(
            effective_mission_poll_interval=0,
            effective_channel_refresh_interval=10_000,
            effective_inventory_poll_interval=10_000,
        )
        calls = 0
        event_calls = 0

        class StopAfterTwoPolls:
            wait_calls = 0

            def is_set(self) -> bool:
                return self.wait_calls >= 2

            async def wait(self) -> None:
                self.wait_calls += 1
                if self.wait_calls < 2:
                    raise asyncio.TimeoutError

        miner._stop = StopAfterTwoPolls()

        async def poll_missions() -> None:
            nonlocal calls
            calls += 1
            miner._missions = [old] if calls == 1 else [old, new]

        async def no_channel_sync() -> None:
            return None

        async def no_inventory() -> None:
            return None

        async def poll_events() -> None:
            nonlocal event_calls
            event_calls += 1
            miner._events = [_event("old")] if event_calls == 1 else [_event("old"), _event("new")]

        miner._fetch_missions = poll_missions
        miner._fetch_events = poll_events
        miner._sync_channel = no_channel_sync
        miner._try_claim = no_inventory
        await miner._poll_loop()

        assert calls >= 2
        assert event_calls >= 2
        assert {mission.drops_idx for mission in miner._missions} == {"old", "new"}
        assert {activity.drops_idx for activity in miner._events} == {"old", "new"}

    asyncio.run(scenario())


def test_stale_manual_priority_falls_back_to_active_mission() -> None:
    active = _mission("new")
    ended = _mission("old", active=False)
    assert filter_missions_by_priority([ended, active], "old") == [active]


def test_event_catalog_parses_active_activity_and_mixed_boolean_fields() -> None:
    activity = DropEvent.from_api(
        {
            "dropsIdx": "owwc-day-2",
            "title": "OWWC FINALS DAY 2",
            "filter": "progress",
            "giveCon": "term",
            "dupFlag": "Y",
            "live": "Y",
            "acctConn": "N",
            "startDate": "2026-01-01",
            "endDate": "2099-12-31",
        }
    )
    assert activity.is_event_active
    assert activity.dup_flag
    assert not activity.acct_conn

    mission = Mission.from_api(
        {
            "dropsIdx": "mission",
            "title": "Mission",
            "filter": "progress",
            "live": "N",
            "giveCon": "term",
            "itemList": [{"missionSuccess": "N"}],
        }
    )
    assert not mission.is_event_active
    assert not mission.items[0].mission_success


def test_legacy_cookie_migration_does_not_recreate_deleted_account() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        old_accounts_dir = auth.ACCOUNTS_DIR
        old_cookies_path = auth.COOKIES_PATH
        try:
            auth.ACCOUNTS_DIR = root / "accounts"
            auth.COOKIES_PATH = root / "cookies.json"
            auth.COOKIES_PATH.write_text('{"BbsTicket":"legacyuser"}', encoding="utf-8")

            assert auth.list_accounts() == ["legacyuser"]
            assert not auth.COOKIES_PATH.exists()
            assert auth.remove_account("legacyuser") is True
            assert auth.list_accounts() == []
            assert not (auth.ACCOUNTS_DIR / "legacyuser").exists()
        finally:
            auth.ACCOUNTS_DIR = old_accounts_dir
            auth.COOKIES_PATH = old_cookies_path
