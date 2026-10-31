"""The multi-stage engineering task (ssh to the test server, inspect, fix, test, deploy, verify) through the real bridge,
with a scripted model that has the habits seen in the Trinodal sessions and three forced compactions."""
import pytest

from guardkit import call
from sarness_cli.guard import Ledger
from sarness_cli.guard.ledger import normalize_command
from sarness_cli.openai_bridge import BridgeServer
from scenario_rig import CORE_FIXED, HOST, TARGET, BridgeHarness, Persona, Sandbox, ScenarioEngine
from test_openai_bridge import MODEL


@pytest.fixture
def run_scenario(tmp_path, monkeypatch):
    servers = []

    def go(*, stubborn=False, mode="full-access", guard=True, keep_request=True, compact_at=("inspected", "tested", "deployed"),
           ask_first=True, session="ses_scenario"):
        sb = Sandbox(tmp_path / f"sb{len(servers)}")
        sb.apply_env(monkeypatch)
        persona = Persona("tui", stubborn=stubborn, proj=str(sb.proj), ask_first=ask_first)
        engine = ScenarioEngine(persona)
        server = BridgeServer(engine, [MODEL], mode=mode, workspace=sb.proj, guard=guard)
        server.start()
        servers.append(server)
        harness = BridgeHarness(server, sb, persona, session=session, compact_at=compact_at, keep_request=keep_request)
        final = harness.run()
        return sb, persona, engine, server, harness, final
    yield go
    for s in servers:
        s.shutdown()
        s.server_close()


def duplicate_diagnostics(harness):
    """Shell commands that ran more than once with no reason: same normalised command, excluding test runs and deploys."""
    seen: dict[str, int] = {}
    for name, args in harness.executed:
        if name == "shell" and "pytest" not in args["command"] and "deploy.sh" not in args["command"]:
            key = normalize_command(args["command"])
            seen[key] = seen.get(key, 0) + 1
    return {k: n for k, n in seen.items() if n > 1}


def assert_task_really_done(sb, led):
    assert sb.local_core() == CORE_FIXED, "the source was really changed"
    assert sb.tests_pass_now(), "the tests really pass on the changed code"
    assert sb.remote_core() == sb.local_core(), "the deployment really carries the change"
    assert sb.restarted(), "the service was really restarted"
    assert led.unmet() == [], led.unmet()


def test_the_whole_task_completes_through_three_compactions_with_real_evidence(run_scenario):
    sb, persona, engine, server, h, final = run_scenario()
    led = Ledger.open("ses_scenario")
    assert final.startswith("Done."), final
    assert_task_really_done(sb, led)
    assert h.compactions == 3 and len(led.d["compactions"]) == 3
    kinds = [i["kind"] for i in led.d["interventions"]]
    assert kinds.count("confirmation") == 1, "the opening 'Should I proceed?' was continued, once"
    assert kinds.count("ssh-repair") == 1 and kinds.count("premature-complete") == 2
    assert "known-failure" in kinds and "duplicate" in kinds
    assert "give-up" not in kinds
    # the glued command ran in repaired form; the doubted git status never ran a second time
    assert led.interventions("ssh-repair")[0]["detail"].startswith("`sshfylume@")
    git_status = [n for n, a in h.executed if n == "shell" and "git status" in a["command"]]
    assert len(git_status) == 1
    assert duplicate_diagnostics(h) == {}, duplicate_diagnostics(h)
    # tests ran against the changed code, deployment verified independently
    assert led.d["tests"][-1]["exit"] == 0 and led.d["tests"][-1]["rev"] == led.rev
    assert led.d["deploys"][-1]["verified"]
    assert led.d["phase"] == "done"


def test_verified_findings_are_in_front_of_the_model_after_every_compaction(run_scenario):
    sb, persona, engine, server, h, final = run_scenario()
    after_compaction = [msgs for msgs in engine.calls if any("<conversation-checkpoint>" in str(m.get("content")) for m in msgs)]
    assert len({len(m) for m in after_compaction}) >= 1 and after_compaction
    notes = [m[-1]["content"] for m in after_compaction if str(m[-1].get("content", "")).startswith("[sarness-guard] TASK STATE")]
    assert len(notes) >= 3
    assert all("SSH into fylume@testhost" in n and "STILL IN FORCE" in n and "NEXT ACTION" in n for n in notes)
    assert any("systemctl --user" in n and "deployment copy" in n for n in notes[1:])
    assert any("Files changed" in n for n in notes)
    assert any("Deployments:" in n and "NOT yet verified" in n for n in notes), "the note says deployment is unverified until verified"
    assert any("(current)" in n for n in notes), "the note records that tests ran against the current code"


def test_a_model_that_ignores_the_note_and_repeats_old_diagnostics_is_stopped_each_time(run_scenario):
    sb, persona, engine, server, h, final = run_scenario(stubborn=True)
    led = Ledger.open("ses_scenario")
    assert final.startswith("Done.")
    assert_task_really_done(sb, led)
    scope_refusals = led.interventions("systemd-scope")
    assert len(scope_refusals) == 3, "one refusal after each of the three compactions"
    system_scope_runs = [a["command"] for n, a in h.executed if n == "shell" and "systemctl status" in a["command"]]
    assert len(system_scope_runs) == 1, "the wrong systemd scope ran once, before it was known, never again"


def test_without_the_guard_the_original_failure_reproduces(run_scenario):
    sb, persona, engine, server, h, final = run_scenario(guard=False)
    assert "Should I proceed with the SSH connection now?" in final, "the model's question ended the run"
    assert h.executed == [] and sb.local_core() != CORE_FIXED, "nothing was done"


def test_without_the_guard_compaction_makes_the_model_repeat_its_diagnostics(run_scenario):
    sb, persona, engine, server, h, final = run_scenario(guard=False, ask_first=False, stubborn=True)
    assert duplicate_diagnostics(h), "diagnostics were repeated after compaction when nothing carried the findings across"


def test_even_if_the_terminal_loses_the_users_request_the_ledger_keeps_it(run_scenario):
    sb, persona, engine, server, h, final = run_scenario(keep_request=False)
    led = Ledger.open("ses_scenario")
    assert final.startswith("Done.")
    assert_task_really_done(sb, led)
    note = next(m[-1]["content"] for m in engine.calls[::-1] if str(m[-1].get("content", "")).startswith("[sarness-guard] TASK STATE"))
    assert "SSH into fylume@testhost" in note


@pytest.mark.parametrize("mode", ["ask", "auto", "full-access", "bypass"])
def test_every_permission_mode_completes_and_states_who_approves(run_scenario, mode):
    sb, persona, engine, server, h, final = run_scenario(mode=mode)
    led = Ledger.open("ses_scenario")
    assert final.startswith("Done.")
    assert_task_really_done(sb, led)
    notes = [m[-1]["content"] for m in engine.calls if str(m[-1].get("content", "")).startswith("[sarness-guard] TASK STATE")]
    nudges = [m[-1]["content"] for m in engine.calls if str(m[-1].get("content", "")).startswith("[sarness-guard] The user")]
    if mode == "bypass":
        assert all("none are required" in n for n in notes) and all("No approval is needed" in n for n in nudges)
    else:
        assert all("asks the user" in n for n in notes) and all("approval itself" in n for n in nudges), \
            "outside bypass the runtime still asks for approvals; the guard only stops the chat question"


def test_the_guard_never_grants_anything_a_tool_result_cannot_create_authority(run_scenario):
    sb, persona, engine, server, h, final = run_scenario(ask_first=False, compact_at=())
    led = Ledger.open("ses_scenario")
    assert "production" not in str(led.d["objective"]["scope"])
