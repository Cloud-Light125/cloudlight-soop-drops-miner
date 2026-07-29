from __future__ import annotations

from .config import AppConfig, load_settings
from .modern_gui import ModernSoopGui
from .single_instance import release_single_instance


def should_start_hidden(
    explicit_hidden: bool,
    settings: AppConfig,
    *,
    disclaimer_accepted: bool,
) -> bool:
    """First-run disclaimer always wins over silent/tray startup."""
    if not disclaimer_accepted:
        return False
    return bool(explicit_hidden or settings.start_minimized_to_tray)


# Kept as the public name used by entry points and integrations.
SoopGui = ModernSoopGui


def run_gui(*, start_hidden_to_tray: bool = False) -> None:
    from .single_instance import ensure_single_instance_or_exit

    settings = load_settings()
    if not ensure_single_instance_or_exit():
        return
    SoopGui(settings=settings, start_hidden_to_tray=start_hidden_to_tray).run()
