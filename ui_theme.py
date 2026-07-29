from __future__ import annotations

import customtkinter as ctk

FONT_FAMILY = "Microsoft YaHei UI"
FONT_CJK = "Microsoft YaHei UI"
WINDOW_SIZE = "1320x900"
MIN_WINDOW_SIZE = (1180, 760)
CARD_RADIUS = 12
CONTROL_RADIUS = 9
CARD_PAD = 16
SECTION_GAP = 12

COLORS = {
    "accent": ("#2563EB", "#4F8CFF"),
    "accent_hover": ("#1D4ED8", "#3978E6"),
    "success": ("#16825D", "#46B88A"),
    "warning": ("#A16207", "#E1A93B"),
    "danger": ("#B42318", "#E06A62"),
    "muted": ("#667085", "#A9B1BD"),
    "surface": ("#FFFFFF", "#20242B"),
    "surface_alt": ("#F4F6F8", "#181B20"),
    "row": ("#F7F8FA", "#292E36"),
    "row_selected": ("#E8F0FE", "#253B5C"),
    "border": ("#E1E5EA", "#363C46"),
}


def configure_appearance(mode: str) -> None:
    mapping = {"system": "System", "light": "Light", "dark": "Dark"}
    ctk.set_appearance_mode(mapping.get(mode.lower(), "System"))
    ctk.set_default_color_theme("blue")


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)
