from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from .auth import login, userid_from_cookies
from .center import BridgeSession
from .config import AppConfig, load_settings, snapshot_settings
from .constants import (
    HEARTBEAT_INTERVAL,
    NETWORK_RECOVER_COOLDOWN,
)
from .drops import ClaimStatus, DropsAuthenticationError, DropsClient
from .channel import (
    ChannelConfig,
    ONE_STREAM_NOTICE,
    active_progress_missions,
    channel_matches_mission_category,
    collect_mission_channels,
    ended_fixed_missions,
    fetch_live_drops_channels,
    has_active_fixed_missions,
    has_active_lottery_missions,
    is_category_fixed_mission,
    manual_channel_mismatch_warnings,
    mission_progresses_on_channel,
    missions_for_channel,
    pick_channel,
)
from .models import DropEvent, InventoryItem, LiveChannel, Mission
from .watch import HeartbeatStatus, WatchHeartbeat
from .network import AccountNetworkContext, AccountSession

logger = logging.getLogger("SoopDropsMiner")


class _AccountLogAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return f"[{self.extra['uid']}] {msg}", kwargs


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
    events: list[DropEvent] = field(default_factory=list)
    inventory: list[InventoryItem] = field(default_factory=list)
    available_channels: list[LiveChannel] = field(default_factory=list)
    heartbeat_last_success: str | None = None
    heartbeat_failures: int = 0
    heartbeat_result: str | None = None
    heartbeat_status: str | None = None
    connection_healthy: bool = False
    network_uploaded: int = 0
    network_downloaded: int = 0
    network_last_minute_bps: float = 0.0
    network_upload_bps: float = 0.0
    network_download_bps: float = 0.0
    bridge_last_activity_seconds: float | None = None
    network_estimated: bool = True


class SoopMiner:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        on_state: StateCallback | None = None,
        channel_config: ChannelConfig | None = None,
        app_config: AppConfig | None = None,
    ):
        self.cookies = cookies
        self.uid = userid_from_cookies(cookies)
        self._log = _AccountLogAdapter(logger, {"uid": self.uid})
        self._on_state = on_state
        self._channel_config = channel_config or ChannelConfig()
        self._app_config = snapshot_settings(app_config or load_settings())
        self._network = AccountNetworkContext(self.uid, self.cookies, self._app_config)
        self._session: AccountSession | None = None
        self._drops: DropsClient | None = None
        self._heartbeat: WatchHeartbeat | None = None
        self._bridge: BridgeSession | None = None
        self._current: LiveChannel | None = None
        self._missions: list[Mission] = []
        self._events: list[DropEvent] = []
        self._inventory: list[InventoryItem] = []
        self._available_channels: list[LiveChannel] = []
        self._stall_polls: dict[str, int] = {}
        self._last_view_times: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._running = False
        self._ended_logged: set[str] = set()
        self._empty_missions_logged = False
        self._had_missions = False
        self._last_network_recover = 0.0
        self._heartbeat_fail_streak = 0
        self._claimed_or_attempted: set[str] = set()
        self._bridge_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._recover_lock = asyncio.Lock()
        self._bridge_failed = asyncio.Event()
        self._auth_invalid = False

    @staticmethod
    def _is_session_closed_error(exc: BaseException) -> bool:
        return isinstance(exc, RuntimeError) and "session is closed" in str(exc).lower()

    async def _ensure_session(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        self._session = await self._network.open()
        self._drops = DropsClient(self._session)

    async def _recover_network(self, reason: str) -> None:
        async with self._recover_lock:
            now = time.monotonic()
            if now - self._last_network_recover < NETWORK_RECOVER_COOLDOWN:
                return
            self._last_network_recover = now
            self._log.warning("网络异常，正在恢复账号独立会话 (%s)…", reason)
            if self._bridge:
                try:
                    await self._bridge.close()
                except Exception:
                    pass
                self._bridge = None
            self._session = await self._network.recreate()
            self._drops = DropsClient(self._session)
            self._bridge_failed.clear()
            self._emit_state("重连中")
            if self._current and not self._stop.is_set():
                await self._ensure_bridge()
                if self._heartbeat and self._bridge and self._bridge.is_connected:
                    try:
                        heartbeat_status = await self._heartbeat.send(self._session)
                        if heartbeat_status is not HeartbeatStatus.FAILURE:
                            self._heartbeat_fail_streak = 0
                            self._log.info("账号会话已重建并收到心跳响应")
                            return
                    except Exception as exc:
                        self._log.warning("重建后的首次心跳失败: %s", exc)
            self._log.warning("账号会话已重建，但尚未确认心跳恢复")

    def _log_stall(self, mission: Mission, item) -> None:
        bridge_ok = self._bridge is not None and self._bridge.is_connected
        ch = self._current
        ch_info = f"{ch.user_nick}({ch.user_id})" if ch else "无"
        cate_hint = ""
        if mission.is_lottery and ch:
            ok = channel_matches_mission_category(mission, ch)
            need = mission.category_name or mission.category_no or "?"
            cate_hint = f" 分类匹配={'是' if ok else f'否(需要 {need})'}"
        self._log.warning(
            "进度 %d 分钟无变化 (%d/%d) · 直播间 %s · bridge=%s%s",
            self._stall_polls.get(mission.drops_idx, 0),
            item.view_time,
            item.give_term,
            ch_info,
            "已连接" if bridge_ok else "未连接",
            cate_hint,
        )
        if mission.is_event_ended or mission.is_truly_ended:
            self._log.warning("  ↳ 该任务已结束，进度不会再增加")
        elif mission.is_lottery and ch and not channel_matches_mission_category(mission, ch):
            self._log.warning("  ↳ 抽奖型需挂「%s」分类的 #드롭스 直播间", mission.category_name or mission.category_no)
        elif mission.is_fixed and ch and is_category_fixed_mission(mission):
            if not channel_matches_mission_category(mission, ch):
                self._log.warning(
                    "  ↳ 分类固定型需挂「%s」分类的 #드롭스 直播间",
                    mission.category_name or mission.category_no,
                )

    def _log_channel_missions(self, channel: LiveChannel) -> None:
        served = missions_for_channel(self._missions, channel)
        if served:
            parts: list[str] = []
            for m in served:
                item = m.active_item()
                if item:
                    parts.append(f"{m.title[:24]} ({item.view_time}/{item.give_term})")
            self._log.info("本频道可累计: %s", " · ".join(parts))
        pending = [
            m
            for m in active_progress_missions(self._missions)
            if not mission_progresses_on_channel(m, channel)
        ]
        if pending:
            hints = [f"{m.category_name or m.title[:16]}" for m in pending]
            self._log.info(
                "需换台或调整优先任务才能累计: %s。%s",
                "、".join(hints),
                ONE_STREAM_NOTICE,
            )

    async def __aenter__(self) -> SoopMiner:
        await self._ensure_session()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._bridge:
            await self._bridge.close()
        self._bridge = None
        await self._network.close()
        self._session = None
        self._drops = None

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        self._emit_state("已停止")

    def get_state(self) -> MinerState:
        heartbeat = self._heartbeat
        stats = self._network.stats
        return MinerState(
            uid=self.uid,
            running=self._running and not self._stop.is_set(),
            status=self._status_text(),
            channel_id=self._current.user_id if self._current else None,
            channel_nick=self._current.user_nick if self._current else None,
            broad_no=self._current.broad_no if self._current else None,
            bridge_connected=self._bridge is not None and self._bridge.is_connected,
            missions=list(self._missions),
            events=list(self._events),
            inventory=list(self._inventory),
            available_channels=list(self._available_channels),
            heartbeat_last_success=(
                heartbeat.last_success_time.isoformat() if heartbeat and heartbeat.last_success_time else None
            ),
            heartbeat_failures=heartbeat.consecutive_failures if heartbeat else 0,
            heartbeat_result=heartbeat.last_response_result if heartbeat else None,
            heartbeat_status=heartbeat.last_response_status.value if heartbeat else None,
            connection_healthy=bool(
                heartbeat and heartbeat.connection_healthy and self._bridge and self._bridge.is_connected
            ),
            network_uploaded=stats.uploaded,
            network_downloaded=stats.downloaded,
            network_last_minute_bps=stats.last_minute_bps,
            network_upload_bps=stats.last_minute_upload_bps,
            network_download_bps=stats.last_minute_download_bps,
            bridge_last_activity_seconds=(self._bridge.seconds_since_last_activity if self._bridge else None),
        )

    def _status_text(self) -> str:
        if self._auth_invalid:
            return "登录已失效，请重新添加账号"
        if self._stop.is_set():
            return "已停止"
        if not self._running:
            return "空闲"
        if not self._current:
            return "等待直播间"
        if self._bridge is None:
            return "连接中"
        if ended_fixed_missions(self._missions) and not has_active_fixed_missions(self._missions):
            if any(m.is_not_yet_open for m in self._missions if m.is_fixed):
                return "未开放掉宝"
            if has_active_lottery_missions(self._missions):
                return "挂机中·固定型已结束"
            return "活动已结束"
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

    async def _resolve_channel(self) -> LiveChannel | None:
        await self._ensure_session()
        assert self._session
        return await pick_channel(self._session, self.cookies, self._missions, self._channel_config)

    def _log_progress(self) -> None:
        for mission in self._missions:
            if not mission.items:
                continue
            tag = mission.type_label
            active = mission.active_item()
            if active is None:
                self._log.info("[%s] %s 全部档位已完成", tag, mission.title)
                continue
            self._log.info("[%s] %s 累计观看 %d 分钟", tag, mission.title, active.view_time)
            active_idx = mission.items.index(active)
            for i, item in enumerate(mission.items):
                if item.mission_success:
                    self._log.info("  %s %s: 已完成", item.term_label, item.item_name)
                elif i == active_idx:
                    self._log.info(
                        "  %s %s: %d/%d 分钟 (%d%%) ← 当前",
                        item.term_label,
                        item.item_name,
                        item.view_time,
                        item.give_term,
                        item.percent,
                    )
                else:
                    self._log.info(
                        "  %s %s: %d/%d 分钟 (%d%%)",
                        item.term_label,
                        item.item_name,
                        item.view_time,
                        item.give_term,
                        item.percent,
                    )

    async def _fetch_missions(self) -> None:
        """拉取 mission 列表并记录进度（不选台）。"""
        await self._ensure_session()
        assert self._drops
        prev_times: dict[str, int] = {}
        prev_count = len(self._missions)
        for m in self._missions:
            item = m.active_item()
            if item:
                prev_times[m.drops_idx] = item.view_time

        self._missions = await self._drops.get_missions()
        self._available_channels = collect_mission_channels(self._missions)
        new_count = len(self._missions)

        if not self._missions:
            if not self._empty_missions_logged:
                progress_events = list(self._events)
                self._log.warning(
                    self._drops.empty_missions_hint(
                        self.uid,
                        progress_count=len(progress_events) or None,
                    )
                )
                if progress_events:
                    self._log.info(self._drops.progress_events_summary(progress_events))
                if self._channel_config.hang_without_missions:
                    self._log.info("无任务也会先进房；进房观看后 mission 任务会自动出现。")
                self._empty_missions_logged = True
        else:
            if not self._had_missions and new_count > 0:
                self._log.info("mission 任务已出现：共 %d 个", new_count)
                self._empty_missions_logged = False
            self._had_missions = True

            for mission in self._missions:
                if mission.is_event_ended and mission.drops_idx not in self._ended_logged:
                    if mission.is_not_yet_open:
                        self._log.warning(
                            "[%s] %s 当前未开放掉宝（截止 %s）",
                            mission.type_label,
                            mission.title,
                            mission.end_date or "?",
                        )
                    else:
                        self._log.warning(
                            "[%s] %s 活动已结束（截止 %s），继续挂机不会累计该任务进度",
                            mission.type_label,
                            mission.title,
                            mission.end_date or "?",
                        )
                    self._ended_logged.add(mission.drops_idx)

            self._log_progress()

            trackable = {m.drops_idx for m in active_progress_missions(self._missions)}
            for drops_idx in list(self._stall_polls):
                if drops_idx not in trackable:
                    self._stall_polls.pop(drops_idx, None)
                    self._last_view_times.pop(drops_idx, None)

            for mission in self._missions:
                if mission.drops_idx not in trackable:
                    continue
                if not mission_progresses_on_channel(mission, self._current):
                    continue
                item = mission.active_item()
                if not item:
                    continue
                did = mission.drops_idx
                if did in self._last_view_times:
                    delta = item.view_time - self._last_view_times[did]
                    if delta > 0:
                        self._log.info("  ↳ [%s] 观看进度 +%d 分钟", mission.title[:30], delta)
                        self._stall_polls[did] = 0
                    elif item.view_time == self._last_view_times[did]:
                        self._stall_polls[did] = self._stall_polls.get(did, 0) + 1
                        if self._stall_polls[did] >= 3:
                            self._log_stall(mission, item)
                self._last_view_times[did] = item.view_time

        # Every mission refresh is a new GUI snapshot: progress can change while
        # the mission count stays constant.
        self._emit_state()

    async def _fetch_events(self) -> None:
        """拉取活动总览，覆盖尚未加入当前账号 mission 的新活动。"""
        await self._ensure_session()
        assert self._drops
        self._events = await self._drops.get_progress_events()
        self._log.info("SOOP activity catalog refreshed events=%d", len(self._events))
        self._emit_state()

    async def _refresh_available_channels(self) -> None:
        """Refresh the live channel snapshot used by the Worker/UI."""
        await self._ensure_session()
        assert self._session
        if self._channel_config.mode == "smart":
            live, _ = await fetch_live_drops_channels(self._session, self.cookies, self._missions)
            self._available_channels = live
        else:
            channel = await self._resolve_channel()
            self._available_channels = [channel] if channel is not None else []
        self._emit_state()

    async def force_refresh(self) -> MinerState:
        """Immediately refresh missions, inventory and available channels.

        The previous in-memory snapshot is restored if any remote request
        fails, so callers can continue displaying the last successful data.
        """
        async with self._refresh_lock:
            previous = (
                self._missions,
                self._events,
                self._inventory,
                self._available_channels,
                self._current,
            )
            self._log.info("SOOP mission refresh started")
            try:
                await self._ensure_session()
                if not self._running and self._drops is not None:
                    await self._drops.ensure_drops_ready(on_info=self._log.info)
                await self._refresh_missions()
                await self._fetch_events()
                await self._refresh_inventory()
                await self._refresh_available_channels()
            except Exception:
                (
                    self._missions,
                    self._events,
                    self._inventory,
                    self._available_channels,
                    self._current,
                ) = previous
                self._emit_state()
                raise

            state = self.get_state()
            active = sum(1 for mission in state.missions if mission.is_event_active)
            self._log.info(
                "SOOP mission refresh completed missions=%d events=%d active=%d channels=%d",
                len(state.missions),
                len(state.events),
                active,
                len(state.available_channels),
            )
            self._emit_state()
            return state

    async def _sync_channel(self) -> None:
        """按策略选台并更新当前直播间。"""
        channel = await self._resolve_channel()
        if channel is None:
            mode_hint = {
                "smart": "智能选台",
                "manual": "手动选台",
                "owesports": f"仅 {self._channel_config.preferred_bjid}",
            }.get(self._channel_config.mode, "")
            if self._current is None:
                self._log.warning("没有可用的 Drops 直播间（%s），等待中...", mode_hint)
            self._emit_state("等待直播间")
            return

        if self._current is None or self._current.user_id != channel.user_id:
            self._log.info("切换到直播间: %s (%s) broadNo=%s", channel.user_nick, channel.user_id, channel.broad_no)
            if self._bridge:
                await self._bridge.close()
                self._bridge = None
            self._current = channel
            if self._heartbeat is None:
                self._heartbeat = WatchHeartbeat(self.uid, channel)
            else:
                self._heartbeat.switch_channel(channel)
            self._stall_polls.clear()
            self._last_view_times.clear()
            self._log_channel_missions(channel)
            if self._channel_config.mode == "manual":
                for msg in manual_channel_mismatch_warnings(channel, self._missions):
                    self._log.warning(msg)
        elif channel.broad_no and self._current.broad_no != channel.broad_no:
            self._log.info("刷新 broadNo: %s → %s", self._current.broad_no, channel.broad_no)
            self._current = channel
            if self._heartbeat:
                self._heartbeat.switch_channel(channel)

        self._emit_state()

    async def _refresh_missions(self, *, channel_first: bool = False) -> None:
        """刷新任务并同步直播间。启动时 channel_first=True：先进房再拉任务。"""
        if channel_first and self._channel_config.hang_without_missions:
            await self._sync_channel()
            await self._fetch_missions()
            await self._sync_channel()
        else:
            await self._fetch_missions()
            await self._sync_channel()

    async def _ensure_bridge(self) -> None:
        assert self._session and self._current
        async with self._bridge_lock:
            if self._bridge is not None and self._bridge.is_connected:
                return
            if self._bridge is not None:
                await self._bridge.close()
                self._bridge = None

            def _on_disconnect(exc: BaseException) -> None:
                self._bridge_failed.set()
                self._log.warning("Bridge 已失效: %s", exc)

            bridge = BridgeSession(
                self.cookies,
                self._current.user_id,
                on_disconnect=_on_disconnect,
            )
            try:
                await bridge.connect(self._session)
            except Exception as exc:
                await bridge.close()
                self._log.error("加入直播间失败: %s", exc)
                self._emit_state("进房失败")
                return
            self._bridge = bridge
            self._bridge_failed.clear()
        self._log.info(
            "bridge 已连接 → %s broadNo=%s center=%s:%s",
            self._current.user_id,
            bridge.broad_no or "?",
            bridge.center_ip or "?",
            bridge.center_port or "?",
        )
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
        if not self._app_config.auto_claim_enabled:
            await self._refresh_inventory()
            return
        items = await self._drops.get_inventory(with_codes=False)
        claimable = [
            it for it in items if it.can_claim and it.item_code_idx not in self._claimed_or_attempted
        ]
        for item in claimable:
            self._claimed_or_attempted.add(item.item_code_idx)
            try:
                result = await self._drops.claim_and_verify(item.item_code_idx, max_attempts=2)
                if result.status == ClaimStatus.CLAIMED:
                    masked = self._mask_code(result.redeem_code) if result.redeem_code else None
                    if masked:
                        self._log.info("已验证领取: %s  兑换码: %s", item.item_name, masked)
                    else:
                        self._log.info("已验证领取: %s", item.item_name)
                else:
                    self._log.warning("领取接口未确认 %s: %s", item.item_name, result.message)
            except Exception as exc:
                self._log.error("领取失败 %s: %s", item.item_name, exc)
        await self._refresh_inventory()

    @staticmethod
    def _mask_code(code: str) -> str:
        if len(code) <= 6:
            return "*" * len(code)
        return f"{code[:3]}{'*' * (len(code) - 6)}{code[-3:]}"

    async def _heartbeat_loop(self) -> None:
        tick = 0
        while not self._stop.is_set():
            if self._heartbeat and self._current:
                try:
                    await self._ensure_session()
                    assert self._session
                    if self._bridge is None or not self._bridge.is_connected:
                        await self._ensure_bridge()
                    if self._bridge is None or not self._bridge.is_connected:
                        raise ConnectionError("Bridge 未连接")
                    heartbeat_status = await self._heartbeat.send(self._session)
                    if heartbeat_status is HeartbeatStatus.SUCCESS:
                        self._heartbeat_fail_streak = 0
                    elif heartbeat_status is HeartbeatStatus.UNKNOWN:
                        self._heartbeat_fail_streak = 0
                        self._log.debug("心跳响应已收到，业务状态待确认: %s", self._heartbeat.last_response_result)
                    else:
                        self._heartbeat_fail_streak += 1
                        self._log.warning("心跳发送失败")
                        if self._heartbeat_fail_streak >= 3:
                            await self._recover_network("连续心跳失败")
                    self._emit_state()
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    self._heartbeat_fail_streak += 1
                    self._log.warning("心跳网络异常: %s", exc)
                    if self._heartbeat_fail_streak >= 3:
                        await self._recover_network("连续心跳网络异常")
                except RuntimeError as exc:
                    if self._is_session_closed_error(exc):
                        await self._recover_network("心跳")
                    else:
                        self._log.error("心跳异常: %s", exc)
                except Exception as exc:
                    self._heartbeat_fail_streak += 1
                    self._log.warning("心跳异常: %s", exc)
                    if self._heartbeat_fail_streak >= 3:
                        await self._recover_network("连续心跳异常")
                tick += 1
                if tick % 6 == 0 and self._heartbeat_fail_streak == 0:
                    self._log.debug("心跳 OK → %s", self._current.user_id)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

    async def _poll_loop(self) -> None:
        mission_due = 0.0
        inventory_due = 0.0
        channel_due = 0.0
        stats_due = 0.0
        while not self._stop.is_set():
            now = asyncio.get_event_loop().time()
            if now >= mission_due:
                try:
                    async with self._refresh_lock:
                        await self._fetch_missions()
                        await self._fetch_events()
                except asyncio.CancelledError:
                    raise
                except DropsAuthenticationError as exc:
                    self._handle_auth_expired(exc)
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    self._log.warning("刷新任务网络异常: %s", exc)
                    await self._recover_network("任务刷新")
                except RuntimeError as exc:
                    if self._is_session_closed_error(exc):
                        await self._recover_network("任务刷新")
                    else:
                        self._log.error("刷新任务失败: %s", exc)
                except Exception as exc:
                    self._log.error("刷新任务失败: %s", exc)
                mission_due = now + self._app_config.effective_mission_poll_interval

            if now >= channel_due:
                try:
                    async with self._refresh_lock:
                        await self._sync_channel()
                except Exception as exc:
                    self._log.warning("刷新直播间失败: %s", exc)
                channel_due = now + self._app_config.effective_channel_refresh_interval

            if now >= inventory_due:
                try:
                    async with self._refresh_lock:
                        await self._try_claim()
                except asyncio.CancelledError:
                    raise
                except DropsAuthenticationError as exc:
                    self._handle_auth_expired(exc)
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    self._log.warning("检查背包网络异常: %s", exc)
                    await self._recover_network("背包")
                except RuntimeError as exc:
                    if self._is_session_closed_error(exc):
                        await self._recover_network("背包")
                    else:
                        self._log.error("检查背包失败: %s", exc)
                except Exception as exc:
                    self._log.error("检查背包失败: %s", exc)
                inventory_due = now + self._app_config.effective_inventory_poll_interval

            if now >= stats_due:
                bps = self._network.stats.last_minute_bps
                if bps >= 1_000_000:
                    details = sorted(
                        self._network.stats.by_type.items(),
                        key=lambda pair: pair[1].uploaded + pair[1].downloaded,
                        reverse=True,
                    )
                    culprit = ", ".join(
                        f"{name}={(bucket.uploaded + bucket.downloaded) / 1024:.1f} KiB"
                        for name, bucket in details[:3]
                    )
                    self._log.warning("估算流量异常偏高: %.2f Mbps；累计分类: %s", bps / 1_000_000, culprit)
                stats_due = now + 60.0

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                break
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        self._running = True
        self._stop.clear()
        mode_labels = {
            "smart": "智能选台",
            "manual": "手动选台",
            "owesports": f"仅 {self._channel_config.preferred_bjid}",
        }
        self._log.info("直播间策略: %s", mode_labels.get(self._channel_config.mode, self._channel_config.mode))
        try:
            await self._ensure_session()
            if self._session:
                await self._drops.ensure_drops_ready(on_info=self._log.info)
            await self._refresh_missions(channel_first=True)
            try:
                await self._fetch_events()
            except DropsAuthenticationError:
                raise
            except Exception as exc:
                self._log.warning("加载活动目录失败，稍后按任务刷新间隔重试: %s", exc)
        except DropsAuthenticationError as exc:
            self._handle_auth_expired(exc)
            return
        if not self._current:
            self._log.error("无法开始：没有可用的 Drops 直播间")
            self._running = False
            self._emit_state("无可用直播间")
            return

        self._log.info(
            "开始挂机 → %s (心跳 %.0fs · 任务刷新 %.0fs)",
            self._current.user_id,
            HEARTBEAT_INTERVAL,
            self._app_config.effective_mission_poll_interval,
        )
        try:
            await self._refresh_inventory()
        except DropsAuthenticationError as exc:
            self._handle_auth_expired(exc)
            return
        except Exception as exc:
            self._log.warning("加载背包失败: %s", exc)
        self._emit_state("挂机中")
        hb_task = asyncio.create_task(self._heartbeat_loop(), name=f"hb-{self.uid}")
        poll_task = asyncio.create_task(self._poll_loop(), name=f"poll-{self.uid}")
        try:
            await asyncio.gather(hb_task, poll_task)
        finally:
            for task in (hb_task, poll_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(hb_task, poll_task, return_exceptions=True)
        self._running = False
        self._emit_state("已停止")

    def _handle_auth_expired(self, exc: BaseException) -> None:
        self._auth_invalid = True
        self._running = False
        self._stop.set()
        self._log.error("登录已失效，请重新添加账号: %s", exc)
        self._emit_state("登录已失效，请重新添加账号")


async def run_miner(*, userid: str | None = None, password: str | None = None) -> None:
    from .multi_miner import MultiMinerManager

    app_config = load_settings()
    cookies_map = load_all_cookies()
    if userid and password:
        cookies_map[userid] = await login(userid, password, config=app_config)
    elif not cookies_map:
        raise SystemExit('请先登录: python "entry.py" --cli --userid 账号 --password 密码')

    loop = asyncio.get_running_loop()
    manager = MultiMinerManager(app_config=app_config)

    def _handle_stop() -> None:
        logger.info("正在停止...")
        manager.stop_all()

    if hasattr(signal, "SIGINT"):
        try:
            loop.add_signal_handler(signal.SIGINT, _handle_stop)
            loop.add_signal_handler(signal.SIGTERM, _handle_stop)
        except NotImplementedError:
            pass

    await manager.start_all(cookies_map)
    await manager.wait()
    await manager.shutdown()
