from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, fields
from typing import Callable, Generic, Hashable, Iterable, Mapping, TypeVar

from .miner import MinerState
from .models import InventoryItem, Mission


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class FieldUpdate:
    key: Hashable
    changes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegistryDiff(Generic[K, V]):
    added: tuple[K, ...]
    removed: tuple[K, ...]
    updated: tuple[FieldUpdate, ...]

    @property
    def unchanged(self) -> bool:
        return not self.added and not self.removed and not self.updated


def diff_registry(old: Mapping[K, V], new: Mapping[K, V]) -> RegistryDiff[K, V]:
    """Return the smallest key/field-level change set for immutable UI snapshots."""
    old_keys, new_keys = set(old), set(new)
    updates: list[FieldUpdate] = []
    for key in sorted(old_keys & new_keys, key=str):
        before, after = old[key], new[key]
        if before == after:
            continue
        if hasattr(before, "__dataclass_fields__") and type(before) is type(after):
            changed = tuple(
                field.name for field in fields(before) if getattr(before, field.name) != getattr(after, field.name)
            )
        else:
            changed = ("value",)
        updates.append(FieldUpdate(key, changed))
    return RegistryDiff(
        added=tuple(sorted(new_keys - old_keys, key=str)),
        removed=tuple(sorted(old_keys - new_keys, key=str)),
        updated=tuple(updates),
    )


@dataclass(frozen=True, slots=True)
class AccountUiState:
    uid: str
    running: bool
    status: str
    channel: str
    mission: str
    progress: str
    bridge: str
    heartbeat: str
    rate: str


@dataclass(frozen=True, slots=True)
class TierUiState:
    key: tuple[str, str, str]
    name: str
    required_minutes: int
    current_minutes: int
    percent: int
    claim_status: str


@dataclass(frozen=True, slots=True)
class MissionUiState:
    key: tuple[str, str]
    title: str
    drops_type: str
    start_date: str
    end_date: str
    channel: str
    channel_matches: bool
    current_minutes: int
    status: str
    not_started: bool
    ended: bool
    needs_switch: bool
    tiers: tuple[TierUiState, ...]


@dataclass(frozen=True, slots=True)
class InventoryUiState:
    key: tuple[str, str]
    uid: str
    name: str
    claim_status: str
    masked_code: str
    received_at: str
    expires_at: str
    full_code: str | None


def format_rate(bits_per_second: float) -> str:
    """Format the estimated application traffic as user-friendly bytes/second."""
    bytes_per_second = max(0.0, bits_per_second) / 8.0
    if bytes_per_second >= 1024 * 1024:
        return f"{bytes_per_second / (1024 * 1024):.2f} MB/s"
    if bytes_per_second >= 1024:
        return f"{bytes_per_second / 1024:.1f} KB/s"
    return f"{bytes_per_second:.0f} B/s"


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for suffix in ("B", "KB", "MB", "GB"):
        if amount < 1024 or suffix == "GB":
            return f"{amount:.1f} {suffix}"
        amount /= 1024
    return "0 B"


def mask_code(code: str | None) -> str:
    if not code:
        return "—"
    text = code.strip()
    if len(text) <= 8:
        return text[:2] + "*" * max(2, len(text) - 4) + text[-2:]
    return f"{text[:4]}-****-****-{text[-4:]}"


def _mission_progress(mission: Mission) -> tuple[int, int]:
    if not mission.items:
        return 0, 0
    current = max(item.view_time for item in mission.items)
    percent = max(0, min(100, max(item.percent for item in mission.items)))
    return current, percent


def account_ui_state(state: MinerState) -> AccountUiState:
    mission = state.missions[0] if state.missions else None
    current, percent = _mission_progress(mission) if mission else (0, 0)
    heartbeat = friendly_watch_status(state)
    return AccountUiState(
        uid=state.uid,
        running=state.running,
        status=friendly_account_status(state.status, running=state.running),
        channel=state.channel_nick or state.channel_id or "—",
        mission=mission.title if mission else "—",
        progress=f"{current} 分钟 · {percent}%" if mission else "—",
        bridge=friendly_connection_status(state),
        heartbeat=heartbeat,
        rate=format_rate(state.network_last_minute_bps),
    )


def friendly_account_status(status: str, *, running: bool = False) -> str:
    mapping = {
        "空闲": "未启动",
        "已停止": "已停止",
        "连接中": "正在连接",
        "重连中": "正在重新连接",
        "等待直播间": "正在寻找直播间",
        "挂机中": "正在累计掉宝进度",
        "挂机中·固定型已结束": "正在累计其他掉宝进度",
        "未开放掉宝": "当前没有符合条件的直播",
        "活动已结束": "当前没有符合条件的直播",
        "进房失败": "连接异常",
        "无可用直播间": "当前没有符合条件的直播",
        "登录失败": "登录失败",
    }
    return mapping.get(status, "正在运行" if running else "未启动")


def friendly_connection_status(state: MinerState) -> str:
    if state.bridge_connected:
        return "连接正常"
    if "重连" in state.status:
        return "正在重新连接"
    if state.status in {"进房失败", "连接异常"}:
        return "连接异常"
    if state.running:
        return "正在连接"
    return "未建立"


def friendly_watch_status(state: MinerState) -> str:
    if state.connection_healthy:
        return "掉宝计时正常"
    if state.heartbeat_failures > 0:
        return "验证失败"
    if "重连" in state.status:
        return "正在重新连接"
    if state.running and state.bridge_connected:
        return "正在验证观看状态"
    return "等待开始"


def mission_ui_states(uid: str, missions: Iterable[Mission], channel_name: str = "") -> dict[tuple[str, str], MissionUiState]:
    result: dict[tuple[str, str], MissionUiState] = {}
    for mission in missions:
        mission_key = (uid, mission.drops_idx)
        current, _ = _mission_progress(mission)
        tiers: list[TierUiState] = []
        for index, item in enumerate(mission.items):
            stable = item.raw.get("itemCodeIdx") or item.raw.get("item_code_idx") or str(index)
            tiers.append(
                TierUiState(
                    key=(uid, mission.drops_idx, str(stable)),
                    name=item.item_name or "未命名奖励",
                    required_minutes=item.give_term,
                    current_minutes=item.view_time,
                    percent=max(0, min(100, item.percent)),
                    claim_status="可以领取" if item.mission_success else "尚未达到领取条件",
                )
            )
        ended = mission.is_truly_ended
        not_started = mission.is_not_yet_open
        channel_matches = bool(channel_name) and any(
            channel_name in {ch.user_id, ch.user_nick} for ch in mission.channels
        )
        needs_switch = bool(channel_name) and bool(mission.channels) and not channel_matches
        status = (
            "尚未开始"
            if not_started
            else "已结束"
            if ended
            else "当前直播间不符合要求"
            if needs_switch
            else "正在进行"
            if mission.is_event_active
            else "进度已暂停"
        )
        result[mission_key] = MissionUiState(
            key=mission_key,
            title=mission.title or f"任务 {mission.drops_idx}",
            drops_type=mission.type_label,
            start_date=mission.start_date or "—",
            end_date=mission.end_date or "—",
            channel=channel_name or "等待符合条件的直播",
            channel_matches=channel_matches or not mission.channels,
            current_minutes=current,
            status=status,
            not_started=not_started,
            ended=ended,
            needs_switch=needs_switch,
            tiers=tuple(tiers),
        )
    return result


def inventory_ui_states(items: Iterable[tuple[str, InventoryItem]]) -> dict[tuple[str, str], InventoryUiState]:
    result: dict[tuple[str, str], InventoryUiState] = {}
    for uid, item in items:
        key = (uid, item.item_code_idx)
        result[key] = InventoryUiState(
            key=key,
            uid=uid,
            name=item.item_name or "未命名奖励",
            claim_status="领取已确认" if item.claimed else "需要前往官方背包领取",
            masked_code=mask_code(item.redeem_code),
            received_at=item.receive_date or "—",
            expires_at=item.exp_date or "—",
            full_code=item.redeem_code,
        )
    return result


class LatestStateMailbox:
    """Thread-safe coalescing mailbox: one latest snapshot per account."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, MinerState] = {}
        self._closed = False

    def submit(self, state: MinerState) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._latest[state.uid] = state
            return True

    def drain(self) -> dict[str, MinerState]:
        with self._lock:
            drained, self._latest = self._latest, {}
            return drained

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._latest.clear()


class CallbackMailbox:
    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._closed = False
        self._lock = threading.Lock()

    def submit(self, callback: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._queue.put(callback)
            return True

    def drain(self, limit: int = 100) -> list[Callable[[], None]]:
        result: list[Callable[[], None]] = []
        while len(result) < limit:
            try:
                result.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.drain(100_000)


class BoundedLogBuffer:
    def __init__(self, limit: int = 4000, trim_to: int = 3500) -> None:
        if not 0 < trim_to <= limit:
            raise ValueError("trim_to must be between 1 and limit")
        self.limit = limit
        self.trim_to = trim_to
        self._lines: list[str] = []

    def extend(self, lines: Iterable[str]) -> int:
        self._lines.extend(lines)
        removed = 0
        if len(self._lines) > self.limit:
            removed = len(self._lines) - self.trim_to
            del self._lines[:removed]
        return removed

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self._lines)


class ProgressAnimationState:
    """Pure state used to prove replacement/cancellation semantics without Tk."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = max(0.0, min(1.0, value))
        self.target = self.value
        self.generation = 0

    def retarget(self, value: float) -> bool:
        target = max(0.0, min(1.0, value))
        if abs(target - self.target) < 0.0001:
            return False
        self.target = target
        self.generation += 1
        return True


def dispatcher_interval_ms(*, hidden: bool) -> int:
    return 1000 if hidden else 300
