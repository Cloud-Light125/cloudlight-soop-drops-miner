from __future__ import annotations

from typing import Any

import aiohttp

from .constants import DROPS_API, DROPS_ORIGIN, USER_AGENT
from .models import InventoryItem, Mission


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
            return data

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
        data = await self._request(
            "POST",
            "get_drops_use_info.php",
            json_body={"itemCodeIdx": item_code_idx},
            referer=f"{DROPS_ORIGIN}/inventory",
        )
        payload = data.get("data") or data
        return payload if isinstance(payload, dict) else {}

    async def claim_item(self, item_code_idx: str) -> dict[str, Any]:
        return await self.get_item_detail(item_code_idx)
