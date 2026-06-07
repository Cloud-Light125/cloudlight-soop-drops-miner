from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Callable

import aiohttp

from .auth import apply_cookies, load_cookies, login, userid_from_cookies
from .constants import APP_NAME, WINDOW_TITLE
from .drops import DropsClient
from .miner import MinerState, SoopMiner
from .models import InventoryItem

logger = logging.getLogger("SoopDropsMiner")

_STATUS_COLORS = {
    "挂机中": "#2e7d32",
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
        self.root.geometry("620x760")
        self.root.minsize(540, 680)

        self._cookies = load_cookies()
        self._miner: SoopMiner | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._inventory: list[InventoryItem] = []

        self._build_ui()
        self._setup_logging()
        self._refresh_login_hint()
        if self._cookies:
            self._fetch_inventory_async()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = self.root
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        header = ttk.Frame(root, padding=(12, 10, 12, 6))
        header.pack(fill=tk.X)

        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        self._account_badge = tk.StringVar(value="未登录")
        ttk.Label(
            header,
            textvariable=self._account_badge,
            font=("Segoe UI", 10, "bold"),
            foreground="#1565c0",
            padding=(8, 2),
        ).pack(side=tk.RIGHT)

        sub = ttk.Frame(root, padding=(12, 0, 12, 8))
        sub.pack(fill=tk.X)
        self._login_var = tk.StringVar(value="")
        ttk.Label(sub, textvariable=self._login_var).pack(side=tk.LEFT)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        mine_tab = ttk.Frame(notebook, padding=0)
        inv_tab = ttk.Frame(notebook, padding=10)
        notebook.add(mine_tab, text="挂机")
        notebook.add(inv_tab, text="奖励背包")

        status_frame = ttk.LabelFrame(mine_tab, text="运行状态", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(status_frame)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="状态:").pack(side=tk.LEFT)
        self._status_var = tk.StringVar(value="空闲")
        self._status_label = ttk.Label(row1, textvariable=self._status_var, font=("Segoe UI", 10, "bold"))
        self._status_label.pack(side=tk.LEFT, padx=(6, 0))

        row2 = ttk.Frame(status_frame)
        row2.pack(fill=tk.X, pady=(6, 0))
        self._channel_var = tk.StringVar(value="直播间: —")
        ttk.Label(row2, textvariable=self._channel_var).pack(side=tk.LEFT)

        row3 = ttk.Frame(status_frame)
        row3.pack(fill=tk.X, pady=(4, 0))
        self._bridge_var = tk.StringVar(value="Bridge: 未连接")
        ttk.Label(row3, textvariable=self._bridge_var).pack(side=tk.LEFT)

        progress_frame = ttk.LabelFrame(mine_tab, text="任务进度（全部档位）", padding=10)
        progress_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        progress_scroll = ttk.Frame(progress_frame)
        progress_scroll.pack(fill=tk.BOTH, expand=True)
        self._progress_canvas = tk.Canvas(progress_scroll, height=240, highlightthickness=0)
        progress_bar = ttk.Scrollbar(progress_scroll, orient=tk.VERTICAL, command=self._progress_canvas.yview)
        self._progress_host = ttk.Frame(self._progress_canvas)
        self._progress_host.bind(
            "<Configure>",
            lambda _e: self._progress_canvas.configure(scrollregion=self._progress_canvas.bbox("all")),
        )
        self._progress_canvas.create_window((0, 0), window=self._progress_host, anchor=tk.NW)
        self._progress_canvas.configure(yscrollcommand=progress_bar.set)
        self._progress_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        progress_bar.pack(side=tk.RIGHT, fill=tk.Y)

        login_frame = ttk.LabelFrame(mine_tab, text="账号登录", padding=10)
        login_frame.pack(fill=tk.X, pady=(0, 8))

        cred_row = ttk.Frame(login_frame)
        cred_row.pack(fill=tk.X)
        ttk.Label(cred_row, text="账号").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self._userid_var = tk.StringVar(value="")
        ttk.Entry(cred_row, textvariable=self._userid_var, width=22).grid(row=0, column=1, sticky=tk.W, pady=2)
        ttk.Label(cred_row, text="密码").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self._password_var = tk.StringVar()
        ttk.Entry(cred_row, textvariable=self._password_var, width=22, show="•").grid(
            row=1, column=1, sticky=tk.W, pady=2
        )

        btn_row = ttk.Frame(mine_tab, padding=(0, 0))
        btn_row.pack(fill=tk.X)
        self._start_btn = ttk.Button(btn_row, text="开始挂机", command=self._on_start)
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(btn_row, text="停止", command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(mine_tab, text="日志", padding=(10, 8))
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._log = scrolledtext.ScrolledText(
            log_frame,
            height=12,
            state=tk.DISABLED,
            font=("Consolas", 9),
            wrap=tk.WORD,
        )
        self._log.pack(fill=tk.BOTH, expand=True)

        inv_toolbar = ttk.Frame(inv_tab)
        inv_toolbar.pack(fill=tk.X, pady=(0, 8))
        self._inv_count_var = tk.StringVar(value="共 0 件奖励")
        ttk.Label(inv_toolbar, textvariable=self._inv_count_var).pack(side=tk.LEFT)
        ttk.Button(inv_toolbar, text="刷新", command=self._fetch_inventory_async).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(inv_toolbar, text="复制全部兑换码", command=self._copy_all_codes).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(inv_toolbar, text="复制选中", command=self._copy_selected_code).pack(side=tk.RIGHT)

        inv_tree_frame = ttk.Frame(inv_tab)
        inv_tree_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("name", "code", "receive", "exp")
        self._inv_tree = ttk.Treeview(
            inv_tree_frame,
            columns=columns,
            show="headings",
            height=14,
            selectmode="browse",
        )
        self._inv_tree.heading("name", text="奖励名称")
        self._inv_tree.heading("code", text="兑换码")
        self._inv_tree.heading("receive", text="领取时间")
        self._inv_tree.heading("exp", text="过期时间")
        self._inv_tree.column("name", width=180, anchor=tk.W)
        self._inv_tree.column("code", width=220, anchor=tk.W)
        self._inv_tree.column("receive", width=130, anchor=tk.W)
        self._inv_tree.column("exp", width=130, anchor=tk.W)
        inv_scroll = ttk.Scrollbar(inv_tree_frame, orient=tk.VERTICAL, command=self._inv_tree.yview)
        self._inv_tree.configure(yscrollcommand=inv_scroll.set)
        self._inv_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inv_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._inv_tree.bind("<Double-1>", lambda _e: self._copy_selected_code())

        ttk.Label(
            inv_tab,
            text="双击一行可复制该兑换码 · 数据来自 drops.sooplive.com/inventory",
            foreground="#757575",
        ).pack(anchor=tk.W, pady=(8, 0))

        footer = ttk.Frame(root, padding=(12, 0, 12, 8))
        footer.pack(fill=tk.X)
        ttk.Label(footer, text="SOOP Live Drops", foreground="#757575").pack(side=tk.RIGHT)

    def _setup_logging(self) -> None:
        handler = _TkLogHandler(self._append_log)
        handler.setLevel(logging.INFO)
        logging.getLogger("SoopDropsMiner").addHandler(handler)
        self._log_handler = handler

    def _refresh_login_hint(self) -> None:
        if self._cookies:
            uid = userid_from_cookies(self._cookies)
            self._login_var.set(f"已保存登录 · {uid}")
            self._account_badge.set(uid)
        else:
            self._login_var.set("未登录，请填写 SOOP 账号和密码")
            self._account_badge.set("未登录")

    def _append_log(self, text: str) -> None:
        def _write() -> None:
            self._log.configure(state=tk.NORMAL)
            self._log.insert(tk.END, text + "\n")
            self._log.see(tk.END)
            self._log.configure(state=tk.DISABLED)

        self.root.after(0, _write)

    def _on_state(self, state: MinerState) -> None:
        self.root.after(0, lambda: self._apply_state(state))

    def _apply_state(self, state: MinerState) -> None:
        self._status_var.set(state.status)
        color = _STATUS_COLORS.get(state.status, "#424242")
        self._status_label.configure(foreground=color)

        if state.channel_id:
            nick = state.channel_nick or state.channel_id
            bno = state.broad_no or "—"
            self._channel_var.set(f"直播间: {nick} ({state.channel_id})  ·  broadNo={bno}")
        else:
            self._channel_var.set("直播间: —")

        if state.bridge_connected:
            self._bridge_var.set("Bridge: 已连接")
        elif state.running:
            self._bridge_var.set("Bridge: 连接中…")
        else:
            self._bridge_var.set("Bridge: 未连接")

        self._render_missions(state.missions)
        if state.inventory:
            self._render_inventory(state.inventory)

        if state.running:
            self._start_btn.configure(state=tk.DISABLED)
            self._stop_btn.configure(state=tk.NORMAL)
        else:
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)

    def _render_missions(self, missions: list) -> None:
        for child in self._progress_host.winfo_children():
            child.destroy()

        if not missions or not any(m.items for m in missions):
            ttk.Label(self._progress_host, text="暂无进行中的任务", foreground="#757575").pack(anchor=tk.W)
            return

        for mission in missions:
            if not mission.items:
                continue
            active = mission.active_item()
            active_idx = mission.items.index(active) if active else -1
            view_time = mission.items[0].view_time

            block = ttk.Frame(self._progress_host)
            block.pack(fill=tk.X, pady=(0, 10))

            title = mission.title
            if len(title) > 48:
                title = title[:45] + "…"
            header = f"{title}  ·  累计 {view_time} 分钟"
            ttk.Label(block, text=header, font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)

            for i, item in enumerate(mission.items):
                tier = ttk.Frame(block)
                tier.pack(fill=tk.X, pady=(4, 0))

                if item.mission_success:
                    status, name_color = "已完成", "#9e9e9e"
                elif i == active_idx:
                    status, name_color = "进行中", "#1565c0"
                else:
                    status, name_color = "未达成", "#616161"

                label_row = ttk.Frame(tier)
                label_row.pack(fill=tk.X)
                ttk.Label(
                    label_row,
                    text=f"[{item.term_label}]",
                    font=("Segoe UI", 9, "bold"),
                    width=5,
                ).pack(side=tk.LEFT)
                name = item.item_name
                if len(name) > 32:
                    name = name[:29] + "…"
                ttk.Label(label_row, text=name, foreground=name_color).pack(side=tk.LEFT, padx=(4, 0))
                ttk.Label(label_row, text=status, foreground=name_color).pack(side=tk.RIGHT)

                bar_row = ttk.Frame(tier)
                bar_row.pack(fill=tk.X, pady=(1, 0))
                value = item.give_term if item.mission_success else item.view_time
                ttk.Progressbar(
                    bar_row,
                    maximum=max(item.give_term, 1),
                    value=value,
                    length=320,
                ).pack(side=tk.LEFT, fill=tk.X, expand=True)

                if item.mission_success:
                    detail = f"{item.give_term}/{item.give_term} 分 ✓"
                else:
                    detail = f"{item.view_time}/{item.give_term} 分 ({item.percent}%)"
                ttk.Label(bar_row, text=detail, width=18).pack(side=tk.RIGHT, padx=(6, 0))

    def _render_inventory(self, items: list[InventoryItem]) -> None:
        self._inventory = list(items)
        for row in self._inv_tree.get_children():
            self._inv_tree.delete(row)
        for item in items:
            code = item.redeem_code or ("待领取" if item.can_claim else "—")
            self._inv_tree.insert(
                "",
                tk.END,
                iid=item.item_code_idx,
                values=(
                    item.item_name,
                    code,
                    item.receive_date or "—",
                    item.exp_date or "—",
                ),
            )
        self._inv_count_var.set(f"共 {len(items)} 件奖励")

    def _copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_selected_code(self) -> None:
        selected = self._inv_tree.selection()
        if not selected:
            messagebox.showinfo("复制", "请先选中一行奖励")
            return
        code = self._inv_tree.item(selected[0], "values")[1]
        if not code or code in ("—", "待领取"):
            messagebox.showinfo("复制", "该奖励暂无兑换码")
            return
        self._copy_to_clipboard(code)
        self._append_log(f"已复制兑换码: {code}")

    def _copy_all_codes(self) -> None:
        codes = [it.redeem_code for it in self._inventory if it.redeem_code]
        if not codes:
            messagebox.showinfo("复制", "当前没有可复制的兑换码")
            return
        text = "\n".join(codes)
        self._copy_to_clipboard(text)
        self._append_log(f"已复制 {len(codes)} 个兑换码到剪贴板")

    def _fetch_inventory_async(self) -> None:
        cookies = load_cookies()
        if not cookies:
            return

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                items = loop.run_until_complete(self._load_inventory(cookies))
            except Exception as exc:
                self.root.after(0, lambda: self._append_log(f"背包加载失败: {exc}"))
                loop.close()
                return
            self.root.after(0, lambda: self._render_inventory(items))
            loop.close()

        threading.Thread(target=_worker, name="InventoryFetch", daemon=True).start()

    async def _load_inventory(self, cookies: dict[str, str]) -> list[InventoryItem]:
        async with aiohttp.ClientSession() as session:
            apply_cookies(session, cookies)
            client = DropsClient(session)
            return await client.get_inventory(with_codes=True)

    def _on_start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        async def _prepare_cookies() -> dict[str, str] | None:
            cookies = load_cookies()
            password = self._password_var.get().strip()
            userid = self._userid_var.get().strip()
            if password and userid:
                return await login(userid, password)
            return cookies

        def _runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                cookies = self._loop.run_until_complete(_prepare_cookies())
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("登录失败", str(exc)))
                self.root.after(0, lambda: self._start_btn.configure(state=tk.NORMAL))
                self._loop.close()
                return

            if not cookies:
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "需要登录",
                        "请填写 SOOP 账号和密码后点击「开始挂机」。",
                    ),
                )
                self.root.after(0, lambda: self._start_btn.configure(state=tk.NORMAL))
                self._loop.close()
                return

            self._cookies = cookies
            self.root.after(0, self._refresh_login_hint)

            async def _run() -> None:
                async with SoopMiner(cookies, on_state=self._on_state) as miner:
                    self._miner = miner
                    await miner.run()

            try:
                self._loop.run_until_complete(_run())
            except Exception as exc:
                logger.error("运行异常: %s", exc)
            finally:
                self._miner = None
                self.root.after(0, lambda: self._apply_state(MinerState(uid=userid_from_cookies(cookies))))
                self._loop.close()

        self._start_btn.configure(state=tk.DISABLED)
        self._thread = threading.Thread(target=_runner, name="SoopMiner", daemon=True)
        self._thread.start()

    def _on_stop(self) -> None:
        if self._miner:
            self._miner.stop()
        self._stop_btn.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self._miner:
            self._miner.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_gui() -> None:
    SoopGui().run()
