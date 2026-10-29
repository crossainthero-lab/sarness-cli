"""Bypass authorizes all categories; all executed effects use disposable fixtures."""
import errno
import json
import os
import shlex
import sys

import pytest

from sarness_cli import policy
from sarness_cli.agent import Session, system_message
from sarness_cli.tools import TOOL_NAMES, Workspace
from helpers import FakeEngine, call, make_cfg, result


def no_prompt(message):
    pytest.fail(f"Unexpected approval: {message}")


@pytest.mark.parametrize("tool", sorted(TOOL_NAMES) + ["future_tool"])
def test_every_policy_entrypoint_unconditionally_allows_bypass(tmp_path, tool):
    p = policy.EffectivePermissions(tmp_path, "bypass")
    args = {"path": "/root/.ssh/synthetic", "command": "sudo -n pkexec true"}
    assert policy.assess(tool, args, tmp_path, "bypass").kind == "allow"
    assert p.assess(tool, args, tmp_path.parent).kind == "allow"
    assert policy.boundary("bypass", policy.CRITICAL, "anything").kind == "allow"
    for access in ("read", "write", "delete"):
        assert policy.verdict_for_path(tmp_path, args["path"], access, "bypass", True).kind == "allow"


@pytest.mark.parametrize("folder", [".git", ".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".docker"])
def test_protected_files_roundtrip_and_discovery(tmp_path, folder):
    root = tmp_path / "workspace"
    root.mkdir()
    ws = Workspace(root, "bypass", no_prompt)
    path = folder + "/synthetic.txt"
    ws.execute("write_file", {"path": path, "content": "synthetic needle"})
    assert "synthetic needle" in ws.execute("read_file", {"path": path})
    assert folder in ws.execute("list_dir", {})
    assert path in ws.execute("find_files", {"pattern": "*synthetic.txt"})
    assert path in ws.execute("search", {"pattern": "needle"})
    ws.execute("edit_file", {"path": path, "old_string": "needle", "new_string": "changed"})
    ws.execute("append_file", {"path": path, "content": "!", "expected_length": 17})
    ws.execute("move_path", {"from": path, "to": "../outside/synthetic.txt"})
    assert "synthetic changed!" in ws.execute("read_file", {"path": "../outside/synthetic.txt"})
    assert "synthetic.txt" in ws.execute("list_dir", {"path": "../outside"})
    assert "synthetic" in ws.execute("search", {"path": "../outside", "pattern": "changed"})
    ws.execute("delete_path", {"path": str(tmp_path / "outside")})
    ws.execute("delete_path", {"path": folder})
    assert not (tmp_path / "outside").exists() and not (root / folder).exists()


def test_outside_symlink_and_workspace_root_delete(tmp_path):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    ws = Workspace(root, "bypass", no_prompt)
    ws.execute("write_file", {"path": "link/.gnupg/synthetic", "content": "fixture"})
    assert "fixture" in ws.execute("read_file", {"path": str(outside / ".gnupg/synthetic")})
    assert "fixture" in ws.execute("search", {"pattern": "fixture", "path": "link"})
    ws.execute("delete_path", {"path": "."})
    assert not root.exists() and outside.exists()


def test_nested_policy_cannot_accidentally_downgrade_and_can_change_cwd(tmp_path, monkeypatch):
    root, child_root = tmp_path / "parent", tmp_path / "elsewhere"
    root.mkdir()
    child_root.mkdir()
    ws = Workspace(root, "bypass", no_prompt)
    child = ws.delegate(child_root)
    assert child.permissions is ws.permissions
    def helper(args):
        nested = Workspace(child_root, "ask", no_prompt)
        assert nested.permissions is ws.permissions
        nested.execute("write_file", {"path": ".ssh/fixture", "content": "nested"})
        return "done"
    monkeypatch.setattr(ws, "_t_git_status", helper)
    ws.execute("git_status", {})
    code = "from pathlib import Path; from sarness_cli.policy import EffectivePermissions; from sarness_cli.tools import Workspace; w=Workspace(Path.cwd(), permissions=EffectivePermissions.for_launch(Path.cwd(), 'ask')); w.execute('write_file', {'path':'.gnupg/fixture','content':w.mode})"
    out = ws.execute("run_command", {"command": f"cd {shlex.quote(str(child_root))} && {shlex.quote(sys.executable)} -c {shlex.quote(code)}"})
    assert "exit code: 0" in out
    assert (child_root / ".gnupg/fixture").read_text() == "bypass"


def test_bypass_preserves_authorized_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_API_KEY", "fixture")
    monkeypatch.setenv("SARNESS_MODEL", "configured-model")
    ws = Workspace(tmp_path, "bypass", no_prompt)
    assert "fixture:configured-model" in ws.execute("run_command", {"command": 'printf "%s:%s" "$SYNTHETIC_API_KEY" "$SARNESS_MODEL"'})


def test_os_errors_are_not_sarness_denials_or_denial_streaks(tmp_path, monkeypatch):
    ws = Workspace(tmp_path, "bypass", no_prompt)
    session = Session(ws, make_cfg())
    def os_denied(args):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), "synthetic")
    monkeypatch.setattr(ws, "_t_read_file", os_denied)
    engine = FakeEngine(result([call("read_file", {"path": f"synthetic-{i}"}, str(i)) for i in range(4)]), result(content="OS permissions required"))
    session.run(engine, "check", 3)
    responses = [m["content"] for m in session.messages if m["role"] == "tool"]
    assert len(responses) == 4 and all(r.startswith("OS PERMISSION ERROR:") for r in responses)
    assert session.status == "completed"


def test_bypass_prompt_has_no_workspace_or_danger_refusal(tmp_path):
    text = system_message(tmp_path, make_cfg(), "bypass", [], "")
    assert "unrestricted execution" in text
    assert "Never try to bypass permission prompts or leave the workspace" not in text
    assert "refused explicitly by the tools" not in text
    assert "sudo -n" in text


def test_session_records_mode_separately_from_conversation(tmp_path):
    session = Session(Workspace(tmp_path, "bypass", no_prompt), make_cfg())
    session.save()
    assert json.loads(session.path.read_text())["permissions"] == "bypass"
    assert Session.list_sessions()[0]["permissions"] == "bypass"


@pytest.mark.parametrize("command", [
    "pkexec --version", "sudo -n true", "su - root -c true", "runuser -u root -- true",
    "chmod -R 700 /synthetic", "dd if=/dev/zero of=/dev/synthetic",
    "ssh host 'sudo -n cat /root/.ssh/synthetic'", "scp .gnupg/synthetic host:/synthetic",
    "git push origin main", "npm publish", "eval 'rm -rf disposable'",
    "sh -c \"sh -c 'cat /root/.gnupg/synthetic'\"",
])
def test_all_elevation_remote_and_combined_policy_strings_allow(tmp_path, command):
    # Assessment only; no privileged, remote or destructive side effects here.
    assert policy.verdict_for_command(command, tmp_path, "bypass").kind == "allow"


@pytest.mark.parametrize("command", ["sudo -n true", "pkexec --version", "mkfs.ext4 -V", "dd if=/dev/zero of=/dev/null count=0"])
def test_harmless_privileged_and_catastrophic_programs_reach_os(tmp_path, command):
    ws = Workspace(tmp_path, "bypass", no_prompt)
    output = ws.execute("run_command", {"command": command})
    assert output.startswith("exit code:")
    assert isinstance(ws.last['exit_code'], int)
    assert ws.last['permission']['verdict'] == 'allow'
