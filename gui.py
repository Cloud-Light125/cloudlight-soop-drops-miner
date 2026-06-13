from __future__ import annotations

import asyncio
import logging
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk
from typing import Callable

import aiohttp

from .auth import (
    apply_cookies,
    list_accounts,
    load_all_cookies,
    load_cookies,
    login,
    remove_account,
)
from .constants import (
    APP_NAME,
    DEFAULT_CHANNEL_BJID,
    DISCLAIMER_ACCEPTED_PATH,
    DISCLAIMER_TEXT,
    DROPS_EVENT_URL,
    DROPS_MISSION_URL,
    AUTHOR_BY,
    VERSION,
    WINDOW_TITLE,
    PLAY_ORIGIN,
)
from .channel import (
    ChannelConfig,
    ONE_STREAM_NOTICE,
    PRIORITY_MISSION_AUTO,
    active_progress_missions,
    collect_mission_channels,
    fetch_hashtag_drops_channels,
    fetch_live_drops_channels,
    filter_missions_by_priority,
    fixed_column_summary,
    fixed_column_warning,
    format_channel_drops_label,
    format_channel_preview,
    manual_channel_mismatch_warnings,
    mission_pick_label,
    missions_for_channel,
    parse_stream_input,
    pick_channel,
)
from .drops import DropsClient
from .miner import MinerState
from .models import DropEvent, InventoryItem, LiveChannel, Mission
from .multi_miner import MultiMinerManager
from .systray import WinSystray, consume_show_request, create_systray

logger = logging.getLogger("SoopDropsMiner")

_STATUS_COLORS = {
    "挂机中": "#2e7d32",
    "挂机中·固定型已结束": "#ef6c00",
    "未开放掉宝": "#ef6c00",
    "活动已结束": "#9e9e9e",
    "连接中": "#ef6c00",
    "等待直播间": "#ef6c00",
    "已停止": "#757575",
    "空闲": "#757575",
    "进房失败": "#c62828",
    "无可用直播间": "#c62828",
}


class _TkLogHandler(logging.Handler):
    def __init__(self, append: Callable[[str], None]) -> None:
        super().__init__()
        self._append = append
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._append(self.format(record))
        except Exception:
            pass


class SoopGui:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1080x920")
        self.root.minsize(960, 760)

        self._manager: MultiMinerManager | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._states: dict[str, MinerState] = {}
        self._selected_uid: str | None = None
        self._all_inventory: list[tuple[str, InventoryItem]] = []
        self._drops_channel_map: dict[str, LiveChannel] = {}
        self._priority_mission_map: dict[str, str] = {}
        self._channel_loading = False
        self._hashtag_live: list[LiveChannel] = []
        self._cached_online: list[LiveChannel] = []
        self._cached_missions: list[Mission] = []
        self._cached_events: list[DropEvent] = []
        self._channel_list_loaded = False
        self._smart_preview_channel: LiveChannel | None = None
        self._channel_refresh_timer: str | None = None
        self._stopping = False
        self._last_root_size: tuple[int, int] | None = None
        self._tray: WinSystray | None = None
        self._in_tray = False
        self._restore_poll_id: str | None = None

        self._build_ui()
        self._setup_logging()
        self._refresh_account_list()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tray = create_systray(
            tip=WINDOW_TITLE,
            on_show=self._restore_from_tray,
            on_exit=self._quit_app,
        )
        self._start_restore_poll()
        self.root.after(100, self._maybe_show_first_disclaimer)

    def _build_ui(self) -> None:
        root = self.root
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        header = ttk.Frame(root, padding=(12, 10, 12, 6))
        header.pack(fill=tk.X)
        title_row = ttk.Frame(header)
        title_row.pack(side=tk.LEFT)
        ttk.Label(title_row, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        ttk.Label(title_row, text=VERSION, font=("Segoe UI", 10), foreground="#757575").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self._summary_var = tk.StringVar(value="0 个账号")
        ttk.Label(
            header,
            textvariable=self._summary_var,
            font=("Segoe UI", 10),
            foreground="#616161",
        ).pack(side=tk.RIGHT)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        mine_tab = ttk.Frame(notebook, padding=8)
        inv_tab = ttk.Frame(notebook, padding=10)
        notebook.add(mine_tab, text="多账号挂机")
        notebook.add(inv_tab, text="奖励背包")

        # --- 挂机主操作（置顶醒目）---
        action_bar = tk.Frame(mine_tab, bg="#f0f4f8", padx=14, pady=12)
        action_bar.pack(fill=tk.X, pady=(0, 10))
        action_left = tk.Frame(action_bar, bg="#f0f4f8")
        action_left.pack(side=tk.LEFT)
        self._start_btn = tk.Button(
            action_left,
            text="▶  全部开始",
            command=self._on_start_all,
            font=("Segoe UI", 12, "bold"),
            width=12,
            padx=8,
            pady=8,
            bg="#2e7d32",
            fg="white",
            activebackground="#1b5e20",
            activeforeground="white",
            relief=tk.RAISED,
            bd=2,
            cursor="hand2",
        )
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = tk.Button(
            action_left,
            text="■  全部停止",
            command=self._on_stop_all,
            state=tk.DISABLED,
            font=("Segoe UI", 12, "bold"),
            width=12,
            padx=8,
            pady=8,
            bg="#bdbdbd",
            fg="#757575",
            activebackground="#b71c1c",
            activeforeground="white",
            disabledforeground="#757575",
            relief=tk.RAISED,
            bd=2,
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(12, 0))
        action_right = tk.Frame(action_bar, bg="#f0f4f8")
        action_right.pack(side=tk.RIGHT, fill=tk.Y)
        self._action_hint_var = tk.StringVar(value="请先添加至少一个账号")
        self._action_hint_lbl = tk.Label(
            action_right,
            textvariable=self._action_hint_var,
            font=("Segoe UI", 11, "bold"),
            fg="#ef6c00",
            bg="#f0f4f8",
            anchor=tk.E,
            justify=tk.RIGHT,
        )
        self._action_hint_lbl.pack(side=tk.TOP, anchor=tk.E)
        tk.Label(
            action_right,
            text="挂机需手动点击开始 · 关闭窗口前请先停止",
            font=("Segoe UI", 9),
            fg="#757575",
            bg="#f0f4f8",
            anchor=tk.E,
        ).pack(side=tk.TOP, anchor=tk.E, pady=(4, 0))

        # --- 账号列表 ---
        acct_frame = ttk.LabelFrame(mine_tab, text="账号列表", padding=8)
        acct_frame.pack(fill=tk.X, pady=(0, 8))

        acct_toolbar = ttk.Frame(acct_frame)
        acct_toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(acct_toolbar, text="账号").pack(side=tk.LEFT)
        self._userid_var = tk.StringVar()
        ttk.Entry(acct_toolbar, textvariable=self._userid_var, width=14).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(acct_toolbar, text="密码").pack(side=tk.LEFT)
        self._password_var = tk.StringVar()
        ttk.Entry(acct_toolbar, textvariable=self._password_var, width=14, show="•").pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(acct_toolbar, text="添加", command=self._on_add_account).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(acct_toolbar, text="删除", command=self._on_remove_account).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(acct_toolbar, text="Mission 页", command=self._open_drops_mission_page).pack(side=tk.LEFT)
        ttk.Button(acct_toolbar, text="活动页", command=self._open_drops_event_page).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        acct_btn = ttk.Frame(acct_frame)
        acct_btn.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(acct_btn, text="刷新背包", command=self._fetch_inventory_async).pack(side=tk.RIGHT)

        acct_tree_frame = ttk.Frame(acct_frame)
        acct_tree_frame.pack(fill=tk.X)
        acct_cols = ("uid", "status", "progress", "channel")
        self._acct_tree = ttk.Treeview(acct_tree_frame, columns=acct_cols, show="headings", height=4)
        self._acct_tree.heading("uid", text="账号")
        self._acct_tree.heading("status", text="状态")
        self._acct_tree.heading("progress", text="累计进度")
        self._acct_tree.heading("channel", text="直播间 (点击打开)")
        self._acct_tree.column("uid", width=110, anchor=tk.W)
        self._acct_tree.column("status", width=80, anchor=tk.W)
        self._acct_tree.column("progress", width=140, anchor=tk.W)
        self._acct_tree.column("channel", width=280, anchor=tk.W)
        self._acct_tree.tag_configure("channel_link", foreground="#1565c0")
        self._acct_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._acct_tree.bind("<<TreeviewSelect>>", self._on_account_select)
        self._acct_tree.bind("<ButtonRelease-1>", self._on_acct_tree_channel_click)

        # --- 直播间选择 ---
        ch_frame = ttk.LabelFrame(mine_tab, text="直播间（自动进房，无需先在浏览器观看）", padding=8)
        ch_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(
            ch_frame,
            text=ONE_STREAM_NOTICE,
            foreground="#616161",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))

        mode_row = ttk.Frame(ch_frame)
        mode_row.pack(fill=tk.X)
        self._channel_mode = tk.StringVar(value="smart")
        ttk.Radiobutton(
            mode_row,
            text="智能选台",
            variable=self._channel_mode,
            value="smart",
            command=self._on_channel_mode_change,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            mode_row,
            text="手动选台",
            variable=self._channel_mode,
            value="manual",
            command=self._on_channel_mode_change,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            mode_row,
            text=f"仅 {DEFAULT_CHANNEL_BJID}",
            variable=self._channel_mode,
            value="owesports",
            command=self._on_channel_mode_change,
        ).pack(side=tk.LEFT)

        self._smart_panel = ttk.Frame(ch_frame)
        self._smart_panel.pack(fill=tk.X, pady=(6, 0))
        smart_row = ttk.Frame(self._smart_panel)
        smart_row.pack(fill=tk.X)
        ttk.Label(smart_row, text="优先任务").pack(side=tk.LEFT)
        self._priority_mission_var = tk.StringVar(value="自动（固定型优先）")
        self._priority_mission_combo = ttk.Combobox(
            smart_row, textvariable=self._priority_mission_var, width=48, state="readonly"
        )
        self._priority_mission_combo.pack(side=tk.LEFT, padx=(4, 8), fill=tk.X, expand=True)
        self._priority_mission_combo.bind("<<ComboboxSelected>>", self._on_priority_mission_change)
        self._smart_preview_var = tk.StringVar(value="加载任务与列表后，这里会显示预计进入的直播间")
        ttk.Label(self._smart_panel, textvariable=self._smart_preview_var, foreground="#1565c0").pack(
            anchor=tk.W, pady=(4, 0)
        )
        self._smart_other_var = tk.StringVar(value="")
        ttk.Label(
            self._smart_panel,
            textvariable=self._smart_other_var,
            foreground="#ef6c00",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))

        self._manual_panel = ttk.Frame(ch_frame)
        self._channel_url_row = ttk.Frame(self._manual_panel)
        self._channel_url_row.pack(fill=tk.X)
        ttk.Label(self._channel_url_row, text="链接/ID").pack(side=tk.LEFT)
        self._channel_url_var = tk.StringVar(value="")
        self._channel_url_entry = ttk.Entry(
            self._channel_url_row, textvariable=self._channel_url_var
        )
        self._channel_url_entry.pack(side=tk.LEFT, padx=(4, 8), fill=tk.X, expand=True)
        self._channel_url_var.trace_add("write", self._on_manual_input_change)

        self._channel_list_row = ttk.Frame(self._manual_panel)
        self._channel_list_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(self._channel_list_row, text="可掉宝").pack(side=tk.LEFT)
        self._drops_channel_var = tk.StringVar()
        self._drops_channel_combo = ttk.Combobox(
            self._channel_list_row, textvariable=self._drops_channel_var, width=40, state="readonly"
        )
        self._drops_channel_combo.pack(side=tk.LEFT, padx=(4, 8), fill=tk.X, expand=True)
        self._drops_channel_combo.bind("<<ComboboxSelected>>", self._on_drops_channel_pick)
        self._channel_refresh_btn = ttk.Button(
            self._channel_list_row, text="刷新列表", command=self._fetch_drops_channels_async
        )
        self._channel_refresh_btn.pack(side=tk.RIGHT)

        load_row = ttk.Frame(ch_frame)
        load_row.pack(fill=tk.X, pady=(4, 0))
        self._channel_load_bar = ttk.Progressbar(load_row, mode="indeterminate", length=200)
        self._channel_load_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._channel_load_bar.pack_forget()

        self._drops_channel_hint = tk.StringVar(value="")
        ttk.Label(ch_frame, textvariable=self._drops_channel_hint, foreground="#757575").pack(anchor=tk.W, pady=(4, 0))
        self._apply_channel_mode_ui()

        # --- 任务进度 + 日志（可拖拽分栏，保证日志与页脚始终可见）---
        self._mine_split = ttk.Panedwindow(mine_tab, orient=tk.VERTICAL)
        self._mine_split.pack(fill=tk.BOTH, expand=True)

        progress_frame = ttk.LabelFrame(mine_tab, text="任务进度（选中账号）", padding=10)
        self._progress_frame = progress_frame
        self._progress_status_var = tk.StringVar(value="等待加载任务…")
        (
            _col,
            self._progress_host,
            self._progress_warn_frame,
            self._progress_warn_var,
            self._progress_subtitle_lbl,
            self._progress_status_lbl,
            self._progress_warn_before,
        ) = self._build_progress_column(
            progress_frame,
            title="",
            subtitle="加载中…",
            padx=(0, 0),
            summary_var=self._progress_status_var,
            framed=False,
        )

        log_frame = ttk.LabelFrame(mine_tab, text="日志", padding=(10, 8))
        self._log = scrolledtext.ScrolledText(
            log_frame, height=6, state=tk.DISABLED, font=("Consolas", 9), wrap=tk.WORD
        )
        self._log.pack(fill=tk.BOTH, expand=True)

        self._mine_split.add(progress_frame, weight=3)
        self._mine_split.add(log_frame, weight=1)
        self.root.after(120, self._balance_mine_panes)
        self.root.bind("<Configure>", self._on_root_configure, add="+")

        # --- 奖励背包 ---
        inv_toolbar = ttk.Frame(inv_tab)
        inv_toolbar.pack(fill=tk.X, pady=(0, 8))
        self._inv_count_var = tk.StringVar(value="共 0 件奖励")
        ttk.Label(inv_toolbar, textvariable=self._inv_count_var).pack(side=tk.LEFT)
        ttk.Button(inv_toolbar, text="刷新", command=self._fetch_inventory_async).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(inv_toolbar, text="复制全部兑换码", command=self._copy_all_codes).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(inv_toolbar, text="复制选中", command=self._copy_selected_code).pack(side=tk.RIGHT)

        inv_tree_frame = ttk.Frame(inv_tab)
        inv_tree_frame.pack(fill=tk.BOTH, expand=True)
        inv_cols = ("account", "name", "code", "receive", "exp")
        self._inv_tree = ttk.Treeview(inv_tree_frame, columns=inv_cols, show="headings", height=16)
        self._inv_tree.heading("account", text="账号")
        self._inv_tree.heading("name", text="奖励名称")
        self._inv_tree.heading("code", text="兑换码")
        self._inv_tree.heading("receive", text="领取时间")
        self._inv_tree.heading("exp", text="过期时间")
        self._inv_tree.column("account", width=100, anchor=tk.W)
        self._inv_tree.column("name", width=220, anchor=tk.W)
        self._inv_tree.column("code", width=260, anchor=tk.W)
        self._inv_tree.column("receive", width=130, anchor=tk.W)
        self._inv_tree.column("exp", width=130, anchor=tk.W)
        inv_scroll = ttk.Scrollbar(inv_tree_frame, orient=tk.VERTICAL, command=self._inv_tree.yview)
        self._inv_tree.configure(yscrollcommand=inv_scroll.set)
        self._inv_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inv_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._inv_tree.bind("<Double-1>", lambda _e: self._copy_selected_code())

        ttk.Label(
            inv_tab,
            text="汇总所有已保存账号的奖励 · 双击复制兑换码",
            foreground="#757575",
        ).pack(anchor=tk.W, pady=(8, 0))

        footer = ttk.Frame(root, padding=(12, 0, 12, 8))
        footer.pack(fill=tk.X)
        ttk.Button(footer, text="退出", command=self._quit_app, width=8).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(footer, text="免责说明", command=self._show_disclaimer, width=10).pack(side=tk.LEFT)
        credit_row = ttk.Frame(footer)
        credit_row.pack(side=tk.RIGHT)
        ttk.Label(credit_row, text=AUTHOR_BY, font=("Segoe UI", 9), foreground="#9e9e9e").pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Label(credit_row, text=VERSION, font=("Segoe UI", 9), foreground="#bdbdbd").pack(side=tk.LEFT)

    _TYPE_DISPLAY = {
        "fixed": ("固定型", "观看达标后自动发放（Mission/Fixed）"),
        "lottery": ("抽奖型", "观看达标后参与抽奖（Mission/Lottery）"),
        "random": ("随机型", "观看期间随机发放（Random Type）"),
        "other": ("当前任务", "进行中的掉宝任务"),
    }

    def _build_progress_column(
        self,
        parent: ttk.Frame,
        *,
        title: str,
        subtitle: str,
        padx: tuple[int, int],
        summary_var: tk.StringVar | None = None,
        with_warning: bool = True,
        framed: bool = True,
    ) -> tuple[
        ttk.LabelFrame | None,
        ttk.Frame,
        ttk.Frame | None,
        tk.StringVar | None,
        ttk.Label,
        ttk.Label | None,
        ttk.Label,
    ]:
        if framed:
            col = ttk.LabelFrame(parent, text=title, padding=8)
            col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=padx)
            container = col
        else:
            col = None
            container = parent
        warn_var: tk.StringVar | None = None
        warn_frame: ttk.Frame | None = None
        if with_warning:
            warn_var = tk.StringVar()
            warn_frame = ttk.Frame(container)
            ttk.Label(
                warn_frame,
                textvariable=warn_var,
                foreground="#c62828",
                font=("Segoe UI", 9, "bold"),
                wraplength=900,
            ).pack(anchor=tk.W)
        subtitle_lbl = ttk.Label(container, text=subtitle, foreground="#757575")
        subtitle_lbl.pack(anchor=tk.W, pady=(0, 2))
        summary_lbl: ttk.Label | None = None
        if summary_var is not None:
            summary_lbl = ttk.Label(
                container,
                textvariable=summary_var,
                font=("Segoe UI", 9, "bold"),
                wraplength=900,
            )
            summary_lbl.pack(anchor=tk.W, pady=(0, 4))
        body = ttk.Frame(container)
        body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        host = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=host, anchor=tk.NW)

        def _sync_scroll(_event: tk.Event | None = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        host.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return col, host, warn_frame, warn_var, subtitle_lbl, summary_lbl, subtitle_lbl

    def _balance_mine_panes(self) -> None:
        """为日志区保留固定可视高度，任务进度在上方区域内滚动。"""
        if not hasattr(self, "_mine_split"):
            return
        try:
            total = self._mine_split.winfo_height()
        except tk.TclError:
            return
        if total <= 1:
            self.root.after(120, self._balance_mine_panes)
            return
        log_height = max(150, min(240, int(total * 0.30)))
        sash = max(120, total - log_height)
        try:
            if self._mine_split.sashpos(0) != sash:
                self._mine_split.sashpos(0, sash)
        except tk.TclError:
            pass

    def _on_root_configure(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if self._last_root_size == size:
            return
        self._last_root_size = size
        self._balance_mine_panes()

    def _missions_for_display(self) -> list[Mission]:
        return self._missions_for_warnings()

    def _missions_for_warnings(self) -> list[Mission]:
        missions: list[Mission] = []
        if self._selected_uid:
            state = self._states.get(self._selected_uid)
            if state and state.missions:
                missions = state.missions
        if not missions:
            missions = self._cached_missions
        return missions

    def _online_channels_for_warnings(self) -> list[LiveChannel]:
        channels = list(self._cached_online)
        seen = {ch.user_id for ch in channels}
        if self._selected_uid:
            state = self._states.get(self._selected_uid)
            if state and state.available_channels:
                for ch in state.available_channels:
                    if ch.user_id and ch.user_id not in seen:
                        seen.add(ch.user_id)
                        channels.append(ch)
        return channels

    def _mission_type_key(self, mission: Mission) -> str:
        if mission.is_fixed:
            return "fixed"
        if mission.is_lottery:
            return "lottery"
        if mission.is_random:
            return "random"
        return "other"

    def _owesports_preview_channel(self) -> LiveChannel:
        for candidate in self._cached_online + self._hashtag_live:
            if candidate.user_id == DEFAULT_CHANNEL_BJID:
                return candidate
        return LiveChannel(
            user_id=DEFAULT_CHANNEL_BJID,
            user_nick=DEFAULT_CHANNEL_BJID,
            broad_no=None,
            on_air=False,
        )

    def _effective_channel_for_progress(self) -> LiveChannel | None:
        state = self._states.get(self._selected_uid or "") if self._selected_uid else None
        # 挂机中：以实际所在直播间为准
        if state and state.running and state.channel_id:
            return self._current_live_channel()

        # 未挂机：按当前选台模式预览
        mode = self._channel_mode.get()
        if mode == "manual":
            return self._local_manual_channel()
        if mode == "smart":
            return self._smart_preview_channel
        if mode == "owesports":
            return self._owesports_preview_channel()
        return None

    def _missions_for_current_display(
        self, missions: list[Mission]
    ) -> tuple[str, str, str, list[Mission]]:
        """返回 (类型名, 副标题, type_key, 当前类型下要展示的任务)。"""
        active = [m for m in missions if m.items]
        cfg = self._get_channel_config()

        if cfg.mode == "owesports":
            ow = self._owesports_preview_channel()
            served = [m for m in missions_for_channel(missions, ow) if m.items]
            title, subtitle = self._TYPE_DISPLAY["fixed"]
            return title, subtitle, "fixed", served

        if cfg.mode == "manual":
            channel = self._local_manual_channel()
            if not channel:
                return "任务", "请选择或输入直播间", "other", []
            served = [m for m in missions_for_channel(missions, channel) if m.items]
            if served:
                key = self._mission_type_key(served[0])
                same = [m for m in served if self._mission_type_key(m) == key]
                title, subtitle = self._TYPE_DISPLAY.get(key, self._TYPE_DISPLAY["other"])
                return title, subtitle, key, same
            return "任务", "当前所选直播间无匹配任务", "other", []

        channel = self._effective_channel_for_progress()
        if channel:
            served = [m for m in missions_for_channel(missions, channel) if m.items]
            if served:
                key = self._mission_type_key(served[0])
                same = [m for m in served if self._mission_type_key(m) == key]
                title, subtitle = self._TYPE_DISPLAY.get(key, self._TYPE_DISPLAY["other"])
                return title, subtitle, key, same

        targets = [m for m in filter_missions_by_priority(missions, cfg.priority_mission_id) if m.items]
        if not targets:
            for key in ("fixed", "lottery", "random", "other"):
                typed = [m for m in active if self._mission_type_key(m) == key]
                if typed:
                    targets = typed
                    break

        if targets:
            key = self._mission_type_key(targets[0])
            same = [m for m in targets if self._mission_type_key(m) == key]
            title, subtitle = self._TYPE_DISPLAY.get(key, self._TYPE_DISPLAY["other"])
            return title, subtitle, key, same

        return "任务", "暂无进行中的掉宝任务", "other", []

    def _progress_summary_for_type(self, type_key: str, missions: list[Mission]) -> tuple[str, str]:
        if type_key == "fixed":
            return fixed_column_summary(missions)
        if not missions:
            return "暂无任务", "#757575"
        active = [m for m in missions if m.is_event_active]
        color = "#2e7d32" if active else "#757575"
        return f"进行中 {len(active)} 个", color

    def _rerender_missions_if_cached(self) -> None:
        if self._selected_uid or self._cached_missions:
            self._render_selected_progress()

    def _render_selected_progress(self) -> None:
        missions: list[Mission] = []
        if self._selected_uid and self._selected_uid in self._states:
            missions = self._states[self._selected_uid].missions
        if not missions:
            missions = self._cached_missions
        refresh = self._channel_mode.get() == "smart"
        self._render_missions(missions, refresh_smart_preview=refresh)

    def _current_live_channel(self) -> LiveChannel | None:
        if not self._selected_uid:
            return None
        state = self._states.get(self._selected_uid)
        if not state or not state.channel_id:
            return None
        for ch in self._hashtag_live:
            if ch.user_id == state.channel_id:
                return ch
        return LiveChannel(
            user_id=state.channel_id,
            user_nick=state.channel_nick or state.channel_id,
            broad_no=state.broad_no,
            on_air=True,
        )

    def _set_column_warning(
        self,
        frame: ttk.Frame | None,
        var: tk.StringVar | None,
        before: ttk.Label,
        show: bool,
        text: str,
    ) -> None:
        if frame is None or var is None:
            return
        if show:
            var.set(text)
            frame.pack_forget()
            frame.pack(fill=tk.X, pady=(0, 6), before=before)
        else:
            var.set("")
            frame.pack_forget()

    def _update_progress_warnings(self) -> None:
        missions = self._missions_for_warnings()
        type_title, _, type_key, display_missions = self._missions_for_current_display(missions)
        online = self._online_channels_for_warnings()
        state = self._states.get(self._selected_uid or "")
        running_no_channel = False
        if state and state.running:
            if state.status in ("无可用直播间", "等待直播间", "进房失败"):
                running_no_channel = True
            elif not state.channel_id:
                running_no_channel = True

        warn = ""
        cfg = self._get_channel_config()
        if cfg.mode == "owesports" and not display_missions:
            ow = self._owesports_preview_channel()
            if not ow.on_air:
                warn = f"⚠ {DEFAULT_CHANNEL_BJID} 未开播，当前模式不会累计其他任务进度"
            else:
                warn = f"⚠ 无关联 {DEFAULT_CHANNEL_BJID} 的进行中固定型任务"
        elif type_key == "fixed" and display_missions:
            warn = fixed_column_warning(
                display_missions,
                online,
                running_no_channel=running_no_channel,
            )
        elif running_no_channel:
            warn = "⚠ 等待可用直播间，当前类型任务暂无法累计进度"
        elif self._channel_mode.get() == "manual":
            ch = self._effective_channel_for_progress()
            if ch:
                mismatches = manual_channel_mismatch_warnings(ch, missions)
                if mismatches:
                    warn = mismatches[0]

        self._set_column_warning(
            self._progress_warn_frame,
            self._progress_warn_var,
            self._progress_warn_before,
            bool(warn),
            warn,
        )
        summary, color = self._progress_summary_for_type(type_key, display_missions)
        if not display_missions:
            summary = f"暂无{type_title}任务"
            color = "#757575"
        self._progress_status_var.set(summary)
        if self._progress_status_lbl is not None:
            self._progress_status_lbl.configure(foreground=color)
        self._progress_frame.configure(text=f"任务进度 · {type_title}（选中账号）")

    def _schedule_running_channel_refresh(self) -> None:
        if self._channel_refresh_timer:
            self.root.after_cancel(self._channel_refresh_timer)
            self._channel_refresh_timer = None
        if not any(s.running for s in self._states.values()):
            return
        if not self._channel_loading:
            self._fetch_drops_channels_async(silent=True)

        def _tick() -> None:
            if any(s.running for s in self._states.values()):
                if not self._channel_loading:
                    self._fetch_drops_channels_async(silent=True)
                self._channel_refresh_timer = self.root.after(120_000, _tick)

        self._channel_refresh_timer = self.root.after(120_000, _tick)

    def _show_disclaimer(self, *, first_run: bool = False) -> None:
        win = tk.Toplevel(self.root)
        win.title("免责说明")
        win.geometry("520x380")
        win.resizable(False, False)
        win.transient(self.root)
        if first_run:
            win.grab_set()

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Segoe UI", 9), height=16)
        text.pack(fill=tk.BOTH, expand=True)
        text.insert(tk.END, DISCLAIMER_TEXT)
        text.configure(state=tk.DISABLED)

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, pady=(10, 0))

        def _accept() -> None:
            DISCLAIMER_ACCEPTED_PATH.write_text("1", encoding="utf-8")
            win.destroy()

        if first_run:
            ttk.Button(btn_row, text="我已阅读并同意", command=_accept).pack(side=tk.RIGHT)
            ttk.Button(btn_row, text="退出", command=self._quit_app).pack(side=tk.RIGHT, padx=(0, 8))
            win.protocol("WM_DELETE_WINDOW", self._quit_app)
        else:
            ttk.Button(btn_row, text="关闭", command=win.destroy).pack(side=tk.RIGHT)

        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    def _maybe_show_first_disclaimer(self) -> None:
        if not DISCLAIMER_ACCEPTED_PATH.is_file():
            self._show_disclaimer(first_run=True)

    def _setup_logging(self) -> None:
        handler = _TkLogHandler(self._append_log)
        handler.setLevel(logging.INFO)
        logging.getLogger("SoopDropsMiner").addHandler(handler)

    def _schedule_ui(self, callback: Callable[[], None]) -> None:
        """从后台线程安全调度 UI 更新（须在主循环启动后调用）。"""

        def _run() -> None:
            try:
                callback()
            except (tk.TclError, RuntimeError):
                pass

        try:
            self.root.after(0, _run)
        except RuntimeError:
            pass

    def _append_log(self, text: str) -> None:
        def _write() -> None:
            self._log.configure(state=tk.NORMAL)
            self._log.insert(tk.END, text + "\n")
            self._log.see(tk.END)
            self._log.configure(state=tk.DISABLED)

        self._schedule_ui(_write)

    def _progress_summary(self, state: MinerState | None) -> str:
        if not state or not state.missions:
            return "—"
        _, _, _, display = self._missions_for_current_display(state.missions)
        for mission in display:
            active = mission.active_item()
            if active:
                return f"{active.view_time} 分钟"
            if mission.items:
                return f"{mission.items[-1].view_time} 分钟 (完成)"
        return "—"

    def _update_action_bar(self) -> None:
        if self._stopping:
            self._action_hint_var.set("正在停止…")
            self._action_hint_lbl.configure(fg="#757575")
            self._start_btn.configure(state=tk.DISABLED, bg="#bdbdbd", fg="#757575", cursor="")
            self._stop_btn.configure(state=tk.DISABLED, bg="#bdbdbd", fg="#757575", cursor="")
            return

        uids = list_accounts()
        thread_alive = bool(self._thread and self._thread.is_alive())
        running_count = sum(1 for s in self._states.values() if s.running)
        any_running = running_count > 0 or thread_alive

        if any_running:
            count_text = f"{running_count} 个" if running_count else "启动中"
            self._action_hint_var.set(f"● {count_text}账号运行中")
            self._action_hint_lbl.configure(fg="#2e7d32")
            self._start_btn.configure(state=tk.DISABLED, bg="#bdbdbd", fg="#757575", cursor="")
            self._stop_btn.configure(
                state=tk.NORMAL,
                bg="#c62828",
                fg="white",
                cursor="hand2",
            )
            return

        if uids:
            self._action_hint_var.set("▶  设置完成后，点击左侧「全部开始」启动挂机")
            self._action_hint_lbl.configure(fg="#ef6c00")
            self._start_btn.configure(state=tk.NORMAL, bg="#2e7d32", fg="white", cursor="hand2")
        else:
            self._action_hint_var.set("请先添加至少一个账号")
            self._action_hint_lbl.configure(fg="#757575")
            self._start_btn.configure(state=tk.DISABLED, bg="#bdbdbd", fg="#757575", cursor="")
        self._stop_btn.configure(state=tk.DISABLED, bg="#bdbdbd", fg="#757575", cursor="")

    @staticmethod
    def _live_stream_url(user_id: str, broad_no: str | None = None) -> str:
        url = f"{PLAY_ORIGIN}/{user_id}"
        if broad_no:
            url += f"/{broad_no}"
        return url

    def _open_live_stream(self, user_id: str, broad_no: str | None = None) -> None:
        webbrowser.open(self._live_stream_url(user_id, broad_no))

    def _on_acct_tree_channel_click(self, event: tk.Event) -> None:
        if self._acct_tree.identify_region(event.x, event.y) != "cell":
            return
        if self._acct_tree.identify_column(event.x) != "#4":
            return
        row = self._acct_tree.identify_row(event.y)
        if not row:
            return
        state = self._states.get(row)
        if state and state.channel_id:
            self._open_live_stream(state.channel_id, state.broad_no)

    def _refresh_account_list(self) -> None:
        uids = list_accounts()
        running = sum(1 for u in uids if self._states.get(u) and self._states[u].running)
        self._summary_var.set(f"{len(uids)} 个账号 · {running} 个运行中")

        selected = self._selected_uid
        for row in self._acct_tree.get_children():
            self._acct_tree.delete(row)

        for uid in uids:
            state = self._states.get(uid)
            status = state.status if state else "空闲"
            progress = self._progress_summary(state)
            channel = "—"
            tags: tuple[str, ...] = ()
            if state and state.channel_id:
                nick = state.channel_nick or state.channel_id
                channel = nick
                tags = ("channel_link",)
            self._acct_tree.insert("", tk.END, iid=uid, values=(uid, status, progress, channel), tags=tags)

        if selected and selected in uids:
            self._acct_tree.selection_set(selected)
        elif uids and not self._selected_uid:
            self._acct_tree.selection_set(uids[0])
            self._selected_uid = uids[0]
            self._render_selected_progress()
        self._update_action_bar()

    def _on_account_select(self, _event: tk.Event | None = None) -> None:
        sel = self._acct_tree.selection()
        if sel:
            self._selected_uid = sel[0]
            self._render_selected_progress()
            state = self._states.get(self._selected_uid)
            if not state or not state.missions:
                self._fetch_missions_display_async()

    def _on_state(self, state: MinerState) -> None:
        self.root.after(0, lambda: self._apply_state(state))

    def _apply_state(self, state: MinerState) -> None:
        self._states[state.uid] = state
        self._refresh_account_list()
        if state.uid == self._selected_uid:
            missions = state.missions or self._cached_missions
            refresh = self._channel_mode.get() == "smart"
            self._render_missions(missions, refresh_smart_preview=refresh)
        if self._selected_uid:
            self._update_progress_warnings()
        if state.inventory:
            self._merge_inventory_from_state(state)

        any_running = any(s.running for s in self._states.values())
        if any_running:
            self._schedule_running_channel_refresh()
        elif not (self._thread and self._thread.is_alive()):
            if self._channel_refresh_timer:
                self.root.after_cancel(self._channel_refresh_timer)
                self._channel_refresh_timer = None
        if not self._stopping:
            self._update_action_bar()

    def _merge_inventory_from_state(self, state: MinerState) -> None:
        self._all_inventory = [(u, it) for u, it in self._all_inventory if u != state.uid]
        self._all_inventory.extend((state.uid, it) for it in state.inventory)
        self._render_inventory_table()

    def _item_status(self, mission, item, *, active_idx: int, item_idx: int) -> tuple[str, str]:
        if mission.is_event_ended:
            if mission.is_not_yet_open:
                return "未开放", "#ef6c00"
            return "活动已结束", "#9e9e9e"
        if mission.is_lottery:
            if item.mission_success:
                return "已中奖", "#2e7d32"
            if item.view_time >= item.give_term:
                return "已达标 · 待抽奖", "#ef6c00"
            if item_idx == active_idx:
                return "观看累计中", "#1565c0"
            return "未达标", "#616161"
        if mission.is_random:
            if item.mission_success:
                return "已获得", "#2e7d32"
            if item_idx == active_idx:
                return "观看中 · 随机发放", "#1565c0"
            return "待触发", "#616161"

        if item.mission_success:
            return "已完成", "#9e9e9e"
        if item_idx == active_idx:
            return "进行中", "#1565c0"
        return "未达成", "#616161"

    def _render_mission_block(
        self,
        parent: ttk.Frame,
        uid: str,
        mission,
        *,
        hint_wrap: int = 420,
    ) -> None:
        if not mission.items:
            return
        active = mission.active_item()
        active_idx = mission.items.index(active) if active else -1
        view_time = mission.items[0].view_time

        block = ttk.Frame(parent)
        block.pack(fill=tk.X, pady=(0, 10))

        title = mission.title
        if len(title) > 44:
            title = title[:41] + "…"
        meta = f"累计 {view_time} 分钟"
        if mission.category_name:
            meta += f"  ·  {mission.category_name}"
        if mission.is_event_ended:
            title_color = "#ef6c00" if mission.is_not_yet_open else "#9e9e9e"
        else:
            title_color = "black"
        ttk.Label(
            block,
            text=f"[{uid}] {title}  ·  {meta}",
            font=("Segoe UI", 9, "bold"),
            foreground=title_color,
        ).pack(anchor=tk.W)

        if mission.is_lottery and mission.guide_str:
            hint = mission.guide_str.replace("\n", " ")
            if len(hint) > 72:
                hint = hint[:69] + "…"
            ttk.Label(block, text=hint, foreground="#757575", wraplength=hint_wrap).pack(
                anchor=tk.W, pady=(2, 0)
            )
        elif mission.is_random:
            ttk.Label(
                block,
                text="随机型奖励在观看期间发放，mission 页可能无详细进度",
                foreground="#757575",
                wraplength=hint_wrap,
            ).pack(anchor=tk.W, pady=(2, 0))

        for i, item in enumerate(mission.items):
            tier = ttk.Frame(block)
            tier.pack(fill=tk.X, pady=(4, 0))
            status, name_color = self._item_status(mission, item, active_idx=active_idx, item_idx=i)

            label_row = ttk.Frame(tier)
            label_row.pack(fill=tk.X)
            ttk.Label(label_row, text=f"[{item.term_label}]", font=("Segoe UI", 9, "bold"), width=5).pack(
                side=tk.LEFT
            )
            name = item.item_name[:29] + "…" if len(item.item_name) > 32 else item.item_name
            ttk.Label(label_row, text=name, foreground=name_color).pack(side=tk.LEFT, padx=(4, 0))
            ttk.Label(label_row, text=status, foreground=name_color).pack(side=tk.RIGHT)

            bar_row = ttk.Frame(tier)
            bar_row.pack(fill=tk.X, pady=(1, 0))
            if mission.is_lottery or mission.is_random:
                value = min(item.view_time, item.give_term)
                detail = (
                    f"{item.give_term}/{item.give_term} 分 ✓"
                    if item.view_time >= item.give_term or item.mission_success
                    else f"{item.view_time}/{item.give_term} 分 ({item.percent}%)"
                )
            else:
                value = item.give_term if item.mission_success else item.view_time
                detail = (
                    f"{item.give_term}/{item.give_term} 分 ✓"
                    if item.mission_success
                    else f"{item.view_time}/{item.give_term} 分 ({item.percent}%)"
                )
            ttk.Progressbar(bar_row, maximum=max(item.give_term, 1), value=value).pack(
                side=tk.LEFT, fill=tk.X, expand=True
            )
            ttk.Label(bar_row, text=detail, width=18).pack(side=tk.RIGHT, padx=(6, 0))

    def _clear_progress_host(self, host: ttk.Frame) -> None:
        for child in host.winfo_children():
            child.destroy()

    def _progress_empty_hint(self) -> str:
        mode = self._channel_mode.get()
        if mode == "owesports":
            return f"仅 {DEFAULT_CHANNEL_BJID} 官方台可累计，其他任务请切换选台模式"
        if mode == "manual":
            return "当前所选直播间不会累计其他类型进度"
        return "当前台不会累计其他类型进度"

    def _render_missions(self, missions: list, *, refresh_smart_preview: bool = True) -> None:
        type_title, subtitle, _type_key, items = self._missions_for_current_display(missions)
        self._progress_frame.configure(text=f"任务进度 · {type_title}（选中账号）")
        self._progress_subtitle_lbl.configure(text=subtitle)
        self._clear_progress_host(self._progress_host)

        uid = self._selected_uid or "—"
        if items:
            for mission in items:
                self._render_mission_block(self._progress_host, uid, mission, hint_wrap=720)
        else:
            ttk.Label(
                self._progress_host,
                text=f"[{uid}] 暂无{type_title}任务（{self._progress_empty_hint()}）",
                foreground="#757575",
            ).pack(anchor=tk.W)

        self._update_progress_warnings()
        self._update_priority_mission_combo(missions)
        if refresh_smart_preview and self._channel_mode.get() == "smart":
            self._refresh_smart_preview_async()

    def _render_inventory_table(self) -> None:
        for row in self._inv_tree.get_children():
            self._inv_tree.delete(row)
        for uid, item in self._all_inventory:
            code = item.redeem_code or ("待领取" if item.can_claim else "—")
            iid = f"{uid}:{item.item_code_idx}"
            self._inv_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(uid, item.item_name, code, item.receive_date or "—", item.exp_date or "—"),
            )
        accounts = len({u for u, _ in self._all_inventory})
        self._inv_count_var.set(f"共 {len(self._all_inventory)} 件奖励 · {accounts} 个账号")

    def _on_channel_mode_change(self) -> None:
        self._apply_channel_mode_ui()
        mode = self._channel_mode.get()
        if mode != "smart":
            self._smart_preview_channel = None
        if mode == "owesports":
            self._drops_channel_hint.set(
                f"仅等待 {DEFAULT_CHANNEL_BJID} 开播；未开播时不自动切换其他频道"
            )
            self._smart_other_var.set("")
        self._rerender_missions_if_cached()
        self._refresh_account_list()
        if mode in ("smart", "manual") and not self._channel_loading:
            self._fetch_drops_channels_async()

    def _on_priority_mission_change(self, _event: tk.Event | None = None) -> None:
        self._rerender_missions_if_cached()
        self._refresh_smart_preview_async()

    def _on_manual_input_change(self, *_args: object) -> None:
        if self._channel_mode.get() != "manual":
            return
        self._show_manual_mismatch_hints(self._local_manual_channel())
        self._rerender_missions_if_cached()

    def _apply_channel_mode_ui(self) -> None:
        mode = self._channel_mode.get()
        if mode == "smart":
            self._smart_panel.pack(fill=tk.X, pady=(6, 0))
            self._manual_panel.pack_forget()
            self._drops_channel_hint.set("按任务自动匹配直播间；可指定优先任务")
        elif mode == "manual":
            self._smart_panel.pack_forget()
            self._manual_panel.pack(fill=tk.X, pady=(6, 0))
            self._drops_channel_hint.set(
                "手动选台：分类与任务不一致时仅警告、不阻止开始；进度可能不涨"
            )
        else:
            self._smart_panel.pack_forget()
            self._manual_panel.pack_forget()
            self._drops_channel_hint.set(
                f"仅等待 {DEFAULT_CHANNEL_BJID} 开播；未开播时不自动切换其他频道"
            )

    def _priority_mission_id(self) -> str:
        label = self._priority_mission_var.get()
        return self._priority_mission_map.get(label, PRIORITY_MISSION_AUTO)

    def _update_priority_mission_combo(self, missions: list[Mission]) -> None:
        self._priority_mission_map.clear()
        labels = ["自动（固定型优先）"]
        self._priority_mission_map[labels[0]] = PRIORITY_MISSION_AUTO
        for mission in active_progress_missions(missions):
            label = mission_pick_label(mission)
            if label not in self._priority_mission_map:
                labels.append(label)
                self._priority_mission_map[label] = mission.drops_idx
        current = self._priority_mission_var.get()
        self._priority_mission_combo["values"] = labels
        if current in labels:
            self._priority_mission_var.set(current)
        else:
            self._priority_mission_var.set(labels[0])

    def _smart_other_tasks_hint(
        self,
        channel: LiveChannel,
        missions: list[Mission],
        priority_mission_id: str,
    ) -> str:
        active = active_progress_missions(missions)
        served_ids = {m.drops_idx for m in missions_for_channel(missions, channel)}
        if priority_mission_id != PRIORITY_MISSION_AUTO:
            pending = [m for m in active if m.drops_idx != priority_mission_id]
        else:
            pending = [m for m in active if m.drops_idx not in served_ids]
        if not pending:
            return ""
        hints = [mission_pick_label(m) for m in pending[:3]]
        extra = f" 等 {len(pending)} 个" if len(pending) > 3 else ""
        return (
            f"以下任务在本台不会累计，需换台或调整优先任务：{'、'.join(hints)}{extra}。"
            f" {ONE_STREAM_NOTICE}"
        )

    def _local_manual_channel(self) -> LiveChannel | None:
        cfg = self._get_channel_config()
        if cfg.mode != "manual":
            return None
        raw = cfg.manual_input.strip()
        if not raw:
            sel = self._drops_channel_var.get()
            if sel and sel in self._drops_channel_map and not sel.startswith("[未开播"):
                return self._drops_channel_map[sel]
            return None
        bjid, bno = parse_stream_input(raw)
        pools = list(self._cached_online) + list(self._hashtag_live)
        for ch in pools:
            if bjid and ch.user_id == bjid:
                if bno is None or ch.broad_no == bno:
                    return ch
            if not bjid and bno and ch.broad_no == bno:
                return ch
        if sel := self._drops_channel_var.get():
            if sel in self._drops_channel_map and not sel.startswith("[未开播"):
                return self._drops_channel_map[sel]
        return None

    def _show_manual_mismatch_hints(self, channel: LiveChannel | None) -> None:
        if channel is None:
            return
        warnings = manual_channel_mismatch_warnings(channel, self._cached_missions)
        if warnings:
            self._drops_channel_hint.set(warnings[0])
        else:
            cate = channel.category_names[0] if channel.category_names else ""
            extra = f" · {cate}" if cate else ""
            self._drops_channel_hint.set(
                f"已选择: {channel.user_nick or channel.user_id} ({channel.user_id}){extra}，分类与任务匹配"
            )

    def _log_manual_mismatch_warnings(self) -> None:
        channel = self._local_manual_channel()
        if channel is None:
            return
        for msg in manual_channel_mismatch_warnings(channel, self._cached_missions):
            self._append_log(f"⚠ {msg}")

    def _refresh_smart_preview_async(self) -> None:
        if self._channel_mode.get() != "smart":
            return
        uids = list_accounts()
        if not uids or not self._cached_missions:
            return

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                cookies = load_cookies(uids[0])
                cfg = self._get_channel_config()

                async def _pick() -> LiveChannel | None:
                    async with aiohttp.ClientSession() as session:
                        apply_cookies(session, cookies)
                        return await pick_channel(
                            session, cookies, self._cached_missions, cfg
                        )

                channel = loop.run_until_complete(_pick())
            except Exception:
                loop.close()
                return

            def _done() -> None:
                if self._channel_mode.get() != "smart":
                    return
                self._smart_preview_channel = channel
                if channel is None:
                    self._smart_preview_var.set(format_channel_preview(None))
                    self._smart_other_var.set("")
                else:
                    self._smart_preview_var.set(format_channel_preview(channel))
                    self._smart_other_var.set(
                        self._smart_other_tasks_hint(
                            channel,
                            self._cached_missions,
                            cfg.priority_mission_id,
                        )
                    )
                self._rerender_missions_if_cached()

            self.root.after(0, _done)
            loop.close()

        threading.Thread(target=_worker, name="SmartPreview", daemon=True).start()

    def _set_channel_loading(self, loading: bool, hint: str | None = None) -> None:
        self._channel_loading = loading
        if hint is not None:
            self._drops_channel_hint.set(hint)
        if loading:
            self._channel_refresh_btn.configure(state=tk.DISABLED)
            self._channel_load_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._channel_load_bar.start(12)
        else:
            self._channel_refresh_btn.configure(state=tk.NORMAL)
            self._channel_load_bar.stop()
            self._channel_load_bar.pack_forget()

    def _get_channel_config(self) -> ChannelConfig:
        mode = self._channel_mode.get()
        if mode == "manual":
            sel = self._drops_channel_var.get()
            picked = self._channel_url_var.get().strip()
            if not picked and sel and sel in self._drops_channel_map and not sel.startswith("[未开播"):
                ch = self._drops_channel_map[sel]
                picked = f"{ch.user_id}/{ch.broad_no}" if ch.broad_no else ch.user_id
            return ChannelConfig(
                mode="manual",
                manual_input=picked,
                preferred_bjid=DEFAULT_CHANNEL_BJID,
            )
        if mode == "owesports":
            return ChannelConfig(
                mode="owesports",
                manual_input="",
                preferred_bjid=DEFAULT_CHANNEL_BJID,
            )
        return ChannelConfig(
            mode="smart",
            manual_input="",
            preferred_bjid=DEFAULT_CHANNEL_BJID,
            priority_mission_id=self._priority_mission_id(),
        )

    def _update_drops_channel_combo(
        self,
        channels: list[LiveChannel],
        *,
        candidates: list[LiveChannel] | None = None,
        missions: list[Mission] | None = None,
        events: list[DropEvent] | None = None,
    ) -> None:
        self._drops_channel_map.clear()
        labels: list[str] = []
        missions = missions if missions is not None else self._cached_missions
        events = events if events is not None else self._cached_events
        for ch in channels:
            type_tag = format_channel_drops_label(ch, missions, events)
            cate = ""
            if ch.category_names:
                cate = f" · {ch.category_names[0]}"
            label = f"[{type_tag}]{cate} {ch.user_nick or ch.user_id} ({ch.user_id})"
            if ch.broad_no:
                label += f" · {ch.broad_no}"
            labels.append(label)
            self._drops_channel_map[label] = ch

        offline = candidates or []
        if not channels and offline:
            for ch in offline:
                type_tag = format_channel_drops_label(ch, missions, events)
                label = f"[未开播·{type_tag}] {ch.user_nick or ch.user_id} ({ch.user_id})"
                labels.append(label)
                self._drops_channel_map[label] = ch

        self._drops_channel_combo["values"] = labels
        cand_n = len(candidates) if candidates else len(channels)

        if channels:
            self._drops_channel_var.set(labels[0])
            extra = f"，另有 {cand_n} 个官方频道未开播" if cand_n else ""
            self._drops_channel_hint.set(
                f"已加载 {len(channels)} 个 #드롭스 在线（方括号内为掉宝类型）{extra}"
            )
        elif labels:
            self._drops_channel_var.set(labels[0])
            self._drops_channel_hint.set(
                f"标签搜索暂无在线直播，但有 {cand_n} 个官方掉宝频道未开播（开播后请再刷新）"
            )
        else:
            self._drops_channel_var.set("")
            self._drops_channel_hint.set("未找到 #드롭스 在线直播间或官方掉宝频道")

    def _on_drops_channel_pick(self, _event: tk.Event | None = None) -> None:
        sel = self._drops_channel_var.get()
        if sel.startswith("[未开播"):
            self._drops_channel_hint.set("该直播间当前未开播，请选在线项或等待开播后刷新")
            return
        if sel and sel in self._drops_channel_map:
            ch = self._drops_channel_map[sel]
            self._channel_url_var.set(
                f"https://play.sooplive.com/{ch.user_id}"
                + (f"/{ch.broad_no}" if ch.broad_no else "")
            )
            self._show_manual_mismatch_hints(ch)
        self._rerender_missions_if_cached()

    def _fetch_drops_channels_async(self, *, silent: bool = False) -> None:
        if self._channel_loading:
            return
        uids = list_accounts()
        if not uids:
            if not silent:
                self._drops_channel_hint.set("请先添加账号后再刷新直播间列表")
            return

        if not silent:
            self.root.after(0, lambda: self._set_channel_loading(True, "正在加载可掉宝直播间…"))

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                channels, candidates, missions, hashtag_live, events = loop.run_until_complete(
                    self._load_drops_channels(uids[0])
                )
            except Exception as exc:
                err = str(exc)
                if not silent:
                    self.root.after(0, lambda: self._set_channel_loading(False, f"刷新失败: {err}"))
                loop.close()
                return

            def _done() -> None:
                if not silent:
                    self._set_channel_loading(False)
                self._hashtag_live = hashtag_live
                self._cached_online = channels
                self._cached_missions = missions
                self._cached_events = events
                self._channel_list_loaded = True
                self._update_priority_mission_combo(missions)
                mode = self._channel_mode.get()
                if not silent or mode in ("smart", "manual"):
                    self._update_drops_channel_combo(
                        channels, candidates=candidates, missions=missions, events=events
                    )
                if mode == "smart":
                    self._refresh_smart_preview_async()
                elif mode == "manual":
                    self._show_manual_mismatch_hints(self._local_manual_channel())
                missions_view = self._missions_for_warnings()
                if missions_view and self._selected_uid:
                    self._render_missions(missions_view)
                else:
                    self._update_progress_warnings()
                if silent:
                    return
                if channels:
                    self._append_log(
                        f"直播间列表已更新: {len(channels)} 个 #드롭스 在线"
                        + (f" / {len(candidates)} 个官方频道未开播" if candidates else "")
                    )
                elif candidates:
                    self._append_log(
                        f"标签搜索无在线直播，{len(candidates)} 个官方掉宝频道均未开播"
                    )

            self.root.after(0, _done)
            loop.close()

        threading.Thread(target=_worker, name="ChannelFetch", daemon=True).start()

    def _fetch_missions_display_async(self) -> None:
        uids = list_accounts()
        if not uids:
            return
        uid = self._selected_uid or uids[0]

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                missions = loop.run_until_complete(self._load_missions_only(uid))
            except Exception:
                loop.close()
                return

            def _done() -> None:
                self._cached_missions = missions
                if self._selected_uid == uid:
                    self._render_missions(missions)
                    self._update_progress_warnings()

            self.root.after(0, _done)
            loop.close()

        threading.Thread(target=_worker, name="MissionFetch", daemon=True).start()

    async def _load_missions_only(self, uid: str) -> list[Mission]:
        cookies = load_cookies(uid)
        if not cookies:
            return []
        async with aiohttp.ClientSession() as session:
            apply_cookies(session, cookies)
            return await DropsClient(session).get_missions()

    async def _load_drops_channels(
        self, uid: str
    ) -> tuple[list[LiveChannel], list[LiveChannel], list[Mission], list[LiveChannel], list[DropEvent]]:
        cookies = load_cookies(uid)
        if not cookies:
            return [], [], [], [], []
        async with aiohttp.ClientSession() as session:
            apply_cookies(session, cookies)
            client = DropsClient(session)
            missions = await client.get_missions()
            events = await client.get_progress_events()
            hashtag_live = await fetch_hashtag_drops_channels(session, cookies)
            online, candidates = await fetch_live_drops_channels(session, cookies, missions)
            return online, candidates, missions, hashtag_live, events

    def _copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_selected_code(self) -> None:
        selected = self._inv_tree.selection()
        if not selected:
            messagebox.showinfo("复制", "请先选中一行奖励")
            return
        code = self._inv_tree.item(selected[0], "values")[2]
        if not code or code in ("—", "待领取"):
            messagebox.showinfo("复制", "该奖励暂无兑换码")
            return
        self._copy_to_clipboard(code)
        self._append_log(f"已复制兑换码: {code}")

    def _copy_all_codes(self) -> None:
        codes = [it.redeem_code for _, it in self._all_inventory if it.redeem_code]
        if not codes:
            messagebox.showinfo("复制", "当前没有可复制的兑换码")
            return
        self._copy_to_clipboard("\n".join(codes))
        self._append_log(f"已复制 {len(codes)} 个兑换码到剪贴板")

    def _fetch_inventory_async(self) -> None:
        uids = list_accounts()
        if not uids:
            return

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                items = loop.run_until_complete(self._load_all_inventory(uids))
            except Exception as exc:
                err = str(exc)
                self._schedule_ui(lambda: self._append_log(f"背包加载失败: {err}"))
                loop.close()
                return
            self._schedule_ui(lambda: self._set_inventory(items))
            loop.close()

        threading.Thread(target=_worker, name="InventoryFetch", daemon=True).start()

    async def _load_all_inventory(self, uids: list[str]) -> list[tuple[str, InventoryItem]]:
        result: list[tuple[str, InventoryItem]] = []
        async with aiohttp.ClientSession() as session:
            for uid in uids:
                cookies = load_cookies(uid)
                if not cookies:
                    continue
                apply_cookies(session, cookies)
                client = DropsClient(session)
                try:
                    inv = await client.get_inventory(with_codes=True)
                    result.extend((uid, it) for it in inv)
                except Exception as exc:
                    logger.warning("[%s] 背包加载失败: %s", uid, exc)
        return result

    def _set_inventory(self, items: list[tuple[str, InventoryItem]]) -> None:
        self._all_inventory = items
        self._render_inventory_table()

    def _open_drops_mission_page(self) -> None:
        webbrowser.open(DROPS_MISSION_URL)

    def _open_drops_event_page(self) -> None:
        webbrowser.open(DROPS_EVENT_URL)

    async def _prepare_drops_account(self, uid: str) -> tuple[list[Mission], list]:
        cookies = load_cookies(uid)
        if not cookies:
            return [], []
        async with aiohttp.ClientSession() as session:
            apply_cookies(session, cookies)
            client = DropsClient(session)
            await client.ensure_drops_ready()
            missions = await client.get_missions()
            events = await client.get_progress_events() if not missions else []
            return missions, events

    def _prepare_drops_account_async(self, uid: str) -> None:
        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                missions, progress_events = loop.run_until_complete(self._prepare_drops_account(uid))
            except Exception as exc:
                self.root.after(0, lambda: self._append_log(f"[{uid}] Drops 初始化失败: {exc}"))
                loop.close()
                return
            count = len(missions)

            def _done() -> None:
                if count:
                    self._append_log(f"[{uid}] Drops 已就绪，mission 页 {count} 个任务")
                    self._cached_missions = missions
                    if self._selected_uid == uid:
                        self._render_missions(missions)
                else:
                    self._append_log(
                        DropsClient.empty_missions_hint(
                            uid,
                            progress_count=len(progress_events) or None,
                        )
                    )
                    if progress_events:
                        self._append_log(DropsClient.progress_events_summary(progress_events))
                self._fetch_drops_channels_async()

            self.root.after(0, _done)
            loop.close()

        threading.Thread(target=_worker, name=f"PrepareDrops-{uid}", daemon=True).start()

    def _on_add_account(self) -> None:
        userid = self._userid_var.get().strip()
        password = self._password_var.get().strip()
        if not userid or not password:
            messagebox.showwarning("添加账号", "请填写账号和密码")
            return

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(login(userid, password))
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("登录失败", str(exc)))
                loop.close()
                return
            self.root.after(0, lambda: self._after_add_account(userid))
            loop.close()

        threading.Thread(target=_worker, name="AddAccount", daemon=True).start()

    def _after_add_account(self, userid: str) -> None:
        self._password_var.set("")
        self._selected_uid = userid
        self._append_log(f"已添加账号: {userid}，正在初始化 Drops…")
        self._refresh_account_list()
        self._prepare_drops_account_async(userid)
        self._fetch_inventory_async()

    def _on_remove_account(self) -> None:
        sel = self._acct_tree.selection()
        if not sel:
            messagebox.showinfo("删除账号", "请先在列表中选中要删除的账号")
            return
        uid = sel[0]
        if not messagebox.askyesno("删除账号", f"确定删除账号 {uid} 及其本地登录信息？"):
            return
        if self._manager:
            self._manager.stop_account(uid)
        remove_account(uid)
        self._states.pop(uid, None)
        if self._selected_uid == uid:
            self._selected_uid = None
        self._all_inventory = [(u, it) for u, it in self._all_inventory if u != uid]
        self._append_log(f"已删除账号: {uid}")
        self._refresh_account_list()
        self._render_inventory_table()

    def _on_start_all(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._channel_mode.get() == "manual":
            self._log_manual_mismatch_warnings()

        def _runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._run_all_miners())
            except Exception as exc:
                logger.error("运行异常: %s", exc)
            finally:
                self._manager = None
                self.root.after(0, self._on_miners_stopped)
                self._loop.close()

        self._thread = threading.Thread(target=_runner, name="MultiMiner", daemon=True)
        self._thread.start()
        self._update_action_bar()

    async def _run_all_miners(self) -> None:
        cookies_map = load_all_cookies()
        userid = self._userid_var.get().strip()
        password = self._password_var.get().strip()
        if userid and password:
            cookies_map[userid] = await login(userid, password)
            self.root.after(0, lambda: self._after_add_account(userid))

        if not cookies_map:
            self.root.after(
                0,
                lambda: messagebox.showwarning("需要账号", "请先添加至少一个 SOOP 账号。"),
            )
            return

        self._manager = MultiMinerManager(
            on_state=self._on_state,
            channel_config=self._get_channel_config(),
        )
        started = await self._manager.start_all(cookies_map)
        if not started:
            return
        self.root.after(0, lambda: self._append_log(f"已启动 {len(started)} 个账号: {', '.join(started)}"))
        await self._manager.wait()
        await self._manager.shutdown()

    def _on_miners_stopped(self) -> None:
        self._stopping = False
        for uid in list(self._states.keys()):
            st = self._states[uid]
            if st.running:
                self._states[uid] = MinerState(uid=uid, status="已停止")
        self._refresh_account_list()

    def _on_stop_all(self) -> None:
        if self._manager:
            self._stopping = True
            self._update_action_bar()
            self._manager.stop_all()

    def _start_restore_poll(self) -> None:
        if sys.platform != "win32":
            return

        def _tick() -> None:
            if consume_show_request():
                self._restore_from_tray()
            self._restore_poll_id = self.root.after(400, _tick)

        self._restore_poll_id = self.root.after(400, _tick)

    def _restore_from_tray(self) -> None:
        def _show() -> None:
            self._in_tray = False
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()

        self._schedule_ui(_show)

    def _minimize_to_tray(self) -> None:
        if sys.platform != "win32" or self._tray is None:
            self._quit_app()
            return
        self._in_tray = True
        self.root.withdraw()

    def _quit_app(self) -> None:
        def _shutdown() -> None:
            if self._restore_poll_id:
                try:
                    self.root.after_cancel(self._restore_poll_id)
                except tk.TclError:
                    pass
                self._restore_poll_id = None
            if self._manager:
                self._manager.stop_all()
            if self._tray:
                self._tray.stop()
                self._tray = None
            self.root.destroy()

        if threading.current_thread() is threading.main_thread():
            _shutdown()
        else:
            self._schedule_ui(_shutdown)

    def _on_close(self) -> None:
        self._minimize_to_tray()

    def run(self) -> None:
        def _bootstrap() -> None:
            if list_accounts():
                self._fetch_inventory_async()
                self._fetch_drops_channels_async()

        self.root.after(0, _bootstrap)
        self.root.mainloop()


def run_gui() -> None:
    from .single_instance import ensure_single_instance_or_exit

    if not ensure_single_instance_or_exit():
        return
    SoopGui().run()
