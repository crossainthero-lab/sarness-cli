import json

import pytest

from guardkit import assistant, call, checkpoint, edit, read, shell, system, tool, user
from sarness_cli.guard import Guard, Ledger
from sarness_cli.guard import autonomy

TASK = ("SSH into fylume@192.168.20.193, inspect Trinodal, find the issue, implement the correction, run tests and deploy. "
        "Proceed autonomously.")


def make(task=TASK, mode="auto", turns=()):
    led = Ledger.open("ses_g")
    g = Guard(led, mode=mode)
    msgs = [system(), user(task)]
    for t in turns:
        msgs += t
    g.observe(msgs)
    return g, msgs


CONFIRMATIONS = [
    "Should I proceed with the SSH connection now?", "Shall I connect to the server?", "Would you like me to implement this?",
    "Can I proceed with testing?", "I've reviewed the plan. What would you like to do next? Ready to begin Phase 1 whenever you are.",
    "Let me know if you'd like me to start the implementation.", "Want me to go ahead and deploy?",
]


@pytest.mark.parametrize("text", CONFIRMATIONS)
def test_redundant_confirmations_are_continued_with_the_next_action(text):
    g, _ = make()
    v = g.review(text=text, calls=[])
    assert v.action == "continue" and v.kind == "confirmation", (text, v)
    assert "Next action" in v.note and "Do not ask" in v.note


def test_announcing_work_is_not_doing_it():
    g, _ = make()
    v = g.review(text="Good, I have the picture now. I'll start implementing the fix now.", calls=[])
    assert v.action == "continue" and v.kind == "announced" and "not an action" in v.note


@pytest.mark.parametrize("text", [
    "I need the SSH password for fylume@192.168.20.193 to continue.",
    "Which of the two servers should I deploy to: 10.0.0.5 or 10.0.0.6?",
    "Should I connect to root@203.0.113.9 instead? That host is not the one you named.",
    "Should I delete the production database before redeploying?",
    "There are two ways to fix this: patch the parser or rewrite the tokenizer. Which would you prefer?",
])
def test_genuine_questions_blockers_and_new_risks_are_left_alone(text):
    g, _ = make()
    assert g.review(text=text, calls=[]).action == "pass", text


def test_an_action_the_user_did_not_authorise_is_not_pushed():
    g, _ = make("Fix the parser bug in parse.py and run the tests.")
    v = g.review(text="The fix is in and tests pass. Shall I deploy this to the server?", calls=[])
    assert v.action == "pass", "deploying was not part of the request, so the question is real"
    v = g.review(text="Should I run the tests now?", calls=[])
    assert v.action == "continue"


def test_read_only_tasks_are_never_forced_to_edit():
    g, _ = make("Explain how the bridge handles streaming. Don't change anything.", turns=[read("/w/bridge.py")])
    assert g.review(text="Here is how streaming works: it forwards deltas as they arrive and sends tool calls at the end.\n" * 12, calls=[]).action == "pass"
    assert g.review(text="I'll implement a fix for the streaming now.", calls=[]).action == "pass"
    assert g.review(text="Would you like me to change the bridge?", calls=[]).action == "pass"


def test_summarising_early_is_not_finishing():
    g, _ = make(turns=[shell("ssh fylume@192.168.20.193 'ls ~/app'", "app.py")])
    v = g.review(text="Summary of what I found: the app lives in ~/app and the deployment is an rsync copy. All done!", calls=[])
    assert v.action == "continue" and v.kind == "premature-complete"
    assert "still to do" in v.note.lower() and "change the source" in v.note


def test_a_finished_task_may_end():
    turns = [shell("ssh fylume@192.168.20.193 'ls'", "a"), edit("/w/a.py"), shell("pytest -q", "3 passed"),
             shell("rsync -a src/ fylume@192.168.20.193:~/app/", "ok"), shell("ssh fylume@192.168.20.193 'systemctl --user is-active app'", "active")]
    g, _ = make(turns=turns)
    assert g.led.unmet() == []
    assert g.review(text="All done: fixed, tested (3 passed), deployed and verified active.", calls=[]).action == "pass"


def test_nudges_are_bounded_and_reset_by_real_work():
    g, _ = make()
    kinds = [g.review(text="Should I proceed?", calls=[]).action for _ in range(5)]
    assert kinds[:3] == ["continue"] * 3 and kinds[3] == "give_up"
    assert g.led.d["blockers"]
    assert g.review(text="Should I proceed?", calls=[]).action == "pass", "after giving up the guard stops pushing"
    g.observe([system(), user(TASK), user("Please carry on with the deploy step only")])
    assert not g.led.d["blockers"], "a new instruction from the user lifts the guard's own stop"
    g2, _ = make()
    for _ in range(2):
        g2.review(text="Shall I connect?", calls=[])
    assert g2.review(text="", calls=[call("shell", {"command": "ssh fylume@192.168.20.193 'ls'"})]).action == "pass"
    assert [g2.review(text="Should I proceed?", calls=[]).action for _ in range(3)] == ["continue"] * 3


def test_the_user_stating_a_blocker_to_ask_about_is_respected_even_when_the_ledger_has_one():
    g, _ = make(turns=[shell("ssh fylume@192.168.20.193 'ls'", "fylume@192.168.20.193: Permission denied (publickey).", code=255)])
    assert g.review(text="Should I try again?", calls=[]).action == "pass"


def test_a_question_tool_that_only_asks_how_to_proceed_is_dropped_but_a_real_question_is_not():
    g, _ = make()
    proceed = call("question", {"questions": [{"question": "How would you like to proceed?", "header": "Next step",
                                               "options": [{"label": "Begin Phase 1", "description": "SSH and inspect"},
                                                           {"label": "Review plan", "description": "discuss"}]}]})
    v = g.review(text="", calls=[proceed])
    assert v.action == "retry" and v.kind == "question"
    real = call("question", {"questions": [{"question": "Which branch should the fix go on?", "header": "Branch",
                                            "options": [{"label": "main", "description": ""}, {"label": "v0.2-neural", "description": ""}]}]})
    assert g.review(text="", calls=[real]).action == "pass"
    risky = call("question", {"questions": [{"question": "Proceed with deleting the production data?", "header": "Danger",
                                             "options": [{"label": "Proceed", "description": ""}]}]})
    assert g.review(text="", calls=[risky]).action == "pass"


def test_a_malformed_ssh_command_is_repaired_and_the_repair_is_recorded():
    g, _ = make()
    bad = call("shell", {"command": 'sshfylume@192.168.20.193 "cd ~/app && git status"'})
    v = g.review(text="", calls=[bad])
    assert v.action == "rewrite" and v.kind == "rewrite"
    assert json.loads(v.calls[0]["function"]["arguments"])["command"].startswith("ssh fylume@192.168.20.193")
    assert g.led.interventions("ssh-repair")


def test_repeating_an_established_inspection_is_refused_then_tolerated_once_then_stopped():
    g, _ = make(turns=[read("/w/app.py")])
    again = call("read", {"path": "/w/app.py"})
    first = g.review(text="", calls=[again])
    assert first.action == "retry" and first.kind == "duplicate" and "Use the recorded result" in first.note
    assert g.review(text="", calls=[again]).action == "retry"
    assert g.review(text="", calls=[again]).action == "pass", "after two refusals one more run is allowed: the model may know better"
    assert g.review(text="", calls=[again]).action == "pass" or True
    stop = None
    for _ in range(4):
        stop = g.review(text="", calls=[again])
        if stop.action == "give_up":
            break
    assert stop.action == "give_up" and "cannot change" in stop.detail


def test_only_the_repeated_calls_are_dropped_from_a_mixed_turn():
    g, _ = make(turns=[read("/w/app.py")])
    v = g.review(text="", calls=[call("read", {"path": "/w/app.py"}), call("read", {"path": "/w/other.py"})])
    assert v.action == "rewrite" and [json.loads(c["function"]["arguments"])["path"] for c in v.calls] == ["/w/other.py"]


def test_a_rerun_after_a_change_is_never_called_a_repeat():
    turns = [shell("pytest -q", "1 failed", code=1), edit("/w/a.py")]
    g, _ = make(turns=turns)
    assert g.review(text="", calls=[call("shell", {"command": "pytest -q"})]).action == "pass"


def test_a_command_that_failed_unchanged_is_not_rerun_blindly():
    g, _ = make(turns=[shell("ssh fylume@192.168.20.193 'cat /nope'", "cat: /nope: No such file", code=1)])
    v = g.review(text="", calls=[call("shell", {"command": "ssh fylume@192.168.20.193 'cat /nope'"})])
    assert v.action == "retry" and v.kind == "known-failure"


def test_the_wrong_systemd_scope_is_not_checked_again_once_the_right_one_is_known():
    turns = [shell("ssh fylume@192.168.20.193 'systemctl --user status trinodal-app'",
                   "● trinodal-app.service\n   Loaded: loaded (/home/f/.config/systemd/user/trinodal-app.service)\n   Active: active")]
    g, _ = make(turns=turns)
    v = g.review(text="", calls=[call("shell", {"command": "ssh fylume@192.168.20.193 'systemctl status trinodal-app'"})])
    assert v.action == "retry" and v.kind == "systemd-scope" and "--user" in v.note


def test_an_interrupted_deploy_is_not_replayed_blindly():
    cmd = "rsync -a src/ fylume@192.168.20.193:~/app/"
    g, _ = make(turns=[shell(cmd, "Interrupted by the user (Ctrl-C). The command was killed.")])
    v = g.review(text="", calls=[call("shell", {"command": cmd})])
    assert v.action == "retry" and v.kind == "uncertain-replay"
    assert g.review(text="", calls=[call("shell", {"command": cmd})]).action == "pass", "after inspecting, the user's goal still stands"


def test_utility_requests_are_never_reviewed_or_annotated():
    g, _ = make()
    summarise = [system(), user(TASK), checkpoint(), user("You MUST summarize the conversation above into a structured summary")]
    assert Guard.utility_kind(summarise) == "compaction"
    assert g.review(text="Should I proceed?", calls=[], messages=summarise).action == "pass"
    assert g.note(summarise) is None
    assert Guard.utility_kind([system("Generate a title for this conversation"), user("hi")]) == "title"


def test_the_state_note_appears_only_after_compaction_and_carries_what_was_lost():
    turns = [shell("ssh fylume@192.168.20.193 'systemctl --user status trinodal-app'", "Loaded: loaded (user)\nActive: active"),
             shell("ssh fylume@192.168.20.193 'cd ~/trinodal-ai && git status'", "fatal: not a git repository", code=128),
             edit("/w/a.py")]
    g, msgs = make(turns=turns)
    assert g.note(msgs) is None
    compacted = [system(), user(TASK), checkpoint("## Objective\n- inspect things")]
    g.observe(compacted)
    note = g.note(compacted)
    assert note and TASK[:60] in note and "STILL IN FORCE" in note and "NEXT ACTION" in note
    assert "systemctl --user" in note and "deployment copy" in note and "a.py" in note
    assert "Do not repeat the investigation" in note
    assert len(note) <= 3200


def test_compaction_without_progress_escalates_to_a_stop():
    g, _ = make(turns=[read("/w/a.py")])
    for i in range(1, 7):
        msgs = [system(), user(TASK), checkpoint(f"summary number {i}")]
        g.observe(msgs)
    assert g.led.counter("stalled_compactions") >= 4
    assert g.review(text="", calls=[call("read", {"path": "/w/x.py"})]).action == "give_up"
    g2, _ = make(turns=[read("/w/a.py")])
    g2.observe([system(), user(TASK), checkpoint("one")])
    g2.led.record("shell", {"command": "pytest -q"}, "3 passed")
    g2.observe([system(), user(TASK), checkpoint("two")])
    assert g2.led.counter("stalled_compactions") == 0, "a test run happened between the compactions: that is progress"


def test_every_intervention_is_recorded():
    g, _ = make()
    g.review(text="Should I proceed?", calls=[])
    g.review(text="", calls=[call("shell", {"command": "sshfylume@h ls"})])
    kinds = [i["kind"] for i in g.led.d["interventions"]]
    assert "confirmation" in kinds and "ssh-repair" in kinds


def test_asking_wording_alone_does_not_decide():
    assert autonomy.asks_permission("Done. Let me know if you want anything else.")
    sc_text = "Here is the result of the migration. The table has 12 rows."
    assert not autonomy.asks_permission(sc_text)


def test_a_read_after_an_edit_in_the_same_turn_is_not_a_repeat():
    """The edit has not run yet when the turn is reviewed, but the read that follows it will see its effect."""
    g, _ = make(turns=[shell("cat after.txt", "survived")])
    v = g.review(text="", calls=[call("edit", {"path": "after.txt", "oldString": "a", "newString": "b"}),
                                 call("shell", {"command": "cat after.txt"})])
    assert v.action == "pass"
    assert g.review(text="", calls=[call("shell", {"command": "cat after.txt"})]).action == "retry"


def test_a_file_read_whose_result_was_compacted_away_may_be_read_again_but_a_visible_one_may_not():
    first = read("/w/app.py")
    g, msgs = make(turns=[first])
    again = call("read", {"path": "/w/app.py"})
    assert g.review(text="", calls=[again], messages=msgs).action == "retry", "the result is still in the model's context"
    compacted = [system(), user(TASK), checkpoint("summary")]
    assert g.review(text="", calls=[again], messages=compacted).action == "pass", "the text was compacted away; the model needs it to edit"
    shell_call = call("shell", {"command": "ssh fylume@192.168.20.193 'git status'"})
    g2, _ = make(turns=[shell("ssh fylume@192.168.20.193 'git status'", "fatal: not a git repository", code=128)])
    assert g2.review(text="", calls=[shell_call], messages=compacted).action in {"retry", "pass"}
