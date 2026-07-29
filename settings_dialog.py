from __future__ import annotations

import asyncio
import queue
import sys
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import messagebox, ttk
from typing import Callable

from .config import (
    AppConfig,
    SETTINGS_VERSION,
    reset_settings,
    save_settings,
    validate_proxy_url,
)
from .network import ProxyTestResult, test_proxy_connectivity
from .windows_startup import apply_auto_start_setting, disable_auto_start, enable_auto_start


class SettingsDialog:
    def __init__(
        self,
        parent: tk.Misc,
        settings: AppConfig,
        *,
        on_saved: Callable[[AppConfig], None],
    ) -> None:
        self._current = replace(settings)
        self._baseline = replace(settings)
        self._on_saved = on_saved
        self._test_queue: queue.Queue[list[ProxyTestResult] | BaseException] = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.title("设置")
        self.window.geometry("620x690")
        self.window.minsize(580, 620)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

        self._auto_start = tk.BooleanVar()
        self._start_tray = tk.BooleanVar()
        self._close_tray = tk.BooleanVar()
        self._proxy_enabled = tk.BooleanVar()
        self._proxy_url = tk.StringVar()
        self._proxy_fallback = tk.BooleanVar()
        self._auto_claim = tk.BooleanVar()
        self._low_bandwidth = tk.BooleanVar()
        self._mission_interval = tk.StringVar()
        self._inventory_interval = tk.StringVar()
        self._channel_interval = tk.StringVar()
        self._appearance_mode = tk.StringVar()
        self._load_vars(settings)
        self._build()
        self.window.grab_set()

    def _build(self) -> None:
        host = ttk.Frame(self.window, padding=12)
        host.pack(fill=tk.BOTH, expand=True)

        general = ttk.LabelFrame(host, text="常规", padding=10)
        general.pack(fill=tk.X, pady=(0, 10))
        auto = ttk.Checkbutton(
            general,
            text="开机自启（当前 Windows 用户）",
            variable=self._auto_start,
            command=self._on_auto_start_toggle,
        )
        auto.pack(anchor=tk.W)
        if sys.platform != "win32":
            auto.configure(state=tk.DISABLED)
        ttk.Checkbutton(
            general,
            text="启动后静默进入系统托盘",
            variable=self._start_tray,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Checkbutton(
            general,
            text="关闭主窗口时隐藏到托盘",
            variable=self._close_tray,
        ).pack(anchor=tk.W, pady=(4, 0))

        network = ttk.LabelFrame(host, text="网络", padding=10)
        network.pack(fill=tk.X, pady=(0, 10))
        ttk.Checkbutton(network, text="启用代理", variable=self._proxy_enabled).grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(network, text="代理地址").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(network, textvariable=self._proxy_url).grid(
            row=1, column=1, sticky=tk.EW, padx=(8, 0), pady=(6, 0)
        )
        ttk.Label(network, text="例如：http://127.0.0.1:7897", foreground="#757575").grid(
            row=2, column=1, sticky=tk.W, padx=(8, 0)
        )
        ttk.Checkbutton(
            network,
            text="代理失败后允许直连",
            variable=self._proxy_fallback,
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        self._test_button = ttk.Button(network, text="测试代理", command=self._test_proxy)
        self._test_button.grid(row=4, column=0, sticky=tk.W, pady=(8, 0))
        self._test_status = tk.StringVar(value="")
        ttk.Label(
            network,
            textvariable=self._test_status,
            justify=tk.LEFT,
            wraplength=520,
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        ttk.Label(
            network,
            text="新代理设置将在下次启动挂机或网络重连时生效。",
            foreground="#ef6c00",
        ).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        network.columnconfigure(1, weight=1)

        drops = ttk.LabelFrame(host, text="掉宝", padding=10)
        drops.pack(fill=tk.X, pady=(0, 10))
        ttk.Checkbutton(drops, text="自动领取奖励", variable=self._auto_claim).grid(
            row=0, column=0, columnspan=2, sticky=tk.W
        )
        ttk.Label(
            drops,
            text="领取后会重新读取 Inventory 验证；无法确认时请在官方背包页手动领取。",
            foreground="#757575",
            wraplength=530,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(2, 6))
        ttk.Checkbutton(drops, text="低流量模式", variable=self._low_bandwidth).grid(
            row=2, column=0, columnspan=2, sticky=tk.W
        )
        rows = (
            ("Mission 刷新间隔（30～600 秒）", self._mission_interval),
            ("背包刷新间隔（60～1800 秒）", self._inventory_interval),
            ("频道列表刷新间隔（60～1800 秒）", self._channel_interval),
        )
        for row, (label, variable) in enumerate(rows, start=3):
            ttk.Label(drops, text=label).grid(row=row, column=0, sticky=tk.W, pady=(6, 0))
            ttk.Entry(drops, textvariable=variable, width=10).grid(
                row=row, column=1, sticky=tk.E, pady=(6, 0)
            )
        ttk.Label(
            drops,
            text="观看心跳固定 5 秒；Bridge Keepalive 固定 20 秒。",
            foreground="#757575",
        ).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        drops.columnconfigure(0, weight=1)

        buttons = ttk.Frame(host)
        buttons.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(buttons, text="恢复默认值", command=self._reset_draft).pack(side=tk.LEFT)
        ttk.Button(buttons, text="保存", command=self._save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side=tk.RIGHT, padx=(0, 8))

    def _load_vars(self, settings: AppConfig) -> None:
        self._auto_start.set(settings.auto_start_enabled)
        self._start_tray.set(settings.start_minimized_to_tray)
        self._close_tray.set(settings.close_to_tray)
        self._proxy_enabled.set(settings.proxy_enabled)
        self._proxy_url.set(settings.proxy_url)
        self._proxy_fallback.set(settings.proxy_fallback_direct)
        self._auto_claim.set(settings.auto_claim_enabled)
        self._low_bandwidth.set(settings.low_bandwidth_mode)
        self._mission_interval.set(str(settings.mission_poll_interval))
        self._inventory_interval.set(str(settings.inventory_poll_interval))
        self._channel_interval.set(str(settings.channel_refresh_interval))
        self._appearance_mode.set(settings.appearance_mode)

    def _draft(self) -> AppConfig:
        try:
            mission = int(self._mission_interval.get().strip())
            inventory = int(self._inventory_interval.get().strip())
            channel = int(self._channel_interval.get().strip())
        except ValueError as exc:
            raise ValueError("刷新间隔必须是整数") from exc
        proxy_url = self._proxy_url.get().strip()
        if proxy_url:
            validate_proxy_url(proxy_url)
        return AppConfig(
            settings_version=SETTINGS_VERSION,
            auto_claim_enabled=bool(self._auto_claim.get()),
            low_bandwidth_mode=bool(self._low_bandwidth.get()),
            proxy_enabled=bool(self._proxy_enabled.get()),
            proxy_url=proxy_url,
            proxy_fallback_direct=bool(self._proxy_fallback.get()),
            auto_start_enabled=bool(self._auto_start.get()),
            start_minimized_to_tray=bool(self._start_tray.get()),
            close_to_tray=bool(self._close_tray.get()),
            appearance_mode=self._appearance_mode.get() or "system",
            mission_poll_interval=mission,
            inventory_poll_interval=inventory,
            channel_refresh_interval=channel,
        ).validated()

    @staticmethod
    def _set_registry(enabled: bool) -> None:
        if enabled:
            enable_auto_start()
        else:
            disable_auto_start()

    def _on_auto_start_toggle(self) -> None:
        target = bool(self._auto_start.get())
        previous = self._current.auto_start_enabled
        try:
            saved = apply_auto_start_setting(self._current, target)
        except Exception as exc:
            self._auto_start.set(previous)
            messagebox.showerror("开机自启", f"修改开机启动项失败：{exc}", parent=self.window)
            return
        self._current = saved
        self._baseline = replace(self._baseline, auto_start_enabled=target)
        self._on_saved(saved)

    def _save(self) -> None:
        previous = self._current
        try:
            draft = self._draft()
        except ValueError as exc:
            messagebox.showerror("设置", str(exc), parent=self.window)
            return
        registry_changed = draft.auto_start_enabled != previous.auto_start_enabled
        try:
            if registry_changed:
                self._set_registry(draft.auto_start_enabled)
            saved = save_settings(draft)
        except Exception as exc:
            if registry_changed:
                try:
                    self._set_registry(previous.auto_start_enabled)
                except Exception:
                    pass
            messagebox.showerror("设置", f"保存失败，原设置未改变：{exc}", parent=self.window)
            return
        self._current = saved
        self._baseline = replace(saved)
        self._on_saved(saved)
        self.window.destroy()

    def _reset_draft(self) -> None:
        self._load_vars(reset_settings())

    def _is_dirty(self) -> bool:
        try:
            return self._draft() != self._baseline
        except ValueError:
            return True

    def _cancel(self) -> None:
        if self._is_dirty() and not messagebox.askyesno(
            "设置",
            "存在未保存的修改，确定放弃吗？",
            parent=self.window,
        ):
            return
        self.window.destroy()

    def _test_proxy(self) -> None:
        try:
            draft = self._draft()
            if not draft.proxy_enabled:
                raise ValueError("请先启用代理")
        except ValueError as exc:
            messagebox.showerror("测试代理", str(exc), parent=self.window)
            return
        self._test_button.configure(state=tk.DISABLED)
        self._test_status.set("正在测试代理链路…")

        def worker() -> None:
            try:
                self._test_queue.put(asyncio.run(test_proxy_connectivity(draft)))
            except BaseException as exc:
                self._test_queue.put(exc)

        threading.Thread(target=worker, name="ProxyConnectivityTest", daemon=True).start()
        self.window.after(100, self._poll_proxy_test)

    def _poll_proxy_test(self) -> None:
        try:
            result = self._test_queue.get_nowait()
        except queue.Empty:
            if self.window.winfo_exists():
                self.window.after(100, self._poll_proxy_test)
            return
        self._test_button.configure(state=tk.NORMAL)
        if isinstance(result, BaseException):
            self._test_status.set(f"测试失败：{result}")
            return
        lines: list[str] = []
        for item in result:
            if item.ok and item.complete:
                status = "成功"
            elif item.ok:
                status = "可达（未完整鉴权）"
            else:
                status = "失败"
            lines.append(f"{item.target}：{status} · {item.elapsed_ms} ms · {item.detail}")
        self._test_status.set("\n".join(lines))
