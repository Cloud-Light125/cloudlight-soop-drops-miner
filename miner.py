from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from .auth import apply_cookies, load_cookies, login, userid_from_cookies
from .center import BridgeSession
from .constants import HEARTBEAT_INTERVAL, INVENTORY_POLL_INTERVAL, MISSION_POLL_INTERVAL
from .drops import DropsClient
from .models import InventoryItem, LiveChannel, Mission
from .watch import WatchHeartbeat

logger = logging.getLogger("SoopDropsMiner")

StateCallback = Callable[["MinerState"], None]


@dataclass
class MinerState:
    uid: str
    running: bool = False
    status: str = "空闲"
    channel_id: str | None = None
    channel_nick: str | None = None
    broad_no: str | None = None
    bridge_connected: bool = False
    missions: list[Mission] = field(default_factory=list)
    inventory: list[InventoryItem] = field(default_factory=list)


class SoopMiner:
    def __init__(self, cookies: dict[str, str], *, on_state: StateCallback | None = None):
        self.cookies = cookies
        self.uid = userid_from_cookies(cookies)
        self._on_state = on_state
        self._session: aiohttp.ClientSession | None = None
        self._drops: DropsClient | None = None
        self._heartbeat: WatchHeartbeat | None = None
        self._bridge: BridgeSession | None = None
        self._current: LiveChannel | None = None
        self._missions: list[Mission] = []
        self._inventory: list[InventoryItem] = []
        self._stall_polls = 0
        self._last_view_time: int | None = None
        self._stop = asyncio.Event()
        self._running = False

    async def __aenter__(self) -> SoopMiner:
        self._session = aiohttp.ClientSession()
        apply_cookies(self._session, self.cookies)
        self._drops = DropsClient(self._session)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._bridge:
            await self._bridge.close()
        if self._session:
            await self._session.close()

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        self._emit_state("已停止")

    def get_state(self) -> MinerState:
        return MinerState(
            uid=self.uid,
            running=self._running and not self._stop.is_set(),
            status=self._status_text(),
            channel_id=self._current.user_id if self._current else None,
            channel_nick=self._current.user_nick if self._current else None,
            broad_no=self._current.broad_no if self._current else None,
            bridge_connected=self._bridge is not None and self._bridge.is_connected,
            missions=list(self._missions),
            inventory=list(self._inventory),
        )

    def _status_text(self) -> str:
        if self._stop.is_set():
            return "已停止"
        if not self._running:
            return "空闲"
        if not self._current:
            return "等待直播间"
        if self._bridge is None:
            return "连接中"
        return "挂机中"

    def _emit_state(self, status: str | None = None) -> None:
        if self._on_state is None:
            return
        state = self.get_state()
        if status is not None:
            state.status = status
        try:
            self._on_state(state)
        except Exception:
            pass

    def _pick_channel(self) -> LiveChannel | None:
        online: list[LiveChannel] = []
        for mission in self._missions:
            online.extend(mission.online_channels())
        if not online:
            return None
        if self._current:
            for ch in online:
                if ch.user_id == self._current.user_id:
                    return ch
        return online[0]

    def _log_progress(self) -> None:
        for mission in self._missions:
            if not mission.items:
                continue
            active = mission.active_item()
            if active is None:
                logger.info("[%s] 全部档位已完成", mission.title)
                continue
            logger.info("[%s] 累计观看 %d 分钟", mission.title, active.view_time)
            active_idx = mission.items.index(active)
            for i, item in enumerate(mission.items):
                if item.mission_success:
                    logger.info("  %s %s: 已完成", item.term_label, item.item_name)
                elif i == active_idx:
                    logger.info(
                        "  %s %s: %d/%d 分钟 (%d%%) ← 当前",
                        item.term_label,
                        item.item_name,
                        item.view_time,
                        item.give_term,
                        item.percent,
                    )
                else:
                    logger.info(
                        "  %s %s: %d/%d 分钟 (%d%%)",
                        item.term_label,
                        item.item_name,
                        item.view_time,
                        item.give_term,
                        item.percent,
                    )

    async def _refresh_missions(self) -> None:
        assert self._drops
        prev_times: dict[str, int] = {}
        for m in self._missions:
            item = m.active_item()
            if item:
                prev_times[m.drops_idx] = item.view_time

        self._missions = await self._drops.get_missions()
        if not self._missions:
            logger.warning("当前没有进行中的 Drops 任务")
            return

        self._log_progress()

        for mission in self._missions:
            item = mission.active_item()
            if item and mission.drops_idx in prev_times:
                delta = item.view_time - prev_times[mission.drops_idx]
                if delta > 0:
                    logger.info("  ↳ 观看进度 +%d 分钟", delta)
                    self._stall_polls = 0
                    self._last_view_time = item.view_time
                elif self._last_view_time is not None and item.view_time == self._last_view_time:
                    self._stall_polls += 1
                    if self._stall_polls >= 3:
                        logger.warning(
                            "进度已 %d 分钟无变化 (%d/%d)，请确认 bridge 进房会话正常",
                            self._stall_polls,
                            item.view_time,
                            item.give_term,
                        )
            elif item and self._last_view_time is None:
                self._last_view_time = item.view_time

        channel = self._pick_channel()
        if channel is None:
            logger.warning("没有在线的 Drops 直播间，等待中...")
            self._emit_state("等待直播间")
            return

        if self._current is None or self._current.user_id != channel.user_id:
            logger.info("切换到直播间: %s (%s) broadNo=%s", channel.user_nick, channel.user_id, channel.broad_no)
            if self._bridge:
                await self._bridge.close()
                self._bridge = None
            self._current = channel
            if self._heartbeat is None:
                self._heartbeat = WatchHeartbeat(self.uid, channel)
            else:
                self._heartbeat.switch_channel(channel)
        elif channel.broad_no and self._current.broad_no != channel.broad_no:
            logger.info("刷新 broadNo: %s → %s", self._current.broad_no, channel.broad_no)
            self._current = channel
            if self._heartbeat:
                self._heartbeat.switch_channel(channel)

        self._emit_state()

    async def _ensure_bridge(self) -> None:
        assert self._session and self._current
        if self._bridge is not None:
            return
        bridge = BridgeSession(self.cookies, self._current.user_id)
        try:
            await bridge.connect(self._session)
        except Exception as exc:
            logger.error("加入直播间失败: %s", exc)
            self._emit_state("进房失败")
            return
        self._bridge = bridge
        self._emit_state()
        if self._heartbeat and bridge.center_ip and bridge.center_port:
            self._heartbeat.switch_channel(
                self._current,
                center_ip=bridge.center_ip,
                center_port=bridge.center_port,
            )
        if self._current.broad_no != bridge.broad_no and bridge.broad_no:
            self._current = LiveChannel(
                user_id=self._current.user_id,
                user_nick=self._current.user_nick,
                broad_no=bridge.broad_no,
                on_air=True,
            )
            if self._heartbeat:
                self._heartbeat.switch_channel(
                    self._current,
                    center_ip=bridge.center_ip or None,
                    center_port=bridge.center_port or None,
                )

    async def _refresh_inventory(self) -> None:
        assert self._drops
        self._inventory = await self._drops.get_inventory(with_codes=True)
        self._emit_state()

    async def _try_claim(self) -> None:
        assert self._drops
        items = await self._drops.get_inventory(with_codes=False)
        claimable = [it for it in items if it.can_claim]
        for item in claimable:
            try:
                result = await self._drops.claim_item(item.item_code_idx)
                code = result.get("itemCode") if isinstance(result, dict) else None
                if code:
                    logger.info("已领取: %s  兑换码: %s", item.item_name, code)
                else:
                    logger.info("已领取: %s", item.item_name)
            except Exception as exc:
                logger.error("领取失败 %s: %s", item.item_name, exc)
        await self._refresh_inventory()

    async def _heartbeat_loop(self) -> None:
        assert self._session
        tick = 0
        while not self._stop.is_set():
            if self._heartbeat and self._current:
                if self._bridge is None:
                    await self._ensure_bridge()
                ok = await self._heartbeat.send(self._session)
                if not ok:
                    logger.warning("心跳发送失败")
                tick += 1
                if tick % 6 == 0:
                    logger.debug("心跳 OK → %s", self._current.user_id)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

    async def _poll_loop(self) -> None:
        mission_due = 0.0
        inventory_due = 0.0
        while not self._stop.is_set():
            now = asyncio.get_event_loop().time()
            if now >= mission_due:
                try:
                    await self._refresh_missions()
                except Exception as exc:
                    logger.error("刷新任务失败: %s", exc)
                mission_due = now + MISSION_POLL_INTERVAL

            if now >= inventory_due:
                try:
                    await self._try_claim()
                except Exception as exc:
                    logger.error("检查背包失败: %s", exc)
                inventory_due = now + INVENTORY_POLL_INTERVAL

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                break
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        self._running = True
        self._stop.clear()
        await self._refresh_missions()
        if not self._current:
            logger.error("无法开始：没有可用的 Drops 直播间")
            self._running = False
            self._emit_state("无可用直播间")
            return

        logger.info("开始挂机 → %s (每 %.0fs 心跳)", self._current.user_id, HEARTBEAT_INTERVAL)
        try:
            await self._refresh_inventory()
        except Exception as exc:
            logger.warning("加载背包失败: %s", exc)
        self._emit_state("挂机中")
        await asyncio.gather(self._heartbeat_loop(), self._poll_loop())
        self._running = False
        self._emit_state("已停止")


async def run_miner(*, userid: str | None = None, password: str | None = None) -> None:
    cookies = load_cookies()
    if userid and password:
        cookies = await login(userid, password)
    elif not cookies:
        raise SystemExit("请先登录: python -m soop_miner --userid 账号 --password 密码")

    loop = asyncio.get_running_loop()
    miner: SoopMiner | None = None

    async with SoopMiner(cookies) as m:
        miner = m

        def _handle_stop() -> None:
            logger.info("正在停止...")
            if miner:
                miner.stop()

        if hasattr(signal, "SIGINT"):
            try:
                loop.add_signal_handler(signal.SIGINT, _handle_stop)
                loop.add_signal_handler(signal.SIGTERM, _handle_stop)
            except NotImplementedError:
                pass

        await m.run()
