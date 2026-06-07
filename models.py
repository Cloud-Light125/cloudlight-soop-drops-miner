from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


@dataclass
class Mission:
    drops_idx: str
    title: str
    start_date: str
    end_date: str
    ingame_give: bool
    live: bool
    channels: list[LiveChannel]
    items: list[DropItem]
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Mission:
        channels = [
            LiveChannel(
                user_id=str(ch.get("userId", "")),
                user_nick=str(ch.get("userNick", "")),
                broad_no=str(ch["broadNo"]) if ch.get("broadNo") else None,
                on_air=bool(ch.get("onAir")),
            )
            for ch in data.get("broadIdList") or []
        ]
        items = [
            DropItem(
                item_name=str(it.get("itemName", "")),
                give_term=int(it.get("giveTerm") or 0),
                view_time=int(it.get("viewTime") or 0),
                percent=int(it.get("percent") or 0),
                mission_success=bool(it.get("missionSuccess")),
                raw=it,
            )
            for it in data.get("itemList") or []
        ]
        return cls(
            drops_idx=str(data.get("dropsIdx", "")),
            title=str(data.get("title", "")),
            start_date=str(data.get("startDate", "")),
            end_date=str(data.get("endDate", "")),
            ingame_give=str(data.get("ingameGiveYn", "")).upper() == "Y",
            live=bool(data.get("live")),
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
