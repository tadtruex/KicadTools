"""The SSH tunnel to the parts database server (``DB_PLAN.md`` step 4,
decision 2).

The host refuses MariaDB logins from outside, and accepts them from the
server itself, so the Cockpit reaches the database through an SSH local
forward: ``ssh -N -L 127.0.0.1:<port>:127.0.0.1:3306 user@host``, on a free
local port, started when the database is opened and stopped on exit.

- With an SSH key on the server, ssh runs with ``BatchMode=yes`` and never
  prompts.
- With an *askpass* program (the Cockpit's password dialog,
  :mod:`kicadtools.gui.askpass`), ssh asks for the SSH password through it
  (``SSH_ASKPASS``, ``SSH_ASKPASS_REQUIRE=force``), so a workstation with no
  key on the server needs no setup.
- With a configured *local_port* that already answers, that tunnel (one the
  user runs) is used and left running.

A new host key is accepted on first use (``StrictHostKeyChecking=accept-new``)
and checked on every later one. ``ServerAlive`` probes end a tunnel whose
connection has gone, so a dead tunnel shows as a failed query rather than a
hang.

A failure to start is a :class:`TunnelError` holding ssh's own message
(``Permission denied (publickey,password)``, ``Could not resolve hostname``);
the Cockpit treats it as offline.
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence

# Lines ssh prints that are not about the failure (OpenSSH 10 warns about
# the server's key exchange on every connection).
_NOISE = ("post-quantum", "pq.html", "store now, decrypt later", "may need to be upgraded")


class TunnelError(RuntimeError):
    """The tunnel did not start; the message is ssh's, without its noise."""


def free_port() -> int:
    """A local TCP port no process listens on now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_answers(port: int, timeout: float = 0.5) -> bool:
    """Whether something accepts connections on 127.0.0.1:*port*."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _clean(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not any(n in ln for n in _NOISE)]
    return "; ".join(lines)


def askpass_script(module: str = "kicadtools.gui.askpass",
                   python: Optional[str] = None) -> Path:
    """A small executable that runs *module* with this Python, for
    ``SSH_ASKPASS`` (which names a program, not a command line). Written to
    a temporary directory; the caller may delete it after the tunnel opens."""
    import sys

    python = python or sys.executable
    folder = Path(tempfile.mkdtemp(prefix="kicadtools-askpass-"))
    script = folder / "askpass"
    script.write_text(f'#!/bin/sh\nexec "{python}" -m {module} "$@"\n')
    script.chmod(0o700)
    return script


class Tunnel:
    """An SSH local forward to MariaDB on *host*.

    *ssh* is the ssh program (a stand-in in tests). *askpass* is a program
    for ``SSH_ASKPASS``; without one, ssh runs in batch mode and fails rather
    than prompt. *timeout* bounds the wait for the local port to answer,
    including the time a user takes at the password dialog."""

    def __init__(self, host: str, user: str, remote_port: int = 3306, *,
                 local_port: Optional[int] = None, askpass: Optional[Path] = None,
                 ssh: Sequence[str] = ("ssh",), timeout: float = 120.0) -> None:
        self.host = host
        self.user = user
        self.remote_port = int(remote_port)
        self.local_port = local_port
        self.askpass = askpass
        self.ssh = list(ssh)
        self.timeout = timeout
        self.port: Optional[int] = None
        self.owned = False          # this object started the ssh process
        self._proc: Optional[subprocess.Popen] = None

    def command(self, port: int) -> List[str]:
        """The ssh command line for local *port*."""
        opts = ["-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=15",
                "-o", "StrictHostKeyChecking=accept-new"]
        if self.askpass is None:
            opts += ["-o", "BatchMode=yes"]
        return (self.ssh + ["-N"] + opts
                + ["-L", f"127.0.0.1:{port}:127.0.0.1:{self.remote_port}",
                   f"{self.user}@{self.host}"])

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.askpass is not None:
            env["SSH_ASKPASS"] = str(self.askpass)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")   # ssh reads SSH_ASKPASS only with one
        return env

    def open(self) -> int:
        """Start the tunnel (or adopt the user's) and return the local port.
        Raises :class:`TunnelError` when ssh exits or the port does not
        answer within *timeout*."""
        if self.port is not None:
            return self.port
        if self.local_port is not None and port_answers(self.local_port):
            self.port = self.local_port
            return self.port
        port = self.local_port or free_port()
        try:
            self._proc = subprocess.Popen(
                self.command(port), env=self._env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
        except OSError as exc:
            raise TunnelError(f"could not run ssh: {exc}") from None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                err = _clean(self._proc.stderr.read() if self._proc.stderr else "")
                self._proc = None
                raise TunnelError(err or f"ssh exited with status {code}")
            if port_answers(port):
                self.port, self.owned = port, True
                return port
            time.sleep(0.1)
        self.close()
        raise TunnelError(f"no answer on local port {port} after {self.timeout:g} s")

    @property
    def alive(self) -> bool:
        """The tunnel is up: the ssh process runs (or the adopted port
        answers)."""
        if self.port is None:
            return False
        if not self.owned:
            return port_answers(self.port)
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        """Stop the ssh process this object started; a tunnel the user runs
        is left running."""
        proc, self._proc = self._proc, None
        self.port, self.owned = None, False
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def __enter__(self) -> "Tunnel":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
