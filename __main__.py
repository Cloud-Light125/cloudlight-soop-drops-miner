from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .gui import run_gui
from .miner import run_miner


def main() -> None:
    parser = argparse.ArgumentParser(description="SOOP Live Drops 挂机工具")
    parser.add_argument("--gui", action="store_true", help="启动图形界面（默认）")
    parser.add_argument("--cli", action="store_true", help="使用命令行模式")
    parser.add_argument("--userid", help="SOOP 账号")
    parser.add_argument("--password", help="SOOP 密码")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="详细日志")
    args = parser.parse_args()

    use_gui = args.gui or not args.cli

    if use_gui and not args.userid and not args.password:
        run_gui()
        return

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(run_miner(userid=args.userid, password=args.password))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
