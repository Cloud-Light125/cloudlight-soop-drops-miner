"""CloudLight SOOP Drops Miner flat-source entry point (source and PyInstaller)."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

_RUNTIME_PACKAGE = "_cloudlight_soop_miner_app"


def _load_main():
    root = Path(__file__).resolve().parent
    if _RUNTIME_PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _RUNTIME_PACKAGE,
            root / "__init__.py",
            submodule_search_locations=[str(root)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法初始化平铺源码包")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_RUNTIME_PACKAGE] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{_RUNTIME_PACKAGE}.__main__").main


def _run() -> None:
    _load_main()()


if __name__ == "__main__":
    _run()
