"""SOOP Drops Miner 启动入口（开发 / PyInstaller 共用）。"""
from __future__ import annotations

import sys


def _run() -> None:
    if len(sys.argv) == 1:
        sys.argv.append("--gui")
    from soop_miner.__main__ import main

    main()


if __name__ == "__main__":
    _run()
