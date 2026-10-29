"""SSH local-forward tunnels to an engine that only listens on a LAN address.

`ssh -f` authenticates in the foreground (so a password/passphrase prompt still works in the
terminal) and then detaches into its own session, which means Ctrl-C used to cancel a model turn
cannot kill the tunnel. Host keys are always verified by OpenSSH (we never disable that).
"""
from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import state_dir


class TunnelError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        return True
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Tunnel:
    def __init__(self, name: str, target: str, engine_host: str, engine_port: int, ssh_args: list[str] | None = None):
        if shutil.which("ssh") is None:
            raise TunnelError("OpenSSH client ('ssh') is not installed")
        if target.startswith("-") or any(c.isspace() for c in target):
            raise TunnelError(f"Invalid SSH target '{target}'")
        self.name, self.target = name, target
        self.engine_host, self.engine_port = engine_host, int(engine_port)
        self.ssh_args = list(ssh_args or [])
        self.dir = state_dir() / "tunnels"
        self.ctl = self.dir / f"{name}.{os.getpid()}.sock"
        self.port: int | None = None
        self._registered = False

    @property
    def url(self) -> str:
        if self.port is None:
            raise TunnelError("tunnel not open")
        return f"http://127.0.0.1:{self.port}"

    def _ssh(self, *extra: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["ssh", "-S", str(self.ctl), *extra, self.target], capture_output=True, text=True, timeout=15)

    def alive(self) -> bool:
        if self.port is None or not self.ctl.exists():
            return False
        try:
            return self._ssh("-O", "check").returncode == 0 and _port_open(self.port)
        except (subprocess.TimeoutExpired, OSError):
            return False

    def open(self, *, interactive: bool | None = None) -> str:
        """Start (or restart) the tunnel; returns its local URL. Idempotent when healthy."""
        if self.alive():
            return self.url
        self.close()
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        prune_stale(self.dir)
        interactive = sys.stdin.isatty() if interactive is None else interactive
        last = ""
        for _ in range(3):
            port = _free_port()
            cmd = ["ssh", "-f", "-N", "-M", "-S", str(self.ctl),
                   "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                   "-o", "ControlPersist=no", "-o", f"BatchMode={'no' if interactive else 'yes'}",
                   "-L", f"127.0.0.1:{port}:{self.engine_host}:{self.engine_port}", *self.ssh_args, self.target]
            if interactive:
                res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, timeout=180)  # may prompt on the tty
            else:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            last = (res.stderr or "").strip()
            if res.returncode == 0 and self.ctl.exists():
                self.port = port
                for _ in range(40):
                    if _port_open(port):
                        break
                    time.sleep(0.1)
                else:
                    self.close()
                    raise TunnelError("SSH tunnel started but its local port never opened")
                if not self._registered:
                    atexit.register(self.close)
                    self._registered = True
                return self.url
            if "Address already in use" not in last and "cannot listen" not in last:
                break
        hint = ""
        if "Permission denied" in last:
            hint = (" Key authentication failed. Check 'ssh " + self.target + "' works from this terminal "
                    "(sarness never stores SSH passwords).")
        elif "Host key verification failed" in last or "REMOTE HOST IDENTIFICATION" in last:
            hint = " The host key is unknown or changed; verify it with a plain 'ssh' first. Sarness will not bypass this check."
        raise TunnelError(f"Could not open the SSH tunnel to {self.target}: {(last or 'unknown error').rstrip('.')}.{hint}")

    def close(self) -> None:
        if self.ctl.exists():
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                self._ssh("-O", "exit")
        with contextlib.suppress(OSError):
            self.ctl.unlink()
        self.port = None


def prune_stale(directory: Path) -> int:
    """Close tunnels whose owning sarness process has died (crash / kill -9)."""
    n = 0
    for sock in directory.glob("*.sock"):
        try:
            pid = int(sock.name.rsplit(".", 2)[-2])
        except (ValueError, IndexError):
            continue
        if not _pid_alive(pid):
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(["ssh", "-S", str(sock), "-O", "exit", "placeholder"], capture_output=True, timeout=10)
            with contextlib.suppress(OSError):
                sock.unlink()
            n += 1
    return n
