import json
import os
import subprocess
import sys

import pytest

from sarness_cli import __version__, cli, config
from fake_engine_server import FakeEngineServer, TOKEN, done


@pytest.fixture
def engine():
    servers = []

    def make(turns, **kw):
        s = FakeEngineServer(turns, **kw)
        servers.append(s)
        cfg = config.load_config()
        cfg["profiles"]["fake"] = {"transport": "direct", "url": s.url, "model": "local:coder@Q4"}
        cfg["settings"].update({"server": "fake", "effort": "medium", "permissions": "auto"})
        config.save_config(cfg)
        config.set_secret("fake", TOKEN)
        return s
    yield make
    for s in servers:
        s.stop()


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (p / "test_calc.py").write_text("from calc import add\nassert add(2, 3) == 5, 'add is broken'\nprint('ok')\n")
    subprocess.run(["git", "init", "-q"], cwd=p)
    return p


def run_cli(*argv):
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    return e.value.code


def test_end_to_end_task_edit_test_and_honest_summary(engine, project, capsys):
    srv = engine([
        [done(calls=[("read_file", {"path": "calc.py"})])],
        [done(calls=[("run_command", {"command": "python3 test_calc.py"})])],
        [done(calls=[("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})])],
        [done(calls=[("run_command", {"command": "python3 test_calc.py"})])],
        [{"type": "delta", "content": "Fixed add().", "reasoning": ""}, done(content="Fixed add().")],
    ])
    code = run_cli("run", "-C", str(project), "Fix the failing test")
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "a + b" in (project / "calc.py").read_text()
    assert "Fixed add()." in out.out
    assert "exited 0" in out.err and "calc.py" in out.err                         # summary derived from real results
    first = srv.completions[0]
    assert first["model_ref"] == "local:coder@Q4" and first["params"]["ctx"] == 16384 and len(first["tools"]) >= 10
    assert first["messages"][0]["role"] == "system" and TOKEN not in json.dumps(first)
    tool_results = [m["content"] for m in srv.completions[2]["messages"] if m["role"] == "tool"]
    assert any("exit code: 1" in t and "add is broken" in t for t in tool_results)   # the model saw the genuine failure
    assert len(config.state_dir().joinpath("sessions").glob("*.json").__iter__().__next__().read_text()) > 100


def test_never_evicts_a_model_someone_else_has_loaded(engine, project, capsys):
    srv = engine([[done(content="should never be requested")]],
                 llm_status={"state": "ready", "model_id": "small@Q8", "model_name": "Small Q8", "ctx": 8192, "gpu_layers": 999,
                             "active_streams": 0, "last_used": 1})
    code = run_cli("run", "-C", str(project), "hello")
    assert code == 1 and srv.completions == []
    assert "UNLOAD" in capsys.readouterr().err


def test_loaded_model_is_pinned_to_its_own_context(engine, project):
    srv = engine([[done(content="hi")]],
                 llm_status={"state": "ready", "model_id": "coder@Q4", "model_name": "Coder", "ctx": 32768, "gpu_layers": 777,
                             "active_streams": 0, "last_used": 1})
    assert run_cli("run", "-C", str(project), "hello") == 0
    assert srv.completions[0]["params"]["ctx"] == 32768 and srv.completions[0]["params"]["gpu_layers"] == 777


def test_effort_and_thinking_reach_the_engine_as_documented(engine, project):
    srv = engine([[done(content="a")], [done(content="b")], [done(content="c")]])
    run_cli("run", "-C", str(project), "x", "--effort", "high")
    run_cli("run", "-C", str(project), "x", "--effort", "low", "--max-tokens", "1500")
    run_cli("run", "-C", str(project), "x", "--thinking", "off")
    p = [c["params"] for c in srv.completions]
    assert p[0]["thinking"] is True and p[0]["max_tokens"] == int(16384 * 0.45)   # high: thinking on; budget capped to keep 55% of the window for the prompt
    assert p[1]["thinking"] is False and p[1]["max_tokens"] == 1500            # low: off, explicit user cap respected
    assert p[2]["thinking"] is False


def test_bypass_executes_formerly_refused_commands(engine, project, capsys):
    srv = engine([
        [done(calls=[("run_command", {"command": "git reset --hard; rm -rf build"}), ("run_command", {"command": "git push origin main"}),
                     ("run_command", {"command": "mkdir -p .ssh; printf synthetic > .ssh/fixture; cat .ssh/fixture"})])],
        [done(content="done")]])
    assert run_cli("run", "-C", str(project), "go", "--dangerously-skip-permissions") == 0
    err = capsys.readouterr().err
    assert "BYPASS PERMISSIONS" in err
    results = [m["content"] for m in srv.completions[1]["messages"] if m["role"] == "tool"]
    assert results[0].startswith("exit code:")                                 # routine + local-destructive ran with no prompt
    assert results[1].startswith("exit code:")
    assert "approval needed" not in err
    assert results[2].startswith("exit code: 0") and "synthetic" in results[2]


def test_modes_are_enforced_by_the_tool_layer(engine, project):
    srv = engine([[done(calls=[("write_file", {"path": "n.txt", "content": "x"})])], [done(content="ok")]])
    assert run_cli("run", "-C", str(project), "go", "--mode", "ask") == 0           # no tty: approval impossible -> denied
    assert not (project / "n.txt").exists()
    assert any(m["content"].startswith("DENIED") for m in srv.completions[1]["messages"] if m["role"] == "tool")


def test_bypass_cannot_be_selected_or_saved_through_config_or_mode(engine, project, capsys):
    engine([])
    assert run_cli("run", "-C", str(project), "x", "--mode", "bypass") == 1
    assert "dangerously-skip-permissions" in capsys.readouterr().err
    assert run_cli("config", "set", "permissions", "bypass") == 1
    assert config.load_config()["settings"]["permissions"] == "auto"


def test_models_command_does_not_invent_capabilities(engine, capsys):
    engine([])
    assert run_cli("models") == 0
    out = capsys.readouterr().out
    assert "local:coder@Q4" in out and "tools: unverified" in out and "reasoning" in out and "ctx max 65,536" in out
    assert run_cli("models", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["models"][0]["reasoning"] is True and data["models"][1]["reasoning"] is False


def test_probe_never_loads_a_model(engine, capsys):
    srv = engine([])
    assert run_cli("models", "--probe") == 0
    assert srv.completions == [] and "probe skipped" in capsys.readouterr().out


def test_model_command_validates_and_saves_per_project(engine, project, capsys):
    engine([])
    assert run_cli("model", "small", "-C", str(project)) == 0
    assert config.load_config()["profiles"]["fake"]["model"] == "local:small@Q8"      # belongs to the server, not global
    assert "model" not in config.load_config()["settings"]
    assert run_cli("model", "coder", "--project", "-C", str(project)) == 0
    s, o = config.effective_settings(project)
    assert s["model"] == "local:coder@Q4" and o["model"] == "project (private)"
    assert run_cli("model", "nonexistent-model") == 1


def test_config_and_server_commands(engine, capsys):
    engine([])
    assert run_cli("config", "set", "max_steps", "25") == 0
    assert run_cli("config", "set", "effort", "ludicrous") == 1
    assert run_cli("config") == 0
    out = capsys.readouterr().out
    assert "max_steps" in out and "25" in out and TOKEN not in out and "token: stored" in out
    assert run_cli("server", "list") == 0 and run_cli("server", "use", "nope") == 1


def test_doctor_reports_healthy_connection(engine, capsys, project):
    engine([])
    code = run_cli("doctor", "-C", str(project))
    out = capsys.readouterr().out
    assert code == 0 and "engine reachable" in out and "token + /v1/agent/models" in out and "default model installed" in out


def test_doctor_fails_loudly_on_wrong_token(engine, capsys, project):
    engine([])
    config.set_secret("fake", "wrong")
    assert run_cli("doctor", "-C", str(project)) == 1
    assert "✗" in capsys.readouterr().out


def test_version_and_help():
    out = subprocess.run([sys.executable, "-m", "sarness_cli", "--help"], capture_output=True, text=True, env={**os.environ})
    assert out.returncode == 0 and "--dangerously-skip-permissions" in out.stdout and "--effort" in out.stdout
    v = subprocess.run([sys.executable, "-m", "sarness_cli", "--version"], capture_output=True, text=True)
    assert v.stdout.strip() == f"sarness-cli {__version__}"


def test_resume_after_crash_does_not_replay_side_effects(engine, project):
    srv = engine([
        [done(calls=[("write_file", {"path": "marker.txt", "content": "1"})])],
        [{"type": "delta", "content": "partial", "reasoning": ""}],     # stream dies mid-turn (no `done`): no retry budget -> blocked
    ])
    config.update_settings({"retries": 0})
    assert run_cli("run", "-C", str(project), "do it") == 1
    sid = next(config.state_dir().joinpath("sessions").glob("*.json")).stem
    (project / "marker.txt").write_text("edited-by-user")
    srv.turns += [[done(content="continuing")]]
    assert run_cli("resume", sid, "-C", str(project)) == 0
    assert (project / "marker.txt").read_text() == "edited-by-user"     # nothing was replayed over the user's edit


def test_session_dir_and_workspace_settings_are_honoured(engine, project, tmp_path, monkeypatch):
    engine([[done(content="ok")]])
    custom = tmp_path / "my-sessions"
    assert run_cli("config", "set", "session_dir", str(custom)) == 0
    assert run_cli("config", "set", "workspace", str(project)) == 0
    monkeypatch.chdir(tmp_path)                      # not inside the project: the workspace setting decides
    assert run_cli("run", "hello") == 0
    assert list(custom.glob("*.json")) and not list(config.state_dir().joinpath("sessions").glob("*.json"))


def test_blocked_runs_still_print_the_independent_summary(engine, project, capsys):
    engine([[done(calls=[("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})])]] +
           [[done(calls=[("list_dir", {})])]] * 8)
    assert run_cli("run", "-C", str(project), "go") == 1
    err = capsys.readouterr().err
    assert "stopped:" in err and "calc.py" in err and "NOT RUN" in err


def test_consented_swap_proceeds_and_later_foreign_swap_is_still_caught(engine, project, capsys):
    idle_other = {"state": "ready", "model_id": "small@Q8", "model_name": "Small Q8", "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 1}
    srv = engine([[done(content="ok")]], llm_status=idle_other)
    assert run_cli("run", "-C", str(project), "hi", "--allow-model-swap") == 0          # consented: the old model is expected at turn 1
    assert srv.completions and srv.completions[0]["params"]["ctx"] == 16384
    # same setup but WITHOUT consent still refuses before any request
    srv2 = engine([[done(content="no")]], llm_status=idle_other)
    assert run_cli("run", "-C", str(project), "hi") == 1 and srv2.completions == []


def test_resume_restores_bypass_unless_user_explicitly_selects_normal(engine, project, capsys):
    from helpers import make_cfg
    from sarness_cli.agent import Session
    from sarness_cli.tools import Workspace
    saved = Session(Workspace(project, "bypass"), make_cfg(model="local:coder@Q4"))
    saved.save()
    srv = engine([
        [done(calls=[("write_file", {"path": ".gnupg/synthetic", "content": "restored"}),
                     ("run_command", {"command": "cat .gnupg/synthetic"})])],
        [done(content="done")],
        [done(calls=[("write_file", {"path": "normal.txt", "content": "must ask"})])],
        [done(content="done")],
    ])
    assert run_cli("resume", saved.id, "continue") == 0
    assert "BYPASS PERMISSIONS" in capsys.readouterr().err
    assert (project / ".gnupg/synthetic").read_text() == "restored"
    assert run_cli("resume", saved.id, "continue", "--mode", "ask") == 0
    assert not (project / "normal.txt").exists()
    assert "approval needed" in capsys.readouterr().err
    assert "Permissions: Bypass" in srv.completions[0]["messages"][0]["content"]
