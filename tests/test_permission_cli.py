"""Interactive, multi-prompt/resume/delegation checks through the actual CLI process.

Set SARNESS_TEST_EXECUTABLE=/absolute/path/to/sarness to verify an installed build.
The inference server is scripted, but no executor, approval or filesystem is mocked.
"""
import fcntl
import json
import os
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from fake_engine_server import FakeEngineServer, TOKEN, done
from sarness_cli import config


def command():
    installed = os.environ.get("SARNESS_TEST_EXECUTABLE")
    return [installed] if installed else [sys.executable, "-m", "sarness_cli.cli"]


def configure(server):
    cfg = config.load_config()
    cfg["profiles"]["fake"] = {"transport": "direct", "url": server.url, "model": "local:coder@Q4"}
    cfg["settings"].update(server="fake", permissions="ask", effort="low")
    config.save_config(cfg)
    config.set_secret("fake", TOKEN)


class Terminal:
    def __init__(self, root, *args):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 200, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", PROMPT_TOOLKIT_NO_CPR="1")
        self.proc = subprocess.Popen(command() + ["-C", str(root), *args], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        self.output = ""
        self.offset = 0

    def until(self, marker):
        deadline = time.monotonic() + 20
        while marker not in self.output[self.offset:]:
            if time.monotonic() > deadline:
                pytest.fail(f"Timed out waiting for {marker}: {self.output[-6000:]}")
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    pytest.fail(f"CLI exited waiting for {marker}: {self.output[-6000:]}")
                self.output += chunk.decode("utf-8", "replace")
        self.offset = self.output.index(marker, self.offset) + len(marker)

    def send(self, text):
        os.write(self.master, (text + "\r").encode())

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=5)
        os.close(self.master)


def test_multiple_prompts_delegated_cli_and_resume(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    child = " ".join(shlex.quote(p) for p in command()) + ' run "delegated coding task"'
    server = FakeEngineServer([
        [done(calls=[("write_file", {"path": "calc.py", "content": "value = 1\n"}),
                     ("edit_file", {"path": "calc.py", "old_string": "1", "new_string": "2"}),
                     ("read_file", {"path": "calc.py"}),
                     ("write_file", {"path": "Makefile", "content": "test:\n\tpython3 -c 'from calc import value; assert value == 2'\n"}),
                     ("run_command", {"command": "make test"})])],
        [{"type": "delta", "content": "FIRST_COMPLETE"}, done(content="FIRST_COMPLETE")],
        [done(calls=[("run_command", {"command": child})])],
        [done(calls=[("write_file", {"path": "delegated.txt", "content": "inherited bypass"}),
                     ("run_command", {"command": "make test"})])],
        [{"type": "delta", "content": "CHILD_COMPLETE"}, done(content="CHILD_COMPLETE")],
        [{"type": "delta", "content": "SECOND_COMPLETE"}, done(content="SECOND_COMPLETE")],
        [done(calls=[("edit_file", {"path": "calc.py", "old_string": "2", "new_string": "3"}),
                     ("run_command", {"command": "python3 -c 'from calc import value; assert value == 3'"})])],
        [{"type": "delta", "content": "RESUME_COMPLETE"}, done(content="RESUME_COMPLETE")],
    ])
    configure(server)
    terminal = Terminal(root, "--dangerously-skip-permissions")
    try:
        terminal.until("❯")
        terminal.send("Fix the coding task")
        terminal.until("FIRST_COMPLETE")
        terminal.until("❯")
        terminal.send("Delegate the followup")
        terminal.until("SECOND_COMPLETE")
        terminal.until("❯")
        terminal.send("/exit")
        terminal.proc.wait(timeout=5)
        assert "Allow?" not in terminal.output and "Approve?" not in terminal.output
        assert "approval needed" not in terminal.output and "DENIED" not in terminal.output
        assert (root / "delegated.txt").read_text() == "inherited bypass"
        sid = next((config.state_dir() / "sessions").glob("*.json")).stem
        # The parent session includes the child command; identify it independently of timestamps.
        for path in (config.state_dir() / "sessions").glob("*.json"):
            data = json.loads(path.read_text())
            if any(c["command"] == child for c in data.get("commands", [])):
                sid = path.stem
        resumed = subprocess.run(command() + ["resume", sid, "continue", "--dangerously-skip-permissions"],
                                 capture_output=True, text=True, timeout=20)
        assert resumed.returncode == 0, resumed.stderr
        assert "BYPASS" in resumed.stderr and "approval needed" not in resumed.stderr
        assert (root / "calc.py").read_text() == "value = 3\n"
        assert all("Permissions: Bypass" in c["messages"][0]["content"] for c in server.completions)
    finally:
        terminal.close()
        server.stop()


def test_installed_boundary_denials_and_normal_approvals(tmp_path):
    server = FakeEngineServer([
        [done(calls=[("run_command", {"command": "git push origin main"}),
                     ("run_command", {"command": "cat ~/.ssh/id_ed25519"}),
                     ("run_command", {"command": "rm -rf /"})])],
        [done(content="BOUNDARIES_COMPLETE")],
        [done(calls=[("write_file", {"path": "should-not-exist", "content": "no"})])],
        [done(content="ASK_COMPLETE")],
    ])
    configure(server)
    try:
        result = subprocess.run(command() + ["run", "check", "-C", str(tmp_path), "--dangerously-skip-permissions"],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        results = [m["content"] for m in server.completions[1]["messages"] if m["role"] == "tool"]
        assert len(results) == 3 and all(r.startswith("DENIED") for r in results)
        assert "approval needed" not in result.stderr  # bypass boundaries are explicit denials
        result = subprocess.run(command() + ["run", "check", "-C", str(tmp_path), "--mode", "ask"],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0 and "approval needed" in result.stderr
        assert not (tmp_path / "should-not-exist").exists()
    finally:
        server.stop()


def test_repl_inventory_boundaries_failures_new_and_resume(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / '.git').mkdir()
    server = FakeEngineServer([
        [done(calls=[('run_command', {'command': "find . -type f -not -path './.git/*'"}),
                     ('run_command', {'command': 'false'}),
                     ('read_file', {'path': 'missing.txt'}),
                     ('run_command', {'command': 'git push origin main'}),
                     ('write_file', {'path': 'after.txt', 'content': 'survived'}),
                     ('run_command', {'command': 'cat after.txt'})])],
        [{'type': 'delta', 'content': 'INVENTORY_COMPLETE'}, done(content='INVENTORY_COMPLETE')],
        [done(calls=[('edit_file', {'path': 'after.txt', 'old_string': 'survived', 'new_string': 'resumed'}),
                     ('run_command', {'command': 'cat after.txt'})])],
        [{'type': 'delta', 'content': 'RESTORED_COMPLETE'}, done(content='RESTORED_COMPLETE')],
    ])
    configure(server)
    terminal = Terminal(root, '--dangerously-skip-permissions')
    try:
        terminal.until('❯')
        terminal.send('/permissions bypass')  # already enabled: no second confirmation
        terminal.until('❯')
        terminal.send('inventory')
        terminal.until('INVENTORY_COMPLETE')
        terminal.until('❯')
        sid = next((config.state_dir() / 'sessions').glob('*.json')).stem
        terminal.send('/new')
        terminal.until('❯')
        terminal.send('/resume ' + sid)
        terminal.until('❯')
        terminal.send('edit again')
        terminal.until('RESTORED_COMPLETE')
        terminal.until('❯')
        terminal.send('/exit')
        terminal.proc.wait(timeout=5)
        terminal.until('Resume: sarness')
        assert 'Allow?' not in terminal.output and 'Approve?' not in terminal.output
        assert 'Type \'bypass\'' not in terminal.output
        assert 'approval needed' not in terminal.output
        assert 'Refused in BYPASS' in terminal.output
        assert (root / 'after.txt').read_text() == 'resumed'
        assert 'BYPASS' in terminal.output
    finally:
        terminal.close()
        server.stop()


def test_normal_repl_really_prompts_before_writing(tmp_path):
    server = FakeEngineServer([
        [done(calls=[('write_file', {'path': 'normal.txt', 'content': 'approved'})])],
        [{'type': 'delta', 'content': 'NORMAL_COMPLETE'}, done(content='NORMAL_COMPLETE')],
    ])
    configure(server)
    terminal = Terminal(tmp_path, '--mode', 'ask')
    try:
        terminal.until('❯')
        terminal.send('write the file')
        terminal.until('Allow?')
        assert not (tmp_path / 'normal.txt').exists()
        terminal.send('y')
        terminal.until('NORMAL_COMPLETE')
        assert (tmp_path / 'normal.txt').read_text() == 'approved'
    finally:
        terminal.close()
        server.stop()
