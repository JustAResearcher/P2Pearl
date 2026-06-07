"""P2Pearl command-line entry point.

Subcommands:
  demo    run the local end-to-end demo (no node/GPU/native build needed)
  daemon  wire and run a live pool node (needs pearld + pearl_mining + bitcoinutils)
"""

from __future__ import annotations

import argparse
import asyncio

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p2pearl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"p2pearl {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", help="run the local end-to-end demo")
    sub.add_parser("daemon", help="run a live pool node")

    args = parser.parse_args(argv)
    if args.command == "demo":
        from . import demo
        asyncio.run(demo.main())
        return 0
    if args.command == "daemon":
        from . import daemon
        return daemon.main()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
