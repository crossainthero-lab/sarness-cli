"""Ctrl+C in the Sarness terminal (`sarness` / `sarness code`), driven through a real pseudo-terminal.

The Sarness terminal is a compiled binary, so these run only where one is available (an installed build, or
SARNESS_TUI_BIN); CI has none and skips them, while the install checks on a real machine run them. Each test sends the
actual 0x03 byte a keyboard sends, once, and checks the visible effect, the Engine's active stream count, and that no
process this test started survives.

Root causes these guard against (found with this harness against 0.4.0):
  * a Ctrl+C sent between the terminal turning raw and the app's keymap mounting was dropped (no SIGINT in raw mode,
    nobody reading the key), so the user pressed again once the screen was up;
  * while a turn or command ran, Ctrl+C either did nothing useful (text in the composer was merely cleared) or quit the
    whole app, because cancelling was only reachable as Escape pressed twice.
"""
import os
import pwd
import signal
import subprocess
import termios
import time
from pathlib import Path

import pytest

from ctrl_c_support import (REPLY, command, configure, engine_for, marked_processes, running_sleeps,
                            sleep_tag, stream_count, unique_marker)
from ptyharness import PtySession


def _find_binary() -> str | None:
    explicit = os.environ.get("SARNESS_TUI_BIN")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)       # the real home: the isolated_home fixture redirects $HOME
    data = Path(os.environ.get("SARNESS_REAL_XDG_DATA_HOME") or home / ".local/share")
    found = data / "sarness-cli" / "tui" / "current" / "sarness-tui"
    return str(found) if os.access(found, os.X_OK) else None


TUI = _find_binary()
pytestmark = [
    pytest.mark.skipif(TUI is None, reason="no Sarness terminal (sarness-tui) installed; run `sarness tui install`"),
    pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs Linux /proc to look for stray processes"),
]

READY = "ctrl+p commands"            # the composer's status row: the app is up and reading keys


@pytest.fixture
def tui(tmp_path, monkeypatch):
    marker = unique_marker()
    server = engine_for("shell", marker)
    configure(server, runtime="tui")
    work = tmp_path / "work"
    work.mkdir()
    for name in ("data", "cache"):
        (tmp_path / name).mkdir()
    env = dict(os.environ, TERM="xterm-256color", SARNESS_TUI_BIN=TUI, SARNESS_CTRLC_MARKER=marker,
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"))
    sessions = []

    def start(wait_ready=True):
        session = PtySession([*command(), "-C", str(work), "code"], env=env, cwd=str(work), rows=40, cols=120)
        sessions.append(session)
        if wait_ready:
            session.wait_for(READY, 40)
            session.pump(0.5)
        return session

    start.server, start.marker, start.work = server, marker, work
    yield start
    for session in sessions:
        session.cleanup()
    server.stop()
    for pid in marked_processes(marker):                 # nothing this test started may outlive it
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def terminal_is_sane(session) -> bool:
    try:
        lflag = termios.tcgetattr(session.fd)[3]
    except termios.error:
        return True
    return bool(lflag & termios.ISIG) and bool(lflag & termios.ICANON) and bool(lflag & termios.ECHO)


def assert_clean_exit(session, marker, within=6.0):
    took = session.wait_exit(within)
    assert took is not None, "the program did not exit on Ctrl+C\n" + session.tail()
    assert session.exit_status in (0, 130), session.exit_status
    assert terminal_is_sane(session), "the terminal was left in raw mode"
    assert session.until(lambda: not marked_processes(marker), 8), \
        f"processes outlived the session: {[(p, session.describe_members()) for p in marked_processes(marker)]}"
    return took


def submit(session, text):
    """Type a message and press Enter the way a person does (a pause between the last key and Enter)."""
    session.type(text)
    session.pump(0.4)
    session.send("\r")


def still_running(session, seconds=1.5) -> bool:
    """True if the program is still up after the time quitting would take (about half a second)."""
    session.pump(seconds)
    return session.poll() is None


def generating(tui_start):
    session = tui_start()
    submit(session, "SLOWGEN")
    assert session.until(lambda: stream_count(tui_start.server) == 1, 30), session.tail()
    session.wait_screen("word word", 20)
    return session


@pytest.mark.parametrize("delay", [0.2, 0.6, 0.9, 1.2, 1.5, 2.5])
def test_ctrl_c_works_whenever_it_is_pressed_during_startup(tui, delay):
    """The dropped-key window: terminal raw, app not yet mounted. A press must exit, never be lost."""
    session = tui(wait_ready=False)
    session.pump(delay)
    session.ctrl_c()
    assert_clean_exit(session, tui.marker)


def test_idle_prompt_exits_on_the_first_ctrl_c(tui):
    session = tui()
    session.ctrl_c()
    assert assert_clean_exit(session, tui.marker) < 4


def test_typed_text_is_cleared_first_then_the_next_ctrl_c_exits(tui):
    session = tui()
    session.type("half a thought")
    session.wait_screen("half a thought", 5)
    session.ctrl_c()
    assert still_running(session, 1.2), "the first Ctrl+C clears the composer, it must not quit while text is being edited"
    assert "half a thought" not in session.screen(), session.screen()
    session.ctrl_c()
    assert_clean_exit(session, tui.marker)


def test_ctrl_c_cancels_a_generation_and_releases_the_engine_stream(tui):
    session = generating(tui)
    server = tui.server
    session.ctrl_c()
    assert session.until(lambda: stream_count(server) == 0 and server.disconnects >= 1, 8), \
        f"engine stream not released: streams={stream_count(server)} disconnects={server.disconnects}\n" + session.tail()
    assert still_running(session), "Ctrl+C quit the whole app instead of cancelling the generation"
    session.wait_screen("interrupted", 5)                                    # the turn is marked, with a notice
    # control is back at the prompt and the same session carries on
    submit(session, "hello")
    session.wait_screen(REPLY, 25)
    session.ctrl_c()
    assert_clean_exit(session, tui.marker)


def test_cancelling_keeps_what_is_being_typed_and_a_further_press_only_clears_it(tui):
    session = generating(tui)
    server = tui.server
    session.type("follow up")
    session.wait_screen("follow up", 5)
    session.ctrl_c()                                                         # 1: cancels the generation
    assert session.until(lambda: stream_count(server) == 0, 8)
    assert still_running(session) and "follow up" in session.screen(), session.screen()
    session.ctrl_c()                                                         # 2: nothing running, so it clears
    assert still_running(session, 1.2) and "follow up" not in session.screen()
    session.ctrl_c()                                                         # 3: idle and empty, so it exits
    assert_clean_exit(session, tui.marker)


def test_ctrl_c_interrupts_the_agents_shell_command_and_the_session_can_continue(tui):
    session = tui()
    marker = tui.marker
    submit(session, "SLEEPCMD")
    assert session.until(lambda: running_sleeps(marker), 30), session.tail()
    bystander = subprocess.Popen(["/bin/sh", "-c", f"exec sleep 600 # UNRELATED-{marker}"], start_new_session=True)
    try:
        started = time.monotonic()
        session.ctrl_c()
        assert session.until(lambda: not running_sleeps(marker), 10), "the interrupted command is still running"
        assert time.monotonic() - started < 8
        assert still_running(session), "Ctrl+C quit the whole app instead of stopping the command"
        assert bystander.poll() is None, "an unrelated process was killed"
        session.type("hello")
        session.send("\r")
        session.wait_screen(REPLY, 25)
        assert not running_sleeps(marker), "the interrupted command was run again"
        session.ctrl_c()
        assert_clean_exit(session, marker)
    finally:
        bystander.kill()
        bystander.wait()


def test_ctrl_c_interrupts_a_bang_shell_command_the_user_started(tui):
    session = tui()
    marker = tui.marker
    session.type("!")
    session.pump(0.4)
    submit(session, f"sleep {sleep_tag(marker)}")
    assert session.until(lambda: running_sleeps(marker), 30), session.tail()
    session.ctrl_c()
    assert session.until(lambda: not running_sleeps(marker), 10), "the command is still running"
    assert still_running(session), "Ctrl+C quit the whole app instead of stopping the command"
    session.ctrl_c()
    assert_clean_exit(session, marker)


def test_a_command_that_ignores_sigint_and_sigterm_is_killed_in_bounded_time(tui, monkeypatch):
    monkeypatch.setenv("CTRL_C_TEST_PREFIX", "trap '' INT TERM; ")
    session = tui()
    marker = tui.marker
    submit(session, "SLEEPCMD")
    assert session.until(lambda: running_sleeps(marker), 30), session.tail()
    started = time.monotonic()
    session.ctrl_c()
    assert session.until(lambda: not running_sleeps(marker), 15), "a stubborn command outlived Ctrl+C"
    assert time.monotonic() - started < 12                                  # SIGTERM to its group, SIGKILL after 3 s
    assert still_running(session)
    session.ctrl_c()
    assert_clean_exit(session, marker)
