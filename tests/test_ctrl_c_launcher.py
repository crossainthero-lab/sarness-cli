"""The launcher that runs the Sarness terminal as the foreground program (`run_foreground`), exercised with stub
children on a real pseudo-terminal: it must not die on Ctrl+C (the terminal program owns that key), must hand
SIGTERM/SIGHUP on to the program it started and stop it in bounded time, and must leave the user's shell usable when
that program dies without cleaning up."""
import os
import re
import signal
import sys
import termios
import textwrap

import pytest

from ptyharness import PtySession, visible

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs Linux /proc")

DRIVER = textwrap.dedent('''
    import sys
    from sarness_cli.openai_bridge import run_foreground
    child = sys.argv[1]
    grace = float(sys.argv[2])
    code = run_foreground([sys.executable, "-c", child], stop_grace=grace)
    print("LAUNCHER-DONE rc=%d" % code, flush=True)
''')


def launch(child_source, grace=5.0):
    env = dict(os.environ, TERM="xterm-256color")
    session = PtySession([sys.executable, "-c", DRIVER, child_source, str(grace)], env=env, cwd=os.getcwd(), rows=24, cols=100)
    return session


def done_code(session, within=10):
    session.wait_for("LAUNCHER-DONE", within)
    session.pump(0.2)
    match = re.search(r"LAUNCHER-DONE rc=(-?\d+)", visible(session.raw))
    assert match, visible(session.raw)[-400:]
    return int(match.group(1))


@pytest.fixture
def sessions():
    made = []
    yield made
    for session in made:
        session.cleanup()


def test_the_launcher_survives_ctrl_c_and_reports_how_the_program_ended(sessions):
    session = launch("import time; print('CHILD-UP', flush=True); time.sleep(60)")
    sessions.append(session)
    session.wait_for("CHILD-UP", 10)
    session.ctrl_c()                      # cooked mode here: SIGINT goes to the whole foreground group
    assert done_code(session) == 130      # the child died of SIGINT (reported like a shell does); the launcher did not
    assert session.wait_exit(5) is not None and session.exit_status == 0


def test_a_program_that_handles_ctrl_c_itself_keeps_running_and_the_launcher_waits(sessions):
    child = ("import signal, time, sys\n"
             "signal.signal(signal.SIGINT, lambda *a: (print('CHILD-SAW-INT', flush=True), sys.exit(0)))\n"
             "print('CHILD-UP', flush=True); time.sleep(60)")
    session = launch(child)
    sessions.append(session)
    session.wait_for("CHILD-UP", 10)
    session.ctrl_c()
    session.wait_for("CHILD-SAW-INT", 5)
    assert done_code(session) == 0


def test_sigterm_is_forwarded_to_the_program_and_the_launcher_waits_for_it(sessions):
    child = ("import signal, sys, time\n"
             "signal.signal(signal.SIGTERM, lambda *a: (print('CHILD-SAW-TERM', flush=True), sys.exit(7)))\n"
             "print('CHILD-UP', flush=True); time.sleep(60)")
    session = launch(child)
    sessions.append(session)
    session.wait_for("CHILD-UP", 10)
    os.kill(session.pid, signal.SIGTERM)                       # the launcher, not the child
    session.wait_for("CHILD-SAW-TERM", 5)
    assert done_code(session) == 7


def test_a_program_that_ignores_sigterm_is_killed_after_the_grace_period_and_nothing_else_is(sessions):
    child = ("import signal, time\n"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             "print('CHILD-UP', flush=True); time.sleep(60)")
    session = launch(child, grace=0.6)
    sessions.append(session)
    session.wait_for("CHILD-UP", 10)
    os.kill(session.pid, signal.SIGTERM)
    assert done_code(session, 10) == 128 + signal.SIGKILL      # 137: the launcher stayed up, reaped it and reported it


def test_a_crashed_terminal_program_leaves_the_users_terminal_usable(sessions):
    child = ("import os, signal, sys, tty\n"
             "tty.setraw(0); sys.stdout.write('\\x1b[?1049h\\x1b[?1000h\\x1b[?25l'); sys.stdout.flush()\n"
             "print('CHILD-RAW', flush=True)\n"
             "os.kill(os.getpid(), signal.SIGKILL)")
    session = launch(child)
    sessions.append(session)
    assert done_code(session) == 128 + signal.SIGKILL
    assert b"\x1b[?1049l" in session.raw and b"\x1b[?1000l" in session.raw and b"\x1b[?25h" in session.raw, \
        "the alternate screen, mouse reporting and cursor were not reset"
    lflag = termios.tcgetattr(session.fd)[3]
    assert lflag & termios.ISIG and lflag & termios.ICANON and lflag & termios.ECHO, "the terminal was left raw"


def test_a_program_stopped_by_sigint_is_reported_as_130(sessions):
    child = ("import os, signal\n"
             "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
             "print('CHILD-UP', flush=True); os.kill(os.getpid(), signal.SIGINT); import time; time.sleep(5)")
    session = launch(child)
    sessions.append(session)
    assert done_code(session) == 130
