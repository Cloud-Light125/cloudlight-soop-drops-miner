from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import customtkinter as ctk
import tkinter as tk

from .ui_state import AccountUiState, InventoryUiState, MissionUiState, ProgressAnimationState, TierUiState
from .ui_theme import CARD_PAD, CARD_RADIUS, COLORS, CONTROL_RADIUS, font


def set_if_changed(widget: Any, option: str, value: Any, cache: dict[str, Any]) -> bool:
    if cache.get(option) == value:
        return False
    cache[option] = value
    widget.configure(**{option: value})
    return True


class ToolTip:
    """Small user-facing hover hint for controls with limited space."""

    def __init__(self, widget: Any, text: str) -> None:
        self.widget = widget
        self.text = text
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _event: Any = None) -> None:
        if self._window is not None or not self.text:
            return
        window = tk.Toplevel(self.widget)
        self._window = window
        window.wm_overrideredirect(True)
        window.wm_attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            window,
            text=self.text,
            justify="left",
            wraplength=320,
            padx=9,
            pady=6,
            relief="solid",
            borderwidth=1,
            font=("Microsoft YaHei UI", 9),
            background="#FFFBEA",
            foreground="#202124",
        )
        label.pack()

    def _hide(self, _event: Any = None) -> None:
        if self._window is not None:
            self._window.destroy()
            self._window = None


class CollapsibleCard(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        title: str,
        *,
        expanded: bool = True,
        expanded_button_text: str = "收起",
        collapsed_button_text: str = "展开",
        **kwargs: Any,
    ) -> None:
        super().__init__(master, corner_radius=CARD_RADIUS, fg_color=COLORS["surface"], **kwargs)
        self._expanded = expanded
        self._expanded_button_text = expanded_button_text
        self._collapsed_button_text = collapsed_button_text
        self.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=CARD_PAD, pady=(12, 8))
        header.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(header, text=title, font=font(16, "bold"), anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.toggle_button = ctk.CTkButton(
            header,
            width=76,
            height=28,
            corner_radius=CONTROL_RADIUS,
            text=expanded_button_text if expanded else collapsed_button_text,
            fg_color="transparent",
            border_width=1,
            text_color=("#344054", "#D0D5DD"),
            command=self.toggle,
        )
        self.toggle_button.grid(row=0, column=1, sticky="e")
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=CARD_PAD, pady=(0, CARD_PAD))
        self.body.grid_columnconfigure(0, weight=1)
        if not expanded:
            self.body.grid_remove()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        if expanded:
            self.body.grid()
            self.toggle_button.configure(text=self._expanded_button_text)
        else:
            self.body.grid_remove()
            self.toggle_button.configure(text=self._collapsed_button_text)


class AccountRow(ctk.CTkFrame):
    FIELDS = ("uid", "status", "channel", "mission", "progress", "bridge", "heartbeat", "rate")

    def __init__(self, master: Any, state: AccountUiState, on_select: Callable[[str], None]) -> None:
        super().__init__(master, corner_radius=8, fg_color=COLORS["row"], cursor="hand2")
        self.uid = state.uid
        self._state: AccountUiState | None = None
        self._selected = False
        self._labels: dict[str, ctk.CTkLabel] = {}
        widths = (82, 70, 92, 104, 84, 66, 74, 88)
        for index, (name, width) in enumerate(zip(self.FIELDS, widths)):
            self.grid_columnconfigure(index, weight=2 if name in {"channel", "mission"} else 1, minsize=width)
            label = ctk.CTkLabel(self, text="", anchor="w", font=font(12))
            label.grid(row=0, column=index, sticky="ew", padx=(10 if index == 0 else 4, 8), pady=9)
            label.bind("<Button-1>", lambda _e, uid=state.uid: on_select(uid))
            self._labels[name] = label
        self.bind("<Button-1>", lambda _e: on_select(state.uid))
        self.update_state(state)

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self.configure(fg_color=COLORS["row_selected"] if selected else COLORS["row"])

    def update_state(self, state: AccountUiState) -> tuple[str, ...]:
        if self._state == state:
            return ()
        changed: list[str] = []
        for name in self.FIELDS:
            value = getattr(state, name)
            if self._state is None or getattr(self._state, name) != value:
                self._labels[name].configure(text=value)
                changed.append(name)
        self._state = state
        return tuple(changed)


class SmoothProgressBar(ctk.CTkProgressBar):
    def __init__(self, master: Any, *, duration_ms: int = 220, **kwargs: Any) -> None:
        super().__init__(master, corner_radius=6, height=10, **kwargs)
        self._duration_ms = duration_ms
        self._value = 0.0
        self._target = 0.0
        self._animation_state = ProgressAnimationState()
        self._after_id: str | None = None
        self.set(0)

    @property
    def target(self) -> float:
        return self._target

    def update_value(self, value: float, *, animate: bool = True) -> bool:
        target = max(0.0, min(1.0, float(value)))
        if not self._animation_state.retarget(target):
            return False
        self.cancel_animation()
        self._target = target
        if not animate:
            self._value = target
            self.set(target)
            return True
        start = self._value
        started = time.monotonic()

        def step() -> None:
            elapsed = (time.monotonic() - started) * 1000
            ratio = min(1.0, elapsed / self._duration_ms)
            eased = 1 - (1 - ratio) ** 3
            self._value = start + (target - start) * eased
            self.set(self._value)
            if ratio < 1:
                self._after_id = self.after(16, step)
            else:
                self._after_id = None
                self._value = target

        self._after_id = self.after(0, step)
        return True

    def cancel_animation(self, *, snap_to_target: bool = False) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if snap_to_target:
            self._value = self._target
            self.set(self._target)

    def destroy(self) -> None:
        self.cancel_animation()
        super().destroy()


class MissionTierRow(ctk.CTkFrame):
    def __init__(self, master: Any, state: TierUiState, *, visible: Callable[[], bool]) -> None:
        super().__init__(master, fg_color=COLORS["row"], corner_radius=8)
        self.key = state.key
        self._state: TierUiState | None = None
        self._visible = visible
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=1)
        self.name = ctk.CTkLabel(self, text="", anchor="w", font=font(12, "bold"))
        self.name.grid(row=0, column=0, sticky="w", padx=12, pady=(9, 2))
        self.status = ctk.CTkLabel(self, text="", anchor="e", font=font(12))
        self.status.grid(row=0, column=1, sticky="e", padx=12, pady=(9, 2))
        self.progress_text = ctk.CTkLabel(self, text="", anchor="w", text_color=COLORS["muted"], font=font(11))
        self.progress_text.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12)
        self.progress = SmoothProgressBar(self)
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(5, 10))
        self.update_state(state, initial=True)

    def update_state(self, state: TierUiState, *, initial: bool = False) -> tuple[str, ...]:
        if self._state == state:
            return ()
        changed: list[str] = []
        if self._state is None or self._state.name != state.name:
            self.name.configure(text=state.name)
            changed.append("name")
        if self._state is None or self._state.claim_status != state.claim_status:
            self.status.configure(text=state.claim_status)
            changed.append("claim_status")
        if self._state is None or (
            self._state.current_minutes != state.current_minutes
            or self._state.required_minutes != state.required_minutes
            or self._state.percent != state.percent
        ):
            self.progress_text.configure(
                text=(
                    f"已观看：{state.current_minutes} 分钟　"
                    f"目标时长：{state.required_minutes} 分钟　"
                    f"完成进度：{state.percent}%"
                )
            )
            self.progress.update_value(state.percent / 100, animate=not initial and self._visible())
            changed.extend(("current_minutes", "percent"))
        self._state = state
        return tuple(dict.fromkeys(changed))

    def stop_animation(self) -> None:
        self.progress.cancel_animation(snap_to_target=True)


class MissionCard(ctk.CTkFrame):
    def __init__(self, master: Any, state: MissionUiState, *, visible: Callable[[], bool]) -> None:
        super().__init__(master, corner_radius=10, fg_color=COLORS["surface_alt"], border_width=1, border_color=COLORS["border"])
        self.key = state.key
        self._state: MissionUiState | None = None
        self._visible = visible
        self._tiers: dict[tuple[str, str, str], MissionTierRow] = {}
        self.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 3))
        top.grid_columnconfigure(0, weight=1)
        self.title = ctk.CTkLabel(top, text="", anchor="w", font=font(15, "bold"))
        self.title.grid(row=0, column=0, sticky="w")
        self.status = ctk.CTkLabel(top, text="", anchor="e", font=font(12, "bold"))
        self.status.grid(row=0, column=1, sticky="e")
        self.meta = ctk.CTkLabel(self, text="", anchor="w", justify="left", text_color=COLORS["muted"], font=font(11))
        self.meta.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.tier_host = ctk.CTkFrame(self, fg_color="transparent")
        self.tier_host.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.tier_host.grid_columnconfigure(0, weight=1)
        self.update_state(state, initial=True)

    @property
    def tiers(self) -> dict[tuple[str, str, str], MissionTierRow]:
        return self._tiers

    def update_state(self, state: MissionUiState, *, initial: bool = False) -> tuple[str, ...]:
        changed: list[str] = []
        old = self._state
        if old is None or old.title != state.title:
            self.title.configure(text=state.title)
            changed.append("title")
        if old is None or old.status != state.status:
            self.status.configure(text=state.status)
            changed.append("status")
        channel_status = "符合活动要求" if state.channel_matches else "不符合活动要求"
        meta_value = (
            f"掉宝类型：{state.drops_type}　活动时间：{state.start_date} 至 {state.end_date}\n"
            f"当前直播间：{state.channel}　直播间状态：{channel_status}"
            + ("，建议更换直播间" if state.needs_switch else "")
        )
        old_meta = None if old is None else (
            old.drops_type, old.start_date, old.end_date, old.channel, old.channel_matches, old.needs_switch
        )
        new_meta = (state.drops_type, state.start_date, state.end_date, state.channel, state.channel_matches, state.needs_switch)
        if old_meta != new_meta:
            self.meta.configure(text=meta_value)
            changed.append("meta")

        new_tiers = {tier.key: tier for tier in state.tiers}
        for key in tuple(self._tiers):
            if key not in new_tiers:
                self._tiers.pop(key).destroy()
        for row_index, tier in enumerate(state.tiers):
            row = self._tiers.get(tier.key)
            if row is None:
                row = MissionTierRow(self.tier_host, tier, visible=self._visible)
                row.grid(row=row_index, column=0, sticky="ew", pady=(0, 7))
                self._tiers[tier.key] = row
            else:
                row.update_state(tier, initial=initial)
        self._state = state
        return tuple(changed)

    def stop_animations(self) -> None:
        for row in self._tiers.values():
            row.stop_animation()


class InventoryRow(ctk.CTkFrame):
    FIELDS = ("uid", "name", "claim_status", "masked_code", "received_at", "expires_at")

    def __init__(self, master: Any, state: InventoryUiState, on_select: Callable[[tuple[str, str]], None]) -> None:
        super().__init__(master, corner_radius=8, fg_color=COLORS["row"], cursor="hand2")
        self.key = state.key
        self._state: InventoryUiState | None = None
        self._selected = False
        self._labels: dict[str, ctk.CTkLabel] = {}
        widths = (120, 240, 135, 165, 150, 150)
        for index, (name, width) in enumerate(zip(self.FIELDS, widths)):
            self.grid_columnconfigure(index, weight=2 if name == "name" else 1, minsize=width)
            label = ctk.CTkLabel(self, text="", anchor="w", font=font(12))
            label.grid(row=0, column=index, sticky="ew", padx=(10 if index == 0 else 4, 8), pady=9)
            label.bind("<Button-1>", lambda _e, key=state.key: on_select(key))
            self._labels[name] = label
        self.bind("<Button-1>", lambda _e: on_select(state.key))
        self.update_state(state)

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self.configure(fg_color=COLORS["row_selected"] if selected else COLORS["row"])

    def update_state(self, state: InventoryUiState) -> tuple[str, ...]:
        if self._state == state:
            return ()
        changed: list[str] = []
        for name in self.FIELDS:
            value = getattr(state, name)
            if self._state is None or getattr(self._state, name) != value:
                self._labels[name].configure(text=value)
                changed.append(name)
        self._state = state
        return tuple(changed)
