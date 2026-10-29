import os
import sys
import time

import pytest

from sarness_cli.tools import Workspace, ToolArgError, validate_args


def ws_for(tmp_path, mode="auto", approve=lambda _: True, **kw):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return Workspace(root, mode, approve, **kw), root


def test_file_tools_roundtrip_and_diff_info(tmp_path):
    ws, root = ws_for(tmp_path)
    assert "Created" in ws.execute("write_file", {"path": "a.py", "content": "x = 1\n"})
    assert "Syntax check: OK" in ws.execute("edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert ws.last["added"] == 1 and ws.last["removed"] == 1 and "+x = 2" in ws.last["diff"]
    assert "2: " not in ws.execute("read_file", {"path": "a.py"}) and "1: x = 2" in ws.execute("read_file", {"path": "a.py"})
    assert "WARNING: Python syntax error" in ws.execute("write_file", {"path": "bad.py", "content": "def (:\n"})


def test_append_is_idempotent_and_detects_mismatch(tmp_path):
    ws, root = ws_for(tmp_path)
    ws.execute("write_file", {"path": "a.txt", "content": "hello"})
    with pytest.raises(ValueError, match="position mismatch"):
        ws.execute("append_file", {"path": "a.txt", "content": " world", "expected_length": 4})
    ws.execute("append_file", {"path": "a.txt", "content": " world", "expected_length": 5})
    # the model retries the same append after a lost response: must not duplicate
    assert "Already applied" in ws.execute("append_file", {"path": "a.txt", "content": " world", "expected_length": 5})
    assert (root / "a.txt").read_text() == "hello world"
    assert "identical content" in ws.execute("write_file", {"path": "a.txt", "content": "hello world"})


def test_placeholder_elision_is_refused(tmp_path):
    ws, root = ws_for(tmp_path)
    with pytest.raises(ValueError, match="elision placeholder"):
        ws.execute("write_file", {"path": "a.py", "content": "def f():\n    pass\n# ... rest of the file unchanged\n"})
    assert not (root / "a.py").exists()


def test_string_integers_are_coerced_but_wrong_types_rejected():
    assert validate_args("read_file", {"path": "a", "offset": "5"})["offset"] == 5
    assert validate_args("edit_file", {"path": "a", "old_string": "x", "new_string": "y", "replace_all": "true"})["replace_all"] is True
    for name, args in [("read_file", {"path": 5}), ("read_file", {"path": "a", "offset": "x"}), ("nope", {}), ("read_file", {}), ("read_file", {"path": "a", "zzz": 1}),
                       ("update_plan", {"items": "not a list"})]:
        with pytest.raises(ToolArgError):
            validate_args(name, args)


def test_sensitive_and_symlink_and_traversal_denied(tmp_path):
    ws, root = ws_for(tmp_path, approve=lambda _: False)  # even if asked, user says no
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (root / "jump").symlink_to(outside)
    (root / ".env").write_text("TOKEN=abc")
    for name in ["../outside", "jump", "/etc/passwd", "../../etc/hosts"]:
        with pytest.raises(PermissionError):
            ws.execute("read_file", {"path": name})
    for name in [".env", ".git/config"]:
        with pytest.raises(PermissionError):  # asked -> user declines
            ws.execute("read_file", {"path": name})
    with pytest.raises(PermissionError):
        ws.execute("write_file", {"path": "jump", "content": "overwrite"})
    assert outside.read_text() == "secret"


def test_list_dir_and_search_hide_sensitive_and_ignored(tmp_path):
    ws, root = ws_for(tmp_path)
    (root / ".env").write_text("SECRET=1")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("needle")
    (root / "src.py").write_text("needle here\n")
    listing = ws.execute("list_dir", {})
    assert "src.py" in listing and ".env" not in listing and "node_modules" not in listing
    found = ws.execute("search", {"pattern": "needle"})
    assert "src.py:1" in found and "node_modules" not in found
    assert "src.py" in ws.execute("find_files", {"pattern": "*.py"})


def test_long_command_output_is_bounded_and_logged(tmp_path):
    logs = tmp_path / "logs"
    ws, root = ws_for(tmp_path, log_dir=logs)
    res = ws.execute("run_command", {"command": f'{sys.executable} -c "print(\'x\'*200000)"', "timeout_s": 20})
    assert "exit code: 0;" in res and len(res) < 13000
    assert "full log:" in res
    assert (logs / "cmd-001.log").stat().st_size > 190000
    assert ws.last["exit_code"] == 0


def test_command_timeout_kills_process_group(tmp_path):
    ws, root = ws_for(tmp_path)
    t0 = time.time()
    res = ws.execute("run_command", {"command": "sleep 30 & sleep 30", "timeout_s": 1})
    assert time.time() - t0 < 8 and "TIMED OUT" in res and ws.last["timed_out"]


def test_command_env_is_scrubbed_stdin_closed_and_secrets_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue1234567890")
    monkeypatch.setenv("SARNESS_TOKEN", "tok")
    ws, root = ws_for(tmp_path, redact=["hunter2hunter2"])
    out = ws.execute("run_command", {"command": "env; echo hunter2hunter2; cat"})
    assert "supersecretvalue" not in out and "SARNESS_TOKEN" not in out and "hunter2hunter2" not in out and "[REDACTED]" in out
    assert "exit code: 0" in out  # `cat` saw EOF instead of hanging on the terminal


def test_delete_and_move(tmp_path):
    ws, root = ws_for(tmp_path)
    (root / "a.txt").write_text("x")
    ws.execute("move_path", {"from": "a.txt", "to": "sub/b.txt"})
    assert (root / "sub/b.txt").exists() and not (root / "a.txt").exists()
    ws.execute("delete_path", {"path": "sub"})
    assert not (root / "sub").exists()
    with pytest.raises(PermissionError):
        ws.execute("delete_path", {"path": "."})


def test_plan_tool(tmp_path):
    ws, _ = ws_for(tmp_path)
    ws.execute("update_plan", {"items": [{"text": "a", "status": "doing"}]})
    assert ws.plan == [{"text": "a", "status": "doing"}]
    with pytest.raises(ValueError):
        ws.execute("update_plan", {"items": [{"text": "a", "status": "weird"}]})
