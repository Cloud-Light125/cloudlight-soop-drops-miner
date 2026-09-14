from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    SOOP_TIMEZONE = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:  # pragma: no cover - only minimal Python images lack tzdata
    # KST has no daylight-saving transitions.  This fallback preserves the
    # same explicit wire-time semantics when the host has no IANA tz database.
    SOOP_TIMEZONE = timezone(timedelta(hours=9), "Asia/Seoul")


class TaskLifecycleState(str, Enum):
    """The account-facing lifecycle of a Drops task."""

    UPCOMING = "upcoming"
    ACTIVE = "active"
    COMPLETED = "completed"
    ENDED = "ended"
    UNKNOWN = "unknown"


def api_bool(value: Any) -> bool:
    """Parse SOOP's mixed boolean encodings without treating ``\"N\"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _as_soop_now(value: datetime | None = None) -> datetime:
    """Normalize a comparison instant to an aware Asia/Seoul datetime."""
    if value is None:
        return datetime.now(SOOP_TIMEZONE)
    if value.tzinfo is None:
        # A naive fixture is explicitly a SOOP/KST wall-clock value.  It is
        # never interpreted using the machine's local timezone.
        return value.replace(tzinfo=SOOP_TIMEZONE)
    return value.astimezone(SOOP_TIMEZONE)


def _aware_soop_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SOOP_TIMEZONE)
    return value.astimezone(SOOP_TIMEZONE)


def _unix_timestamp(value: float) -> datetime | None:
    if not math.isfinite(value):
        return None
    # SOOP has used both seconds and millisecond timestamps in surrounding
    # APIs.  The unit is determined from the magnitude, not from a timezone
    # offset or a host-local conversion.
    seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone(SOOP_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return None


def parse_mission_datetime(value: Any) -> datetime | None:
    """Parse a SOOP date/time value into an aware Asia/Seoul datetime.

    Current SOOP responses use ``YYYY-MM-DD HH:mm:ss`` without an offset and
    those strings mean KST.  The parser also accepts ISO-8601 values with an
    explicit offset and Unix timestamps so all supported wire forms retain a
    single, documented comparison semantic.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _aware_soop_datetime(value)
    if isinstance(value, (int, float)):
        return _unix_timestamp(float(value))

    text = str(value).strip()
    if not text or text.casefold() in {"none", "null"}:
        return None
    if re.fullmatch(r"[+-]?\d{9,17}(?:\.\d+)?", text):
        return _unix_timestamp(float(text))

    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y.%m.%d",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    return _aware_soop_datetime(parsed) if parsed is not None else None


def _is_date_only(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", str(value or "").strip()))


def _end_boundary(value: Any, parsed: datetime) -> datetime:
    # Existing SOOP date-only responses represent the whole calendar day.
    return parsed + timedelta(days=1) if _is_date_only(value) else parsed


def event_lifecycle_state_at(
    start_date: Any,
    end_date: Any,
    *,
    filter_value: Any = "",
    live: Any = False,
    now: datetime | None = None,
) -> TaskLifecycleState:
    """Return the activity-window state using SOOP/KST time semantics."""
    current = _as_soop_now(now)
    start_at = parse_mission_datetime(start_date)
    end_at = parse_mission_datetime(end_date)
    if end_at is not None and current >= _end_boundary(end_date, end_at):
        return TaskLifecycleState.ENDED
    if start_at is not None and current < start_at:
        return TaskLifecycleState.UPCOMING
    if start_at is not None or end_at is not None:
        return TaskLifecycleState.ACTIVE

    # With no usable dates, retain a conservative fallback for legacy rows.
    # ``live`` is not consulted when a date window is present: the activity
    # catalog's live flag is not the official lifecycle indicator.
    if str(filter_value or "").strip().casefold() == "progress" and api_bool(live):
        return TaskLifecycleState.ACTIVE
    return TaskLifecycleState.UNKNOWN


def _api_date_text(value: Any) -> str:
    return "" if value is None else str(value)


def format_watch_term(minutes: int) -> str:
    """观看时长档位标签，如 60 → 1h、360 → 6h。"""
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


@dataclass
class DropItem:
    item_name: str
    give_term: int
    view_time: int
    percent: int
    mission_success: bool
    raw: dict[str, Any] = field(repr=False)

    @property
    def term_label(self) -> str:
        return format_watch_term(self.give_term)


@dataclass
class LiveChannel:
    user_id: str
    user_nick: str
    broad_no: str | None
    on_air: bool
    category_no: str | None = None
    category_names: list[str] = field(default_factory=list)
    has_drops: bool = True


@dataclass
class Mission:
    drops_idx: str
    title: str
    start_date: str
    end_date: str
    ingame_give: bool
    live: bool
    give_con: str
    drops_type: str
    filter: str
    dp_flag: str | None
    category_name: str | None
    category_no: str | None
    guide_str: str | None
    channels: list[LiveChannel]
    items: list[DropItem]
    raw: dict[str, Any] = field(repr=False)

    @property
    def is_fixed(self) -> bool:
        """固定型：观看达标即发放（如 owesports / OW 官方频道）。"""
        return self.give_con == "term"

    @property
    def is_lottery(self) -> bool:
        """抽奖型：观看达标后参与抽奖，非立即发放。"""
        return self.give_con == "draw"

    @property
    def is_random(self) -> bool:
        """随机型：观看期间随机发放（官网 Random Type，giveCon=none）。"""
        return self.give_con == "none"

    @property
    def type_label(self) -> str:
        if self.is_fixed:
            return "固定型"
        if self.is_lottery:
            return "抽奖型"
        if self.is_random:
            return "随机型"
        return "其他"

    @property
    def type_short(self) -> str:
        """列表/标签用短名。"""
        if self.is_fixed:
            return "固定"
        if self.is_lottery:
            return "抽奖"
        if self.is_random:
            return "随机"
        return "掉宝"

    @property
    def completed(self) -> bool:
        """Whether every reward tier has been completed for this account."""
        return bool(self.items) and all(item.mission_success for item in self.items)

    @property
    def is_completed(self) -> bool:
        """Compatibility/readability alias for the structured completion state."""
        return self.completed

    def event_lifecycle_state_at(self, now: datetime | None = None) -> TaskLifecycleState:
        return event_lifecycle_state_at(
            self.start_date,
            self.end_date,
            filter_value=self.filter,
            live=self.live,
            now=now,
        )

    def lifecycle_state_at(self, now: datetime | None = None) -> TaskLifecycleState:
        if self.completed:
            return TaskLifecycleState.COMPLETED
        return self.event_lifecycle_state_at(now)

    @property
    def lifecycle_state(self) -> TaskLifecycleState:
        return self.lifecycle_state_at()

    def is_event_active_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.ACTIVE

    def is_event_ended_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.ENDED

    def is_not_yet_open_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.UPCOMING

    @property
    def is_event_active(self) -> bool:
        """活动进行中；状态由时间窗口决定，不由 activity ``live`` 字段决定。"""
        return self.is_event_active_at()

    @property
    def is_event_ended(self) -> bool:
        """活动已结束（官网显示在已结束区域）。"""
        return self.is_event_ended_at()

    @property
    def is_not_yet_open(self) -> bool:
        """当前时间早于活动开始时间。"""
        return self.is_not_yet_open_at()

    def is_truly_ended_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.ENDED

    @property
    def is_truly_ended(self) -> bool:
        """当前时间已达到活动截止时间。"""
        return self.is_truly_ended_at()

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Mission:
        channels = [
            LiveChannel(
                user_id=str(ch.get("userId", "")),
                user_nick=str(ch.get("userNick", "")),
                broad_no=str(ch["broadNo"]) if ch.get("broadNo") else None,
                on_air=api_bool(ch.get("onAir")),
            )
            for ch in data.get("broadIdList") or []
        ]
        items = [
            DropItem(
                item_name=str(it.get("itemName", "")),
                give_term=int(it.get("giveTerm") or 0),
                view_time=int(it.get("viewTime") or 0),
                percent=int(it.get("percent") or 0),
                mission_success=api_bool(it.get("missionSuccess")),
                raw=it,
            )
            for it in data.get("itemList") or []
        ]
        guide = data.get("guideStr")
        return cls(
            drops_idx=str(data.get("dropsIdx", "")),
            title=str(data.get("title", "")),
            start_date=_api_date_text(data.get("startDate")),
            end_date=_api_date_text(data.get("endDate")),
            ingame_give=str(data.get("ingameGiveYn", "")).upper() == "Y",
            live=api_bool(data.get("live")),
            give_con=str(data.get("giveCon") or ""),
            drops_type=str(data.get("dropsType") or ""),
            filter=str(data.get("filter") or ""),
            dp_flag=str(data["dpFlag"]) if data.get("dpFlag") else None,
            category_name=str(data["cateName"]) if data.get("cateName") else None,
            category_no=str(data["cateNo"]) if data.get("cateNo") else None,
            guide_str=str(guide) if guide else None,
            channels=channels,
            items=items,
            raw=data,
        )

    def online_channels(self) -> list[LiveChannel]:
        return [ch for ch in self.channels if ch.on_air and ch.broad_no and ch.user_id]

    def active_item(self) -> DropItem | None:
        pending = [it for it in self.items if not it.mission_success]
        return pending[0] if pending else None


@dataclass
class DropEvent:
    """活动总览页 /event 返回的单条 Drops 活动（未必已加入 mission 列表）。"""

    drops_idx: str
    title: str
    filter: str
    give_con: str
    dup_flag: bool
    live: bool
    acct_conn: bool
    start_date: str
    end_date: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DropEvent:
        return cls(
            drops_idx=str(data.get("dropsIdx", "")),
            title=str(data.get("title", "")),
            filter=str(data.get("filter") or ""),
            give_con=str(data.get("giveCon") or ""),
            dup_flag=api_bool(data.get("dupFlag")),
            live=api_bool(data.get("live")),
            acct_conn=api_bool(data.get("acctConn")),
            start_date=_api_date_text(data.get("startDate")),
            end_date=_api_date_text(data.get("endDate")),
            raw=data,
        )

    @property
    def is_fixed(self) -> bool:
        return self.give_con == "term"

    @property
    def is_lottery(self) -> bool:
        return self.give_con == "draw"

    @property
    def is_random(self) -> bool:
        return self.give_con == "none"

    @property
    def type_label(self) -> str:
        if self.is_fixed:
            return "固定型"
        if self.is_lottery:
            return "抽奖型"
        if self.is_random:
            return "随机型"
        return "其他"

    @property
    def type_short(self) -> str:
        if self.is_fixed:
            return "固定"
        if self.is_lottery:
            return "抽奖"
        if self.is_random:
            return "随机"
        return "掉宝"

    @property
    def completed(self) -> bool:
        """Activity catalog rows do not carry account reward completion."""
        return False

    @property
    def is_completed(self) -> bool:
        return False

    def event_lifecycle_state_at(self, now: datetime | None = None) -> TaskLifecycleState:
        return event_lifecycle_state_at(
            self.start_date,
            self.end_date,
            filter_value=self.filter,
            live=self.live,
            now=now,
        )

    def lifecycle_state_at(self, now: datetime | None = None) -> TaskLifecycleState:
        return self.event_lifecycle_state_at(now)

    @property
    def lifecycle_state(self) -> TaskLifecycleState:
        return self.lifecycle_state_at()

    def is_event_active_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.ACTIVE

    def is_event_ended_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.ENDED

    def is_not_yet_open_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.UPCOMING

    @property
    def is_event_active(self) -> bool:
        """活动目录中的当前进行中状态，由时间窗口决定。"""
        return self.is_event_active_at()

    @property
    def is_event_ended(self) -> bool:
        return self.is_event_ended_at()

    @property
    def is_not_yet_open(self) -> bool:
        return self.is_not_yet_open_at()

    def is_truly_ended_at(self, now: datetime | None = None) -> bool:
        return self.event_lifecycle_state_at(now) is TaskLifecycleState.ENDED

    @property
    def is_truly_ended(self) -> bool:
        return self.is_truly_ended_at()

    def matches_channel(self, channel: LiveChannel) -> bool:
        for row in self.raw.get("broadIdList") or []:
            if isinstance(row, dict) and str(row.get("userId") or "") == channel.user_id:
                return True
        return False


@dataclass
class InventoryItem:
    item_code_idx: str
    item_name: str
    claimed: bool = False
    redeem_code: str | None = None
    exp_date: str | None = None
    receive_date: str | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def can_claim(self) -> bool:
        return not self.claimed
