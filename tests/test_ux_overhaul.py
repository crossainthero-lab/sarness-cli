"""Automated tests for Sarness CLI Permissions, Session Resume, and UX Overhaul."""
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from prompt_toolkit.formatted_text import FormattedText

from sarness_cli import cli, config
from sarness_cli.agent import RunConfig, Session
from sarness_cli.paste import PASTE_TOKEN_RE, PasteLexer, PasteManager
from sarness_cli.policy import assess, verdict_for_command
from sarness_cli.repl import Repl
from sarness_cli.tools import Workspace
from sarness_cli.ui import Console, Style, banner_bypass, warning_bypass
from fake_engine_server import FakeEngineServer, TOKEN, done


# -----------------------------------------------------------------------------
# 1. Permission Bypass Flag and Mode Tests
# -----------------------------------------------------------------------------

def test_dangerously_skip_permissions_flag_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["--dangerously-skip-permissions"])
    assert args.dangerously_skip_permissions is True


def test_permission_bypass_policy_behavior(tmp_path):
    # Routine shell command in ask mode requires confirmation
    v_ask = verdict_for_command("ls -la", tmp_path, mode="ask")
    assert v_ask.kind == "ask"

    # Routine shell command in bypass mode is allowed immediately
    v_bypass = verdict_for_command("ls -la", tmp_path, mode="bypass")
    assert v_bypass.kind == "allow"

    # Destructive local command in ask asks, in bypass is allowed
    v_dest_ask = verdict_for_command("git reset --hard", tmp_path, mode="ask")
    assert v_dest_ask.kind == "ask"
    v_dest_bypass = verdict_for_command("git reset --hard", tmp_path, mode="bypass")
    assert v_dest_bypass.kind == "allow"

    # Policy assessment only: bypass permits credential paths
    v_cred = verdict_for_command("cat ~/.ssh/id_rsa", tmp_path, mode="bypass")
    assert v_cred.kind == "allow"

    # Policy assessment only: never execute this command in a test
    v_cat = verdict_for_command("rm -rf /", tmp_path, mode="bypass")
    assert v_cat.kind == "allow"


def test_banner_bypass_styling():
    st = Style(enabled=True)
    banner = banner_bypass(st)
    assert "BYPASS PERMISSIONS is ON" in banner
    # Amber/bold styling
    assert "\033[38;5;214m" in banner or "\033[33m" in banner


# -----------------------------------------------------------------------------
# 2. Session Persistence and Resume Tests
# -----------------------------------------------------------------------------

def test_resume_parser_flags():
    parser = cli.build_parser()
    # No resume flag
    args = parser.parse_args([])
    assert args.resume is None

    # --resume without arg (interactive resume)
    args = parser.parse_args(["--resume"])
    assert args.resume == ""

    # --resume with specific session id
    args = parser.parse_args(["--resume", "sess12345"])
    assert args.resume == "sess12345"

    # -r shorthand
    args = parser.parse_args(["-r", "sessABC"])
    assert args.resume == "sessABC"

    # resume subcommand
    args = parser.parse_args(["resume", "sessXYZ"])
    assert args.command == "resume"
    assert args.session == "sessXYZ"


def test_session_save_and_load_restores_model_effort_thinking(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))

    ws = Workspace(tmp_path, "ask", lambda msg: True)
    from sarness_cli.modelinfo import Capabilities
    caps = Capabilities(ref="qwen", local=True, reasoning=True, tools_reported=True, tools_probed=True, ctx_max=32768, ctx_default=8192)
    cfg = RunConfig(model="qwen-2.5-coder-32b", caps=caps, effort="high", thinking="on")

    session = Session(ws, cfg, ui=Console())
    session.messages.append({"role": "user", "content": "Initial user task"})
    session.messages.append({"role": "assistant", "content": "Assistant plan and response"})
    session.commands.append({"command": "pytest", "exit": 0, "step": 1})
    session.files["main.py"] = {"added": 12, "removed": 2}
    session.ws.plan = [{"status": "done", "text": "Step 1"}]
    session.save()

    # Load session back with a default/blank RunConfig
    loaded_cfg = RunConfig(model="default-model", caps=caps, effort="low", thinking="off")
    resumed = Session.load(session.id, ws, loaded_cfg, Console())

    assert resumed.id == session.id
    assert len(resumed.messages) == 3
    assert resumed.commands[0]["command"] == "pytest"
    assert resumed.files["main.py"]["added"] == 12
    assert resumed.ws.plan[0]["text"] == "Step 1"

    # Model, effort, and thinking are restored from the persisted session
    assert loaded_cfg.model == "qwen-2.5-coder-32b"
    assert loaded_cfg.effort == "high"
    assert loaded_cfg.thinking == "on"


# -----------------------------------------------------------------------------
# 3. Collapsed Pasted Prompts Tests
# -----------------------------------------------------------------------------

def test_paste_manager_thresholds():
    pm = PasteManager(char_threshold=200, line_threshold=4)

    # Short single-line paste should not collapse
    assert not pm.should_collapse("https://example.com/api/test")
    assert not pm.should_collapse("ls -la /tmp")

    # Short multiline paste with >= 4 lines should collapse
    assert pm.should_collapse("line1\nline2\nline3\nline4")

    # Large single-line paste (>= 200 chars) should collapse
    large_text = "A" * 250
    assert pm.should_collapse(large_text)


def test_paste_manager_register_and_expand():
    pm = PasteManager()
    full_text = "First line of massive paste\nSecond line\nThird line\nFourth line\nFifth line"
    token = pm.register(full_text)

    # Format check: ▸ Pasted text · {len} characters · {lines} lines #P1
    assert "▸ Pasted text" in token
    assert f"{len(full_text):,} characters" in token
    assert "5 lines" in token
    assert "#P1" in token

    # Expansion in isolation
    assert pm.expand(token) == full_text

    # Expansion with surrounding text
    composer_input = f"Look at this prompt: {token} Can you rewrite it?"
    expected = f"Look at this prompt: {full_text} Can you rewrite it?"
    assert pm.expand(composer_input) == expected


def test_paste_manager_multiple_blocks():
    pm = PasteManager()
    paste1 = "def foo():\n    return 42\n# line 3\n# line 4"
    paste2 = "ERROR: Connection reset by peer\n  at socket.read\n  at http.request\n  at main.run"

    token1 = pm.register(paste1)
    token2 = pm.register(paste2)

    assert "#P1" in token1
    assert "#P2" in token2

    composite = f"Code snippet:\n{token1}\n\nError traceback:\n{token2}\n\nPlease debug."
    expanded = pm.expand(composite)

    assert paste1 in expanded
    assert paste2 in expanded
    assert "Please debug." in expanded


def test_paste_manager_expansion_in_buffer():
    class DummyBuffer:
        def __init__(self, text):
            self.text = text
            self.cursor_position = len(text)

    pm = PasteManager()
    raw = "import os\nimport sys\nimport time\nimport math"
    token = pm.register(raw)

    buf = DummyBuffer(f"Check this: {token}")
    did_expand = pm.expand_buffer(buf)

    assert did_expand is True
    assert buf.text == f"Check this: {raw}"


def test_paste_lexer_highlighting():
    pm = PasteManager()
    token = pm.register("line 1\nline 2\nline 3\nline 4")

    lexer = PasteLexer()
    class DummyDoc:
        lines = [f"Prefix text {token} Suffix text"]

    get_line = lexer.lex_document(DummyDoc())
    tokens = get_line(0)

    # Should contain unstyled prefix, styled pasted-block, and unstyled suffix
    styles = [t[0] for t in tokens]
    texts = [t[1] for t in tokens]

    assert "class:pasted-block" in styles
    block_idx = styles.index("class:pasted-block")
    assert texts[block_idx] == token


# -----------------------------------------------------------------------------
# 4. Restrained Terminal Colour System Tests
# -----------------------------------------------------------------------------

def test_restrained_palette_colors():
    st = Style(enabled=True)

    # Blue: interactive elements, selections, active controls
    b = st.blue("interactive")
    assert "\033[38;5;39m" in b

    # Amber: warnings and notices
    a = st.amber("warning")
    assert "\033[38;5;214m" in a

    # Green: success and connected status
    g = st.green("connected")
    assert "\033[32m" in g

    # Red: errors
    r = st.red("failure")
    assert "\033[31m" in r

    # White / Off-white: primary content
    w = st.white("primary content")
    assert "\033[97m" in w

    # Muted grey: secondary info, metadata, reasoning
    m = st.dim("reasoning...")
    assert "\033[90m" in m


# -----------------------------------------------------------------------------
# 5. Persistent Status Bar Tests
# -----------------------------------------------------------------------------

def test_persistent_status_bar_rendering(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    ws = Workspace(tmp_path, "bypass", lambda msg: True)
    from sarness_cli.modelinfo import Capabilities
    caps = Capabilities(ref="qwen", local=True, reasoning=True, tools_reported=True, tools_probed=True, ctx_max=32768, ctx_default=8192)
    cfg = RunConfig(model="qwen3.8-27b", caps=caps, effort="high", thinking="auto")
    session = Session(ws, cfg, ui=Console())

    repl = Repl(
        session=session,
        engine=None,
        console=Console(),
        models=[],
        settings={"context_tokens": 32768},
        server="spark",
        workspace=tmp_path,
        scope_pref="user",
        connected=True,
    )

    # Simulate DGX Spark unified memory telemetry (121.7 GB RAM and unified memory)
    repl._telemetry = {
        "ram_used_gb": 36.7,
        "ram_total_gb": 121.7,
        "vram_used_gb": 36.7,
        "vram_total_gb": 121.7,
        "unified": True,
        "ts": time.monotonic(),
    }

    # Call toolbar render directly
    tb_content = repl.render_toolbar()
    assert isinstance(tb_content, FormattedText)

    plain_text = "".join(part[1] for part in tb_content)
    # Expected layout: Qwen3.8 27B · High effort · spark ● · RAM 36.7/121.7 GB · Bypass
    assert "qwen3.8-27b" in plain_text
    assert "High effort" in plain_text
    assert "spark ●" in plain_text
    assert "RAM 36.7/121.7 GB" in plain_text
    # Should not duplicate with VRAM since unified is True
    assert "VRAM" not in plain_text
    assert "BYPASS" in plain_text


# -----------------------------------------------------------------------------
# 6. Interactive CLI & PTY Launch Tests
# -----------------------------------------------------------------------------

def test_pty_sarness_bypass_flag(tmp_path):
    """Verify that launching sarness with --dangerously-skip-permissions in a PTY displays the bypass warning banner."""
    master, slave = pty.openpty()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / ".config")
    env["XDG_STATE_HOME"] = str(tmp_path / ".local" / "state")
    env["PYTHONPATH"] = str(Path(__file__).parent.parent.resolve())
    env["TERM"] = "xterm-256color"

    # Configure profile
    (tmp_path / ".config" / "sarness-cli").mkdir(parents=True, exist_ok=True)
    cfg_file = tmp_path / ".config" / "sarness-cli" / "config.json"
    cfg_file.write_text(json.dumps({
        "version": 2,
        "profiles": {
            "fake": {"transport": "direct", "url": "http://127.0.0.1:9999", "model": "local:coder@Q4"}
        },
        "settings": {"server": "fake", "permissions": "ask", "runtime": "native"}
    }))
    cfg_file.chmod(0o600)
    secrets_file = tmp_path / ".config" / "sarness-cli" / "secrets.json"
    secrets_file.write_text(json.dumps({"fake": TOKEN}))
    secrets_file.chmod(0o600)

    proc = subprocess.Popen(
        [sys.executable, "-m", "sarness_cli.cli", "--dangerously-skip-permissions"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=tmp_path,
        env=env,
        close_fds=True,
    )
    os.close(slave)

    output = b""
    start = time.time()
    while time.time() - start < 3.0:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                output += chunk
                if b"sarness" in output:
                    break
            except OSError:
                break

    proc.terminate()
    try:
        proc.wait(timeout=1.0)
    except Exception:
        proc.kill()
    os.close(master)

    decoded = output.decode("utf-8", errors="replace")
    assert "BYPASS PERMISSIONS is ON" in decoded


def test_pty_sarness_resume_specific_session(tmp_path):
    """Verify that sarness --resume <id> resumes the session in a PTY."""
    master, slave = pty.openpty()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / ".config")
    env["XDG_STATE_HOME"] = str(tmp_path / ".local" / "state")
    env["PYTHONPATH"] = str(Path(__file__).parent.parent.resolve())
    env["TERM"] = "xterm-256color"

    (tmp_path / ".config" / "sarness-cli").mkdir(parents=True, exist_ok=True)
    cfg_file = tmp_path / ".config" / "sarness-cli" / "config.json"
    cfg_file.write_text(json.dumps({
        "version": 2,
        "profiles": {
            "fake": {"transport": "direct", "url": "http://127.0.0.1:9999", "model": "local:coder@Q4"}
        },
        "settings": {"server": "fake", "permissions": "ask", "runtime": "native"}
    }))
    cfg_file.chmod(0o600)
    secrets_file = tmp_path / ".config" / "sarness-cli" / "secrets.json"
    secrets_file.write_text(json.dumps({"fake": TOKEN}))
    secrets_file.chmod(0o600)

    # Create a saved session
    sess_dir = tmp_path / ".local" / "state" / "sarness-cli" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    sid = "1234567890abcdef1234"
    sess_file = sess_dir / f"{sid}.json"
    sess_file.write_text(json.dumps({
        "version": 2,
        "id": sid,
        "name": "my-test-feature",
        "workspace": str(tmp_path.resolve()),
        "model": "local:coder@Q4",
        "effort": "max",
        "thinking": "on",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "add feature"},
            {"role": "assistant", "content": "added feature"}
        ],
        "status": "completed",
        "plan": [],
        "files": {},
        "commands": [],
        "created": time.time(),
        "updated": time.time(),
    }))
    sess_file.chmod(0o600)

    proc = subprocess.Popen(
        [sys.executable, "-m", "sarness_cli.cli", "--resume", sid],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=tmp_path,
        env=env,
        close_fds=True,
    )
    os.close(slave)

    output = b""
    start = time.time()
    while time.time() - start < 3.0:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                output += chunk
                if b"sarness" in output:
                    break
            except OSError:
                break

    proc.terminate()
    try:
        proc.wait(timeout=1.0)
    except Exception:
        proc.kill()
    os.close(master)

    decoded = output.decode("utf-8", errors="replace")
    assert sid[:8] in decoded
    assert "Resumed session" in decoded or "3 messages" in decoded
