"""Tests for the GUI's pure helpers (no tkinter / no display needed)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.gui import (  # noqa: E402
    DEFAULTS,
    build_daemon_args,
    load_settings,
    miner_command,
    save_settings,
    self_command,
)


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "gui.json"
    s = dict(DEFAULTS, rpc_url="http://127.0.0.1:44109", peers="1.2.3.4:37900\n5.6.7.8:37900")
    save_settings(s, path)
    assert load_settings(path) == s


def test_load_settings_defaults_when_missing(tmp_path):
    assert load_settings(tmp_path / "nope.json") == DEFAULTS


def test_load_settings_ignores_unknown_keys_and_bad_json(tmp_path):
    path = tmp_path / "gui.json"
    path.write_text('{"rpc_url": "http://x:1", "evil": "ignored"}', encoding="utf-8")
    s = load_settings(path)
    assert s["rpc_url"] == "http://x:1" and "evil" not in s
    path.write_text("{not json", encoding="utf-8")
    assert load_settings(path) == DEFAULTS


def test_build_daemon_args_basic():
    args = build_daemon_args(dict(DEFAULTS))
    assert args[0] == "daemon"
    assert args[args.index("--rpc-url") + 1] == "http://127.0.0.1:44107"
    assert args[args.index("--stratum-port") + 1] == "3360"
    assert args[args.index("--p2p-port") + 1] == "37900"
    assert "--peer" not in args and "--share-target" not in args


def test_build_daemon_args_peers_and_extras():
    s = dict(DEFAULTS, peers="  1.2.3.4:37900\n\n5.6.7.8:37900 ",
             share_target="0xff", pause_cmd="pkill -STOP -x xmrig")
    args = build_daemon_args(s)
    peer_vals = [args[i + 1] for i, a in enumerate(args) if a == "--peer"]
    assert peer_vals == ["1.2.3.4:37900", "5.6.7.8:37900"]
    assert args[args.index("--share-target") + 1] == "0xff"
    assert args[args.index("--pause-cmd") + 1] == "pkill -STOP -x xmrig"
    assert "--resume-cmd" not in args


def test_miner_command_uses_port_and_wallet():
    s = dict(DEFAULTS, stratum_port="4444", wallet="prl1pabc")
    cmd = miner_command(s)
    assert ":4444" in cmd and "--wallet prl1pabc" in cmd and "pearlhash" in cmd
    assert "<your-prl1p" in miner_command(dict(DEFAULTS))   # placeholder when unset


def test_self_command_unfrozen():
    cmd = self_command()
    assert cmd[0] == sys.executable and cmd[-2:] == ["-m", "p2pearl"]
