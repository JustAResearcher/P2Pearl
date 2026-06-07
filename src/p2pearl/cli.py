"""P2Pearl command-line entry point.

Subcommands:
  demo    run the local end-to-end demo (no node/GPU/native build needed)
  daemon  wire and run a live pool node (needs pearld + pearl_mining + bitcoinutils)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__


def _double_clicked() -> bool:
    """True if this exe was launched by double-click (its console holds only this
    process), so we should pause before exit instead of letting the window vanish.
    Windows-only; any failure falls back to False (normal CLI behaviour)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        buf = (ctypes.c_uint * 16)()
        return ctypes.windll.kernel32.GetConsoleProcessList(buf, 16) == 1
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p2pearl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"p2pearl {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", help="run the local end-to-end demo")
    sub.add_parser("daemon", help="run a live pool node")

    args = parser.parse_args(argv)
    standalone = _double_clicked()
    try:
        # A double-clicked exe arrives with no subcommand; run the showcase demo.
        if args.command == "demo" or (args.command is None and standalone):
            if args.command is None:
                print("No subcommand given - running 'demo'. (p2pearl --help for options)\n")
            from . import demo
            asyncio.run(demo.main())
            return 0
        if args.command == "daemon":
            from . import daemon
            return daemon.main()
        parser.print_help()
        return 0
    finally:
        if standalone:
            try:
                input("\nPress Enter to exit . . . ")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
