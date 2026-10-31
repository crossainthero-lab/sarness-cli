"""Exercise the installed terminal and durable session state with a local fake engine."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from ctrl_c_support import configure, engine_for, unique_marker
from ptyharness import PtySession


@pytest.mark.parametrize("subcommand", [[], ["code"]])
@pytest.mark.parametrize("default", ["on", "off"])
def test_autocompact_installed_and_resumed(tmp_path, subcommand, default):
    binary = Path(os.environ.get("SARNESS_TEST_EXECUTABLE", Path.home() / ".local/bin/sarness"))
    if not binary.exists():
        pytest.skip("installed Sarness CLI unavailable")
    server = engine_for("shell", unique_marker("AUTOCOMPACT"))
    configure(server, runtime="tui")
    from sarness_cli import config
    cfg = config.load_config()
    cfg["settings"]["autocompact"] = default            # what a session that never chose starts with
    config.save_config(cfg)
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, TERM="xterm-256color", XDG_DATA_HOME=str(tmp_path / "data"),
               XDG_CACHE_HOME=str(tmp_path / "cache"))
    sessions = []

    def start(extra=()):
        proc = PtySession([str(binary), "-C", str(work), *subcommand, *extra], env=env, cwd=str(work), rows=40, cols=120)
        sessions.append(proc)
        proc.wait_for("ctrl+p commands", 40)
        proc.pump(0.5)
        return proc

    def send(proc, cmd, expected):
        proc.type(cmd)
        proc.pump(0.3)
        proc.send("\r")
        proc.wait_screen(expected, 15)
        proc.pump(0.4)

    def setting():
        paths = list((tmp_path / "data").rglob("*.db"))
        for path in paths:
            try:
                with sqlite3.connect(path) as db:
                    row = db.execute("select metadata from session_v2 order by time_updated desc limit 1").fetchone()
                    if row:
                        return json.loads(row[0] or "{}")
            except sqlite3.Error:
                pass
        return {}

    try:
        p = start()
        send(p, "/autocompact status", f"Auto-compaction: {default.upper()} (current session)")
        if default == "on":
            send(p, "/autocompact off", "Auto-compaction: OFF")
            assert setting().get("sarnessAutoCompact") is False
        send(p, "/autocompact on", "Auto-compaction: ON")
        assert setting().get("sarnessAutoCompact") is True
        send(p, "/autocompact status", "Auto-compaction: ON (current session)")
        p.ctrl_c()
        assert p.wait_exit(8) is not None
        if subcommand:
            p = start(["--continue"])
        else:
            # Resume the same session through the normal `sarness code` entry point.
            subcommand = ["code"]
            p = start(["--continue"])
        send(p, "/autocompact status", "Auto-compaction: ON (current session)")
        send(p, "/autocompact off", "Auto-compaction: OFF")
        assert setting().get("sarnessAutoCompact") is False
        send(p, "/autocompact status", "Auto-compaction: OFF (current session)")
    finally:
        for p in sessions:
            p.cleanup()
        server.stop()


def test_installed_runtime_switches_real_compaction(tmp_path):
    from ctrl_c_support import Dispatch, is_title_request
    from fake_engine_server import done

    if "SARNESS_TEST_EXECUTABLE" not in os.environ or not Path(os.environ["SARNESS_TEST_EXECUTABLE"]).exists():
        pytest.skip("needs SARNESS_TEST_EXECUTABLE pointing at an installed Sarness CLI")
    binary = Path(os.environ["SARNESS_TEST_EXECUTABLE"])
    server = engine_for("shell", unique_marker("AUTOCOMPACT-RUNTIME"))
    server.llm["ctx"] = 4096
    def script(body):
        if is_title_request(body):
            yield done("Test session")
            return
        text = json.dumps(body.get("messages", []))
        if "You MUST use this format" in text:
            summary = "## Objective\n- Preserve work\n## Work State\n### Completed\n- First step\n## Next Move\n1. Continue"
            yield {"type": "delta", "content": summary, "reasoning": ""}
            yield done(summary)
            return
        reply = done("RUNTIME-REPLY")
        reply["usage"] = {"prompt_tokens": 3300, "completion_tokens": 40}
        yield {"type": "delta", "content": "RUNTIME-REPLY", "reasoning": ""}
        yield reply
    server.turns = Dispatch(script)
    configure(server, runtime="tui")
    from sarness_cli import config
    cfg = config.load_config()
    cfg["settings"]["autocompact"] = "off"              # this scenario starts from automatic compaction off
    config.save_config(cfg)
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, TERM="xterm-256color", XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"))
    p = PtySession([str(binary), "-C", str(work), "code"], env=env, cwd=str(work), rows=40, cols=120)
    def submit(text):
        p.type(text)
        p.pump(0.3)
        p.send("\r")
    def compact_count():
        for path in (tmp_path / "data").rglob("*.db"):
            try:
                with sqlite3.connect(path) as db:
                    return db.execute("select count(*) from session_message where type='compaction' and json_extract(data, '$.status')='completed'").fetchone()[0]
            except sqlite3.Error:
                pass
        return 0
    try:
        p.wait_for("ctrl+p commands", 40)
        submit("first request")
        p.wait_screen("RUNTIME-REPLY", 20)
        p.wait_screen("Context is", 10)
        submit("second request")
        assert p.until(lambda: sum("second request" in json.dumps(x) for x in server.completions) >= 1, 20), p.tail()
        p.pump(1)
        assert compact_count() == 0
        submit("/autocompact on")
        p.wait_screen("Auto-compaction: ON", 10)
        submit("third request")
        assert p.until(lambda: compact_count() >= 1, 20), p.tail()
        assert p.until(lambda: any("You MUST use this format" in json.dumps(x) for x in server.completions), 20), p.tail()
        p.pump(2)
        submit("/compact")
        assert p.until(lambda: compact_count() >= 2, 20), p.tail()
        p.pump(2)
        submit("/autocompact off")
        p.wait_screen("Auto-compaction: OFF", 10)
        submit("more work to summarize")
        assert p.until(lambda: any("more work to summarize" in json.dumps(x) for x in server.completions), 20), p.tail()
        p.pump(1)
        submit("/compact")
        assert p.until(lambda: compact_count() >= 3, 20), p.tail()
    finally:
        p.cleanup()
        server.stop()
