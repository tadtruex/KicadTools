"""Tests for the SSH tunnel to the parts database (dbtunnel.py, DB_PLAN.md
step 4), with a stand-in ssh: a script that listens on the forwarded port,
refuses the login, or hangs."""
from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest

from kicadtools.dbtunnel import Tunnel, TunnelError, askpass_script, free_port, port_answers

FAKE_SSH = r'''
import json, os, socket, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "w") as fh:
    json.dump({"args": args, "askpass": os.environ.get("SSH_ASKPASS"),
               "require": os.environ.get("SSH_ASKPASS_REQUIRE")}, fh)
mode = os.environ["FAKE_SSH_MODE"]
if mode == "deny":
    sys.stderr.write("** WARNING: connection is not using a post-quantum key exchange\n")
    sys.stderr.write("tadtruex@host: Permission denied (publickey,password).\n")
    sys.exit(255)
if mode == "listen":
    port = int(args[args.index("-L") + 1].split(":")[1])
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.listen(5)
while True:
    time.sleep(0.2)
'''


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH)
    log = tmp_path / "ssh_log.json"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))

    def make(mode, **kw):
        monkeypatch.setenv("FAKE_SSH_MODE", mode)
        return Tunnel("db.example.edu", "tadtruex", ssh=[sys.executable, str(script)], **kw)

    make.log = lambda: json.loads(log.read_text())
    return make


def _wait_closed(port, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and port_answers(port):
        time.sleep(0.05)
    return not port_answers(port)


def test_open_forwards_a_free_port_and_close_stops_ssh(fake_ssh):
    tunnel = fake_ssh("listen")
    port = tunnel.open()
    assert tunnel.owned and tunnel.alive and port_answers(port)
    args = fake_ssh.log()["args"]
    assert args[0] == "-N"
    assert args[args.index("-L") + 1] == f"127.0.0.1:{port}:127.0.0.1:3306"
    assert args[-1] == "tadtruex@db.example.edu"
    assert "BatchMode=yes" in args and "ExitOnForwardFailure=yes" in args
    tunnel.close()
    assert not tunnel.alive and _wait_closed(port)


def test_refused_login_raises_sshs_message_without_noise(fake_ssh):
    with pytest.raises(TunnelError) as err:
        fake_ssh("deny").open()
    assert str(err.value) == "tadtruex@host: Permission denied (publickey,password)."


def test_no_answer_times_out_and_stops_ssh(fake_ssh):
    tunnel = fake_ssh("hang", timeout=1.0)
    with pytest.raises(TunnelError, match="no answer on local port"):
        tunnel.open()
    assert tunnel._proc is None


def test_askpass_replaces_batch_mode(fake_ssh, tmp_path):
    askpass = tmp_path / "askpass"
    tunnel = fake_ssh("listen", askpass=askpass)
    with tunnel:
        log = fake_ssh.log()
    assert "BatchMode=yes" not in log["args"]
    assert (log["askpass"], log["require"]) == (str(askpass), "force")


def test_a_running_tunnel_on_the_configured_port_is_used_and_left_running(fake_ssh):
    port = free_port()
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(5)
    try:
        tunnel = fake_ssh("deny", local_port=port)   # ssh would fail if it ran
        assert tunnel.open() == port and not tunnel.owned and tunnel.alive
        tunnel.close()
        assert port_answers(port)                     # the user's tunnel still runs
    finally:
        server.close()


def test_configured_port_with_nothing_on_it_gets_our_tunnel(fake_ssh):
    port = free_port()
    tunnel = fake_ssh("listen", local_port=port)
    assert tunnel.open() == port and tunnel.owned
    tunnel.close()


def test_missing_ssh_program():
    with pytest.raises(TunnelError, match="could not run ssh"):
        Tunnel("h", "u", ssh=["/nonexistent/ssh"]).open()


def test_askpass_script_runs_the_module(tmp_path):
    script = askpass_script(module="kicadtools.gui.askpass", python="/usr/bin/python3")
    text = script.read_text()
    assert text.startswith("#!/bin/sh") and '"/usr/bin/python3" -m kicadtools.gui.askpass "$@"' in text
    assert script.stat().st_mode & 0o700 == 0o700
