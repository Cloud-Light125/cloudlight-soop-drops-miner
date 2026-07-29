from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .constants import DATA_DIR

logger = logging.getLogger("CloudLightSoopMiner.config")

SETTINGS_VERSION = 3
SETTINGS_PATH = DATA_DIR / "settings.json"
LEGACY_CONFIG_PATH = DATA_DIR / "config.json"


def validate_proxy_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("代理地址只允许 http:// 或 https://")
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    return value


@dataclass(slots=True)
class AppConfig:
    settings_version: int = SETTINGS_VERSION
    auto_claim_enabled: bool = False
    low_bandwidth_mode: bool = True
    proxy_enabled: bool = False
    proxy_url: str = ""
    proxy_fallback_direct: bool = False
    auto_start_enabled: bool = False
    start_minimized_to_tray: bool = False
    close_to_tray: bool = True
    appearance_mode: str = "system"
    mission_poll_interval: int = 90
    inventory_poll_interval: int = 300
    channel_refresh_interval: int = 300

    @property
    def version(self) -> int:
        """Compatibility alias for the first settings format."""
        return self.settings_version

    @property
    def effective_proxy_url(self) -> str | None:
        if not self.proxy_enabled:
            return None
        return validate_proxy_url(self.proxy_url)

    @property
    def effective_mission_poll_interval(self) -> int:
        if self.low_bandwidth_mode:
            return max(60, min(self.mission_poll_interval, 120))
        return self.mission_poll_interval

    @property
    def effective_inventory_poll_interval(self) -> int:
        return max(300, self.inventory_poll_interval) if self.low_bandwidth_mode else self.inventory_poll_interval

    @property
    def effective_channel_refresh_interval(self) -> int:
        return max(300, self.channel_refresh_interval) if self.low_bandwidth_mode else self.channel_refresh_interval

    def validated(self) -> "AppConfig":
        bool_fields = (
            "auto_claim_enabled",
            "low_bandwidth_mode",
            "proxy_enabled",
            "proxy_fallback_direct",
            "auto_start_enabled",
            "start_minimized_to_tray",
            "close_to_tray",
        )
        for name in bool_fields:
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须是布尔值")
        if self.settings_version != SETTINGS_VERSION:
            raise ValueError(f"设置版本必须为 {SETTINGS_VERSION}")
        if self.appearance_mode not in {"system", "light", "dark"}:
            raise ValueError("主题必须是 system、light 或 dark")
        if not isinstance(self.proxy_url, str):
            raise ValueError("代理地址必须是字符串")
        proxy_url = self.proxy_url.strip()
        if self.proxy_enabled or proxy_url:
            proxy_url = validate_proxy_url(proxy_url)
        ranges = {
            "mission_poll_interval": (30, 600),
            "inventory_poll_interval": (60, 1800),
            "channel_refresh_interval": (60, 1800),
        }
        values: dict[str, int] = {}
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须在 {minimum}～{maximum} 秒之间")
            values[name] = value
        return replace(self, proxy_url=proxy_url, **values)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        defaults = cls()
        known = {field.name for field in fields(cls)}
        values = {name: raw[name] for name in known if name in raw}
        if "settings_version" not in values and "version" in raw:
            values["settings_version"] = raw["version"]
        values["settings_version"] = SETTINGS_VERSION

        for name in (
            "auto_claim_enabled",
            "low_bandwidth_mode",
            "proxy_enabled",
            "proxy_fallback_direct",
            "auto_start_enabled",
            "start_minimized_to_tray",
            "close_to_tray",
        ):
            if name in values and not isinstance(values[name], bool):
                values[name] = getattr(defaults, name)

        bounds = {
            "mission_poll_interval": (30, 600),
            "inventory_poll_interval": (60, 1800),
            "channel_refresh_interval": (60, 1800),
        }
        for name, (minimum, maximum) in bounds.items():
            value = values.get(name, getattr(defaults, name))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                values[name] = getattr(defaults, name)
            else:
                value = int(value)
                values[name] = value if minimum <= value <= maximum else getattr(defaults, name)

        if not isinstance(values.get("proxy_url", ""), str):
            values["proxy_url"] = ""
        if values.get("proxy_url"):
            try:
                values["proxy_url"] = validate_proxy_url(values.get("proxy_url", ""))
            except ValueError as exc:
                logger.warning("代理配置无效，已禁用代理: %s", exc)
                values["proxy_enabled"] = False
                values["proxy_url"] = ""
        if values.get("appearance_mode") not in {"system", "light", "dark"}:
            values["appearance_mode"] = defaults.appearance_mode
        return cls(**values)


def _read_settings(path: Path) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置根节点不是对象")
    return AppConfig.from_dict(raw)


def _atomic_write(settings: AppConfig, path: Path) -> None:
    normalized = settings.validated()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(asdict(normalized), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_settings(path: Path = SETTINGS_PATH, *, sync_registry: bool = True) -> AppConfig:
    source = path
    if not source.is_file() and path == SETTINGS_PATH and LEGACY_CONFIG_PATH.is_file():
        source = LEGACY_CONFIG_PATH
    if source.is_file():
        try:
            settings = _read_settings(source)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("读取设置失败，使用默认值: %s", exc)
            settings = AppConfig()
    else:
        settings = AppConfig()

    registry_changed = False
    if sync_registry:
        try:
            from .windows_startup import reconcile_auto_start_state

            reconciled = reconcile_auto_start_state(settings)
            registry_changed = reconciled != settings
            settings = reconciled
        except Exception as exc:
            logger.warning("同步开机启动状态失败，保留配置值: %s", exc)

    if path == SETTINGS_PATH and (not path.is_file() or source != path or registry_changed):
        try:
            _atomic_write(settings, path)
        except OSError as exc:
            logger.warning("无法写入迁移后的设置: %s", exc)
    return settings


def save_settings(settings: AppConfig, path: Path = SETTINGS_PATH) -> AppConfig:
    normalized = settings.validated()
    _atomic_write(normalized, path)
    return normalized


def update_settings(
    settings: AppConfig | None = None,
    path: Path = SETTINGS_PATH,
    **changes: Any,
) -> AppConfig:
    current = settings or load_settings(path)
    updated = replace(current, **changes)
    return save_settings(updated, path)


def reset_settings() -> AppConfig:
    """Return an unsaved default draft."""
    return AppConfig()


def snapshot_settings(settings: AppConfig) -> AppConfig:
    return replace(settings)


# Backward-compatible names used by the first-round core code and tests.
CONFIG_VERSION = SETTINGS_VERSION
CONFIG_PATH = SETTINGS_PATH
load_config = load_settings
save_config = save_settings
