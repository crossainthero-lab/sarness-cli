"""The guard: one decision point for both agent runtimes.

`observe(messages)` feeds the ledger from the real message history (user requests and finished tool calls);
`note(messages)` gives the state the model must not lose once the history has been compacted; `review(text, calls)`
says what to do with a model turn: let it through, continue the loop (a nudge), retry without the offending calls,
rewrite an obviously malformed command, or give up and checkpoint after bounded attempts.

Nothing here executes or approves anything, and nothing here is a step limit: every bound counts unproductive repeats
(nudges without action, repeats of an unchanged call), and any real progress resets it."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from . import autonomy, classify as C, render, scope as S
from .ledger import PLAN_TOOLS, SHELL_TOOLS, WRITE_TOOLS, Ledger, fingerprint

MAX_NUDGES = 3               # consecutive turns without a tool call that the guard will push before it gives up
MAX_REPEATS = 3              # an unchanged call the model proposes this often is let through once more, then stopped
MAX_STALLED_COMPACTIONS = 4
CHECKPOINT_MARKERS = ("<conversation-checkpoint>", "[sarness] Earlier steps were compacted")
UTILITY_MARKERS = ("You MUST summarize the conversation above", "Update the existing checkpoint in the conversation above",
                   "The previous response did not fill in the required summary template")


@dataclass
class Verdict:
    action: str = "pass"             # pass | continue | retry | rewrite | give_up
    kind: str = ""
    note: str = ""                   # what to tell the model (continue/retry) or the user (give_up)
    calls: list[dict] | None = None  # the calls to forward (rewrite)
    detail: str = ""
    dropped: list[str] = field(default_factory=list)


def message_text(m: dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return ""


def parse_call(c: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    fn = c.get("function") or {}
    raw = fn.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
    except ValueError:
        args = {}
    return str(fn.get("name") or ""), args if isinstance(args, dict) else {}


class Guard:
    def __init__(self, ledger: Ledger, *, mode: str = "auto", runtime: str = "tui", workspace: str | None = None):
        self.led = ledger
        self.mode = mode
        self.runtime = runtime
        self.workspace = workspace
        self.events: list[dict] = []       # interventions made by this instance (tests and debugging)

    # ------------------------------------------------------------------ reading the history
    @staticmethod
    def utility_kind(messages: list[dict[str, Any]]) -> str | None:
        """'compaction' for a summary request, 'title' for a session-title request, else None."""
        last_user = next((message_text(m) for m in reversed(messages) if m.get("role") == "user"), "")
        if any(k in last_user for k in UTILITY_MARKERS):
            return "compaction"
        system = " ".join(message_text(m) for m in messages if m.get("role") == "system")[:400].lower()
        if "title" in system and "generat" in system and not any(m.get("role") == "tool" for m in messages):
            return "title"
        return None

    @staticmethod
    def checkpoint_present(messages: list[dict[str, Any]]) -> bool:
        return any(m.get("role") == "user" and any(message_text(m).lstrip().startswith(k) for k in CHECKPOINT_MARKERS)
                   for m in messages)

    def observe(self, messages: list[dict[str, Any]]) -> list[dict]:
        """Feed the ledger from the request history. Safe to call on every request."""
        led = self.led
        real = [message_text(m) for m in messages if m.get("role") == "user" and S.is_real_user_text(message_text(m))]
        if real:
            if not led.objective:
                led.set_request(real[0])
            led.set_request(real[-1])
        new = led.ingest(messages, workspace=self.workspace)
        if self.checkpoint_present(messages):
            self._note_compaction(messages)
        if new or real:
            led.save()
        return new

    def _note_compaction(self, messages: list[dict[str, Any]]) -> None:
        led = self.led
        cp = next(message_text(m) for m in reversed(messages) if m.get("role") == "user"
                  and any(message_text(m).lstrip().startswith(k) for k in CHECKPOINT_MARKERS))
        sha = hashlib.sha1(cp.encode("utf-8", "replace")).hexdigest()[:10]
        if any(c["sha"] == sha for c in led.d["compactions"]):
            return
        marker = list(led.progress_marker())
        prev = led.d["compactions"][-1]["marker"] if led.d["compactions"] else None
        stalled = prev is not None and prev == marker
        led.d["compactions"].append({"sha": sha, "t": round(time.time(), 1), "marker": marker, "ops": led.d["ops_total"]})
        led.counter("stalled_compactions", reset=not stalled)
        if stalled:
            led.counter("stalled_compactions", 1)
            led.intervene("compaction-stall", f"compaction #{len(led.d['compactions'])} with no new progress since the last one")

    # ------------------------------------------------------------------ what to tell the model after compaction
    def note(self, messages: list[dict[str, Any]], *, budget: int = 3200) -> str | None:
        """The task-state note to append to a request, or None when the model can still see the evidence itself."""
        if self.utility_kind(messages):
            return None
        led = self.led
        if not led.objective:
            return None
        compacted = self.checkpoint_present(messages)
        if not compacted:
            return None
        stalls = led.counter("stalled_compactions")
        recovery = None
        if stalls:
            recovery = (f"the conversation was compacted {stalls + 1} times without new progress; the investigation is complete, "
                        "re-running it only wastes the context window")
        return render.render(led, budget=budget, mode=self.mode, recovery=recovery, compacted=True)

    # ------------------------------------------------------------------ reviewing a model turn
    @staticmethod
    def visible_fingerprints(messages: list[dict[str, Any]] | None) -> set[str] | None:
        """Fingerprints of the finished tool calls whose results are still in the history the model sees."""
        if messages is None:
            return None
        pending: dict[str, str] = {}
        out: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    name, args = parse_call(c)
                    pending[str(c.get("id"))] = fingerprint(name, args)
            elif m.get("role") == "tool" and str(m.get("tool_call_id")) in pending:
                out.add(pending[str(m.get("tool_call_id"))])
        return out

    def review(self, *, text: str, calls: list[dict[str, Any]], messages: list[dict[str, Any]] | None = None) -> Verdict:
        if messages is not None and self.utility_kind(messages):
            return Verdict()
        led = self.led
        if led.counter("stalled_compactions") >= MAX_STALLED_COMPACTIONS:
            return self._give_up("compaction-loop", "the context was compacted repeatedly without any new progress")
        if not calls:
            return self._review_text(text)
        return self._review_calls(text, calls, self.visible_fingerprints(messages))

    def _review_text(self, text: str) -> Verdict:
        led = self.led
        reading = autonomy.read_turn(text, led.scope, blockers=[b["text"] for b in led.d["blockers"]], objective=led.objective)
        pushable = reading.kind in {"confirmation", "announced", "premature-complete"}
        if reading.kind == "announced" and reading.category not in (None, "inspect") and not led.scope.allows(reading.category):
            pushable = False
        if reading.kind == "premature-complete" and (not led.unmet() or led.scope.read_only):
            pushable = False
        if not pushable:
            if reading.kind in {"final", "blocker", "clarification", "none"}:
                led.counter("nudges", reset=True)
            return Verdict(kind=reading.kind)
        n = led.counter("nudges", 1)
        if reading.kind == "premature-complete":
            led.counter("premature", 1)
            if led.counter("premature") > MAX_NUDGES:
                return self._give_up("premature-complete", "the model kept declaring the task finished while required steps had no evidence")
        if n > MAX_NUDGES:
            return self._give_up(reading.kind, "the model kept " + {
                "confirmation": "asking for permission it already has",
                "announced": "announcing work without doing it",
                "premature-complete": "summarising instead of finishing"}[reading.kind])
        led.intervene(reading.kind, reading.reason or text[-120:], category=reading.category, attempt=n)
        self.events.append({"kind": reading.kind, "attempt": n})
        led.save()
        return Verdict("continue", reading.kind, render.nudge_text(reading.kind, led, mode=self.mode), detail=reading.reason)

    def _review_calls(self, text: str, calls: list[dict[str, Any]], visible: set[str] | None = None) -> Verdict:
        led = self.led
        led.counter("nudges", reset=True)
        led.counter("premature", reset=True)
        kept: list[dict] = []
        dropped: list[tuple[str, str, str]] = []          # (kind, detail, fp)
        rewritten = False
        cyc = led.cycle()
        state_may_change = False       # an earlier call in this very turn changes things: later results can differ
        for c in calls:
            name, args = parse_call(c)
            fp = fingerprint(name, args)
            was_changing = state_may_change
            if name in WRITE_TOOLS or (name in SHELL_TOOLS and C.classify(str(args.get("command") or "")).kind
                                       in {"mutate", "deploy", "commit", "other", "test"}):
                state_may_change = True
            if name == "question" and autonomy.question_tool_is_proceed_prompt(args) and not led.d["blockers"] and \
                    (led.scope.actions or led.scope.autonomous):
                if led.repeat_count(fp, 1) <= MAX_REPEATS:
                    dropped.append(("question", "asked the user how to proceed", fp))
                    continue
            if name in SHELL_TOOLS and isinstance(args.get("command"), str):
                fix = C.fix_glued_ssh(args["command"])
                if fix:
                    fixed, why = fix
                    orig = args["command"]
                    led.d["failures"].setdefault(fingerprint(name, args), {
                        "cmd": orig[:300], "count": 1, "error": "command not found (no space after ssh)", "rev": led.rev,
                        "remedy": fixed[:300], "exit": 127, "source": "guard"})
                    args = {**args, "command": fixed}
                    c = {**c, "function": {**c.get("function", {}), "arguments": json.dumps(args, ensure_ascii=False)}}
                    led.intervene("ssh-repair", why)
                    self.events.append({"kind": "ssh-repair", "from": orig[:80], "to": fixed[:80]})
                    rewritten = True
                    fp = fingerprint(name, args)
                scope_issue = self._systemd_scope_issue(args["command"])
                if scope_issue and led.repeat_count("scope:" + fp, 1) <= 2:
                    dropped.append(("systemd-scope", scope_issue, fp))
                    continue
            if name in PLAN_TOOLS and led.plan_streak() >= 4 and led.repeat_count("plan-streak", 1) <= MAX_REPEATS:
                dropped.append(("planning", f"{led.plan_streak()} planning calls in a row with nothing executed", fp))
                continue
            if was_changing:
                kept.append(c)
                continue
            known = led.known_failure(name, args)
            if known and led.repeat_count(fp, 1) <= MAX_REPEATS - 1:
                fix = f"it exited {known.get('exit')}: {known.get('error', '')[:100]}" + (
                    f"; the verified correction is `{known['remedy']}`" if known.get("remedy") else "")
                dropped.append(("known-failure", fix, fp))
                continue
            rep = led.find_repeat(name, args, visible)
            if rep is None and cyc and fp == cyc[0]:
                rep = next((o for o in reversed(led.d["ops"]) if o["fp"] == fp), None)
            if rep is not None:
                n = led.repeat_count(fp, 1)
                if n > MAX_REPEATS + 1:
                    return self._give_up("loop", f"the model is stuck repeating the same call, and its result cannot change ({rep['cmd'][:80]})")
                if n <= MAX_REPEATS - 1:
                    dropped.append(("duplicate", f"#{rep['n']} {rep['cmd'][:80]} -> exit {rep['exit']}"
                                    + (f" - {rep['summary']}" if rep.get("summary") else ""), fp))
                    continue
                led.intervene("repeat-allowed", f"let {rep['cmd'][:80]} run again after {n - 1} refusals")
            uncertain = next((o for o in reversed(led.d["ops"]) if o["fp"] == fp and o["exit"] is None
                              and o["kind"] in {"mutate", "deploy", "commit"}), None)
            if uncertain and led.repeat_count("uncertain:" + fp, 1) <= 1:
                dropped.append(("uncertain-replay", f"`{uncertain['cmd'][:80]}` was interrupted earlier and its effects are unknown; "
                                "inspect the state first", fp))
                continue
            kept.append(c)
        if kept and any(led.is_new_work(*parse_call(c)) for c in kept):
            led.reset_repeat_counters()        # genuine new work: earlier refusals no longer count against the model
        for kind, detail, _ in dropped:
            led.intervene(kind, detail)
            self.events.append({"kind": kind, "detail": detail[:100]})
        if dropped or rewritten:
            led.save()
        if dropped and not kept:
            kind, detail, _ = dropped[0]
            if kind == "question":
                return Verdict("retry", "question", render.nudge_text("question", led, mode=self.mode), dropped=["question"])
            extra = "; ".join(d for _, d, _ in dropped[:3])
            return Verdict("retry", kind, render.nudge_text(kind if kind in {"duplicate", "known-failure", "systemd-scope", "planning"} else "other",
                                                             led, extra if kind != "uncertain-replay" else extra, mode=self.mode),
                           dropped=[k for k, _, _ in dropped])
        if dropped or rewritten:
            return Verdict("rewrite", "rewrite" if rewritten else dropped[0][0], calls=kept, dropped=[k for k, _, _ in dropped])
        return Verdict()

    def _systemd_scope_issue(self, command: str) -> str | None:
        info = C.classify(command)
        sc = info.systemctl
        if not sc or sc["scope"] != "system" or not sc["units"]:
            return None
        host = C.host_of(info.host) or "local"
        if self.led.d["env"]["systemd_scope"].get(host) == "user":
            return (f"on {host} the services run under `systemctl --user` (verified earlier); "
                    f"`systemctl {sc['verbs'][0] if sc['verbs'] else ''} {' '.join(sc['units'])}` in the system scope will not find them. "
                    "Use `systemctl --user`.")
        return None

    def _give_up(self, kind: str, why: str) -> Verdict:
        led = self.led
        led.add_blocker(f"{why} (guard stopped after bounded recovery)", key="guard:" + kind)
        led.intervene("give-up", why, reason=kind)
        led.d["counters"]["checkpoint_reason"] = why
        led.save()
        un = "; ".join(c["text"] for c in led.unmet()) or "nothing"
        msg = (f"Stopped: {why}. The task state is saved (verified operations, facts and what is still to do: {un}). "
               f"Resume with a specific instruction; the next action would be: {led.next_action()}.")
        self.events.append({"kind": "give-up", "reason": kind})
        return Verdict("give_up", kind, msg, detail=why)
