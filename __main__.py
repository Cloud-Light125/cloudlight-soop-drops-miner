from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from .constants import WINDOW_TITLE
from .gui import run_gui
from .miner import run_miner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CloudLight SOOP Live Drops 挂机工具")
    parser.add_argument("--gui", action="store_true", help="启动图形界面（默认）")
    parser.add_argument("--cli", action="store_true", help="使用命令行模式")
    parser.add_argument("--tray", action="store_true", help="静默启动到系统托盘")
    parser.add_argument("--minimized", action="store_true", help="等同于 --tray")
    parser.add_argument("--userid", help="SOOP 账号")
    parser.add_argument("--password", help="SOOP 密码")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="详细日志")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    start_hidden_to_tray = bool(args.tray or args.minimized)
    use_gui = args.gui or start_hidden_to_tray or not args.cli

    if start_hidden_to_tray or (use_gui and not args.userid and not args.password):
        run_gui(start_hidden_to_tray=start_hidden_to_tray)
        return

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.info("%s CLI 启动", WINDOW_TITLE)
    try:
        asyncio.run(run_miner(userid=args.userid, password=args.password))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
