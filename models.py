from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


def api_bool(value: Any) -> bool:
    """Parse SOOP's mixed boolean encodings without treating ``\"N\"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def parse_mission_datetime(value: str) -> datetime | None:
    """解析任务 API 返回的日期时间字符串。"""
    text = (value or "").strip()
    if not text:
        return None
    for fmt, size in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


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
    def is_event_active(self) -> bool:
        """活动进行中（与官网 mission 页「进行中」一致）。"""
        if self.filter != "progress" or not self.live:
            return False
        end_at = parse_mission_datetime(self.end_date)
        if end_at is None:
            return True
        if len((self.end_date or "").strip()) <= 10:
            end_at += timedelta(days=1)
        return datetime.now() < end_at

    @property
    def is_event_ended(self) -> bool:
        """活动已结束（官网显示在已结束区域）。"""
        return not self.is_event_active

    @property
    def is_not_yet_open(self) -> bool:
        """官网标记非进行中，且当前时间早于开始时间。"""
        if self.is_event_active:
            return False
        start_at = parse_mission_datetime(self.start_date)
        if start_at is None:
            return False
        return datetime.now() < start_at

    @property
    def is_truly_ended(self) -> bool:
        """已超过截止时间，或官网非进行中且无有效截止时间。"""
        end_at = parse_mission_datetime(self.end_date)
        if end_at is None:
            return not self.is_event_active and not self.is_not_yet_open
        if len((self.end_date or "").strip()) <= 10:
            end_at += timedelta(days=1)
        return datetime.now() >= end_at

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
            start_date=str(data.get("startDate", "")),
            end_date=str(data.get("endDate", "")),
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
            start_date=str(data.get("startDate", "")),
            end_date=str(data.get("endDate", "")),
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
    def is_event_active(self) -> bool:
        """活动目录中当前进行中的 Drops。"""
        if self.filter != "progress" or not self.live:
            return False
        end_at = parse_mission_datetime(self.end_date)
        if end_at is None:
            return True
        if len((self.end_date or "").strip()) <= 10:
            end_at += timedelta(days=1)
        return datetime.now() < end_at

    @property
    def is_not_yet_open(self) -> bool:
        if self.is_event_active:
            return False
        start_at = parse_mission_datetime(self.start_date)
        return start_at is not None and datetime.now() < start_at

    @property
    def is_truly_ended(self) -> bool:
        end_at = parse_mission_datetime(self.end_date)
        if end_at is None:
            return not self.is_event_active and not self.is_not_yet_open
        if len((self.end_date or "").strip()) <= 10:
            end_at += timedelta(days=1)
        return datetime.now() >= end_at

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
