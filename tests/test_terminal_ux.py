"""Tests for Terminal UX Overhaul: Autocomplete, interactive menus, offline startup, PTY interactions."""
import json
import os
import pty
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input

from sarness_cli import cli, config
from sarness_cli.engine import EngineError, explain_engine_error
from sarness_cli.menu import (MenuItem, TerminalMenu, select_effort, select_model,
                              select_permissions, select_server, select_session, select_thinking)
from sarness_cli.modelinfo import cache_models, load_cached_models
from sarness_cli.repl import Repl, SlashCommandCompleter
from sarness_cli.agent import RunConfig, Session
from sarness_cli.tools import Workspace
from sarness_cli.ui import Console
from fake_engine_server import FakeEngineServer, TOKEN, done


class DummyDoc:
    def __init__(self, text):
        self.text_before_cursor = text


# -----------------------------------------------------------------------------
# 1. Menu Component Tests
# -----------------------------------------------------------------------------

def test_menu_arrow_navigation_and_enter():
    items = [
        MenuItem("low", "Low", "Direct"),
        MenuItem("med", "Medium", "Balanced", current=True),
        MenuItem("high", "High", "Thorough"),
        MenuItem("max", "Maximum", "Max rigor"),
    ]
    menu = TerminalMenu("Reasoning Effort", items, show_filter=False)
    with create_pipe_input() as pipe:
        # Send Down arrow (\x1b[B) then Enter (\r)
        pipe.send_text("\x1b[B\r")
        chosen = menu.run(input_pipe=pipe)
        assert chosen is not None
        assert chosen.key == "high"


def test_menu_escape_cancels():
    items = [
        MenuItem("m1", "Model 1"),
        MenuItem("m2", "Model 2"),
    ]
    menu = TerminalMenu("Models", items)
    with create_pipe_input() as pipe:
        # Send Escape (\x1b)
        pipe.send_text("\x1b")
        chosen = menu.run(input_pipe=pipe)
        assert chosen is None


def test_menu_live_filtering_and_backspace():
    items = [
        MenuItem("qwen", "Qwen 2.5 Coder"),
        MenuItem("deepseek", "DeepSeek R1"),
        MenuItem("llama", "Llama 3.3"),
    ]
    menu = TerminalMenu("Models", items, show_filter=True)
    with create_pipe_input() as pipe:
        # Type "deep" then Enter
        pipe.send_text("deep\r")
        chosen = menu.run(input_pipe=pipe)
        assert chosen is not None
        assert chosen.key == "deepseek"


def test_menu_skips_disabled_items():
    items = [
        MenuItem("m1", "Model 1", current=True),
        MenuItem("m2", "Model 2 (unsupported)", disabled=True, disabled_reason="context size too small"),
        MenuItem("m3", "Model 3"),
    ]
    menu = TerminalMenu("Models", items)
    with create_pipe_input() as pipe:
        # Down should skip disabled m2 and go straight to m3
        pipe.send_text("\x1b[B\r")
        chosen = menu.run(input_pipe=pipe)
        assert chosen is not None
        assert chosen.key == "m3"


# -----------------------------------------------------------------------------
# 2. Interactive Selectors
# -----------------------------------------------------------------------------

def test_select_effort_interactive():
    from sarness_cli.modelinfo import Capabilities
    caps = Capabilities("local:coder", True, True, True, True, 65536, 16384)
    with create_pipe_input() as pipe:
        # Start at medium, press Up (\x1b[A) to reach Low, then Enter (\r)
        pipe.send_text("\x1b[A\r")
        chosen = select_effort("medium", caps, input_pipe=pipe)
        assert chosen == "low"


def test_select_thinking_interactive():
    from sarness_cli.modelinfo import Capabilities
    caps = Capabilities("local:coder", True, True, True, True, 65536, 16384)
    with create_pipe_input() as pipe:
        # Start at auto, press Down (\x1b[B) to reach On, then Enter
        pipe.send_text("\x1b[B\r")
        chosen = select_thinking("auto", caps, input_pipe=pipe)
        assert chosen == "on"


def test_select_permissions_interactive():
    with create_pipe_input() as pipe:
        # Start at auto, press Down to reach full-access, then Enter
        pipe.send_text("\x1b[B\r")
        chosen = select_permissions("auto", input_pipe=pipe)
        assert chosen == "full-access"


def test_select_model_interactive():
    models = [
        {"ref": "local:coder@Q4", "name": "Coder Q4", "provider": "Sarness Engine", "context_max": 65536, "supported": True},
        {"ref": "local:small@Q8", "name": "Small Q8", "provider": "Sarness Engine", "context_max": 32768, "supported": True},
    ]
    with create_pipe_input() as pipe:
        # Down arrow to select Small Q8, then Enter
        pipe.send_text("\x1b[B\r")
        chosen = select_model(models, "local:coder@Q4", "spark", input_pipe=pipe)
        assert chosen is not None
        assert chosen["ref"] == "local:small@Q8"


# -----------------------------------------------------------------------------
# 3. Slash Command Discovery & Autocomplete
# -----------------------------------------------------------------------------

def test_slash_command_completer_discovery():
    cfg = RunConfig("local:coder", None, "medium", "auto", None, 16384, None, False, 60, 3, "spark", False, "basic")
    ws = Workspace(Path("/tmp"), "auto", None)
    session = Session(ws, cfg)
    repl = Repl(session, None, Console(), [], {}, "spark", Path("/tmp"), "global", connected=False)
    completer = SlashCommandCompleter(repl)

    # 1. Typing '/' shows all commands
    completions = list(completer.get_completions(DummyDoc("/"), None))
    cmds = [c.text for c in completions]
    assert "/help" in cmds
    assert "/cabinet" in cmds
    assert "/model" in cmds
    assert "/effort" in cmds
    assert "/server" in cmds
    assert "/status" in cmds
    assert "/reconnect" in cmds
    assert "/exit" in cmds

    # All commands have non-empty readable descriptions
    for c in completions:
        assert c.display_meta_text and len(c.display_meta_text) > 5

    # 2. Live filtering as user types
    ca_completions = list(completer.get_completions(DummyDoc("/ca"), None))
    assert [c.text for c in ca_completions] == ["/cabinet"]

    m_completions = list(completer.get_completions(DummyDoc("/m"), None))
    assert [c.text for c in m_completions] == ["/model"]

    e_completions = list(completer.get_completions(DummyDoc("/e"), None))
    assert set(c.text for c in e_completions) == {"/effort", "/exit"}

    # 3. Argument autocomplete: /effort [low|medium|high|max]
    effort_args = [c.text for c in completer.get_completions(DummyDoc("/effort "), None)]
    assert set(effort_args) == {"low", "medium", "high", "max"}


# -----------------------------------------------------------------------------
# 4. Server Diagnostics & Classification
# -----------------------------------------------------------------------------

def test_diagnostics_distinguishes_error_categories():
    # 1. Connection refused
    err1 = EngineError("Connection refused at http://127.0.0.1:8742", "network", subkind="connection_refused")
    d1 = explain_engine_error(err1, url="http://127.0.0.1:8742", server="spark")
    assert d1["category"] == "Connection Refused"
    assert "not running" in d1["detail"]

    # 2. DNS resolution failure
    err2 = EngineError("DNS lookup failed for https://unknown.host", "network", subkind="dns")
    d2 = explain_engine_error(err2, url="https://unknown.host", server="spark")
    assert d2["category"] == "DNS Resolution Failure"

    # 3. Timeout
    err3 = EngineError("Connection timed out reaching http://10.0.0.1:8742", "timeout")
    d3 = explain_engine_error(err3, url="http://10.0.0.1:8742", server="spark")
    assert d3["category"] == "Connection Timeout"

    # 4. TLS Error
    err4 = EngineError("TLS verification failed: certificate expired", "protocol", subkind="tls")
    d4 = explain_engine_error(err4, url="https://spark.lan:8742", server="spark")
    assert d4["category"] == "TLS / Certificate Error"

    # 5. Auth failure
    err5 = EngineError("Engine rejected the token (HTTP 401)", "auth", status=401)
    d5 = explain_engine_error(err5, url="http://127.0.0.1:8742", server="spark")
    assert d5["category"] == "Authentication Failure"

    # 6. Server busy
    err6 = EngineError("Engine is busy: model loading", "busy", status=409)
    d6 = explain_engine_error(err6, url="http://127.0.0.1:8742", server="spark")
    assert d6["category"] == "Server Busy"

    # 7. Unsupported protocol / incompatible server
    err7 = EngineError("That server answered, but it is not a Sarness Engine", "protocol")
    d7 = explain_engine_error(err7, url="http://127.0.0.1:8742", server="spark")
    assert d7["category"] == "Incompatible Server / Protocol Error"


# -----------------------------------------------------------------------------
# 5. Offline Model Caching
# -----------------------------------------------------------------------------

def test_model_caching_and_offline_retrieval(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    models = [
        {"ref": "local:coder@Q4", "name": "Coder Q4", "context_max": 65536},
        {"ref": "local:small@Q8", "name": "Small Q8", "context_max": 32768},
    ]
    cache_models("my-spark", models)

    cached = load_cached_models("my-spark")
    assert len(cached) == 2
    assert cached[0]["ref"] == "local:coder@Q4"
    assert cached[0]["cached"] is True
    assert cached[1]["cached"] is True


# -----------------------------------------------------------------------------
# 6. Offline CLI Startup & Resilience
# -----------------------------------------------------------------------------

def test_cli_launches_when_server_is_offline(tmp_path, monkeypatch, capsys):
    # Configure a server that points to a dead port
    cfg = config.load_config()
    cfg["profiles"]["dead-server"] = {"transport": "direct", "url": "http://127.0.0.1:1"}
    cfg["settings"]["server"] = "dead-server"
    config.save_config(cfg)
    config.set_secret("dead-server", "some-token")

    # In non-interactive scripted run: fails with clear diagnostic and does not crash
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "-C", str(tmp_path), "some task"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "Cannot reach server dead-server" in err or "Connection refused" in err


def test_repl_offline_mode_and_task_rejection(tmp_path):
    cfg = RunConfig("local:cached-model", None, "medium", "auto", None, 16384, None, False, 60, 3, "dead-srv", False, "basic")
    ws = Workspace(tmp_path, "auto", None)
    session = Session(ws, cfg)
    con = Console()
    repl = Repl(session, None, con, [], {}, "dead-srv", tmp_path, "global", connected=False, connection_error="Connection refused")

    initial_count = len(session.messages)
    # Attempting to run a task while offline gracefully warns and preserves session
    repl.task("Write hello.py")
    assert len(session.messages) == initial_count  # no side effects or crashes


def test_repl_model_switch_while_offline(tmp_path):
    cfg = RunConfig("local:cached-model", None, "medium", "auto", None, 16384, None, False, 60, 3, "dead-srv", False, "basic")
    ws = Workspace(tmp_path, "auto", None)
    session = Session(ws, cfg)
    con = Console()
    models = [{"ref": "local:qwen", "name": "Qwen Coder", "cached": True}]
    repl = Repl(session, None, con, models, {}, "dead-srv", tmp_path, "global", connected=False)

    # Switching model offline updates preferred model setting
    repl.cmd_model(["qwen"])
    assert repl.cfg.model == "local:qwen"


def test_repl_effort_switch_interactively():
    from sarness_cli.modelinfo import Capabilities
    caps = Capabilities("local:coder", True, True, True, True, 65536, 16384)
    cfg = RunConfig("local:coder", caps, "medium", "auto", None, 16384, None, False, 60, 3, "spark", False, "basic")
    ws = Workspace(Path("/tmp"), "auto", None)
    session = Session(ws, cfg)
    repl = Repl(session, None, Console(), [], {}, "spark", Path("/tmp"), "global", connected=True)

    # Change effort directly
    repl.cmd_effort(["high"])
    assert repl.cfg.effort == "high"


# -----------------------------------------------------------------------------
# 7. Real Pseudo-Terminal (PTY) Integration Test
# -----------------------------------------------------------------------------

def test_pty_interactive_menu_arrow_and_enter():
    """Verify that arrow keys actually operate interactive menus in a real pseudo-terminal."""
    master, slave = pty.openpty()

    script = """
import sys
from sarness_cli.menu import MenuItem, TerminalMenu

items = [
    MenuItem("low", "Low"),
    MenuItem("med", "Medium", current=True),
    MenuItem("high", "High"),
]
menu = TerminalMenu("Test PTY Menu", items, show_filter=False)
chosen = menu.run()
print("CHOSEN:" + (chosen.key if chosen else "NONE"))
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    # Allow application to start and render
    time.sleep(0.4)

    # Send Down arrow (\x1b[B) then Enter (\r) through the real master PTY
    os.write(master, b"\x1b[B\r")

    # Read output
    output = b""
    start = time.time()
    while time.time() - start < 3.0:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            chunk = os.read(master, 1024)
            if not chunk:
                break
            output += chunk
            if b"CHOSEN:" in output:
                break

    proc.terminate()
    proc.wait(timeout=2.0)
    os.close(master)

    text = output.decode("utf-8", errors="replace")
    assert "CHOSEN:high" in text
