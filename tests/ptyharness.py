"""Drive a real program through a real pseudo-terminal, the way a keyboard does.

The child gets its own session and the pty as its controlling terminal. Writing byte 0x03 to the master side goes
through the kernel's line discipline exactly like a typed Ctrl+C: when the program has ISIG on, every process in the
terminal's foreground process group gets SIGINT; when it is in raw mode, the program reads 0x03 as an ordinary key.
Calling a cancellation function directly would test neither path, which is why these helpers exist.

Processes "owned" by a session are found by session id (the pty child is its leader): nothing here ever matches
processes by name, so it cannot touch another terminal's programs.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

try:
    import pyte
except ImportError:                                  # pragma: no cover - dev dependency
    pyte = None

ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def visible(raw: bytes) -> str:
    return ANSI.sub(b"", raw).decode("utf-8", "replace")


def squeeze(text: str) -> str:
    """Whitespace-free form: full-screen programs draw spaces with cursor moves, so compare without them."""
    return re.sub(r"\s+", "", text)


def _stat(pid: int) -> tuple[int, int, int, str] | None:
    """(ppid, pgrp, session, state) from /proc, tolerating names that contain spaces or parentheses."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = text[text.rindex(")") + 2:].split()
    return int(rest[1]), int(rest[2]), int(rest[3]), rest[0]


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def session_members(sid: int) -> list[int]:
    """Live (non-zombie) processes in terminal session `sid`."""
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            st = _stat(int(entry))
            if st and st[2] == sid and st[3] != "Z":
                found.append(int(entry))
    return sorted(found)


# Runs in the child: become a session leader, adopt the pty as controlling terminal, then exec the real program (the
# same pid). Done with a helper instead of pty.fork() because forking a multi-threaded test process is unsafe.
_HELPER = ("import os,sys,fcntl,termios;os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0);os.chdir(sys.argv[1]);"
           "os.execvp(sys.argv[2],sys.argv[2:])")


class PtySession:
    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str, rows: int = 40, cols: int = 120):
        self.argv, self.raw, self.exit_status, self.rows, self.cols = argv, b"", None, rows, cols
        self.fd, slave = pty.openpty()
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", _HELPER, cwd, *argv], stdin=slave, stdout=slave,
                                     stderr=slave, env=env, close_fds=True)
        os.close(slave)
        self.pid = self.proc.pid

    # -- output ------------------------------------------------------------------------------------------------
    def pump(self, seconds: float = 0.1) -> None:
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                ready, _, _ = select.select([self.fd], [], [], left)
                if not ready:
                    return
                chunk = os.read(self.fd, 65536)
            except OSError:
                return                                 # EIO: every process on the slave side has gone
            if not chunk:
                return
            self.raw += chunk
            self._answer_queries(chunk)

    def _answer_queries(self, chunk: bytes) -> None:
        """Terminal emulators answer a few capability queries; a TUI waits for them before it starts drawing."""
        if b"\x1b[6n" in chunk:
            os.write(self.fd, b"\x1b[1;1R")            # cursor position
        if b"\x1b[c" in chunk or b"\x1b[0c" in chunk:
            os.write(self.fd, b"\x1b[?62;c")           # primary device attributes
        if b"\x1b[>0q" in chunk:
            os.write(self.fd, b"\x1bP>|xterm(388)\x1b\\")

    @property
    def text(self) -> str:
        return visible(self.raw)

    def screen(self) -> str:
        """What the terminal is showing right now, as a person would read it (needs the `pyte` dev dependency)."""
        if pyte is None:
            return self.text
        if getattr(self, "_emu", None) is None:                # incremental: feed only what arrived since the last look
            screen = pyte.Screen(self.cols, self.rows)
            self._emu = (screen, pyte.ByteStream(screen), [0])
        screen, stream, fed = self._emu
        if fed[0] < len(self.raw):
            stream.feed(self.raw[fed[0]:])
            fed[0] = len(self.raw)
        lines = [line.rstrip() for line in screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def wait_for(self, needle: str, timeout: float = 30.0, since: int = 0) -> float:
        """Seconds until `needle` (whitespace-insensitive) appears in the output after byte offset `since`."""
        want = squeeze(needle)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            self.pump(0.05)
            if want in squeeze(visible(self.raw[since:])):
                return time.monotonic() - start
            if self.poll() is not None:
                self.pump(0.2)
                if want in squeeze(visible(self.raw[since:])):
                    return time.monotonic() - start
                raise AssertionError(f"program exited ({self.exit_status}) before showing {needle!r}\n{self.tail()}")
        raise AssertionError(f"timed out after {timeout}s waiting for {needle!r}\n{self.tail()}")

    def until(self, predicate, timeout: float, step: float = 0.05):
        """Poll `predicate` for up to `timeout` seconds while draining the terminal, so a chatty program never blocks
        on a full pty buffer. Returns the predicate's last value (falsy on timeout)."""
        end = time.monotonic() + timeout
        value = predicate()
        while not value and time.monotonic() < end:
            self.pump(step)
            value = predicate()
        return value

    def wait_screen(self, needle: str, timeout: float = 30.0) -> float:
        """Seconds until `needle` (whitespace-insensitive) is on the rendered screen. Full-screen programs redraw only
        what changed, so their byte stream does not contain the text; the emulated screen does."""
        want = squeeze(needle)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            self.pump(0.1)
            if want in squeeze(self.screen()):
                return time.monotonic() - start
            if self.poll() is not None:
                self.pump(0.2)
                if want in squeeze(self.screen()):
                    return time.monotonic() - start
                raise AssertionError(f"program exited ({self.exit_status}) before showing {needle!r}\n{self.tail()}")
        raise AssertionError(f"timed out after {timeout}s waiting for {needle!r} on screen\n{self.tail()}")

    def tail(self, n: int = 1800) -> str:
        return "--- screen ---\n" + self.screen()[-n:]

    # -- input -------------------------------------------------------------------------------------------------
    def send(self, data: bytes | str) -> None:
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def type(self, text: str, delay: float = 0.01) -> None:
        for ch in text:
            self.send(ch)
            self.pump(delay)

    def ctrl_c(self) -> None:
        self.send(b"\x03")

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def poll(self) -> int | None:
        if self.exit_status is None:
            self.exit_status = self.proc.poll()
        return self.exit_status

    def wait_exit(self, timeout: float) -> float | None:
        """Seconds until the child exits, or None if it is still running after `timeout`."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            self.pump(0.05)
            if self.poll() is not None:
                return time.monotonic() - start
        return None

    def members(self) -> list[int]:
        return session_members(self.pid)

    def describe_members(self) -> list[str]:
        return [f"{p}: {cmdline(p)[:110]}" for p in self.members()]

    def cleanup(self) -> None:
        """Test teardown only: end whatever this session still owns (its own session id, never a name match)."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            members = self.members()
            if not members:
                break
            for pid in members:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            self.pump(0.3)
        self.poll()
        try:
            os.close(self.fd)
        except OSError:
            pass
