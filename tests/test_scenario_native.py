"""The same multi-stage task through the Python runtime (`sarness native`): its real loop, real tools and real permission layer,
with the scripted model and compactions forced after the same stages."""
import pytest

from helpers import make_cfg
from sarness_cli import agent, tools
from sarness_cli.agent import SessionBlocked
from sarness_cli.guard import Ledger
from sarness_cli.guard.ledger import normalize_command
from scenario_rig import CORE_FIXED, TASK, Persona, Sandbox, ScenarioEngine


def drive(sess, engine, persona, compact_at=("inspected", "tested", "deployed"), limit=80):
    """Run the session one model turn at a time and compact it between stages, as the loop would under context pressure."""
    pending = set(compact_at)
    prompt, compactions = TASK, 0
    for _ in range(limit):
        try:
            return sess.run(engine, prompt, max_steps=1), compactions
        except SessionBlocked as exc:
            if "Step limit" not in str(exc):
                raise
        prompt = None
        if persona.milestone in pending:
            pending.discard(persona.milestone)
            if sess.compact_now():
                compactions += 1
    raise AssertionError("the task did not finish")


def start(tmp_path, monkeypatch, mode="bypass", approve=lambda _m: True, stubborn=False, ask_first=True):
    sb = Sandbox(tmp_path / "sb")
    sb.apply_env(monkeypatch)
    persona = Persona("native", stubborn=stubborn, proj=str(sb.proj), ask_first=ask_first)
    engine = ScenarioEngine(persona)
    asked: list[str] = []

    def confirm(message):
        asked.append(message)
        return approve(message)

    sess = agent.Session(tools.Workspace(sb.proj, mode, confirm), make_cfg())
    return sb, persona, engine, sess, asked


def test_the_native_runtime_completes_the_task_through_three_compactions_with_real_evidence(tmp_path, monkeypatch):
    sb, persona, engine, sess, asked = start(tmp_path, monkeypatch)
    final, compactions = drive(sess, engine, persona)
    led = sess.guard.led
    assert final.startswith("Done."), final
    assert sb.local_core() == CORE_FIXED and sb.tests_pass_now() and sb.remote_core() == sb.local_core() and sb.restarted()
    assert led.unmet() == [] and led.d["phase"] == "done"
    assert compactions == 3 and len(led.d["compactions"]) == 3
    kinds = [i["kind"] for i in led.d["interventions"]]
    assert kinds.count("confirmation") == 1 and kinds.count("ssh-repair") == 1 and kinds.count("premature-complete") == 2
    assert "known-failure" in kinds and "duplicate" in kinds and "give-up" not in kinds
    executed = [o for o in led.d["ops"] if o["kind"] in {"read", "verify", "other"} and o["tool"] == "run_command"]
    fps = [normalize_command(o["cmd"]) for o in executed]
    assert len(fps) == len(set(fps)), "no diagnostic ran twice"
    assert led.d["tests"][-1]["exit"] == 0 and led.d["tests"][-1]["rev"] == led.rev and led.d["deploys"][-1]["verified"]
    # the model's own words stay in the transcript next to the guard's push (the follow-up request carries both)
    follow_up = engine.calls[1]
    assert follow_up[-2]["role"] == "assistant" and "Should I proceed" in follow_up[-2]["content"]
    assert follow_up[-1]["content"].startswith("[sarness-guard]")


def test_a_stubborn_model_is_stopped_after_each_compaction(tmp_path, monkeypatch):
    sb, persona, engine, sess, asked = start(tmp_path, monkeypatch, stubborn=True)
    final, _ = drive(sess, engine, persona)
    assert final.startswith("Done.") and sb.remote_core() == CORE_FIXED
    assert len(sess.guard.led.interventions("systemd-scope")) == 3


def test_the_state_note_is_in_every_request_after_a_compaction(tmp_path, monkeypatch):
    sb, persona, engine, sess, asked = start(tmp_path, monkeypatch)
    drive(sess, engine, persona)
    after = [msgs for msgs in engine.calls if any("Earlier steps were compacted" in str(m.get("content")) for m in msgs)]
    assert after and all(str(m[-1].get("content", "")).startswith("[sarness-guard] TASK STATE") for m in after if m[-1]["role"] == "user"
                         and "TASK STATE" in str(m[-1]["content"]))
    last = next(m[-1]["content"] for m in reversed(after) if "TASK STATE" in str(m[-1].get("content")))
    assert "NEXT ACTION" in last and "STILL IN FORCE" in last


@pytest.mark.parametrize("mode", ["ask", "auto", "full-access", "bypass"])
def test_every_permission_mode_completes_and_only_bypass_never_asks(tmp_path, monkeypatch, mode):
    sb, persona, engine, sess, asked = start(tmp_path, monkeypatch, mode=mode)
    final, _ = drive(sess, engine, persona)
    assert final.startswith("Done.") and sb.remote_core() == CORE_FIXED
    if mode == "bypass":
        assert asked == [], "bypass never prompts"
    else:
        assert asked, f"{mode} mode must still ask the user for the actions its policy gates"
        assert all("ssh" in a or "rsync" in a or "write" in a or "edit" in a or "deploy" in a or "systemctl" in a or "sh " in a
                   or "pytest" in a for a in asked), asked


def test_ask_mode_asks_for_more_than_auto_and_both_ask_before_ssh(tmp_path, monkeypatch):
    counts = {}
    for mode in ("ask", "auto", "full-access"):
        sb, persona, engine, sess, asked = start(tmp_path / mode, monkeypatch, mode=mode)
        drive(sess, engine, persona)
        counts[mode] = len(asked)
    assert counts["ask"] > counts["auto"] >= counts["full-access"] > 0 or counts["ask"] >= counts["auto"] >= counts["full-access"], counts
    assert counts["ask"] > 0


def test_a_denied_approval_is_never_overridden_by_the_guard(tmp_path, monkeypatch):
    sb, persona, engine, sess, asked = start(tmp_path, monkeypatch, mode="ask",
                                             approve=lambda m: "ssh" not in m and "rsync" not in m and "sh scripts" not in m)
    with pytest.raises(SessionBlocked, match="denied"):
        sess.run(engine, TASK)                     # the whole loop in one call: four denials in a row pause the task
    assert sess.status == "blocked:denied"
    assert sb.remote_core() != CORE_FIXED and not sb.restarted(), "nothing reached the server without approval"
    assert sess.guard.led.d["deploys"] == [] or all(d["exit"] != 0 for d in sess.guard.led.d["deploys"])


def test_a_model_that_only_asks_is_pushed_a_bounded_number_of_times_and_never_given_tool_calls(tmp_path, monkeypatch):
    sb, persona, engine, sess, asked = start(tmp_path, monkeypatch)
    persona.respond = lambda messages: [("text", "Should I proceed?")]
    with pytest.raises(SessionBlocked, match="asking for permission"):
        drive(sess, engine, persona)
    assert sess.status == "blocked:confirmation_loop" and len(engine.calls) == 4
    assert sess.guard.led.d["ops"] == [] and sb.local_core() != CORE_FIXED
