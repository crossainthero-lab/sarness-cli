"""Permission propagation and dispatch regressions; shell/file effects are real."""
import json
import shlex
import sys
from pathlib import Path

import pytest

from sarness_cli import policy
from sarness_cli.agent import RunConfig, Session
from sarness_cli.cli import build_parser
from sarness_cli.tools import TOOLS, Workspace


def no_prompt(message):
    pytest.fail(f"Unexpected ordinary approval: {message}")


def test_bypass_shell_files_build_install_and_tools(tmp_path):
    ws = Workspace(tmp_path, "bypass", no_prompt)
    ws.execute("write_file", {"path": "calc.py", "content": "value = 1\n"})
    ws.execute("edit_file", {"path": "calc.py", "old_string": "1", "new_string": "2"})
    ws.execute("append_file", {"path": "calc.py", "content": "assert value == 2\n", "expected_length": 10})
    assert "value = 2" in ws.execute("read_file", {"path": "calc.py"})
    ws.execute("write_file", {"path": "Makefile", "content": "test:\n\tpython3 calc.py\n"})
    for command in ("pwd", "make test", "sh -c 'mkdir -p build; cp calc.py build/output.py'", "rm -rf build"):
        assert "exit code: 0" in ws.execute("run_command", {"command": command})
    for command in ("npm install", "python3 -m pip install example", "pnpm build", "./scripts/build.sh"):
        assert ws.permissions.assess("run_command", {"command": command}, ws.root).kind == "allow"
    ws.execute("move_path", {"from": "calc.py", "to": "result.py"})
    ws.execute("delete_path", {"path": "result.py"})
    assert not (tmp_path / "result.py").exists()


def test_missing_context_fails_instead_of_asking(tmp_path):
    with pytest.raises(policy.PermissionContextError, match="Missing permission context"):
        Workspace(tmp_path, confirmer=no_prompt)


def test_delegates_share_effective_policy_and_mode_changes(tmp_path):
    parent = Workspace(tmp_path, "bypass", no_prompt)
    (tmp_path / "sub").mkdir()
    child = parent.delegate(tmp_path / "sub")
    assert child.permissions is parent.permissions
    child.execute("write_file", {"path": "hello", "content": "world"})
    assert "exit code: 0" in child.execute("run_command", {"command": "test -f hello"})
    parent.mode = "ask"
    assert child.mode == "ask"
    with pytest.raises(policy.PermissionContextError, match="outside"):
        parent.delegate(tmp_path.parent)


def test_nested_dispatch_inherits_and_resets_context(tmp_path, monkeypatch):
    parent = Workspace(tmp_path, "bypass", no_prompt)
    def delegated(args):
        child = Workspace(tmp_path, confirmer=no_prompt)
        assert child.permissions is parent.permissions
        return child.execute("write_file", {"path": "child.txt", "content": "inherited"})
    monkeypatch.setattr(parent, "_t_git_status", delegated)
    parent.execute("git_status", {})
    assert policy.ACTIVE_PERMISSIONS.get() is None


def test_shell_carries_scoped_policy_to_real_child_process(tmp_path):
    ws = Workspace(tmp_path, "bypass", no_prompt)
    script = "from pathlib import Path; from sarness_cli.tools import Workspace; from sarness_cli.policy import EffectivePermissions; p=Path.cwd(); w=Workspace(p,permissions=EffectivePermissions.for_launch(p,'ask')); w.execute('write_file',{'path':'child.txt','content':w.mode})"
    result = ws.execute("run_command", {"command": f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"})
    assert "exit code: 0" in result
    assert (tmp_path / "child.txt").read_text() == "bypass"


@pytest.mark.parametrize("raw", ["", "{}", "null", '{"root":"relative","mode":"bypass"}', '{"root":"/tmp","mode":"unknown"}'])
def test_bad_inherited_context_never_falls_back(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("SARNESS_PERMISSION_CONTEXT", raw)
    with pytest.raises(policy.PermissionContextError, match="Invalid inherited"):
        policy.EffectivePermissions.for_launch(tmp_path, "ask")


def test_launch_flag_and_inheritance_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("SARNESS_PERMISSION_CONTEXT", json.dumps({"root": str(tmp_path), "mode": "bypass"}))
    assert policy.EffectivePermissions.for_launch(tmp_path, "ask").mode == "bypass"
    assert policy.EffectivePermissions.for_launch(tmp_path.parent, "auto").mode == "bypass"
    monkeypatch.setenv("SARNESS_PERMISSION_CONTEXT", json.dumps({"root": str(tmp_path), "mode": "auto"}))
    with pytest.raises(policy.PermissionContextError, match="scope"):
        policy.EffectivePermissions.for_launch(tmp_path.parent, "auto")
    monkeypatch.setenv("SARNESS_PERMISSION_CONTEXT", "invalid")
    assert policy.EffectivePermissions.for_launch(tmp_path, "ask", bypass=True).mode == "bypass"


@pytest.mark.parametrize("argv", [["--dangerously-skip-permissions"], ["--dangerously-skip-permissions", "run", "go"], ["resume", "abc", "--dangerously-skip-permissions"], ["--dangerously-skip-permissions", "--resume", "abc"]])
def test_launch_parser_keeps_bypass(argv):
    assert build_parser().parse_args(argv).dangerously_skip_permissions


def test_resume_uses_current_launch_policy_not_saved_messages(tmp_path):
    from sarness_cli.modelinfo import Capabilities
    cfg = RunConfig("local:coder", Capabilities("local:coder", True, True, True, True, 65536, 16384))
    old = Session(Workspace(tmp_path, "ask", no_prompt), cfg)
    old.save()
    ws = Workspace(tmp_path, permissions=policy.EffectivePermissions.for_launch(tmp_path, "ask", bypass=True), confirmer=no_prompt)
    loaded = Session.load(old.id, ws, cfg)
    loaded.ws.execute("write_file", {"path": "resumed.txt", "content": "OK"})
    loaded.save()
    assert json.loads(loaded.path.read_text())["permissions"] == "bypass"
    again = Session.load(old.id, Workspace(tmp_path, "ask", lambda _: False), cfg)
    with pytest.raises(PermissionError):
        again.ws.execute("run_command", {"command": "echo ordinary"})


@pytest.mark.parametrize("command,kind", [("cat ~/.ssh/id_ed25519", "allow"), ("rm -rf /", "allow"), ("mkfs.ext4 /dev/test", "allow"), ("sudo true", "allow"), ("git push origin main", "allow"), ("npm publish", "allow"), ("rm -f /usr/bin/example", "allow"), ("cat .env", "allow"), ("echo changed > /usr/share/example", "allow"), ("echo OK > /dev/null", "allow")])
def test_critical_shell_boundaries(command, kind, tmp_path):
    assert policy.verdict_for_command(command, tmp_path, "bypass").kind == kind


def test_sensitive_file_allowed_without_preview(tmp_path):
    (tmp_path / ".env").write_text("SYNTHETIC")
    ws = Workspace(tmp_path, "bypass", no_prompt)
    ws.execute("edit_file", {"path": ".env", "old_string": "SYNTHETIC", "new_string": "changed"})
    assert "changed" in ws.execute("read_file", {"path": ".env"})


def test_registry_has_no_allow_fallback(tmp_path):
    assert policy.assess("future_unclassified_tool", {}, tmp_path, "auto").kind == "deny"
    assert policy.verdict_for_command("pwd", tmp_path, None).kind == "deny"
    for tool in TOOLS:
        name = tool["function"]["name"]
        assert "No permission policy" not in policy.assess(name, {}, tmp_path, "bypass").reason


def test_normal_approval_with_diff_is_preserved(tmp_path):
    (tmp_path / "a.txt").write_text("before\n")
    prompts = []
    ws = Workspace(tmp_path, "ask", lambda m: prompts.append(m) or False)
    with pytest.raises(PermissionError):
        ws.execute("edit_file", {"path": "a.txt", "old_string": "before", "new_string": "after"})
    assert "-before" in prompts[0] and "+after" in prompts[0]
    assert (tmp_path / "a.txt").read_text() == "before\n"


def test_normal_bulk_search_does_not_follow_escaping_or_secret_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private needle")
    (root / "ordinary.txt").symlink_to(outside)
    ws = Workspace(root, "auto", no_prompt)
    assert "private needle" not in ws.execute("search", {"pattern": "needle"})


def test_remembered_approvals_share_policy_but_not_relative_paths(tmp_path):
    (tmp_path / "sub").mkdir()
    prompts = []
    parent = Workspace(tmp_path, "ask", lambda m: prompts.append(m) or "always")
    child = parent.delegate(tmp_path / "sub")
    parent.execute("run_command", {"command": "true"})
    child.execute("run_command", {"command": "true"})
    assert len(prompts) == 2
    parent.mode = "bypass"
    parent.mode = "ask"
    child.execute("run_command", {"command": "true"})
    assert len(prompts) == 3


@pytest.mark.parametrize('command', [
    "find . -type f -not -path './.git/*' -not -path './.venv/*'",
    "find . -path './.git' -prune -o -type f -print",
    "find . -name '*.key' -print",
    "rg --glob './.git/*' needle .",
    "rg --glob=./.git/* needle .",
    "rg -g./.git/* needle .",
    "grep -r --exclude-dir=.git needle .",
])
def test_search_patterns_are_data_not_file_access(tmp_path, command):
    assert policy.verdict_for_command(command, tmp_path, 'bypass').kind == 'allow'


@pytest.mark.parametrize('command', [
    "find .git -name '*.py'", "rg --glob '*.py' needle .git/config",
    "find . -name '*.py' -exec cat .git/config \\;",
    "rg -- --glob .git/config", "grep --exclude-dir=.git needle .env",
])
def test_pattern_options_never_hide_real_sensitive_operands(tmp_path, command):
    assert policy.verdict_for_command(command, tmp_path, 'auto').kind == 'ask'


@pytest.mark.parametrize("kind", ["ask", "deny"])
def test_bypass_precedes_all_downstream_policy_handlers(tmp_path, monkeypatch, kind):
    ws = Workspace(tmp_path, "bypass", no_prompt)
    monkeypatch.setattr(policy, "assess", lambda *a: policy.Verdict(kind, policy.HIGH, "future boundary"))
    monkeypatch.setattr(ws.permissions, "assess", lambda *a: policy.Verdict(kind, policy.HIGH, "hidden boundary"))
    monkeypatch.setattr(policy, "classify_command", lambda *a: pytest.fail("Classifier ran in bypass"))
    ws.execute("write_file", {"path": ".ssh/allowed", "content": "yes"})
    assert "exit code: 0" in ws.execute("run_command", {"command": "cat .ssh/allowed"})
    assert policy.verdict_for_command("mkfs.ext4 /dev/never-executed", tmp_path, "bypass").kind == "allow"


def test_failure_does_not_drop_active_policy(tmp_path, monkeypatch):
    ws = Workspace(tmp_path, 'bypass', no_prompt)
    def failing(args):
        assert policy.ACTIVE_PERMISSIONS.get() is ws.permissions
        raise RuntimeError('failed helper')
    monkeypatch.setattr(ws, '_t_git_status', failing)
    with pytest.raises(RuntimeError, match='failed helper'):
        ws.execute('git_status', {})
    assert policy.ACTIVE_PERMISSIONS.get() is None
    assert 'exit code: 1' in ws.execute('run_command', {'command': 'false'})
    child = ws.delegate()
    child.execute('write_file', {'path': 'after.txt', 'content': 'still bypass'})
    assert 'exit code: 0' in ws.execute('run_command', {'command': 'cat after.txt'})
