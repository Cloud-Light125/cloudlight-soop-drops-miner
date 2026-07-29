from __future__ import annotations

from dataclasses import replace

from soop_miner.config import AppConfig
from soop_miner.constants import AUTHOR
from soop_miner.miner import MinerState
from soop_miner.models import DropItem, InventoryItem, Mission
from soop_miner.ui_state import (
    AccountUiState,
    BoundedLogBuffer,
    LatestStateMailbox,
    ProgressAnimationState,
    account_ui_state,
    diff_registry,
    dispatcher_interval_ms,
    inventory_ui_states,
    mission_ui_states,
)


def mission(idx: str = "m1", *, percent: int = 10, minutes: int = 5) -> Mission:
    return Mission(
        drops_idx=idx,
        title=f"Mission {idx}",
        start_date="2026-01-01",
        end_date="2099-01-01",
        ingame_give=False,
        live=True,
        give_con="term",
        drops_type="term",
        filter="progress",
        dp_flag=None,
        category_name=None,
        category_no=None,
        guide_str=None,
        channels=[],
        items=[DropItem("Reward", 60, minutes, percent, False, {"itemCodeIdx": "tier1"})],
        raw={},
    )


def test_same_account_state_has_no_diff() -> None:
    state = account_ui_state(MinerState(uid="A"))
    assert diff_registry({"A": state}, {"A": state}).unchanged


def test_one_account_field_change_only_reports_that_field() -> None:
    before = account_ui_state(MinerState(uid="A"))
    after = replace(before, status="挂机中")
    diff = diff_registry({"A": before}, {"A": after})
    assert diff.updated[0].changes == ("status",)


def test_account_add_and_remove_are_key_local() -> None:
    a = account_ui_state(MinerState(uid="A"))
    b = account_ui_state(MinerState(uid="B"))
    added = diff_registry({"A": a}, {"A": a, "B": b})
    removed = diff_registry({"A": a, "B": b}, {"B": b})
    assert added.added == ("B",) and not added.updated
    assert removed.removed == ("A",) and not removed.updated


def test_selection_key_is_not_part_of_registry_refresh() -> None:
    selected = "A"
    states = {uid: account_ui_state(MinerState(uid=uid)) for uid in ("A", "B")}
    assert diff_registry(states, dict(states)).unchanged
    assert selected == "A"


def test_same_mission_does_not_rebuild() -> None:
    before = mission_ui_states("A", [mission()])
    assert diff_registry(before, dict(before)).unchanged


def test_new_and_deleted_mission_are_key_local() -> None:
    one = mission_ui_states("A", [mission("m1")])
    two = mission_ui_states("A", [mission("m1"), mission("m2")])
    assert diff_registry(one, two).added == (("A", "m2"),)
    assert diff_registry(two, one).removed == (("A", "m2"),)


def test_tier_progress_only_changes_progress_fields() -> None:
    before = mission_ui_states("A", [mission(percent=10, minutes=5)])[("A", "m1")].tiers[0]
    after = mission_ui_states("A", [mission(percent=20, minutes=10)])[("A", "m1")].tiers[0]
    changed = diff_registry({before.key: before}, {after.key: after}).updated[0].changes
    assert changed == ("current_minutes", "percent")


def test_same_progress_does_not_start_animation() -> None:
    animation = ProgressAnimationState(0.25)
    assert not animation.retarget(0.25)
    assert animation.generation == 0


def test_new_progress_invalidates_previous_animation_generation() -> None:
    animation = ProgressAnimationState()
    assert animation.retarget(0.2)
    old_generation = animation.generation
    assert animation.retarget(0.7)
    assert animation.generation == old_generation + 1


def test_mailbox_keeps_latest_state_per_account() -> None:
    mailbox = LatestStateMailbox()
    mailbox.submit(MinerState(uid="A", status="连接中"))
    mailbox.submit(MinerState(uid="A", status="挂机中"))
    mailbox.submit(MinerState(uid="B", status="空闲"))
    drained = mailbox.drain()
    assert set(drained) == {"A", "B"}
    assert drained["A"].status == "挂机中"


def test_mailbox_rejects_late_updates_after_shutdown() -> None:
    mailbox = LatestStateMailbox()
    mailbox.close()
    assert not mailbox.submit(MinerState(uid="A"))
    assert mailbox.drain() == {}


def test_hidden_dispatch_interval_is_lower_frequency() -> None:
    assert dispatcher_interval_ms(hidden=True) > dispatcher_interval_ms(hidden=False)


def test_restore_can_apply_latest_pending_state() -> None:
    mailbox = LatestStateMailbox()
    mailbox.submit(MinerState(uid="A", status="连接中"))
    mailbox.submit(MinerState(uid="A", status="挂机中"))
    assert mailbox.drain()["A"].status == "挂机中"


def test_inventory_incremental_diff_does_not_clear_unchanged_rows() -> None:
    a = InventoryItem("1", "A")
    b = InventoryItem("2", "B")
    old = inventory_ui_states([("u", a)])
    new = inventory_ui_states([("u", a), ("u", b)])
    diff = diff_registry(old, new)
    assert diff.added == (("u", "2"),)
    assert not diff.removed and not diff.updated


def test_inventory_claim_change_updates_only_status() -> None:
    old = inventory_ui_states([("u", InventoryItem("1", "A", claimed=False))])
    new = inventory_ui_states([("u", InventoryItem("1", "A", claimed=True))])
    assert diff_registry(old, new).updated[0].changes == ("claim_status",)


def test_log_buffer_trims_in_one_batch() -> None:
    buffer = BoundedLogBuffer(limit=5, trim_to=3)
    assert buffer.extend(["1", "2", "3", "4", "5"]) == 0
    assert buffer.extend(["6"]) == 3
    assert buffer.lines == ("4", "5", "6")


def test_author_constant_is_cloudlight() -> None:
    assert AUTHOR == "cloudlight"


def test_theme_setting_defaults_and_validates() -> None:
    assert AppConfig().appearance_mode == "system"
    assert AppConfig(appearance_mode="dark").validated().appearance_mode == "dark"


def test_old_config_without_theme_uses_system() -> None:
    assert AppConfig.from_dict({"settings_version": 2}).appearance_mode == "system"


def test_one_hundred_equal_snapshots_produce_no_component_churn() -> None:
    snapshot = mission_ui_states("A", [mission(percent=40, minutes=20)])
    create_count = len(snapshot)
    update_count = 0
    current = snapshot
    for _ in range(100):
        incoming = mission_ui_states("A", [mission(percent=40, minutes=20)])
        change = diff_registry(current, incoming)
        create_count += len(change.added)
        update_count += len(change.updated)
        current = incoming
    assert create_count == 1
    assert update_count == 0


def test_progress_zero_to_one_hundred_keeps_stable_tier_key() -> None:
    keys = set()
    animation = ProgressAnimationState()
    for percent in range(101):
        state = mission_ui_states("A", [mission(percent=percent, minutes=percent)])[("A", "m1")]
        keys.add(state.tiers[0].key)
        animation.retarget(percent / 100)
    assert keys == {("A", "m1", "tier1")}
    assert animation.target == 1.0
