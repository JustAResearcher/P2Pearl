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
    dp = sub.add_parser("daemon", help="run a live pool node (serves a stratum port; needs pearld + pearl_mining)")
    dp.add_argument("--rpc-url", help="pearld JSON-RPC URL (default http://127.0.0.1:44107)")
    dp.add_argument("--rpc-user", help="pearld RPC user (or env P2PEARL_RPC_USER; default 'user')")
    dp.add_argument("--rpc-pass", help="pearld RPC password (or env P2PEARL_RPC_PASS; default 'pass')")
    dp.add_argument("--stratum-host", default="0.0.0.0", help="stratum bind host (default 0.0.0.0)")
    dp.add_argument("--stratum-port", type=int, default=3360, help="stratum port (default 3360)")
    dp.add_argument("--p2p-host", default="0.0.0.0", help="P2P bind host (default 0.0.0.0)")
    dp.add_argument("--p2p-port", type=int, default=37900, help="P2P port (default 37900)")
    dp.add_argument("--peer", action="append", metavar="HOST:PORT",
                    help="connect to a peer node for share gossip (repeatable)")
    dp.add_argument("--share-target", help="sidechain share target as int/hex (default: built-in placeholder)")

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
            import os

            from . import config as cfgmod
            from . import daemon
            node_cfg = cfgmod.NodeRPCConfig(
                url=args.rpc_url or cfgmod.PARENT_RPC_DEFAULT_URL,
                user=args.rpc_user or os.environ.get("P2PEARL_RPC_USER", "user"),
                password=args.rpc_pass or os.environ.get("P2PEARL_RPC_PASS", "pass"),
            )
            peers = []
            for spec in (args.peer or []):
                host, sep, port = spec.rpartition(":")
                if sep and port:
                    peers.append((host, int(port)))
            dcfg = cfgmod.DaemonConfig(
                node=node_cfg, stratum_host=args.stratum_host, stratum_port=args.stratum_port,
                p2p_host=args.p2p_host, p2p_port=args.p2p_port, peers=tuple(peers))
            st = int(args.share_target, 0) if args.share_target else None
            return daemon.main(cfg=dcfg, share_target=st)
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
