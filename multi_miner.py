from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .channel import ChannelConfig
from .auth import load_all_cookies, userid_from_cookies
from .miner import MinerState, SoopMiner
from .config import AppConfig, load_settings, snapshot_settings

logger = logging.getLogger("SoopDropsMiner")

StateCallback = Callable[[MinerState], None]


class MultiMinerManager:
    """并行管理多个 SoopMiner 实例。"""

    def __init__(
        self,
        on_state: StateCallback | None = None,
        channel_config: ChannelConfig | None = None,
        app_config: AppConfig | None = None,
    ) -> None:
        self._on_state = on_state
        self._channel_config = channel_config
        self._app_config = snapshot_settings(app_config or load_settings())
        self._miners: dict[str, SoopMiner] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def running_uids(self) -> list[str]:
        return [uid for uid, m in self._miners.items() if m.get_state().running]

    def _callback(self, state: MinerState) -> None:
        if self._on_state:
            try:
                self._on_state(state)
            except Exception:
                pass

    async def start_all(self, cookies_map: dict[str, dict[str, str]] | None = None) -> list[str]:
        """启动全部账号，返回成功启动的 uid 列表。"""
        if cookies_map is None:
            cookies_map = load_all_cookies()
        started: list[str] = []
        for uid, cookies in cookies_map.items():
            if uid in self._miners and self._miners[uid].get_state().running:
                continue
            try:
                await self.start_account(cookies)
                started.append(uid)
            except Exception as exc:
                logger.error("[%s] 启动失败: %s", uid, exc)
        return started

    async def start_account(self, cookies: dict[str, str]) -> None:
        uid = userid_from_cookies(cookies)
        if uid in self._miners:
            if self._miners[uid].get_state().running:
                return
            await self._cleanup_uid(uid)

        miner = SoopMiner(
            cookies,
            on_state=self._callback,
            channel_config=self._channel_config,
            app_config=snapshot_settings(self._app_config),
        )
        await miner.__aenter__()
        self._miners[uid] = miner
        self._tasks[uid] = asyncio.create_task(self._run_miner(uid, miner))

    async def _run_miner(self, uid: str, miner: SoopMiner) -> None:
        try:
            await miner.run()
        except Exception as exc:
            logger.error("[%s] 运行异常: %s", uid, exc)
        finally:
            if uid in self._miners and self._miners[uid] is miner:
                await miner.__aexit__()
                self._miners.pop(uid, None)
                self._tasks.pop(uid, None)
                self._callback(MinerState(uid=uid, status="已停止"))

    def stop_account(self, uid: str) -> None:
        if uid in self._miners:
            self._miners[uid].stop()

    async def stop_account_and_wait(self, uid: str) -> None:
        """Stop one account and wait until its network resources are closed."""
        await self._cleanup_uid(uid)

    def stop_all(self) -> None:
        for miner in self._miners.values():
            miner.stop()

    async def _cleanup_uid(self, uid: str) -> None:
        if uid in self._miners:
            self._miners[uid].stop()
        task = self._tasks.get(uid)
        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        if uid in self._miners:
            await self._miners[uid].__aexit__()
            self._miners.pop(uid, None)
            self._tasks.pop(uid, None)

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def shutdown(self) -> None:
        self.stop_all()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for uid in list(self._miners.keys()):
            await self._miners[uid].__aexit__()
        self._miners.clear()
        self._tasks.clear()

    def get_miner(self, uid: str) -> SoopMiner | None:
        return self._miners.get(uid)
