from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import aiohttp

from .constants import DROPS_API, DROPS_EVENT_URL, DROPS_MISSION_URL, DROPS_ORIGIN, USER_AGENT
from .models import DropEvent, InventoryItem, Mission

logger = logging.getLogger("SoopDropsMiner")


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    ALREADY_CLAIMED = "already_claimed"
    NOT_CLAIMABLE = "not_claimable"
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"


@dataclass(slots=True)
class ClaimResult:
    item_code_idx: str
    status: ClaimStatus
    message: str
    redeem_code: str | None = None
    attempts: int = 0

    @property
    def success(self) -> bool:
        return self.status in {ClaimStatus.CLAIMED, ClaimStatus.ALREADY_CLAIMED}


class DropsClient:
    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> dict[str, Any]:
        url = f"{DROPS_API}/{path}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Origin": DROPS_ORIGIN,
            "Referer": referer or f"{DROPS_ORIGIN}/mission",
        }
        async with self._session.request(method, url, headers=headers, json=json_body) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 401 or (isinstance(data, dict) and data.get("result") == -1):
                msg = data.get("message", "未登录") if isinstance(data, dict) else "未登录"
                raise RuntimeError(f"Drops API 认证失败: {msg}")
            if resp.status >= 400:
                raise RuntimeError(f"Drops API HTTP {resp.status}")
            return data

    async def get_drops_enabled(self) -> bool | None:
        """读取账号 Drops 总开关（对应官网 mission 页顶部开关）。"""
        data = await self._request("POST", "get_drops_enable.php", json_body={"enable": None})
        value = data.get("data")
        if value is None:
            return None
        return value == 1 or value is True

    async def set_drops_enabled(self, enabled: bool) -> bool:
        data = await self._request(
            "POST",
            "get_drops_enable.php",
            json_body={"enable": 1 if enabled else 0},
        )
        return data.get("data") == 1

    async def ensure_drops_ready(
        self,
        *,
        on_info: Callable[[str], None] | None = None,
    ) -> None:
        """自动开启 Drops 开关并预热 mission 会话（替代用户手动打开官网开关）。"""
        enabled = await self.get_drops_enabled()
        if enabled is False:
            msg = "Drops 开关未开启，正在自动开启…"
            if on_info:
                on_info(msg)
            else:
                logger.info(msg)
            await self.set_drops_enabled(True)

        assert self._session is not None
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Referer": DROPS_ORIGIN,
        }
        async with self._session.get(DROPS_MISSION_URL, headers=headers) as resp:
            await resp.read()
        async with self._session.get(DROPS_EVENT_URL, headers=headers) as resp:
            await resp.read()

    @staticmethod
    def empty_missions_hint(uid: str, *, progress_count: int | None = None) -> str:
        event_part = (
            f"官网活动页当前有 {progress_count} 个进行中的活动。"
            if progress_count
            else "可打开「活动页」查看当前全部 Drops 活动。"
        )
        return (
            f"账号 {uid} mission 页暂无任务（已自动开启 Drops 开关）。"
            "mission 仅显示「已参与」的活动：进入带 Drops 的直播间后会自动加入，"
            "观看一段时间后任务才会出现在 mission 页。"
            f"{event_part}"
            "若长期仍无任务，常见原因：活动 dupFlag 限制每人仅一次、需绑定游戏账号、"
            "或该账号不符合活动条件。"
        )

    @staticmethod
    def progress_events_summary(events: list[DropEvent], *, limit: int = 5) -> str:
        if not events:
            return "活动页当前无进行中的 Drops 活动。"
        lines = [f"活动页进行中 {len(events)} 个（mission 需进房观看后才会出现对应任务）："]
        for ev in events[:limit]:
            dup = " · 限一次" if ev.dup_flag else ""
            lines.append(f"  · {ev.title}{dup}")
        if len(events) > limit:
            lines.append(f"  … 另有 {len(events) - limit} 个，见 {DROPS_EVENT_URL}")
        return "\n".join(lines)

    async def get_events(
        self,
        *,
        filter: str = "progress",
        game_idx: str = "all",
        page_no: int = 1,
        page_size: int = 50,
    ) -> list[DropEvent]:
        data = await self._request(
            "POST",
            "get_drops_event_list.php",
            json_body={
                "pageNo": page_no,
                "prePageNo": page_size,
                "filter": filter,
                "gameIdx": game_idx,
            },
            referer=DROPS_EVENT_URL,
        )
        rows = data.get("data") or []
        return [DropEvent.from_api(row) for row in rows if isinstance(row, dict)]

    async def get_progress_events(self) -> list[DropEvent]:
        return await self.get_events(filter="progress")

    async def get_missions(self) -> list[Mission]:
        data = await self._request("GET", "get_drops_mission_list.php")
        rows = data.get("data") or []
        return [Mission.from_api(row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _inventory_from_row(row: dict[str, Any]) -> InventoryItem:
        return InventoryItem(
            item_code_idx=str(row["itemCodeIdx"]),
            item_name=str(row.get("itemName") or row.get("title") or ""),
            claimed=str(row.get("useFlag", "")).upper() == "Y",
            exp_date=str(row.get("expDate") or row.get("useExpDate") or "") or None,
            receive_date=str(row.get("receiveDate") or "") or None,
            raw=row,
        )

    async def get_inventory(self, *, with_codes: bool = True, page_size: int = 50) -> list[InventoryItem]:
        """背包列表，对应 https://drops.sooplive.com/inventory"""
        items: list[InventoryItem] = []
        page = 1
        inventory_ref = f"{DROPS_ORIGIN}/inventory"

        while True:
            data = await self._request(
                "POST",
                "get_drops_list.php",
                json_body={"pageNo": page, "prePageNo": page_size, "division": None},
                referer=inventory_ref,
            )
            rows = data.get("data") or []
            for row in rows:
                if isinstance(row, dict) and row.get("itemCodeIdx") is not None:
                    items.append(self._inventory_from_row(row))

            total = int(data.get("totalCount") or 0)
            if not rows or len(items) >= total:
                break
            page += 1

        if with_codes:
            for item in items:
                # get_drops_use_info.php is the only known detail/use endpoint.  Do
                # not touch an unclaimed row merely to decorate the inventory.
                if item.claimed:
                    await self._fill_redeem_code(item)
        return items

    async def _fill_redeem_code(self, item: InventoryItem) -> None:
        try:
            detail = await self.get_item_detail(item.item_code_idx)
        except Exception:
            return
        code = detail.get("itemCode")
        if code:
            item.redeem_code = str(code)
        if detail.get("expDate"):
            item.exp_date = str(detail["expDate"])
        desc = detail.get("itemDescription") or detail.get("ingameItemStr")
        if desc:
            item.description = str(desc)

    async def get_item_detail(self, item_code_idx: str) -> dict[str, Any]:
        data = await self.get_item_detail_response(item_code_idx)
        payload = data.get("data") or data
        return payload if isinstance(payload, dict) else {}

    async def get_item_detail_response(self, item_code_idx: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "get_drops_use_info.php",
            json_body={"itemCodeIdx": item_code_idx},
            referer=f"{DROPS_ORIGIN}/inventory",
        )

    async def claim_item(self, item_code_idx: str) -> dict[str, Any]:
        """Call the only repository-confirmed use/detail endpoint.

        This method intentionally does not claim success; callers must verify the
        inventory transition with :meth:`claim_and_verify`.
        """
        return await self.get_item_detail_response(item_code_idx)

    @staticmethod
    def _claim_response_confirmed(data: dict[str, Any]) -> bool:
        checks = (
            data.get("result") in (1, "1", True),
            data.get("success") is True,
            data.get("nRet") in (0, "0"),
        )
        return any(checks)

    async def claim_and_verify(
        self,
        item_code_idx: str,
        *,
        max_attempts: int = 2,
    ) -> ClaimResult:
        max_attempts = max(1, min(int(max_attempts), 3))
        before_items = await self.get_inventory(with_codes=False)
        before = next((item for item in before_items if item.item_code_idx == item_code_idx), None)
        if before is None:
            return ClaimResult(item_code_idx, ClaimStatus.NOT_CLAIMABLE, "背包中不存在该物品")
        if before.claimed:
            return ClaimResult(item_code_idx, ClaimStatus.ALREADY_CLAIMED, "物品已处于领取状态")

        last_message = "领取接口未确认"
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self.claim_item(item_code_idx)
                response_ok = self._claim_response_confirmed(response)
                after_items = await self.get_inventory(with_codes=False)
                after = next((item for item in after_items if item.item_code_idx == item_code_idx), None)
                changed = after is not None and not before.claimed and after.claimed
                if response_ok and changed:
                    payload = response.get("data") if isinstance(response.get("data"), dict) else response
                    code = payload.get("itemCode") if isinstance(payload, dict) else None
                    return ClaimResult(
                        item_code_idx,
                        ClaimStatus.CLAIMED,
                        "接口明确成功且 Inventory useFlag 已变为 Y",
                        str(code) if code else None,
                        attempt,
                    )
                if not response_ok:
                    last_message = "领取接口未返回明确成功字段"
                elif not changed:
                    last_message = "领取接口响应成功，但 Inventory useFlag 未变化"
            except Exception as exc:
                last_message = f"领取请求失败: {exc}"
            if attempt < max_attempts:
                import asyncio

                await asyncio.sleep(attempt)

        return ClaimResult(
            item_code_idx,
            ClaimStatus.UNCONFIRMED,
            last_message,
            attempts=max_attempts,
        )
