"""Ctrl+C in the Python runtime (`sarness native`), driven by a real keyboard byte through a real pseudo-terminal.

One press must always have one visible, immediate effect: cancel what is running, else clear the line, else quit.
Regression guard for the bug where the idle prompt ignored Ctrl+C (it only printed a hint, so it took Ctrl-D) and where
a command that ignored the first signal could keep the user waiting.
"""
import json
import os
import signal
import subprocess
import termios
import time

import pytest

from ctrl_c_support import (REPLY, command, configure, engine_for, marked_processes, running_sleeps,
                            stream_count, unique_marker)
from ptyharness import PtySession, visible

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs Linux /proc to look for stray processes")

PROMPT = "sarness ❯"


@pytest.fixture
def native(tmp_path, monkeypatch):
    marker = unique_marker()
    server = engine_for("run_command", marker)
    configure(server)
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, TERM="xterm-256color", PROMPT_TOOLKIT_NO_CPR="1", SARNESS_CTRLC_MARKER=marker)
    sessions = []

    def start():
        session = PtySession([*command(), "-C", str(work), "native"], env=env, cwd=str(work), rows=30, cols=120)
        sessions.append(session)
        session.wait_for(PROMPT, 30)
        return session

    start.server, start.marker, start.work = server, marker, work
    yield start
    for session in sessions:
        session.cleanup()
    server.stop()
    for pid in marked_processes(marker):          # nothing this test started may outlive it
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def terminal_is_sane(session) -> bool:
    try:
        lflag = termios.tcgetattr(session.fd)[3]
    except termios.error:
        return True                                # the terminal is already gone: nothing is left raw
    return bool(lflag & termios.ISIG) and bool(lflag & termios.ICANON) and bool(lflag & termios.ECHO)


def test_idle_prompt_exits_on_the_first_ctrl_c(native):
    session = native()
    session.ctrl_c()
    took = session.wait_exit(5)
    assert took is not None, "Ctrl+C at an idle, empty prompt must exit; it was ignored\n" + session.tail()
    assert took < 3 and session.exit_status == 0
    assert terminal_is_sane(session)
    assert not session.members(), session.describe_members()


def test_typed_text_is_cleared_first_then_the_next_ctrl_c_exits(native):
    session = native()
    session.type("half a thought")
    session.wait_for("half a thought", 5)
    session.ctrl_c()
    session.pump(0.6)
    assert session.poll() is None, "the first Ctrl+C clears the line, it must not quit while text is being edited"
    assert "half a thought" not in session.screen(), "the first Ctrl+C did not clear the line\n" + session.screen()
    session.ctrl_c()
    assert session.wait_exit(5) is not None, session.tail()
    assert session.exit_status == 0


def test_ctrl_c_cancels_a_generation_releases_the_engine_stream_and_the_session_continues(native):
    session = native()
    server = native.server
    session.type("SLOWGEN")
    session.send("\r")
    assert session.until(lambda: stream_count(server) == 1, 20), session.tail()
    session.wait_for("word word", 10)
    before = len(session.raw)
    session.ctrl_c()
    session.wait_for(PROMPT, 5, since=before)       # control returns at once
    assert session.poll() is None
    assert session.until(lambda: stream_count(server) == 0 and server.disconnects >= 1, 5), \
        f"engine stream not released: streams={stream_count(server)} disconnects={server.disconnects}"
    assert "cancel" in visible(session.raw[before:]).lower(), visible(session.raw[before:])
    before = len(session.raw)
    session.type("hello")
    session.send("\r")
    session.wait_for(REPLY, 15, since=before)        # the same session still works
    session.wait_for(PROMPT, 10, since=before)
    session.ctrl_c()
    assert session.wait_exit(5) is not None


def test_ctrl_c_interrupts_a_running_command_saves_the_session_and_does_not_replay_it(native):
    session = native()
    marker = native.marker
    session.type("SLEEPCMD")
    session.send("\r")
    assert session.until(lambda: running_sleeps(marker), 20), session.tail()
    before = len(session.raw)
    started = time.monotonic()
    session.ctrl_c()
    assert session.until(lambda: not running_sleeps(marker), 8), "the interrupted command is still running"
    assert time.monotonic() - started < 6
    session.wait_for(PROMPT, 8, since=before)
    assert session.poll() is None
    saved = [json.loads(p.read_text()) for p in (native.work.parent / "state").rglob("*.json") if p.parent.name != "tunnels"] \
        if (native.work.parent / "state").exists() else []
    assert any(s.get("status") == "interrupted" for s in saved if isinstance(s, dict)), "no interrupted checkpoint was saved"
    # continuing the conversation must not run the command again: the model is told its effects are unknown
    before = len(session.raw)
    session.type("continue")
    session.send("\r")
    session.wait_for(REPLY, 15, since=before)
    assert not running_sleeps(marker)
    assert any("Interrupted by the user" in json.dumps(body["messages"]) for body in native.server.completions[-1:]), \
        "the model was not told the command was interrupted"
    session.ctrl_c()
    assert session.wait_exit(5) is not None


def test_a_command_that_ignores_sigint_and_sigterm_is_killed_in_bounded_time_and_nothing_else_is(native, monkeypatch):
    monkeypatch.setenv("CTRL_C_TEST_PREFIX", "trap '' INT TERM; ")
    session = native()
    marker = native.marker
    # An unrelated long-running process in its own session: Ctrl+C must never touch it.
    bystander = subprocess.Popen(["/bin/sh", "-c", f"exec sleep 600 # UNRELATED-{marker}"], start_new_session=True)
    try:
        session.type("SLEEPCMD")
        session.send("\r")
        assert session.until(lambda: running_sleeps(marker), 20), session.tail()
        started = time.monotonic()
        session.ctrl_c()
        assert session.until(lambda: not running_sleeps(marker), 15), "a stubborn command outlived Ctrl+C"
        assert time.monotonic() - started < 14                    # SIGINT 1.5 s, SIGTERM 3 s, then SIGKILL
        session.wait_for(PROMPT, 10)
        assert bystander.poll() is None, "an unrelated process was killed"
    finally:
        bystander.kill()
        bystander.wait()


def test_a_second_ctrl_c_skips_the_grace_period(native, monkeypatch):
    monkeypatch.setenv("CTRL_C_TEST_PREFIX", "trap '' INT TERM; ")
    session = native()
    marker = native.marker
    session.type("SLEEPCMD")
    session.send("\r")
    assert session.until(lambda: running_sleeps(marker), 20), session.tail()
    started = time.monotonic()
    session.ctrl_c()
    session.pump(0.4)
    session.ctrl_c()                                               # impatient: go straight to SIGKILL
    assert session.until(lambda: not running_sleeps(marker), 5)
    assert time.monotonic() - started < 4
