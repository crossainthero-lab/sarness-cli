"""Regression suite for the 2026-10 incident: a ~30k-char write_file was cut off by the output limit and the
whole turn was thrown away. Covers truncation recovery, chunked writes, broken streams, disconnects, compaction,
shell overflow, Ctrl-C/resume, duplicate-execution prevention and permission behavior."""
import json

import pytest

from sarness_cli import agent, context
from sarness_cli.agent import Interrupted, Session, SessionBlocked, salvage_partial_args
from sarness_cli.engine import Engine
from fake_engine_server import FakeEngineServer, TOKEN, done
from helpers import FakeEngine, call, make_cfg, make_session, result
from test_agent import transcript_is_valid


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def big_source(n_chars, name="m"):
    """Valid Python of at least n_chars characters."""
    lines, i, size = [], 0, 0
    while size < n_chars:
        lines.append(f"def {name}_{i}(x):  # generated line {i}\n    return x + {i}\n")
        size += len(lines[-1]) + 1
        i += 1
    return "\n".join(lines)


def cut(name, args, call_id="c1", keep=0.6):
    """What the engine hands back when the output limit hits mid-call: arguments cut mid-string."""
    raw = json.dumps(args)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw[: int(len(raw) * keep)]}}


class SimModel:
    """A deterministic stand-in for the LLM that behaves like the real one under an output budget: it tries to
    emit a whole file per call, gets cut off when the call does not fit max_tokens, and switches to chunks when
    told a chunk size. It reads the real transcript, so recovery has to work end to end."""
    CPT = 3.0

    def __init__(self, files, fail_script=None):
        self.files = list(files.items())
        self.done_files = set()
        self.fail_script = fail_script or {}      # turn number -> Exception | "malformed"
        self.turn = 0
        self.params = []
        self.cut_count = 0
        self.progress = {}                         # path -> chars already written

    def completion(self, model, messages, tools, params, on_event=None):
        self.turn += 1
        self.params.append(dict(params))
        injected = self.fail_script.get(self.turn)
        if isinstance(injected, BaseException):
            raise injected
        if injected == "malformed":
            return result([{"id": f"m{self.turn}", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "x", "content": "unterm'}}])
        budget_chars = int(params["max_tokens"] * self.CPT * (0.35 if params.get("thinking") else 0.95))
        limit_note = None
        for m in reversed(messages):
            if m["role"] == "user" and "at most" in m["content"]:
                limit_note = int(m["content"].split("at most ")[1].split(" ")[0])
                break
        for path, text in self.files:
            if path in self.done_files:
                continue
            have = self.progress.get(path, 0)
            n = len(text) - have
            size = min(n, limit_note) if limit_note else n
            args = {"path": path, "content": text[have:have + size]} if have == 0 else \
                {"path": path, "content": text[have:have + size], "expected_length": have}
            name = "write_file" if have == 0 else "append_file"
            if len(json.dumps(args)) > budget_chars:          # does not fit in the output budget
                self.cut_count += 1
                return result([cut(name, args, f"t{self.turn}")], finish="length")
            self.progress[path] = have + size
            if self.progress[path] >= len(text):
                self.done_files.add(path)
            return result([call(name, args, f"t{self.turn}")])
        return result(content="All files written.")


def run_sim(tmp_path, files, fail_script=None, **cfgkw):
    sess, root = make_session(tmp_path, **cfgkw)
    sim = SimModel(files, fail_script)
    sess.run(sim, "build the project", 80)
    return sess, root, sim


# ---------------------------------------------------------------------------------------- the incident
def test_30k_write_cut_off_is_recovered_without_losing_the_task(tmp_path):
    text = big_source(30_203)
    sess, root, sim = run_sim(tmp_path, {"engine.py": text}, thinking="on")
    assert sim.cut_count >= 1, "the scenario must really hit the output limit"
    assert (root / "engine.py").read_text() == text
    assert sess.status == "completed" and transcript_is_valid(sess.messages)
    # the retry got a bigger budget and thinking off; the model was told a concrete chunk size
    assert sim.params[1]["max_tokens"] > sim.params[0]["max_tokens"] and sim.params[1].get("thinking") is False
    assert sess.recoveries["output_limit"] >= 1 and sess.chunk_limit <= agent.DEFAULT_CHUNK


def test_100k_file_is_built_from_chunks_exactly(tmp_path):
    text = big_source(100_000)
    sess, root, sim = run_sim(tmp_path, {"big.py": text})
    assert (root / "big.py").read_text() == text and sess.status == "completed"
    compile((root / "big.py").read_text(), "big.py", "exec")


def test_complete_100k_write_that_arrived_intact_is_accepted(tmp_path):
    sess, root = make_session(tmp_path)
    text = big_source(100_000)
    sess.run(FakeEngine(result([call("write_file", {"path": "a.py", "content": text})]), result([call("run_command", {"command": "true"})]),  result(content="ok")), "go", 5)
    assert (root / "a.py").read_text() == text


def test_cut_off_call_is_never_executed_but_earlier_complete_calls_are(tmp_path):
    sess, root = make_session(tmp_path)
    good = call("write_file", {"path": "kept.txt", "content": "complete"}, "g1")
    cut_call = cut("write_file", {"path": "big.txt", "content": "z" * 5000}, "g2", keep=0.9)
    eng = FakeEngine(result([good, cut_call], finish="length"), result([call("run_command", {"command": "true"})]),  result(content="stopping"))
    sess.run(eng, "go", 5)
    assert (root / "kept.txt").read_text() == "complete"
    assert not (root / "big.txt").exists()
    assert transcript_is_valid(sess.messages)
    assert all(c["id"] != "g2" for m in sess.messages for c in m.get("tool_calls", []))   # cut call never entered the transcript
    note = [m["content"] for m in sess.messages if m["role"] == "user" and "output limit" in m["content"]]
    assert note and "NOT executed" in note[0] and "big.txt" in note[0] and "1 earlier complete" in note[0]


def test_partial_content_is_kept_as_draft_outside_the_workspace(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(result([cut("write_file", {"path": "src/a.py", "content": "x = 1\n" * 2000}, keep=0.5)], finish="length"),
                     result(content="gave up"))
    sess.run(eng, "go", 4)
    drafts = list((sess.dir / "drafts").glob("*.partial"))
    assert len(drafts) == 1 and drafts[0].read_text().startswith("x = 1\n")
    assert not (root / "src").exists()


def test_salvage_decoder_handles_escapes_and_cut_mid_escape():
    raw = '{"path": "a/b.py", "content": "q=\\"x\\"\\nline2\\u00e9 \\'
    info = salvage_partial_args(raw)
    assert info["path"] == "a/b.py" and info["content"] == 'q="x"\nline2é '


def test_output_limit_ladder_gives_up_with_resumable_session(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(*[result([cut("write_file", {"path": "a", "content": "x" * 900})], finish="length") for _ in range(agent.MAX_LENGTH_RETRIES)])
    with pytest.raises(SessionBlocked, match="resume with 'sarness resume"):
        sess.run(eng, "go", 20)
    assert sess.status == "blocked:length"
    boosts = [p["max_tokens"] for p in eng.params]
    assert boosts == sorted(boosts) and boosts[-1] > boosts[0]
    # a blocked session resumes where it stopped, without re-sending the original instruction
    again = Session.load(sess.id, sess.ws, make_cfg())
    again.run(FakeEngine(result([call("write_file", {"path": "ok.txt", "content": "fine"})]), result([call("run_command", {"command": "true"})]),  result(content="done")))
    assert (root / "ok.txt").read_text() == "fine" and again.status == "completed"


# ---------------------------------------------------------------------------------------- malformed / broken streams
def test_malformed_tool_json_never_executes_and_recovers(tmp_path):
    text = big_source(9000)
    sess, root, sim = run_sim(tmp_path, {"a.py": text}, fail_script={1: "malformed", 2: "malformed"})
    assert (root / "a.py").read_text() == text and not (root / "x").exists()
    assert sess.recoveries["malformed_tool_call"] == 2


def _client(server):
    return Engine(server.url, TOKEN)


def test_truncated_json_in_stream_and_dropped_connection_are_retried_over_real_http(tmp_path):
    args = {"path": "net.txt", "content": "over the wire"}
    server = FakeEngineServer([
        [{"type": "delta", "content": "writing..."}, "DROP"],                 # server vanishes mid-stream
        [{"type": "delta", "content": "x"}, '{"type": "done", "message": {"role": "assis'],   # event cut mid-JSON
        "http500",
        [done(calls=[("write_file", args)])],
        [done(calls=[("run_command", {"command": "true"})])],
        [done(content="finished")],
    ])
    try:
        sess, root = make_session(tmp_path)
        sess.run(_client(server), "write the file", 10)
    finally:
        server.stop()
    assert (root / "net.txt").read_text() == "over the wire"
    assert sess.status == "completed" and transcript_is_valid(sess.messages)
    writes = [c for m in sess.messages if m["role"] == "assistant" for c in m.get("tool_calls", []) if c["function"]["name"] == "write_file"]
    assert len(writes) == 1                                                  # executed exactly once
    assert len(server.completions) == 6


def test_stream_cut_mid_tool_call_with_length_finish_over_http(tmp_path):
    partial = {"id": "c0", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": "abc'}}
    server = FakeEngineServer([
        [{"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [partial]}, "finish_reason": "length", "usage": {"prompt_tokens": 9, "completion_tokens": 9}}],
        [done(calls=[("write_file", {"path": "a.txt", "content": "abc"})])],
        [done(calls=[("run_command", {"command": "true"})])],
        [done(content="ok")],
    ])
    try:
        sess, root = make_session(tmp_path)
        sess.run(_client(server), "go", 6)
    finally:
        server.stop()
    assert (root / "a.txt").read_text() == "abc" and sess.status == "completed"
    # the retry request really carried the larger output budget
    assert server.completions[1]["params"]["max_tokens"] > server.completions[0]["params"]["max_tokens"]


def test_engine_down_for_a_while_then_back_is_survived(tmp_path):
    from sarness_cli.engine import EngineError
    down = EngineError("Connection refused", "network", retryable=True)
    eng = FakeEngine(down, down, down, down, result([call("write_file", {"path": "a", "content": "1"})]), result([call("run_command", {"command": "true"})]),  result(content="ok"))
    sess, root = make_session(tmp_path, retries=agent.RunConfig.retries)
    sess.run(eng, "go", 10)
    assert (root / "a").read_text() == "1" and eng.reconnects == 4          # default retries now outlast a 4-failure outage


# ---------------------------------------------------------------------------------------- context
def test_old_applied_file_contents_leave_the_prompt_but_stay_valid_json():
    big = "y" * 30_000
    h = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(12):
        h.append({"role": "assistant", "content": "", "tool_calls": [call("write_file", {"path": f"f{i}", "content": big}, f"c{i}")]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": "Created"})
    before = context.request_chars(h)
    view = context.repaired_history(h, 60_000)
    assert before > 300_000 and context.request_chars(view) <= 60_000
    for m in view:
        for c in m.get("tool_calls", []):
            json.loads(c["function"]["arguments"])
    assert context.request_chars(h) == before                                  # stored transcript untouched


def test_long_session_with_many_30k_writes_compacts_and_keeps_going(tmp_path):
    files = {f"mod{i}.py": big_source(30_000, f"m{i}") for i in range(8)}
    sess, root, sim = run_sim(tmp_path, files, ctx=16384)
    assert all((root / n).read_text() == t for n, t in files.items()) and sess.status == "completed"
    assert (sess.dir / "archive.jsonl").exists()


# ---------------------------------------------------------------------------------------- tools / shell / reads
def test_shell_output_overflow_is_bounded(tmp_path):
    sess, root = make_session(tmp_path)
    out = sess.ws.execute("run_command", {"command": "yes 'xxxxxxxxxxxxxxxx' | head -c 70000000", "timeout_s": 60})
    assert len(out) < 20_000 and "exit code" in out


def test_huge_single_line_file_read_is_bounded(tmp_path):
    sess, root = make_session(tmp_path)
    (root / "min.js").write_text("a" * 3_000_000)
    assert len(sess.ws.execute("read_file", {"path": "min.js"})) < 14_000


def test_append_chunks_are_idempotent_on_retry(tmp_path):
    sess, root = make_session(tmp_path)
    sess.ws.execute("write_file", {"path": "a.txt", "content": "AAA"})
    sess.ws.execute("append_file", {"path": "a.txt", "content": "BBB", "expected_length": 3})
    again = sess.ws.execute("append_file", {"path": "a.txt", "content": "BBB", "expected_length": 3})
    assert "Already applied" in again and (root / "a.txt").read_text() == "AAABBB"


# ---------------------------------------------------------------------------------------- Ctrl-C, resume, duplicates
def test_ctrl_c_during_generation_then_resume_continues_without_repeating_the_instruction(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(result([call("write_file", {"path": "one.txt", "content": "1"}, "a1")]), KeyboardInterrupt())
    with pytest.raises(Interrupted):
        sess.run(eng, "do two things", 10)
    assert sess.status == "interrupted"
    again = Session.load(sess.id, sess.ws, make_cfg())
    eng2 = FakeEngine(result([call("write_file", {"path": "two.txt", "content": "2"}, "a2")]), result([call("run_command", {"command": "true"})]),  result(content="done"))
    again.run(eng2)
    assert (root / "one.txt").read_text() == "1" and (root / "two.txt").read_text() == "2"
    assert sum(1 for m in again.messages if m["role"] == "user" and m["content"] == "do two things") == 1
    assert transcript_is_valid(again.messages)


def test_ctrl_c_during_tool_marks_rest_not_executed_and_resume_never_replays(tmp_path, monkeypatch):
    sess, root = make_session(tmp_path)
    real = sess.ws.execute
    def flaky(name, args):
        if args.get("path") == "second.txt":
            raise KeyboardInterrupt
        return real(name, args)
    monkeypatch.setattr(sess.ws, "execute", flaky)
    calls = [call("write_file", {"path": p, "content": p}, f"c{i}") for i, p in enumerate(["first.txt", "second.txt", "third.txt"])]
    with pytest.raises(Interrupted):
        sess.run(FakeEngine(result(calls)), "three files", 5)
    assert (root / "first.txt").exists() and not (root / "third.txt").exists()
    assert transcript_is_valid(sess.messages)
    monkeypatch.undo()


def test_crash_between_calls_is_repaired_on_resume_with_reused_tool_call_ids(tmp_path):
    """Some servers number calls call_0, call_1 on every turn. An old answered call_0 must not hide a new unanswered one."""
    sess, root = make_session(tmp_path)
    sess.messages += [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [call("write_file", {"path": "a", "content": "1"}, "call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": "Created a"},
        {"role": "assistant", "content": "", "tool_calls": [call("run_command", {"command": "touch ran.flag"}, "call_0")]},
    ]                                                                           # process died before the result was recorded
    sess.save()
    again = Session.load(sess.id, sess.ws, make_cfg())
    eng = FakeEngine(result(content="inspected"))
    again.run(eng)
    assert transcript_is_valid(again.messages) and not (root / "ran.flag").exists()
    assert any("status unknown" in m.get("content", "") for m in again.messages if m["role"] == "tool")


def test_engine_reusing_ids_across_turns_gets_unique_recorded_ids(tmp_path):
    sess, root = make_session(tmp_path)
    eng = FakeEngine(result([call("write_file", {"path": "a", "content": "1"}, "call_0")]),
                     result([call("write_file", {"path": "b", "content": "2"}, "call_0")]), result([call("run_command", {"command": "true"})]),  result(content="ok"))
    sess.run(eng, "go", 6)
    ids = [c["id"] for m in sess.messages for c in m.get("tool_calls", [])]
    assert len(ids) == len(set(ids)) == 3 and transcript_is_valid(sess.messages)


def test_corrupt_checkpoint_falls_back_to_previous_good_one(tmp_path):
    sess, root = make_session(tmp_path)
    sess.run(FakeEngine(result([call("write_file", {"path": "a", "content": "1"})]), result([call("run_command", {"command": "true"})]),  result(content="ok")), "go", 5)
    sess.path.write_text("{ torn write")
    again = Session.load(sess.id, sess.ws, make_cfg())
    assert again.messages and (root / "a").read_text() == "1"


# ---------------------------------------------------------------------------------------- acceptance + permissions
def test_acceptance_multi_file_project_with_injected_failures(tmp_path):
    """Build a multi-file project including a 30k+ file and a 100k file while the engine misbehaves, the user
    hits Ctrl-C, and the process is restarted from the checkpoint."""
    from sarness_cli.engine import EngineError
    files = {
        "pkg/__init__.py": "",
        "pkg/core.py": big_source(31_000, "core"),
        "pkg/tables.py": big_source(100_000, "tab"),
        "pkg/util.py": big_source(4000, "util"),
        "README.md": "# project\n" * 20,
    }
    fails = {2: EngineError("Connection lost mid-response", "network", retryable=True), 5: "malformed",
             9: EngineError("Engine stream ended without a complete 'done' event", "stream", retryable=True),
             14: KeyboardInterrupt()}
    sess, root = make_session(tmp_path)
    sim = SimModel(files, fails)
    with pytest.raises(Interrupted):
        sess.run(sim, "build the SCHEMRA engine package", 120)
    snapshot = {p: (root / p).read_text() for p in files if (root / p).exists()}
    resumed = Session.load(sess.id, sess.ws, make_cfg())
    resumed.run(sim, None, 120)                                               # resume: same task, same model state
    assert resumed.status == "completed" and transcript_is_valid(resumed.messages)
    for p, text in files.items():
        assert (root / p).read_text() == text, p
    assert all((root / p).read_text() == t for p, t in snapshot.items() if p != "pkg/tables.py" or len(t) == len(files[p]))
    compile((root / "pkg/tables.py").read_text(), "t", "exec")
    assert sim.cut_count >= 1 and resumed.recoveries.get("output_limit", 0) >= 1


def test_bypass_mode_recovery_never_prompts(tmp_path):
    def forbidden(_):
        raise AssertionError("bypass must not ask for approval")
    text = big_source(30_203)
    sess, root = make_session(tmp_path, mode="bypass", approve=forbidden)
    sim = SimModel({"deep/dir/w.py": text})
    sess.run(sim, "go", 60)
    assert (root / "deep/dir/w.py").read_text() == text and sim.cut_count >= 1


def test_ask_mode_still_asks_for_every_write(tmp_path):
    asked = []
    sess, root = make_session(tmp_path, mode="ask", approve=lambda m: asked.append(m) or True)
    sess.run(FakeEngine(result([call("write_file", {"path": "a", "content": "1"})]), result([call("run_command", {"command": "true"})]),  result(content="ok")), "go", 5)
    assert asked and (root / "a").read_text() == "1"
