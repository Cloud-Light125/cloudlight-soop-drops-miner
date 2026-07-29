from __future__ import annotations

import asyncio
import logging
import platform
import queue
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from typing import Any, Callable

import customtkinter as ctk
from tkinter import messagebox

from .auth import list_accounts, load_all_cookies, load_cookies, login, remove_account
from .channel import (
    ChannelConfig,
    ONE_STREAM_NOTICE,
    PRIORITY_MISSION_AUTO,
    fetch_live_drops_channels,
    format_channel_drops_label,
    parse_stream_input,
)
from .config import AppConfig, SETTINGS_VERSION, load_settings, reset_settings, save_settings, snapshot_settings
from .constants import (
    APP_NAME,
    AUTHOR,
    AUTHOR_BY,
    DEFAULT_CHANNEL_BJID,
    DISCLAIMER_ACCEPTED_PATH,
    DISCLAIMER_TEXT,
    DROPS_INVENTORY_URL,
    GITHUB_REPOSITORY_URL,
    LICENSE_NAME,
    PAGE_SUBTITLE,
    PLAY_ORIGIN,
    VERSION,
    WINDOW_TITLE,
)
from .drops import ClaimStatus, DropsClient
from .miner import MinerState
from .models import InventoryItem, LiveChannel, Mission
from .multi_miner import MultiMinerManager
from .network import AccountNetworkContext, ProxyTestResult, test_proxy_connectivity
from .single_instance import release_single_instance
from .systray import TrayMenuState, WinSystray, consume_show_request, create_systray
from .ui_components import AccountRow, CollapsibleCard, InventoryRow, MissionCard
from .ui_state import (
    BoundedLogBuffer,
    CallbackMailbox,
    LatestStateMailbox,
    account_ui_state,
    format_bytes,
    format_rate,
    inventory_ui_states,
    mission_ui_states,
)
from .ui_theme import CARD_PAD, CARD_RADIUS, COLORS, CONTROL_RADIUS, MIN_WINDOW_SIZE, WINDOW_SIZE, configure_appearance, font
from .windows_startup import apply_auto_start_setting


logger = logging.getLogger("SoopDropsMiner")


class _QueuedLogHandler(logging.Handler):
    def __init__(self, target: queue.SimpleQueue[tuple[str, str, str]]) -> None:
        super().__init__()
        self._target = target
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            # Defensive display redaction. Core loggers already avoid credentials.
            for marker in ("AuthTicket=", "BbsTicket=", "UserTicket=", "password="):
                if marker in text:
                    text = text.split(marker, 1)[0] + marker + "***"
            account = ""
            if "]" in text and "[" in text:
                account = text.split("[", 1)[1].split("]", 1)[0]
            self._target.put((record.levelname, account, text))
        except Exception:
            self.handleError(record)


class ModernSoopGui:
    """CustomTkinter single-page shell around the existing backend services."""

    def __init__(self, *, settings: AppConfig | None = None, start_hidden_to_tray: bool = False) -> None:
        self._app_config = snapshot_settings(settings or load_settings())
        configure_appearance(self._app_config.appearance_mode)
        self._first_run = not DISCLAIMER_ACCEPTED_PATH.is_file()
        self._requested_hidden = bool(start_hidden_to_tray or self._app_config.start_minimized_to_tray)
        self._in_tray = self._requested_hidden and not self._first_run

        self.root = ctk.CTk()
        if self._in_tray:
            self.root.withdraw()
        self.root.title(WINDOW_TITLE)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*MIN_WINDOW_SIZE)

        self._manager: MultiMinerManager | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._states: dict[str, MinerState] = {}
        self._selected_uid: str | None = None
        self._selected_inventory_key: tuple[str, str] | None = None
        self._all_inventory: list[tuple[str, InventoryItem]] = []
        self._cached_missions: list[Mission] = []
        self._cached_channels: list[LiveChannel] = []
        self._channel_map: dict[str, LiveChannel] = {}
        self._account_rows: dict[str, AccountRow] = {}
        self._mission_cards: dict[tuple[str, str], MissionCard] = {}
        self._inventory_rows: dict[tuple[str, str], InventoryRow] = {}
        self._latest_account_ui: dict[str, Any] = {}
        self._latest_mission_ui: dict[tuple[str, str], Any] = {}
        self._latest_inventory_ui: dict[tuple[str, str], Any] = {}
        self._state_mailbox = LatestStateMailbox()
        self._callback_mailbox = CallbackMailbox()
        self._log_queue: queue.SimpleQueue[tuple[str, str, str]] = queue.SimpleQueue()
        self._log_buffer = BoundedLogBuffer(4000, 3500)
        self._visible_log_entries: list[tuple[str, str, str]] = []
        self._after_ids: set[str] = set()
        self._restore_poll_id: str | None = None
        self._channel_refresh_timer: str | None = None
        self._state_poll_id: str | None = None
        self._log_poll_id: str | None = None
        self._quitting = False
        self._stopping = False
        self._starting = False
        self._shutdown_deadline = 0.0
        self._channel_loading = False
        self._inventory_loading = False
        self._proxy_testing = False
        self._tray: WinSystray | None = None
        self._about_window: ctk.CTkToplevel | None = None
        self._settings_dirty = False
        self._settings_baseline = snapshot_settings(self._app_config)

        self._build_ui()
        self._setup_logging()
        self._refresh_accounts_from_disk()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tray = create_systray(
            tip=WINDOW_TITLE,
            on_show=self._restore_from_tray,
            on_start_all=lambda: self._schedule_ui(self._on_start_all),
            on_stop_all=lambda: self._schedule_ui(self._on_stop_all),
            on_exit=lambda: self._schedule_ui(self._quit_app),
        )
        if self._tray is None and self._in_tray:
            self._in_tray = False
            self.root.deiconify()
        self._schedule_dispatch()
        self._start_restore_poll()
        self._refresh_header()
        logger.info("%s GUI 启动%s", WINDOW_TITLE, "（托盘模式）" if self._in_tray else "")

    # ---------- construction ----------
    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self._page = ctk.CTkScrollableFrame(self.root, corner_radius=0, fg_color=COLORS["surface_alt"])
        self._page.grid(row=0, column=0, sticky="nsew")
        self._page.grid_columnconfigure(0, weight=1)

        row = 0
        self._header_card = ctk.CTkFrame(self._page, corner_radius=CARD_RADIUS, fg_color=COLORS["surface"])
        self._header_card.grid(row=row, column=0, sticky="ew", padx=14, pady=(14, 6))
        self._build_header(self._header_card)
        row += 1

        two = ctk.CTkFrame(self._page, fg_color="transparent")
        two.grid(row=row, column=0, sticky="ew", padx=14, pady=6)
        two.grid_columnconfigure(0, weight=3, uniform="main")
        two.grid_columnconfigure(1, weight=2, uniform="main")
        accounts = ctk.CTkFrame(two, corner_radius=CARD_RADIUS, fg_color=COLORS["surface"])
        accounts.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        details = ctk.CTkFrame(two, corner_radius=CARD_RADIUS, fg_color=COLORS["surface"])
        details.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._build_accounts(accounts)
        self._build_details(details)
        row += 1

        channel = ctk.CTkFrame(self._page, corner_radius=CARD_RADIUS, fg_color=COLORS["surface"])
        channel.grid(row=row, column=0, sticky="ew", padx=14, pady=6)
        self._build_channel(channel)
        row += 1

        missions = ctk.CTkFrame(self._page, corner_radius=CARD_RADIUS, fg_color=COLORS["surface"])
        missions.grid(row=row, column=0, sticky="ew", padx=14, pady=6)
        self._build_missions(missions)
        row += 1

        self._inventory_card = CollapsibleCard(self._page, "奖励背包", expanded=True)
        self._inventory_card.grid(row=row, column=0, sticky="ew", padx=14, pady=6)
        self._build_inventory(self._inventory_card.body)
        row += 1

        self._settings_card = CollapsibleCard(self._page, "设置", expanded=False)
        self._settings_card.grid(row=row, column=0, sticky="ew", padx=14, pady=6)
        self._build_settings(self._settings_card.body)
        row += 1

        self._log_card = CollapsibleCard(self._page, "日志", expanded=True)
        self._log_card.grid(row=row, column=0, sticky="ew", padx=14, pady=(6, 14))
        self._build_logs(self._log_card.body)

    def _build_header(self, host: ctk.CTkFrame) -> None:
        host.grid_columnconfigure(0, weight=1)
        title = ctk.CTkFrame(host, fg_color="transparent")
        title.grid(row=0, column=0, sticky="ew", padx=CARD_PAD, pady=(14, 4))
        title.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(title, text=APP_NAME, font=font(25, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title, text=f"{PAGE_SUBTITLE}  ·  版本 {VERSION}  ·  {AUTHOR_BY}", text_color=COLORS["muted"], font=font(12)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._overall_status = ctk.CTkLabel(title, text="就绪", font=font(13, "bold"), text_color=COLORS["success"])
        self._overall_status.grid(row=0, column=1, rowspan=2, sticky="e")

        self._header_stats = ctk.CTkLabel(host, text="", anchor="w", justify="left", font=font(12), text_color=COLORS["muted"])
        self._header_stats.grid(row=1, column=0, sticky="ew", padx=CARD_PAD, pady=(4, 8))
        actions = ctk.CTkFrame(host, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=CARD_PAD, pady=(0, 14))
        self._start_btn = self._button(actions, "全部开始", self._on_start_all, width=112)
        self._start_btn.pack(side="left")
        self._stop_btn = self._button(actions, "全部停止", self._on_stop_all, width=112, secondary=True)
        self._stop_btn.pack(side="left", padx=(8, 0))
        self._button(actions, "刷新状态", self._refresh_visible_data, width=104, secondary=True).pack(side="left", padx=(8, 0))
        self._button(actions, "隐藏到托盘", self._minimize_to_tray, width=116, secondary=True).pack(side="left", padx=(8, 0))
        self._button(actions, "关于", self._show_about, width=82, secondary=True).pack(side="right")

    def _section_title(self, host: Any, title: str, subtitle: str = "") -> None:
        ctk.CTkLabel(host, text=title, font=font(16, "bold"), anchor="w").grid(row=0, column=0, sticky="w", padx=CARD_PAD, pady=(14, 0))
        if subtitle:
            ctk.CTkLabel(host, text=subtitle, font=font(11), text_color=COLORS["muted"], anchor="w").grid(row=1, column=0, sticky="ew", padx=CARD_PAD, pady=(1, 8))

    def _build_accounts(self, host: ctk.CTkFrame) -> None:
        host.grid_columnconfigure(0, weight=1)
        self._section_title(host, "账号管理", "点击账号行查看详情；普通刷新会保持选择与滚动位置。")
        form = ctk.CTkFrame(host, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=CARD_PAD, pady=(0, 8))
        form.grid_columnconfigure((0, 1), weight=1)
        self._userid_var = ctk.StringVar()
        self._password_var = ctk.StringVar()
        ctk.CTkEntry(form, textvariable=self._userid_var, placeholder_text="SOOP 账号", corner_radius=CONTROL_RADIUS).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ctk.CTkEntry(form, textvariable=self._password_var, placeholder_text="密码", show="●", corner_radius=CONTROL_RADIUS).grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self._button(form, "添加账号", self._on_add_account, width=98).grid(row=0, column=2, padx=(10, 0))
        self._button(form, "删除账号", self._on_remove_account, width=98, secondary=True).grid(row=0, column=3, padx=(8, 0))
        headers = ctk.CTkFrame(host, fg_color="transparent")
        headers.grid(row=3, column=0, sticky="ew", padx=CARD_PAD)
        names = ("账号", "状态", "直播间", "任务", "进度", "Bridge", "心跳", "当前速率")
        weights = (1, 1, 2, 2, 1, 1, 1, 1)
        for i, (name, weight) in enumerate(zip(names, weights)):
            headers.grid_columnconfigure(i, weight=weight)
            ctk.CTkLabel(headers, text=name, font=font(11, "bold"), text_color=COLORS["muted"], anchor="w").grid(row=0, column=i, sticky="ew", padx=5)
        self._account_host = ctk.CTkScrollableFrame(host, height=190, fg_color="transparent")
        self._account_host.grid(row=4, column=0, sticky="nsew", padx=CARD_PAD, pady=(5, CARD_PAD))
        self._account_host.grid_columnconfigure(0, weight=1)
        self._account_empty = ctk.CTkLabel(self._account_host, text="尚未保存账号", text_color=COLORS["muted"])
        self._account_empty.grid(row=0, column=0, pady=28)

    def _build_details(self, host: ctk.CTkFrame) -> None:
        host.grid_columnconfigure(0, weight=1)
        self._section_title(host, "当前账号状态", "Bridge、心跳和流量均来自账号自身运行状态。")
        self._detail_hint = ctk.CTkLabel(host, text="请选择一个账号查看详细状态", text_color=COLORS["muted"], font=font(13))
        self._detail_hint.grid(row=2, column=0, pady=48)
        self._detail_grid = ctk.CTkFrame(host, fg_color="transparent")
        self._detail_grid.grid(row=2, column=0, sticky="nsew", padx=CARD_PAD, pady=(0, CARD_PAD))
        self._detail_grid.grid_columnconfigure(1, weight=1)
        self._detail_values: dict[str, ctk.CTkLabel] = {}
        rows = (
            ("UID", "uid"), ("运行状态", "status"), ("频道 / ID", "channel"), ("broadNo", "broad"),
            ("当前任务", "mission"), ("任务进度", "progress"), ("Bridge / 最近活动", "bridge"),
            ("最近心跳", "heartbeat"), ("连续失败 / 响应", "heartbeat_result"),
            ("上传 / 下载", "traffic"), ("累计流量", "total"), ("主要流量来源", "source"),
        )
        for i, (caption, key) in enumerate(rows):
            ctk.CTkLabel(self._detail_grid, text=caption, text_color=COLORS["muted"], anchor="w", font=font(11)).grid(row=i, column=0, sticky="nw", padx=(0, 12), pady=3)
            label = ctk.CTkLabel(self._detail_grid, text="—", anchor="w", justify="left", wraplength=360, font=font(12))
            label.grid(row=i, column=1, sticky="ew", pady=3)
            self._detail_values[key] = label
        self._detail_grid.grid_remove()

    def _build_channel(self, host: ctk.CTkFrame) -> None:
        host.grid_columnconfigure(0, weight=1)
        self._section_title(host, "直播间选择", ONE_STREAM_NOTICE)
        mode_row = ctk.CTkFrame(host, fg_color="transparent")
        mode_row.grid(row=2, column=0, sticky="ew", padx=CARD_PAD, pady=(0, 8))
        self._channel_mode = ctk.StringVar(value="smart")
        self._mode_control = ctk.CTkSegmentedButton(mode_row, values=["智能选台", "手动选台", "仅 owesports"], command=self._on_mode_segment, corner_radius=CONTROL_RADIUS)
        self._mode_control.set("智能选台")
        self._mode_control.pack(side="left")
        self._channel_refresh_btn = self._button(mode_row, "刷新频道", self._fetch_channels_async, width=104, secondary=True)
        self._channel_refresh_btn.pack(side="right")
        self._channel_body = ctk.CTkFrame(host, fg_color=COLORS["row"], corner_radius=10)
        self._channel_body.grid(row=3, column=0, sticky="ew", padx=CARD_PAD, pady=(0, CARD_PAD))
        self._channel_body.grid_columnconfigure(1, weight=1)
        self._channel_field_label = ctk.CTkLabel(self._channel_body, text="优先任务", anchor="w")
        self._priority_var = ctk.StringVar(value="自动优先")
        self._priority = ctk.CTkComboBox(self._channel_body, variable=self._priority_var, values=["自动优先"], state="readonly", corner_radius=CONTROL_RADIUS)
        self._manual_var = ctk.StringVar()
        self._manual_combo = ctk.CTkComboBox(self._channel_body, variable=self._manual_var, values=[""], corner_radius=CONTROL_RADIUS)
        self._channel_hint = ctk.CTkLabel(self._channel_body, text="智能选台会优先匹配当前任务与直播分类。", anchor="w", justify="left", wraplength=1000, text_color=COLORS["muted"])
        self._apply_channel_mode_ui()

    def _build_missions(self, host: ctk.CTkFrame) -> None:
        host.grid_columnconfigure(0, weight=1)
        self._section_title(host, "任务进度", "任务卡片和进度条长期复用，仅更新发生变化的字段。")
        self._mission_host = ctk.CTkFrame(host, fg_color="transparent")
        self._mission_host.grid(row=2, column=0, sticky="ew", padx=CARD_PAD, pady=(0, CARD_PAD))
        self._mission_host.grid_columnconfigure(0, weight=1)
        self._mission_empty = ctk.CTkLabel(self._mission_host, text="选择账号后显示任务进度", text_color=COLORS["muted"])
        self._mission_empty.grid(row=0, column=0, pady=26)

    def _build_inventory(self, host: ctk.CTkFrame) -> None:
        tools = ctk.CTkFrame(host, fg_color="transparent")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._inventory_refresh_btn = self._button(tools, "刷新背包", self._fetch_inventory_async, width=104, secondary=True)
        self._inventory_refresh_btn.pack(side="left")
        self._button(tools, "领取选中", self._claim_selected, width=104, secondary=True).pack(side="left", padx=(8, 0))
        self._button(tools, "复制兑换码", self._copy_selected_code, width=112, secondary=True).pack(side="left", padx=(8, 0))
        self._button(tools, "复制全部", self._copy_all_codes, width=96, secondary=True).pack(side="left", padx=(8, 0))
        self._button(tools, "打开官方背包", lambda: webbrowser.open(DROPS_INVENTORY_URL), width=120, secondary=True).pack(side="right")
        ctk.CTkLabel(host, text="自动领取仅在领取后重新读取 Inventory 并确认状态变化时显示“领取已确认”。兑换码默认掩码。", text_color=COLORS["muted"], anchor="w", font=font(11)).grid(row=1, column=0, sticky="ew", pady=(0, 6))
        headers = ctk.CTkFrame(host, fg_color="transparent")
        headers.grid(row=2, column=0, sticky="ew")
        for i, name in enumerate(("账号", "奖励名称", "领取状态", "兑换码", "获得时间", "过期时间")):
            headers.grid_columnconfigure(i, weight=2 if i == 1 else 1)
            ctk.CTkLabel(headers, text=name, font=font(11, "bold"), text_color=COLORS["muted"], anchor="w").grid(row=0, column=i, sticky="ew", padx=5)
        self._inventory_host = ctk.CTkFrame(host, fg_color="transparent")
        self._inventory_host.grid(row=3, column=0, sticky="ew")
        self._inventory_host.grid_columnconfigure(0, weight=1)
        self._inventory_empty = ctk.CTkLabel(self._inventory_host, text="背包暂无数据", text_color=COLORS["muted"])
        self._inventory_empty.grid(row=0, column=0, pady=24)

    def _build_settings(self, host: ctk.CTkFrame) -> None:
        self._setting_vars: dict[str, Any] = {
            "auto_start_enabled": ctk.BooleanVar(), "start_minimized_to_tray": ctk.BooleanVar(),
            "close_to_tray": ctk.BooleanVar(), "appearance_mode": ctk.StringVar(),
            "proxy_enabled": ctk.BooleanVar(), "proxy_url": ctk.StringVar(), "proxy_fallback_direct": ctk.BooleanVar(),
            "auto_claim_enabled": ctk.BooleanVar(), "low_bandwidth_mode": ctk.BooleanVar(),
            "mission_poll_interval": ctk.StringVar(), "inventory_poll_interval": ctk.StringVar(), "channel_refresh_interval": ctk.StringVar(),
        }
        self._load_settings_vars(self._app_config)
        body = ctk.CTkFrame(host, fg_color="transparent")
        body.grid(row=0, column=0, sticky="ew")
        body.grid_columnconfigure((0, 1, 2), weight=1, uniform="settings")
        general = self._settings_group(body, "常规", 0)
        self._switch(general, "开机自启", "auto_start_enabled", command=self._on_auto_start_toggle).pack(anchor="w", pady=3)
        self._switch(general, "启动后静默进入托盘", "start_minimized_to_tray").pack(anchor="w", pady=3)
        self._switch(general, "关闭窗口时隐藏到托盘", "close_to_tray").pack(anchor="w", pady=3)
        ctk.CTkLabel(general, text="主题", anchor="w").pack(fill="x", pady=(10, 3))
        self._theme_control = ctk.CTkSegmentedButton(general, values=["跟随系统", "浅色", "深色"], command=self._preview_theme)
        self._theme_control.set({"system": "跟随系统", "light": "浅色", "dark": "深色"}[self._app_config.appearance_mode])
        self._theme_control.pack(fill="x")

        network = self._settings_group(body, "网络", 1)
        self._switch(network, "启用代理", "proxy_enabled").pack(anchor="w", pady=3)
        ctk.CTkEntry(network, textvariable=self._setting_vars["proxy_url"], placeholder_text="http://127.0.0.1:7897", corner_radius=CONTROL_RADIUS).pack(fill="x", pady=(5, 3))
        self._switch(network, "代理失败后允许直连", "proxy_fallback_direct").pack(anchor="w", pady=3)
        self._proxy_test_btn = self._button(network, "测试代理", self._test_proxy, width=100, secondary=True)
        self._proxy_test_btn.pack(anchor="w", pady=(8, 4))
        self._proxy_test_status = ctk.CTkLabel(network, text="", justify="left", anchor="w", wraplength=340, text_color=COLORS["muted"], font=font(11))
        self._proxy_test_status.pack(fill="x")

        drops = self._settings_group(body, "掉宝", 2)
        self._switch(drops, "自动领取奖励", "auto_claim_enabled").pack(anchor="w", pady=3)
        self._switch(drops, "低流量模式", "low_bandwidth_mode").pack(anchor="w", pady=3)
        for label, key, range_text in (
            ("Mission 刷新", "mission_poll_interval", "30～600 秒"),
            ("背包刷新", "inventory_poll_interval", "60～1800 秒"),
            ("频道列表刷新", "channel_refresh_interval", "60～1800 秒"),
        ):
            line = ctk.CTkFrame(drops, fg_color="transparent")
            line.pack(fill="x", pady=3)
            ctk.CTkLabel(line, text=f"{label}\n{range_text}", anchor="w", justify="left", font=font(11)).pack(side="left")
            ctk.CTkEntry(line, textvariable=self._setting_vars[key], width=78, corner_radius=CONTROL_RADIUS).pack(side="right")
        self._settings_status = ctk.CTkLabel(host, text="已保存", text_color=COLORS["muted"], anchor="w")
        self._settings_status.grid(row=1, column=0, sticky="w", pady=(10, 0))
        actions = ctk.CTkFrame(host, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._button(actions, "恢复默认值", self._reset_settings_draft, width=112, secondary=True).pack(side="left")
        self._button(actions, "取消修改", self._cancel_settings_draft, width=104, secondary=True).pack(side="right", padx=(8, 0))
        self._button(actions, "保存设置", self._save_inline_settings, width=104).pack(side="right")
        for variable in self._setting_vars.values():
            variable.trace_add("write", lambda *_: self._mark_settings_dirty())

    def _settings_group(self, host: Any, title: str, column: int) -> ctk.CTkFrame:
        group = ctk.CTkFrame(host, fg_color=COLORS["row"], corner_radius=10)
        group.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
        ctk.CTkLabel(group, text=title, font=font(14, "bold"), anchor="w").pack(fill="x", padx=12, pady=(11, 7))
        content = ctk.CTkFrame(group, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        return content

    def _build_logs(self, host: ctk.CTkFrame) -> None:
        tools = ctk.CTkFrame(host, fg_color="transparent")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        self._log_level_var = ctk.StringVar(value="全部级别")
        self._log_account_var = ctk.StringVar(value="全部账号")
        ctk.CTkComboBox(tools, width=130, values=["全部级别", "DEBUG", "INFO", "WARNING", "ERROR"], variable=self._log_level_var, state="readonly", command=lambda _: self._rebuild_log_view()).pack(side="left")
        self._log_account_filter = ctk.CTkComboBox(tools, width=150, values=["全部账号"], variable=self._log_account_var, state="readonly", command=lambda _: self._rebuild_log_view())
        self._log_account_filter.pack(side="left", padx=(8, 0))
        self._log_autoscroll = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tools, text="自动滚动", variable=self._log_autoscroll, font=font(11)).pack(side="left", padx=(12, 0))
        self._button(tools, "复制", self._copy_logs, width=76, secondary=True).pack(side="right")
        self._button(tools, "清空", self._clear_logs, width=76, secondary=True).pack(side="right", padx=(0, 8))
        self._log_text = ctk.CTkTextbox(host, height=230, corner_radius=8, font=("Consolas", 11), wrap="word")
        self._log_text.grid(row=1, column=0, sticky="ew")
        self._log_text.configure(state="disabled")

    def _button(self, master: Any, text: str, command: Callable[[], None], *, width: int = 100, secondary: bool = False) -> ctk.CTkButton:
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=34, corner_radius=CONTROL_RADIUS,
            fg_color="transparent" if secondary else COLORS["accent"],
            hover_color=COLORS["row_selected"] if secondary else COLORS["accent_hover"],
            border_width=1 if secondary else 0, border_color=COLORS["border"],
            text_color=("#344054", "#E4E7EC") if secondary else "white", font=font(12, "bold"),
        )

    def _switch(self, master: Any, text: str, key: str, command: Callable[[], None] | None = None) -> ctk.CTkSwitch:
        return ctk.CTkSwitch(master, text=text, variable=self._setting_vars[key], command=command or self._mark_settings_dirty, font=font(12))

    # ---------- dispatcher / logs ----------
    def _schedule_dispatch(self) -> None:
        if self._quitting:
            return
        interval = 1000 if self._in_tray else 300
        self._state_poll_id = self.root.after(interval, self._drain_ui_mailboxes)
        self._log_poll_id = self.root.after(500 if self._in_tray or not self._log_card.expanded else 150, self._drain_logs)

    def _drain_ui_mailboxes(self) -> None:
        self._state_poll_id = None
        if self._quitting:
            return
        for callback in self._callback_mailbox.drain():
            try:
                callback()
            except Exception:
                logger.exception("GUI 回调失败")
        for state in self._state_mailbox.drain().values():
            self._apply_state(state)
        interval = 1000 if self._in_tray else 300
        self._state_poll_id = self.root.after(interval, self._drain_ui_mailboxes)

    def _schedule_ui(self, callback: Callable[[], None]) -> None:
        self._callback_mailbox.submit(callback)

    def _on_state(self, state: MinerState) -> None:
        self._state_mailbox.submit(state)

    def _setup_logging(self) -> None:
        self._log_handler = _QueuedLogHandler(self._log_queue)
        logger.addHandler(self._log_handler)
        logger.setLevel(logging.INFO)

    def _append_log(self, text: str, *, level: str = "INFO", account: str = "") -> None:
        self._log_queue.put((level, account, text))

    def _drain_logs(self) -> None:
        self._log_poll_id = None
        if self._quitting:
            return
        batch: list[tuple[str, str, str]] = []
        while len(batch) < 250:
            try:
                batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            removed = self._log_buffer.extend(entry[2] for entry in batch)
            self._visible_log_entries.extend(batch)
            if removed:
                del self._visible_log_entries[:removed]
                self._rebuild_log_view()
            elif self._log_card.expanded:
                visible = [entry[2] for entry in batch if self._log_entry_visible(entry)]
                if visible:
                    self._log_text.configure(state="normal")
                    self._log_text.insert("end", "\n".join(visible) + "\n")
                    self._log_text.configure(state="disabled")
                    if self._log_autoscroll.get():
                        self._log_text.see("end")
        interval = 500 if self._in_tray or not self._log_card.expanded else 150
        self._log_poll_id = self.root.after(interval, self._drain_logs)

    def _log_entry_visible(self, entry: tuple[str, str, str]) -> bool:
        level, account, _ = entry
        return (self._log_level_var.get() in {"全部级别", level}) and (self._log_account_var.get() in {"全部账号", account})

    def _rebuild_log_view(self) -> None:
        if not hasattr(self, "_log_text"):
            return
        text = "\n".join(entry[2] for entry in self._visible_log_entries if self._log_entry_visible(entry))
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        if text:
            self._log_text.insert("end", text + "\n")
        self._log_text.configure(state="disabled")
        if self._log_autoscroll.get():
            self._log_text.see("end")

    # ---------- incremental state ----------
    def _refresh_accounts_from_disk(self) -> None:
        uids = list_accounts()
        for uid in tuple(self._account_rows):
            if uid not in uids:
                self._account_rows.pop(uid).destroy()
                self._latest_account_ui.pop(uid, None)
        for uid in uids:
            state = self._states.get(uid, MinerState(uid=uid))
            ui = account_ui_state(state)
            row = self._account_rows.get(uid)
            if row is None:
                row = AccountRow(self._account_host, ui, self._select_account)
                row.grid(row=len(self._account_rows), column=0, sticky="ew", pady=(0, 5))
                self._account_rows[uid] = row
            else:
                row.update_state(ui)
            self._latest_account_ui[uid] = ui
        if uids:
            self._account_empty.grid_remove()
            if self._selected_uid not in uids:
                self._selected_uid = uids[0]
            self._select_account(self._selected_uid)
        else:
            self._selected_uid = None
            self._account_empty.grid()
            self._show_detail(None)
        self._update_log_accounts()
        self._refresh_header()

    def _apply_state(self, state: MinerState) -> None:
        self._states[state.uid] = state
        ui = account_ui_state(state)
        row = self._account_rows.get(state.uid)
        if row is None:
            row = AccountRow(self._account_host, ui, self._select_account)
            row.grid(row=len(self._account_rows), column=0, sticky="ew", pady=(0, 5))
            self._account_rows[state.uid] = row
            self._account_empty.grid_remove()
        else:
            row.update_state(ui)
        self._latest_account_ui[state.uid] = ui
        if state.inventory:
            existing = {(uid, item.item_code_idx): (uid, item) for uid, item in self._all_inventory}
            for item in state.inventory:
                existing[(state.uid, item.item_code_idx)] = (state.uid, item)
            self._set_inventory(list(existing.values()))
        if self._selected_uid == state.uid:
            self._cached_missions = list(state.missions)
            self._show_detail(state)
            self._render_missions_incremental(state.uid, state.missions, state.channel_nick or state.channel_id or "")
        self._refresh_header()

    def _select_account(self, uid: str | None) -> None:
        if not uid:
            return
        previous = self._selected_uid
        self._selected_uid = uid
        if previous in self._account_rows:
            self._account_rows[previous].set_selected(False)
        if uid in self._account_rows:
            self._account_rows[uid].set_selected(True)
        state = self._states.get(uid, MinerState(uid=uid))
        self._show_detail(state)
        self._render_missions_incremental(uid, state.missions, state.channel_nick or state.channel_id or "")
        self._refresh_header()

    def _show_detail(self, state: MinerState | None) -> None:
        if state is None:
            self._detail_grid.grid_remove()
            self._detail_hint.grid()
            return
        self._detail_hint.grid_remove()
        self._detail_grid.grid()
        mission = state.missions[0] if state.missions else None
        source = "—"
        miner = self._manager.get_miner(state.uid) if self._manager else None
        if miner is not None:
            buckets = miner._network.stats.by_type
            if buckets:
                source = max(buckets.items(), key=lambda pair: pair[1].uploaded + pair[1].downloaded)[0]
        values = {
            "uid": state.uid, "status": state.status,
            "channel": f"{state.channel_nick or '—'} / {state.channel_id or '—'}",
            "broad": state.broad_no or "—", "mission": mission.title if mission else "—",
            "progress": account_ui_state(state).progress,
            "bridge": ("已连接" if state.bridge_connected else "未连接") + " / " + (
                f"{state.bridge_last_activity_seconds:.1f} 秒前" if state.bridge_last_activity_seconds is not None else "—"
            ),
            "heartbeat": state.heartbeat_last_success or "—",
            "heartbeat_result": f"{state.heartbeat_failures} 次 / {state.heartbeat_result or '—'}",
            "traffic": f"上传 {format_rate(state.network_upload_bps)} / 下载 {format_rate(state.network_download_bps)}",
            "total": f"{format_bytes(state.network_uploaded + state.network_downloaded)}（应用层估算）",
            "source": source,
        }
        for key, value in values.items():
            if self._detail_values[key].cget("text") != value:
                self._detail_values[key].configure(text=value)

    def _render_missions_incremental(self, uid: str, missions: list[Mission], channel: str) -> None:
        new = mission_ui_states(uid, missions, channel)
        visible_keys = set(new)
        for key, card in tuple(self._mission_cards.items()):
            if key[0] == uid and key not in visible_keys:
                card.destroy()
                self._mission_cards.pop(key)
            elif key[0] != uid:
                card.grid_remove()
        for index, (key, state) in enumerate(new.items()):
            card = self._mission_cards.get(key)
            if card is None:
                card = MissionCard(self._mission_host, state, visible=lambda: not self._in_tray)
                card.grid(row=index, column=0, sticky="ew", pady=(0, 9))
                self._mission_cards[key] = card
            else:
                card.update_state(state)
                if not card.winfo_ismapped():
                    card.grid()
        self._latest_mission_ui = {**{k: v for k, v in self._latest_mission_ui.items() if k[0] != uid}, **new}
        if new:
            self._mission_empty.grid_remove()
        else:
            self._mission_empty.configure(text="该账号暂无任务数据")
            self._mission_empty.grid()

    def _set_inventory(self, items: list[tuple[str, InventoryItem]]) -> None:
        self._all_inventory = list(items)
        new = inventory_ui_states(items)
        for key in tuple(self._inventory_rows):
            if key not in new:
                self._inventory_rows.pop(key).destroy()
        for index, (key, state) in enumerate(new.items()):
            row = self._inventory_rows.get(key)
            if row is None:
                row = InventoryRow(self._inventory_host, state, self._select_inventory)
                row.grid(row=index, column=0, sticky="ew", pady=(0, 5))
                self._inventory_rows[key] = row
            else:
                row.update_state(state)
            row.set_selected(key == self._selected_inventory_key)
        self._latest_inventory_ui = new
        if new:
            self._inventory_empty.grid_remove()
        else:
            self._inventory_empty.grid()

    def _select_inventory(self, key: tuple[str, str]) -> None:
        old = self._selected_inventory_key
        self._selected_inventory_key = key
        if old in self._inventory_rows:
            self._inventory_rows[old].set_selected(False)
        if key in self._inventory_rows:
            self._inventory_rows[key].set_selected(True)

    # ---------- channels / network data ----------
    def _on_mode_segment(self, value: str) -> None:
        self._channel_mode.set({"智能选台": "smart", "手动选台": "manual", "仅 owesports": "owesports"}[value])
        self._apply_channel_mode_ui()

    def _apply_channel_mode_ui(self) -> None:
        for widget in (self._priority, self._manual_combo, self._channel_hint):
            widget.grid_remove()
        mode = self._channel_mode.get()
        if mode == "smart":
            self._channel_field_label.configure(text="优先任务")
            self._channel_field_label.grid(row=0, column=0, padx=12, pady=10)
            self._priority.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=10)
            self._channel_hint.configure(text="智能选台会优先匹配当前任务与直播分类；必要时提示换台原因。")
        elif mode == "manual":
            self._channel_field_label.configure(text="频道 ID 或链接")
            self._channel_field_label.grid(row=0, column=0, padx=12, pady=10)
            self._manual_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=10)
            self._channel_hint.configure(text="选择或输入直播间；分类不匹配时只提示，不复制选台业务逻辑。")
        else:
            self._channel_field_label.grid_remove()
            self._channel_hint.configure(text="只等待并进入 Overwatch Esports 官方频道 owesports。")
        self._channel_hint.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 10))

    def _get_channel_config(self) -> ChannelConfig:
        priority = PRIORITY_MISSION_AUTO
        selected = self._priority_var.get()
        if selected and selected != "自动优先":
            priority = selected.split(" · ", 1)[0]
        manual = self._manual_var.get().strip()
        mapped = self._channel_map.get(manual)
        if mapped:
            manual = f"{mapped.user_id}/{mapped.broad_no or ''}".rstrip("/")
        return ChannelConfig(mode=self._channel_mode.get(), manual_input=manual, preferred_bjid=DEFAULT_CHANNEL_BJID, priority_mission_id=priority)

    def _fetch_channels_async(self, *, silent: bool = False) -> None:
        if self._channel_loading or (self._in_tray and self._app_config.low_bandwidth_mode):
            return
        self._channel_loading = True
        self._channel_refresh_btn.configure(state="disabled", text="刷新中…")
        uids = list_accounts()
        config = snapshot_settings(self._app_config)

        def worker() -> None:
            async def load() -> list[LiveChannel]:
                for uid in uids:
                    cookies = load_cookies(uid)
                    if not cookies:
                        continue
                    context = AccountNetworkContext(uid, cookies, config)
                    session = await context.open()
                    try:
                        return await fetch_live_drops_channels(session, cookies, self._cached_missions)
                    finally:
                        await context.close()
                return []
            try:
                result = asyncio.run(load())
                self._schedule_ui(lambda: self._finish_channels(result, None, silent))
            except Exception as exc:
                self._schedule_ui(lambda exc=exc: self._finish_channels([], exc, silent))
        threading.Thread(target=worker, name="GuiChannelRefresh", daemon=True).start()

    def _finish_channels(self, channels: list[LiveChannel], error: BaseException | None, silent: bool) -> None:
        self._channel_loading = False
        self._channel_refresh_btn.configure(state="normal", text="刷新频道")
        if error:
            if not silent:
                messagebox.showerror("刷新频道", str(error), parent=self.root)
            return
        self._cached_channels = channels
        values: list[str] = []
        self._channel_map.clear()
        for channel in channels:
            label = format_channel_drops_label(channel)
            values.append(label)
            self._channel_map[label] = channel
        self._manual_combo.configure(values=values or [""])
        if values and not self._manual_var.get():
            self._manual_var.set(values[0])
        self._append_log(f"频道列表已刷新：{len(channels)} 个")
        self._schedule_channel_refresh()

    def _schedule_channel_refresh(self) -> None:
        if self._channel_refresh_timer:
            self.root.after_cancel(self._channel_refresh_timer)
        if self._quitting:
            return
        delay = self._app_config.effective_channel_refresh_interval * 1000
        self._channel_refresh_timer = self.root.after(delay, lambda: self._fetch_channels_async(silent=True))

    def _fetch_inventory_async(self) -> None:
        if self._inventory_loading:
            return
        self._inventory_loading = True
        self._inventory_refresh_btn.configure(state="disabled", text="刷新中…")
        uids = list_accounts()
        config = snapshot_settings(self._app_config)

        def worker() -> None:
            async def load() -> list[tuple[str, InventoryItem]]:
                output: list[tuple[str, InventoryItem]] = []
                for uid in uids:
                    cookies = load_cookies(uid)
                    if not cookies:
                        continue
                    context = AccountNetworkContext(uid, cookies, config)
                    session = await context.open()
                    try:
                        for item in await DropsClient(session).get_inventory(with_codes=True):
                            output.append((uid, item))
                    finally:
                        await context.close()
                return output
            try:
                data = asyncio.run(load())
                self._schedule_ui(lambda: self._finish_inventory(data, None))
            except Exception as exc:
                self._schedule_ui(lambda exc=exc: self._finish_inventory([], exc))
        threading.Thread(target=worker, name="GuiInventoryRefresh", daemon=True).start()

    def _finish_inventory(self, items: list[tuple[str, InventoryItem]], error: BaseException | None) -> None:
        self._inventory_loading = False
        self._inventory_refresh_btn.configure(state="normal", text="刷新背包")
        if error:
            messagebox.showerror("刷新背包", str(error), parent=self.root)
            return
        self._set_inventory(items)
        self._append_log(f"背包已刷新：{len(items)} 项")

    # ---------- accounts / actions ----------
    def _on_add_account(self) -> None:
        uid, password = self._userid_var.get().strip(), self._password_var.get()
        if not uid or not password:
            messagebox.showwarning("添加账号", "请填写账号和密码", parent=self.root)
            return
        config = snapshot_settings(self._app_config)
        def worker() -> None:
            try:
                asyncio.run(login(uid, password, config=config))
                self._schedule_ui(lambda: self._after_add_account(uid))
            except Exception as exc:
                self._schedule_ui(lambda exc=exc: messagebox.showerror("登录失败", str(exc), parent=self.root))
        threading.Thread(target=worker, name="GuiLogin", daemon=True).start()

    def _after_add_account(self, uid: str) -> None:
        self._password_var.set("")
        self._selected_uid = uid
        self._refresh_accounts_from_disk()
        self._append_log(f"已添加账号：{uid}", account=uid)
        self._fetch_inventory_async()

    def _on_remove_account(self) -> None:
        uid = self._selected_uid
        if not uid:
            messagebox.showinfo("删除账号", "请先选择账号", parent=self.root)
            return
        if not messagebox.askyesno("删除账号", f"确定停止并删除账号 {uid} 的本地登录信息？", parent=self.root):
            return
        manager, loop = self._manager, self._loop
        def finish() -> None:
            remove_account(uid)
            self._states.pop(uid, None)
            self._all_inventory = [(u, item) for u, item in self._all_inventory if u != uid]
            self._selected_uid = None
            self._refresh_accounts_from_disk()
            self._set_inventory(self._all_inventory)
            self._append_log(f"已删除账号：{uid}")
        if manager and loop and loop.is_running() and manager.get_miner(uid):
            future = asyncio.run_coroutine_threadsafe(manager.stop_account_and_wait(uid), loop)
            def wait_worker() -> None:
                try:
                    future.result(timeout=40)
                    self._schedule_ui(finish)
                except Exception as exc:
                    self._schedule_ui(lambda exc=exc: messagebox.showerror("删除账号", f"停止账号失败：{exc}", parent=self.root))
            threading.Thread(target=wait_worker, name="RemoveAccount", daemon=True).start()
        else:
            finish()

    def _on_start_all(self) -> None:
        if self._quitting or self._starting or (self._thread and self._thread.is_alive()):
            return
        if not list_accounts():
            messagebox.showinfo("全部开始", "请先添加至少一个账号。", parent=self.root)
            self._show_main_window()
            return
        self._starting = True
        self._stopping = False
        self._app_config = load_settings()
        settings = snapshot_settings(self._app_config)
        channel_config = self._get_channel_config()
        self._refresh_header()
        def runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._run_all(settings, channel_config))
            except Exception:
                logger.exception("多账号运行异常")
            finally:
                self._manager = None
                self._loop.close()
                self._loop = None
                self._schedule_ui(self._on_miners_stopped)
        self._thread = threading.Thread(target=runner, name="MultiMiner", daemon=True)
        self._thread.start()

    async def _run_all(self, settings: AppConfig, channel_config: ChannelConfig) -> None:
        self._manager = MultiMinerManager(on_state=self._on_state, channel_config=channel_config, app_config=settings)
        started = await self._manager.start_all(load_all_cookies())
        self._schedule_ui(lambda: self._after_started(started))
        if started:
            await self._manager.wait()
        await self._manager.shutdown()

    def _after_started(self, started: list[str]) -> None:
        self._starting = False
        self._append_log(f"已启动 {len(started)} 个账号" if started else "没有账号成功启动")
        self._refresh_header()
        self._schedule_channel_refresh()

    def _on_miners_stopped(self) -> None:
        self._starting = False
        self._stopping = False
        for uid, state in list(self._states.items()):
            if state.running:
                self._apply_state(MinerState(uid=uid, status="已停止"))
        self._refresh_header()

    def _on_stop_all(self) -> None:
        if self._stopping or not self._manager:
            return
        self._stopping = True
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._manager.stop_all)
        else:
            self._manager.stop_all()
        self._refresh_header()

    def _refresh_visible_data(self) -> None:
        self._refresh_accounts_from_disk()
        if not self._in_tray:
            self._fetch_inventory_async()
            self._fetch_channels_async()

    # ---------- inventory commands ----------
    def _selected_inventory(self) -> tuple[str, InventoryItem] | None:
        if not self._selected_inventory_key:
            return None
        return next(((uid, item) for uid, item in self._all_inventory if (uid, item.item_code_idx) == self._selected_inventory_key), None)

    def _claim_selected(self) -> None:
        selected = self._selected_inventory()
        if not selected:
            messagebox.showinfo("领取奖励", "请先选择奖励", parent=self.root)
            return
        uid, item = selected
        if item.claimed:
            messagebox.showinfo("领取奖励", "该奖励已经确认领取", parent=self.root)
            return
        cookies = load_cookies(uid)
        if not cookies:
            return
        config = snapshot_settings(self._app_config)
        def worker() -> None:
            async def claim() -> Any:
                context = AccountNetworkContext(uid, cookies, config)
                session = await context.open()
                try:
                    return await DropsClient(session).claim_and_verify(item.item_code_idx, max_attempts=2)
                finally:
                    await context.close()
            try:
                result = asyncio.run(claim())
                self._schedule_ui(lambda: self._finish_claim(uid, result))
            except Exception as exc:
                self._schedule_ui(lambda exc=exc: messagebox.showerror("领取奖励", str(exc), parent=self.root))
        threading.Thread(target=worker, name="ManualClaim", daemon=True).start()

    def _finish_claim(self, uid: str, result: Any) -> None:
        if result.status == ClaimStatus.CLAIMED:
            messagebox.showinfo("领取奖励", "领取已确认（Inventory 状态已变化）", parent=self.root)
        else:
            messagebox.showwarning("领取奖励", "领取未确认，请前往官方背包", parent=self.root)
        self._append_log(f"[{uid}] 手动领取结果：{result.status.value}")
        self._fetch_inventory_async()

    def _copy_selected_code(self) -> None:
        selected = self._selected_inventory()
        if not selected or not selected[1].redeem_code:
            messagebox.showinfo("复制兑换码", "所选奖励没有兑换码", parent=self.root)
            return
        self._copy_to_clipboard(selected[1].redeem_code)
        self._append_log("已复制所选兑换码（内容已隐藏）")

    def _copy_all_codes(self) -> None:
        codes = [item.redeem_code for _, item in self._all_inventory if item.redeem_code]
        if not codes:
            messagebox.showinfo("复制兑换码", "没有可复制的兑换码", parent=self.root)
            return
        self._copy_to_clipboard("\n".join(codes))
        self._append_log(f"已复制 {len(codes)} 个兑换码（内容已隐藏）")

    def _copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    # ---------- settings ----------
    def _load_settings_vars(self, settings: AppConfig) -> None:
        values = {
            "auto_start_enabled": settings.auto_start_enabled, "start_minimized_to_tray": settings.start_minimized_to_tray,
            "close_to_tray": settings.close_to_tray, "appearance_mode": settings.appearance_mode,
            "proxy_enabled": settings.proxy_enabled, "proxy_url": settings.proxy_url,
            "proxy_fallback_direct": settings.proxy_fallback_direct, "auto_claim_enabled": settings.auto_claim_enabled,
            "low_bandwidth_mode": settings.low_bandwidth_mode, "mission_poll_interval": str(settings.mission_poll_interval),
            "inventory_poll_interval": str(settings.inventory_poll_interval), "channel_refresh_interval": str(settings.channel_refresh_interval),
        }
        for key, value in values.items():
            self._setting_vars[key].set(value)

    def _settings_draft(self) -> AppConfig:
        try:
            mission = int(self._setting_vars["mission_poll_interval"].get().strip())
            inventory = int(self._setting_vars["inventory_poll_interval"].get().strip())
            channel = int(self._setting_vars["channel_refresh_interval"].get().strip())
        except ValueError as exc:
            raise ValueError("刷新间隔必须是整数") from exc
        return AppConfig(
            settings_version=SETTINGS_VERSION,
            auto_claim_enabled=bool(self._setting_vars["auto_claim_enabled"].get()),
            low_bandwidth_mode=bool(self._setting_vars["low_bandwidth_mode"].get()),
            proxy_enabled=bool(self._setting_vars["proxy_enabled"].get()),
            proxy_url=self._setting_vars["proxy_url"].get().strip(),
            proxy_fallback_direct=bool(self._setting_vars["proxy_fallback_direct"].get()),
            auto_start_enabled=bool(self._setting_vars["auto_start_enabled"].get()),
            start_minimized_to_tray=bool(self._setting_vars["start_minimized_to_tray"].get()),
            close_to_tray=bool(self._setting_vars["close_to_tray"].get()),
            appearance_mode=self._setting_vars["appearance_mode"].get(),
            mission_poll_interval=mission,
            inventory_poll_interval=inventory,
            channel_refresh_interval=channel,
        ).validated()

    def _mark_settings_dirty(self) -> None:
        if not hasattr(self, "_settings_status"):
            return
        try:
            dirty = self._settings_draft() != self._settings_baseline
        except ValueError:
            dirty = True
        self._settings_dirty = dirty
        self._settings_status.configure(text="有未保存修改" if dirty else "已保存", text_color=COLORS["warning"] if dirty else COLORS["muted"])

    def _preview_theme(self, value: str) -> None:
        mode = {"跟随系统": "system", "浅色": "light", "深色": "dark"}[value]
        self._setting_vars["appearance_mode"].set(mode)
        configure_appearance(mode)
        self._mark_settings_dirty()

    def _on_auto_start_toggle(self) -> None:
        target = bool(self._setting_vars["auto_start_enabled"].get())
        previous = self._app_config.auto_start_enabled
        try:
            saved = apply_auto_start_setting(self._app_config, target)
        except Exception as exc:
            self._setting_vars["auto_start_enabled"].set(previous)
            messagebox.showerror("开机自启", f"修改启动项失败：{exc}", parent=self.root)
            return
        self._on_settings_saved(saved)
        self._settings_baseline = replace(self._settings_baseline, auto_start_enabled=target)
        self._mark_settings_dirty()

    def _save_inline_settings(self) -> None:
        previous = snapshot_settings(self._app_config)
        try:
            draft = self._settings_draft()
            saved = save_settings(draft)
        except Exception as exc:
            configure_appearance(previous.appearance_mode)
            messagebox.showerror("保存设置", f"保存失败，原设置未改变：{exc}", parent=self.root)
            return
        self._on_settings_saved(saved)
        self._settings_baseline = snapshot_settings(saved)
        self._settings_dirty = False
        self._settings_status.configure(text="已保存。新代理设置将在下次启动挂机或网络重连时生效。", text_color=COLORS["success"])

    def _on_settings_saved(self, settings: AppConfig) -> None:
        self._app_config = snapshot_settings(settings)
        if self._manager is not None:
            self._manager._app_config = snapshot_settings(settings)
        if getattr(self, "_channel_refresh_timer", None):
            try:
                self.root.after_cancel(self._channel_refresh_timer)
            except Exception:
                pass
            self._channel_refresh_timer = None
            self._schedule_channel_refresh()
        self._refresh_header()
        self._append_log("设置已保存；运行中账号的 Session 保持不变")

    def _cancel_settings_draft(self) -> None:
        self._load_settings_vars(self._settings_baseline)
        configure_appearance(self._settings_baseline.appearance_mode)
        self._theme_control.set({"system": "跟随系统", "light": "浅色", "dark": "深色"}[self._settings_baseline.appearance_mode])
        self._mark_settings_dirty()

    def _reset_settings_draft(self) -> None:
        defaults = reset_settings()
        self._load_settings_vars(defaults)
        configure_appearance(defaults.appearance_mode)
        self._theme_control.set("跟随系统")
        self._mark_settings_dirty()

    def _test_proxy(self) -> None:
        if self._proxy_testing:
            return
        try:
            draft = self._settings_draft()
            if not draft.proxy_enabled:
                raise ValueError("请先启用代理")
        except ValueError as exc:
            messagebox.showerror("测试代理", str(exc), parent=self.root)
            return
        self._proxy_testing = True
        self._proxy_test_btn.configure(state="disabled", text="测试中…")
        self._proxy_test_status.configure(text="正在测试登录域名、Drops API、Live API 与 Bridge WebSocket…")
        def worker() -> None:
            try:
                result = asyncio.run(test_proxy_connectivity(draft))
                self._schedule_ui(lambda: self._finish_proxy_test(result, None))
            except Exception as exc:
                self._schedule_ui(lambda exc=exc: self._finish_proxy_test([], exc))
        threading.Thread(target=worker, name="ProxyConnectivityTest", daemon=True).start()

    def _finish_proxy_test(self, results: list[ProxyTestResult], error: BaseException | None) -> None:
        self._proxy_testing = False
        self._proxy_test_btn.configure(state="normal", text="测试代理")
        if error:
            self._proxy_test_status.configure(text=f"测试失败：{error}")
            return
        lines = []
        for item in results:
            status = "成功" if item.ok and item.complete else "链路可达（完整鉴权未测试）" if item.ok else "失败"
            lines.append(f"{item.target}：{status} · {item.elapsed_ms} ms · {item.detail}")
        self._proxy_test_status.configure(text="\n".join(lines))

    # ---------- header / tray / windows ----------
    def _refresh_header(self) -> None:
        if not hasattr(self, "_header_stats"):
            return
        saved = len(list_accounts())
        running = sum(1 for state in self._states.values() if state.running)
        selected = self._states.get(self._selected_uid or "")
        uploaded = sum(state.network_uploaded for state in self._states.values())
        downloaded = sum(state.network_downloaded for state in self._states.values())
        rate = sum(state.network_last_minute_bps for state in self._states.values())
        bridge = "已连接" if selected and selected.bridge_connected else "未连接"
        heartbeat = "正常" if selected and selected.connection_healthy else "等待/异常"
        text = (
            f"账号：{saved} 已保存 / {running} 运行中    当前：{self._selected_uid or '未选择'}    "
            f"代理：{'已启用' if self._app_config.proxy_enabled else '未启用'}    低流量：{'已启用' if self._app_config.low_bandwidth_mode else '未启用'}\n"
            f"Bridge：{bridge}    最近心跳：{heartbeat}    一分钟速率：{format_rate(rate)}    "
            f"本次累计：{format_bytes(uploaded + downloaded)}（应用层估算）"
        )
        if self._header_stats.cget("text") != text:
            self._header_stats.configure(text=text)
        if self._starting:
            self._overall_status.configure(text="启动中…", text_color=COLORS["warning"])
        elif self._stopping:
            self._overall_status.configure(text="停止中…", text_color=COLORS["warning"])
        else:
            self._overall_status.configure(text=f"运行中：{running} 个账号" if running else "就绪", text_color=COLORS["success"] if running else COLORS["muted"])
        self._start_btn.configure(state="disabled" if saved == 0 or self._starting or self._stopping or running else "normal", text="启动中…" if self._starting else "全部开始")
        self._stop_btn.configure(state="normal" if (running or self._stopping) else "disabled", text="停止中…" if self._stopping else "全部停止")
        if self._tray:
            self._tray.update_state(TrayMenuState(saved, running, self._app_config.proxy_enabled, self._app_config.low_bandwidth_mode, self._starting or self._stopping))

    def _start_restore_poll(self) -> None:
        if sys.platform != "win32":
            return
        def poll() -> None:
            if self._quitting:
                return
            if consume_show_request():
                self._show_main_window()
            self._restore_poll_id = self.root.after(400, poll)
        self._restore_poll_id = self.root.after(400, poll)

    def _restore_from_tray(self) -> None:
        self._schedule_ui(self._show_main_window)

    def _show_main_window(self) -> None:
        if self._quitting:
            return
        self._in_tray = False
        self.root.state("normal")
        self.root.deiconify()
        self.root.update_idletasks()
        width, height = max(self.root.winfo_width(), 1180), max(self.root.winfo_height(), 760)
        x = max(0, min(self.root.winfo_x(), self.root.winfo_screenwidth() - width))
        y = max(0, min(self.root.winfo_y(), self.root.winfo_screenheight() - height))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        for card in self._mission_cards.values():
            card.stop_animations()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(180, lambda: self.root.attributes("-topmost", False) if not self._quitting else None)
        self.root.focus_force()
        if self._app_config.low_bandwidth_mode:
            self._fetch_channels_async(silent=True)

    def _minimize_to_tray(self) -> None:
        if sys.platform != "win32" or self._tray is None:
            return
        self._in_tray = True
        for card in self._mission_cards.values():
            card.stop_animations()
        self.root.withdraw()
        self._refresh_header()

    def _show_disclaimer(self, *, first_run: bool = False) -> None:
        window = ctk.CTkToplevel(self.root)
        window.title(f"{APP_NAME} 免责说明")
        window.geometry("720x570")
        window.transient(self.root)
        window.grab_set()
        box = ctk.CTkTextbox(window, wrap="word", corner_radius=10, font=font(13))
        box.pack(fill="both", expand=True, padx=18, pady=18)
        box.insert("1.0", DISCLAIMER_TEXT)
        box.configure(state="disabled")
        buttons = ctk.CTkFrame(window, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(0, 18))
        def accept() -> None:
            DISCLAIMER_ACCEPTED_PATH.write_text("accepted", encoding="utf-8")
            self._first_run = False
            window.destroy()
        self._button(buttons, "同意并继续", accept, width=120).pack(side="right")
        if first_run:
            self._button(buttons, "拒绝并退出", self._quit_app, width=120, secondary=True).pack(side="right", padx=(0, 8))
            window.protocol("WM_DELETE_WINDOW", self._quit_app)
        else:
            self._button(buttons, "关闭", window.destroy, width=90, secondary=True).pack(side="right", padx=(0, 8))

    def _show_about(self) -> None:
        if self._about_window is not None:
            try:
                if self._about_window.winfo_exists():
                    self._about_window.deiconify(); self._about_window.lift(); self._about_window.focus_force(); return
            except Exception:
                pass
        window = ctk.CTkToplevel(self.root)
        self._about_window = window
        window.title(f"关于 {APP_NAME}")
        window.geometry("560x470")
        window.resizable(False, False)
        window.transient(self.root)
        card = ctk.CTkFrame(window, corner_radius=CARD_RADIUS)
        card.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(card, text=APP_NAME, font=font(22, "bold")).pack(pady=(24, 4))
        ctk.CTkLabel(card, text=f"版本 {VERSION}\n\n{PAGE_SUBTITLE}\n\n作者：{AUTHOR}", justify="center", font=font(13)).pack()
        repo = GITHUB_REPOSITORY_URL or "仓库地址尚未配置"
        ctk.CTkLabel(card, text=f"\n{LICENSE_NAME}\nPython {platform.python_version()}\n\n第三方工具，与 SOOP 官方无关联\n{repo}", justify="center", wraplength=490, text_color=COLORS["muted"], font=font(11)).pack()
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", side="bottom", padx=18, pady=18)
        open_btn = self._button(actions, "打开 GitHub", lambda: webbrowser.open(GITHUB_REPOSITORY_URL), width=110, secondary=True)
        open_btn.pack(side="left")
        copy_btn = self._button(actions, "复制仓库地址", lambda: self._copy_to_clipboard(GITHUB_REPOSITORY_URL), width=126, secondary=True)
        copy_btn.pack(side="left", padx=(8, 0))
        if not GITHUB_REPOSITORY_URL:
            open_btn.configure(state="disabled"); copy_btn.configure(state="disabled")
        self._button(actions, "关闭", window.destroy, width=86).pack(side="right")
        def closed() -> None:
            self._about_window = None
            window.destroy()
        window.protocol("WM_DELETE_WINDOW", closed)

    def _on_close(self) -> None:
        if self._app_config.close_to_tray:
            self._minimize_to_tray()
            return
        running = any(state.running for state in self._states.values()) or bool(self._thread and self._thread.is_alive())
        if running and not messagebox.askokcancel("退出程序", "当前有挂机任务。确定停止全部账号并退出吗？", parent=self.root):
            return
        self._quit_app()

    def _quit_app(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._schedule_ui(self._quit_app)
            return
        if self._quitting:
            return
        self._quitting = True
        self._stopping = True
        self._shutdown_deadline = time.monotonic() + 40
        if hasattr(self, "_start_btn"):
            self._start_btn.configure(state="disabled")
        if hasattr(self, "_stop_btn"):
            self._stop_btn.configure(state="disabled")
        if hasattr(self, "_state_mailbox"):
            self._state_mailbox.close()
        if hasattr(self, "_callback_mailbox"):
            self._callback_mailbox.close()
        for card in getattr(self, "_mission_cards", {}).values():
            card.stop_animations()
        if self._manager:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._manager.stop_all)
            else:
                self._manager.stop_all()
        self._poll_shutdown()

    def _poll_shutdown(self) -> None:
        if self._thread and self._thread.is_alive() and time.monotonic() < self._shutdown_deadline:
            self.root.after(100, self._poll_shutdown)
            return
        if self._thread and self._thread.is_alive():
            logger.error("等待账号网络资源清理超时")
        self._finalize_exit()

    def _finalize_exit(self) -> None:
        for name in ("_restore_poll_id", "_channel_refresh_timer", "_state_poll_id", "_log_poll_id"):
            timer = getattr(self, name, None)
            if timer:
                try:
                    self.root.after_cancel(timer)
                except Exception:
                    pass
                setattr(self, name, None)
        if hasattr(self, "_log_handler"):
            logger.removeHandler(self._log_handler)
        if self._tray:
            self._tray.stop(); self._tray = None
        # Resolve through gui for compatibility with embedders/tests that
        # replace the public cleanup hook.
        try:
            from . import gui as public_gui
            public_gui.release_single_instance()
        except Exception:
            release_single_instance()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _clear_logs(self) -> None:
        self._visible_log_entries.clear()
        self._log_buffer = BoundedLogBuffer(4000, 3500)
        self._rebuild_log_view()

    def _copy_logs(self) -> None:
        self._copy_to_clipboard(self._log_text.get("1.0", "end-1c"))

    def _update_log_accounts(self) -> None:
        values = ["全部账号", *list_accounts()]
        self._log_account_filter.configure(values=values)
        if self._log_account_var.get() not in values:
            self._log_account_var.set("全部账号")

    def run(self) -> None:
        def bootstrap() -> None:
            if self._first_run:
                self._show_main_window()
                self._show_disclaimer(first_run=True)
                return
            if not self._in_tray:
                self._fetch_inventory_async()
                self._fetch_channels_async(silent=True)
        self.root.after(0, bootstrap)
        self.root.mainloop()
