from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from soop_miner import gui, multi_miner, single_instance, windows_startup
from soop_miner.__main__ import parse_args
from soop_miner.config import (
    AppConfig,
    SETTINGS_VERSION,
    load_settings,
    save_settings,
)
from soop_miner.gui import SoopGui, should_start_hidden
from soop_miner.miner import SoopMiner


class FakeKey:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}
        self.fail_write = False

    def CreateKeyEx(self, *args):
        return FakeKey(self)

    def OpenKey(self, *args):
        if not self.values:
            raise FileNotFoundError
        return FakeKey(self)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        if self.fail_write:
            raise OSError("registry denied")
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


@pytest.fixture
def fake_registry(monkeypatch):
    registry = FakeWinreg()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", registry)
    return registry


def test_old_settings_missing_new_fields_receive_defaults(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"version": 1, "proxy_enabled": False, "mission_poll_interval": 120}),
        encoding="utf-8",
    )
    loaded = load_settings(path, sync_registry=False)
    assert loaded.settings_version == SETTINGS_VERSION
    assert loaded.mission_poll_interval == 120
    assert not loaded.auto_start_enabled
    assert not loaded.start_minimized_to_tray
    assert loaded.close_to_tray


def test_settings_save_reload_and_validation(tmp_path) -> None:
    path = tmp_path / "settings.json"
    expected = AppConfig(
        proxy_enabled=True,
        proxy_url="http://127.0.0.1:7897",
        start_minimized_to_tray=True,
        inventory_poll_interval=600,
    )
    save_settings(expected, path)
    assert load_settings(path, sync_registry=False) == expected

    with pytest.raises(ValueError):
        save_settings(replace(expected, proxy_url="socks5://127.0.0.1:1080"), path)
    with pytest.raises(ValueError):
        save_settings(replace(expected, mission_poll_interval=601), path)
    assert load_settings(path, sync_registry=False) == expected


def test_frozen_startup_command_is_quoted_and_uses_tray() -> None:
    command = windows_startup.get_auto_start_command(
        frozen=True,
        executable=r"C:\Program Files\CloudLight\CloudLight_SOOP_Drops_Miner.exe",
    )
    assert command == '"C:\\Program Files\\CloudLight\\CloudLight_SOOP_Drops_Miner.exe" --tray'


def test_source_startup_command_is_absolute_and_handles_spaces(tmp_path) -> None:
    runtime = tmp_path / "Python Runtime"
    runtime.mkdir()
    python = runtime / "python.exe"
    pythonw = runtime / "pythonw.exe"
    entry = tmp_path / "cloudlight soop drops miner" / "entry.py"
    entry.parent.mkdir()
    python.write_text("", encoding="utf-8")
    pythonw.write_text("", encoding="utf-8")
    entry.write_text("", encoding="utf-8")
    command = windows_startup.get_auto_start_command(
        frozen=False,
        executable=python,
        entry_path=entry,
    )
    assert command == f'"{pythonw.resolve()}" "{entry.resolve()}" --tray'
    assert str(Path.cwd()) not in command or Path.cwd() in entry.parents


def test_enable_and_disable_only_own_registry_value(fake_registry) -> None:
    fake_registry.values["Other App"] = '"C:\\other.exe"'
    windows_startup.enable_auto_start()
    assert windows_startup.STARTUP_VALUE_NAME in fake_registry.values
    assert fake_registry.values[windows_startup.STARTUP_VALUE_NAME].endswith(" --tray")
    windows_startup.disable_auto_start()
    assert windows_startup.STARTUP_VALUE_NAME not in fake_registry.values
    assert fake_registry.values["Other App"] == '"C:\\other.exe"'


def test_registry_write_failure_does_not_persist_enabled(tmp_path, fake_registry) -> None:
    path = tmp_path / "settings.json"
    initial = save_settings(AppConfig(auto_start_enabled=False), path)
    fake_registry.fail_write = True
    with pytest.raises(OSError):
        windows_startup.apply_auto_start_setting(initial, True, settings_path=path)
    assert not load_settings(path, sync_registry=False).auto_start_enabled


def test_tray_and_minimized_arguments_parse() -> None:
    assert parse_args(["--tray"]).tray
    assert parse_args(["--minimized"]).minimized
    args = parse_args(["--cli", "--userid", "u", "--password", "p", "-v"])
    assert args.cli and args.userid == "u" and args.verbose == 1


def test_first_disclaimer_overrides_hidden_start() -> None:
    settings = AppConfig(start_minimized_to_tray=True)
    assert not should_start_hidden(True, settings, disclaimer_accepted=False)
    assert should_start_hidden(True, settings, disclaimer_accepted=True)
    assert should_start_hidden(False, settings, disclaimer_accepted=True)


def test_repeated_gui_start_does_not_construct_second_gui(monkeypatch) -> None:
    monkeypatch.setattr(gui, "load_settings", lambda: AppConfig())
    monkeypatch.setattr(single_instance, "ensure_single_instance_or_exit", lambda: False)

    class ForbiddenGui:
        def __init__(self, *args, **kwargs):
            raise AssertionError("second GUI must not be constructed")

    monkeypatch.setattr(gui, "SoopGui", ForbiddenGui)
    gui.run_gui(start_hidden_to_tray=True)


def test_single_instance_duplicate_requests_restore(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(single_instance.sys, "platform", "win32")
    monkeypatch.setattr(single_instance, "_try_acquire_mutex", lambda: False)
    monkeypatch.setattr(single_instance, "_activate_existing_window", lambda: calls.append("show"))
    assert not single_instance.ensure_single_instance_or_exit()
    assert calls == ["show"]


def test_close_to_tray_does_not_stop_manager() -> None:
    app = object.__new__(SoopGui)
    app._app_config = AppConfig(close_to_tray=True)
    calls: list[str] = []
    app._minimize_to_tray = lambda: calls.append("hide")
    app._quit_app = lambda: calls.append("quit")
    app._on_close()
    assert calls == ["hide"]


def test_tray_exit_stops_and_finalizes_cleanup(monkeypatch) -> None:
    calls: list[str] = []

    class DummyButton:
        def configure(self, **kwargs):
            pass

    class DummyManager:
        def stop_all(self):
            calls.append("stop")

    class DummyTray:
        def stop(self):
            calls.append("tray")

    class DummyRoot:
        def after_cancel(self, timer):
            pass

        def destroy(self):
            calls.append("destroy")

    app = object.__new__(SoopGui)
    app._quitting = False
    app._stopping = False
    app._manager = DummyManager()
    app._loop = None
    app._thread = None
    app._start_btn = DummyButton()
    app._stop_btn = DummyButton()
    app._restore_poll_id = None
    app._channel_refresh_timer = None
    app._tray = DummyTray()
    app.root = DummyRoot()
    monkeypatch.setattr(gui, "release_single_instance", lambda: calls.append("mutex"))
    app._quit_app()
    assert calls == ["stop", "tray", "mutex", "destroy"]


def test_new_manager_accounts_receive_settings_snapshot(monkeypatch) -> None:
    captured: list[AppConfig] = []

    class FakeMiner:
        def __init__(self, cookies, *, on_state, channel_config, app_config):
            captured.append(app_config)
            self._running = True
            self._gate = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def run(self):
            await self._gate.wait()

        def stop(self):
            self._running = False
            self._gate.set()

        def get_state(self):
            return SimpleNamespace(running=self._running)

    async def scenario() -> None:
        monkeypatch.setattr(multi_miner, "SoopMiner", FakeMiner)
        settings = AppConfig(proxy_enabled=True, proxy_url="http://127.0.0.1:7897")
        manager = multi_miner.MultiMinerManager(app_config=settings)
        await manager.start_account({"BbsTicket": "A"})
        settings.proxy_url = "http://127.0.0.1:9999"
        assert captured[0].proxy_url == "http://127.0.0.1:7897"
        await manager.shutdown()

    asyncio.run(scenario())


def test_saving_proxy_settings_does_not_replace_running_session() -> None:
    old = AppConfig(proxy_enabled=False)
    miner = SoopMiner({"BbsTicket": "A"}, app_config=old)
    session_marker = object()
    miner._session = session_marker
    manager = multi_miner.MultiMinerManager(app_config=old)
    manager._miners["A"] = miner

    app = object.__new__(SoopGui)
    app._app_config = old
    app._manager = manager
    app._channel_refresh_timer = None
    app._states = {}
    app._tray = None
    app._append_log = lambda text: None
    app._on_settings_saved(
        AppConfig(proxy_enabled=True, proxy_url="http://127.0.0.1:7897")
    )
    assert miner._session is session_marker
    assert not miner._app_config.proxy_enabled
