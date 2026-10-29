import json

import pytest

from sarness_cli import agent, context
from sarness_cli.agent import Interrupted, Session, SessionBlocked, looks_like_text_tool_call
from sarness_cli.engine import EngineError
from helpers import FakeEngine, call, make_cfg, make_session, result


def transcript_is_valid(messages):
    """Every tool message answers a tool_call in the preceding assistant turn and every call is answered."""
    pending = set()
    for m in messages:
        if m["role"] == "assistant":
            assert not pending, f"unanswered calls {pending}"
            pending = {c["id"] for c in m.get("tool_calls", [])}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in pending, "orphan tool message"
            pending.discard(m["tool_call_id"])
    return not pending


def test_truncated_turn_is_discarded_nothing_executes_then_small_calls_work(tmp_path):
    sess, root = make_session(tmp_path)
    broken = result([call("write_file", {"path": "bad.txt", "content": "not safe"})], finish="length")
    eng = FakeEngine(broken, result([call("write_file", {"path": "ok.txt", "content": "yes"})]),
                     result([call("run_command", {"command": "echo ok"})]), result(content="done"))
    sess.run(eng, "make a file", 6)
    assert not (root / "bad.txt").exists() and (root / "ok.txt").read_text() == "yes"
    assert sess.status == "completed" and transcript_is_valid(sess.messages)
    assert "cut off while writing a tool call" in json.dumps(eng.history[1][-1])   # targeted guidance, names the tool


def test_reasoning_that_eats_the_budget_turns_thinking_off_next_turn(tmp_path):
    sess, root = make_session(tmp_path, effort="high")
    eng = FakeEngine(result(finish="length", reasoning="x" * 5000), result(content="ok"))
    sess.run(eng, "think hard", 4)
    assert eng.params[0].get("thinking") is True            # high effort + reasoning-capable model
    assert eng.params[1].get("thinking") is False           # recovered with a smaller strategy
    assert sess.status == "completed"


def test_three_truncations_block_with_actionable_message(tmp_path):
    sess, _ = make_session(tmp_path)
    eng = FakeEngine(*[result([call("write_file", {"path": "a", "content": "x"})], finish="length") for _ in range(3)])
    with pytest.raises(SessionBlocked, match="output limit"):
        sess.run(eng, "go", 10)
    assert sess.status == "blocked:length"


def test_malformed_json_discards_whole_turn_even_if_first_call_is_fine(tmp_path):
    sess, root = make_session(tmp_path)
    good = call("write_file", {"path": "a.txt", "content": "x"}, "g1")
    bad = {"id": "b", "type": "function", "function": {"name": "write_file", "arguments": '{"path":'}}
    eng = FakeEngine(result([good, bad]), result(content="cannot do that"))
    sess.run(eng, "anything", 3)
    assert not (root / "a.txt").exists()                   # a partial turn never executes
    assert sess.status == "completed" and transcript_is_valid(sess.messages)


def test_schema_errors_become_tool_errors_not_crashes(tmp_path):
    sess, _ = make_session(tmp_path)
    eng = FakeEngine(result([call("nope", {}, "a"), call("read_file", {"path": 5}, "b")]), result(content="ok"))
    sess.run(eng, "x", 3)
    tool_msgs = [m["content"] for m in sess.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and all("TOOL ERROR" in t for t in tool_msgs) and transcript_is_valid(sess.messages)


def test_tool_call_written_as_text_is_never_executed_and_model_is_flagged(tmp_path):
    sess, root = make_session(tmp_path)
    fake = '<tool_call>{"name":"write_file","arguments":{"path":"pwn.txt","content":"x"}}</tool_call>'
    eng = FakeEngine(result(content=fake), result(content='{"name": "run_command", "arguments": {"command": "touch pwn2"}}'))
    with pytest.raises(SessionBlocked, match="ordinary text"):
        sess.run(eng, "go", 5)
    assert not (root / "pwn.txt").exists() and not (root / "pwn2").exists()
    assert sess.status == "blocked:unsupported_tools"


def test_text_tool_call_detector_uses_real_json_parsing():
    assert looks_like_text_tool_call('Sure! {"name": "read_file", "arguments": {"path": "a"}}')
    assert looks_like_text_tool_call('{"function": {"name": "run_command", "arguments": "{}"}, "arguments": "{}"}')
    assert looks_like_text_tool_call("<function=read_file><parameter=path>a</parameter>")
    assert not looks_like_text_tool_call('The config is {"name": "web", "port": 80} and I read_file(...) conceptually.')
    assert not looks_like_text_tool_call("All done: I edited a.py and tests pass.")


def test_edit_without_verification_is_challenged_then_accepted_after_a_real_run(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(result([call("write_file", {"path": "a", "content": "x"})]), result(content="all done"),
                     result([call("run_command", {"command": "echo tested"})]), result(content="verified"))
    sess.run(eng, "write a", 6)
    assert sess.status == "completed" and len(eng.history) == 4
    assert any("verification" in l and "exited 0" in l for l in sess.verification())


def test_failed_verification_is_reported_honestly_even_if_model_claims_success(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(result([call("write_file", {"path": "t.py", "content": "assert False\n"})]),
                     result([call("run_command", {"command": "python3 t.py"})]),
                     result(content="Everything passes!"), result(content="Actually it fails."))
    sess.run(eng, "make tests pass", 6)
    assert any("FAILED" in l and "exited 1" in l for l in sess.verification()), sess.verification()
    assert any("exit code: 1" in m["content"] for m in sess.messages if m["role"] == "tool")                 # the model saw the real code
    assert any("exited 1" in m["content"] for m in sess.messages if m["role"] == "user")                      # and was confronted when it claimed success


def test_resume_does_not_repeat_unknown_side_effect(tmp_path):
    sess, root = make_session(tmp_path)
    sess.messages += [{"role": "user", "content": "do thing"},
                      {"role": "assistant", "content": "", "tool_calls": [call("write_file", {"path": "a", "content": "unconfirmed"})]}]
    sess.save()
    restored = Session.load(sess.id, sess.ws, make_cfg())
    eng = FakeEngine(result(content="status unknown"))
    restored.run(eng, max_steps=3)
    assert not (root / "a").exists()
    assert "unknown" in json.dumps(eng.history[0][-2:]).lower() and transcript_is_valid(restored.messages)


def test_session_workspace_mismatch_refused(tmp_path):
    sess, root = make_session(tmp_path)
    sess.save()
    other = tmp_path / "other"
    other.mkdir()
    from sarness_cli.tools import Workspace
    with pytest.raises(ValueError, match="belongs to"):
        Session.load(sess.id, Workspace(other, "ask", None), make_cfg())


def test_network_failure_reconnects_with_bounded_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    sess, _ = make_session(tmp_path)
    boom = EngineError("connection reset", "network", retryable=True)
    eng = FakeEngine(boom, boom, result(content="back"))
    sess.run(eng, "hi", 5)
    assert eng.reconnects == 2 and sess.status == "completed"
    eng2 = FakeEngine(*[boom] * 4)
    sess2, _ = make_session(tmp_path)
    with pytest.raises(SessionBlocked, match="resume with"):
        sess2.run(eng2, "hi", 9)
    assert sess2.status.startswith("blocked:engine") and transcript_is_valid(sess2.messages)


def test_auth_and_busy_errors_do_not_retry(tmp_path):
    for kind, hint in (("auth", "sarness setup"), ("busy", "/model")):
        sess, _ = make_session(tmp_path)
        eng = FakeEngine(EngineError("nope", kind, 409, retryable=False))
        with pytest.raises(SessionBlocked, match=hint):
            sess.run(eng, "hi", 5)
        assert eng.reconnects == 0 and len(eng.history) == 1


def test_context_overflow_shrinks_budget_and_recovers(tmp_path):
    sess, _ = make_session(tmp_path)
    eng = FakeEngine(EngineError("exceeds context", "context", 400), result(content="fine"))
    sess.run(eng, "hi", 4)
    assert sess.ctx_shrink < 1 and sess.status == "completed"


def test_ctrl_c_during_generation_checkpoints_without_corrupting_transcript(tmp_path):
    sess, _ = make_session(tmp_path)
    eng = FakeEngine(KeyboardInterrupt())
    with pytest.raises(Interrupted):
        sess.run(eng, "hi", 3)
    assert sess.status == "interrupted" and transcript_is_valid(sess.messages)
    eng2 = FakeEngine(result(content="resumed fine"))
    assert sess.run(eng2, "again", 3) == "resumed fine"


def test_ctrl_c_during_command_kills_it_and_marks_remaining_calls(tmp_path, monkeypatch):
    sess, root = make_session(tmp_path)
    real = sess.ws.execute

    def exploding(name, args):
        if name == "run_command":
            raise KeyboardInterrupt
        return real(name, args)
    monkeypatch.setattr(sess.ws, "execute", exploding)
    eng = FakeEngine(result([call("run_command", {"command": "sleep 99"}, "c1"), call("write_file", {"path": "late", "content": "x"}, "c2")]))
    with pytest.raises(Interrupted):
        sess.run(eng, "go", 3)
    assert transcript_is_valid(sess.messages) and not (root / "late").exists()
    assert "NOT EXECUTED" in [m for m in sess.messages if m.get("tool_call_id") == "c2"][0]["content"]


def test_extra_calls_in_one_turn_are_not_executed_but_answered(tmp_path):
    sess, root = make_session(tmp_path)
    calls = [call("write_file", {"path": f"f{i}", "content": "x"}, f"id{i}") for i in range(10)]
    eng = FakeEngine(result(calls), result([call("run_command", {"command": "true"})]), result(content="done"))
    sess.run(eng, "many", 5)
    assert (root / "f7").exists() and not (root / "f8").exists() and transcript_is_valid(sess.messages)


def test_repeating_the_same_call_is_stopped(tmp_path):
    sess, _ = make_session(tmp_path)
    eng = FakeEngine(*[result([call("list_dir", {}, f"i{n}")]) for n in range(12)])
    with pytest.raises(SessionBlocked, match="repeating|stuck"):
        sess.run(eng, "loop", 20)
    assert sess.status == "blocked:loop"


def test_repeated_denials_pause_the_task(tmp_path):
    sess, _ = make_session(tmp_path, mode="ask", approve=lambda _: False)
    eng = FakeEngine(*[result([call("write_file", {"path": f"f{n}", "content": "x"}, f"i{n}")]) for n in range(8)])
    with pytest.raises(SessionBlocked, match="denied"):
        sess.run(eng, "write", 10)


def test_step_limit_is_resumable(tmp_path):
    sess, _ = make_session(tmp_path)
    eng = FakeEngine(*[result([call("run_command", {"command": f"echo {n}"}, f"i{n}")]) for n in range(5)])
    with pytest.raises(SessionBlocked, match="Step limit"):
        sess.run(eng, "x", 3)
    assert sess.status == "blocked:step_limit"


def test_history_trim_preserves_pairs():
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "first"}]
    for i in range(20):
        c = call("read_file", {"path": "a"}, f"id{i}")
        history.extend([{"role": "assistant", "content": "", "tool_calls": [c]}, {"role": "tool", "tool_call_id": f"id{i}", "content": "x" * 10000}])
    compact = context.repaired_history(history, 15000)
    assert compact[0]["role"] == "system" and compact[1]["content"] == "first" and transcript_is_valid(compact[1:])
    assert len(json.dumps(compact)) < 15000 and len(history) == 42   # input untouched


def test_in_place_compaction_bounds_the_transcript_and_archives(tmp_path):
    sess, root = make_session(tmp_path)
    sess.messages += [{"role": "user", "content": "task"}]
    for i in range(60):
        sess.messages += [{"role": "assistant", "content": "", "tool_calls": [call("read_file", {"path": "a"}, f"id{i}")]},
                          {"role": "tool", "tool_call_id": f"id{i}", "content": "y" * 3000}]
    before = len(sess.messages)
    sess.files["a.py"] = {"added": 3, "removed": 1}
    assert sess.compact_now() > 0
    assert len(sess.messages) < before and transcript_is_valid(sess.messages[1:])
    assert "a.py" in sess.messages[2]["content"]                              # the digest keeps what matters
    archived = (sess.dir / "archive.jsonl").read_text().splitlines()
    assert len(archived) + len(sess.messages) == before + 0 or len(archived) > 0


def test_automatic_compaction_keeps_requests_inside_a_small_context(tmp_path):
    sess, _ = make_session(tmp_path, ctx=8192)
    calls = [result([call("run_command", {"command": f"python3 -c \"print('z{n}'*3000)\""}, f"i{n}")]) for n in range(12)] + [result(content="done")]
    eng = FakeEngine(*calls)
    sess.ws.result_cap = 5000
    sess.run(eng, "spam", 20)
    assert max(len(json.dumps(h)) for h in eng.history) < 8192 * 3 + 6000     # never sends an unbounded history
    assert len(json.dumps(sess.messages)) < 40000                               # and the stored transcript is bounded too


def test_model_switch_mid_session_keeps_transcript_valid(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(result([call("write_file", {"path": "a", "content": "x"})]), result([call("run_command", {"command": "true"})]), result(content="fine"))
    sess.run(eng, "first", 4)
    sess.cfg.model = "local:other"
    eng2 = FakeEngine(result([call("run_command", {"command": "true"})]), result(content="second model finished"))
    sess.run(eng2, "now continue", 3)
    assert transcript_is_valid(sess.messages) and sess.status == "completed"


def test_preflight_guard_blocks_when_someone_else_swapped_the_model(tmp_path):
    sess, _ = make_session(tmp_path)

    class Guarded(FakeEngine):
        def preflight(self, cfg):
            return "The engine now has other loaded. Continuing will unload that model."
    eng = Guarded(result(content="should not be asked"))
    with pytest.raises(SessionBlocked, match="Paused so nobody's model is evicted"):
        sess.run(eng, "hi", 2)
    assert eng.history == []                       # no request was ever sent
    sess2, _ = make_session(tmp_path)
    sess2.swap_confirm = lambda m: True
    eng2 = Guarded(result(content="ok"))
    assert sess2.run(eng2, "hi", 2) == "ok"


def test_params_pin_context_and_separate_budgets(tmp_path):
    sess, _ = make_session(tmp_path, pin_ctx=True, ctx=16384, effort="max")
    sess.cfg.gpu_layers = 999
    eng = FakeEngine(result(content="ok"))
    sess.run(eng, "x", 2)
    p = eng.params[0]
    assert p["ctx"] == 16384 and p["gpu_layers"] == 999                          # never triggers a reload
    assert p["max_tokens"] <= 16384 * 0.45 + 1 and p["thinking"] is True         # max effort cannot starve the prompt


def test_session_names_and_lookup(tmp_path):
    sess, _ = make_session(tmp_path)
    sess.run(FakeEngine(result(content="ok")), "Fix the failing parser tests please", 2)
    assert sess.name == "fix-the-failing-parser-tests-please"
    assert Session.find("fix-the") == sess.id and Session.find(sess.id[:6]) == sess.id


def test_ctrl_c_at_an_approval_prompt_aborts_the_task_instead_of_denying(tmp_path):
    def ctrl_c(_):
        raise KeyboardInterrupt
    sess, root = make_session(tmp_path, mode="ask", approve=ctrl_c)
    eng = FakeEngine(result([call("write_file", {"path": "a", "content": "x"}, "w1"), call("write_file", {"path": "b", "content": "x"}, "w2")]))
    with pytest.raises(Interrupted):
        sess.run(eng, "go", 3)
    assert not (root / "a").exists() and not (root / "b").exists() and transcript_is_valid(sess.messages)
    assert sess.status == "interrupted"
