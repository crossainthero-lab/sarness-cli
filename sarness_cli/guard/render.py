"""The ledger as text for the model: short, ordered by what matters most when the conversation has just been compacted."""
from __future__ import annotations

from typing import Any

from .ledger import Ledger

HEADER = "[sarness-guard] TASK STATE - recorded by the runtime from tool results it observed; it is not a summary the model wrote."


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _authorised(led: Ledger) -> str:
    sc = led.scope
    parts = list(sc.actions) or ["inspect"]
    targets = ", ".join(sc.hosts + sc.aliases)
    out = ", ".join(parts)
    if targets:
        out += f" (named target: {targets})"
    if sc.autonomous:
        out += "; the user said to proceed without asking"
    if sc.read_only:
        out += "; read-only request - do not change files"
    return out


def render(led: Ledger, *, budget: int = 3200, mode: str | None = None, recovery: str | None = None,
           compacted: bool = False) -> str:
    """The note appended to a request. `budget` is in characters; the parts that matter most come first and the
    list parts are cut from the oldest end."""
    d = led.d
    lines: list[str] = [HEADER]
    lines.append(f"Objective - the user's request, STILL IN FORCE (it is not historical context): "
                 f"\"{_clip(led.objective, 900)}\"")
    lines.append(f"Authorised by that request: {_authorised(led)}.")
    if mode:
        lines.append("Approvals: " + {
            "bypass": "none are required; act.",
            "full-access": "the runtime asks the user itself for risky actions; call the tool instead of asking in chat.",
        }.get(mode, "the runtime itself asks the user for any action that needs approval; call the tool instead of asking in chat."))
    lines.append(f"Phase: {d['phase']}.")
    unmet = led.unmet()
    nxt = led.next_action()
    if recovery:
        lines.append("Recovery: " + recovery)
    lines.append(f"NEXT ACTION: {nxt}." + (" Do not repeat the investigation recorded below." if compacted or d["ops"] else ""))
    facts = [f for f in d["facts"] if f.get("current", True)]
    if facts:
        lines.append("Verified facts:")
        for f in facts[-10:]:
            src = f.get("source") or {}
            where = f" [{f['host']}]" if f.get("host") else ""
            frm = f" (from: {_clip(src.get('cmd', ''), 70)}, exit {src.get('exit')})" if src.get("cmd") else ""
            lines.append(f"- {_clip(f['text'], 280)}{where}{frm}")
    envs = d["env"].get("systemd_scope") or {}
    if envs and not any("systemd:" in (f.get("key") or "") for f in facts):
        lines.append("Environment: " + "; ".join(f"{h}: systemd services run in the {s} scope" for h, s in envs.items()))
    done = [o for o in d["ops"] if o["kind"] in {"mutate", "test", "deploy", "commit"} or o["host"]][-8:]
    if done:
        lines.append("Executed (exit codes are real):")
        for o in done:
            sm = f" - {o['summary']}" if o.get("summary") else ""
            lines.append(f"- #{o['n']} {_clip(o['cmd'], 100)} -> exit {o['exit']}{sm}")
    if d["files"]:
        lines.append("Files changed (content hash): " + "; ".join(
            f"{p} {v.get('sha', '')[:8]} x{v.get('edits', 1)}" for p, v in list(d["files"].items())[-8:]))
    tests = d["tests"][-2:]
    if tests:
        lines.append("Tests: " + "; ".join(
            f"`{_clip(t['cmd'], 60)}` exit {t['exit']} at change #{t['rev']}"
            f"{' (current)' if t['rev'] == led.rev else ' (STALE: code changed after it)'}" for t in tests))
    deps = d["deploys"][-2:]
    if deps:
        lines.append("Deployments: " + "; ".join(
            f"`{_clip(x['cmd'], 60)}` exit {x['exit']}, {'verified by ' + _clip(x['verified_by'] or '', 50) if x['verified'] else 'NOT yet verified'}" for x in deps))
    fails = [f for f in d["failures"].values() if "resolved_at_rev" not in f or f.get("remedy")][-5:]
    if fails:
        lines.append("Commands that failed (do not run them again unchanged):")
        for f in fails:
            fix = f"; verified correction: `{_clip(f['remedy'], 100)}`" if f.get("remedy") else ""
            lines.append(f"- `{_clip(f['cmd'], 90)}` -> exit {f.get('exit')}: {_clip(f.get('error', ''), 90)}{fix}")
    if d["blockers"]:
        lines.append("Genuine blockers: " + "; ".join(_clip(b["text"], 140) for b in d["blockers"][-3:]))
    if unmet:
        lines.append("Still to do: " + "; ".join(c["text"] for c in unmet) + ".")
    text = "\n".join(lines)
    if len(text) > budget:
        # drop from the optional middle first, keep header, objective, next action and the tail
        keep_head = lines[:6 + (1 if recovery else 0)]
        rest = lines[len(keep_head):]
        while rest and len("\n".join(keep_head + rest)) > budget:
            drop = next((i for i, ln in enumerate(rest) if ln.startswith("- #") or ln.startswith("- `")), None)
            if drop is None:
                drop = next((i for i, ln in enumerate(rest) if ln.startswith("- ")), None)
            if drop is None:
                rest.pop(len(rest) // 2)
            else:
                rest.pop(drop)
        text = "\n".join(keep_head + rest)[:budget]
    return text


def nudge_text(kind: str, led: Ledger, detail: str = "", mode: str | None = None) -> str:
    """The instruction the model receives when the guard continues the loop. Always names the next action."""
    nxt = led.next_action()
    approvals = ("The runtime asks the user for approval itself whenever policy requires it, so a chat question adds nothing; "
                 "call the tool and let that prompt appear." if mode not in (None, "bypass") else "No approval is needed; act.")
    if kind == "confirmation":
        return ("[sarness-guard] The user's request already authorises this: " + _authorised(led) + ". Do not ask whether to proceed. "
                + approvals + f" Next action: {nxt}. Make the tool call now.")
    if kind == "announced":
        return (f"[sarness-guard] You said you would act but made no tool call, and a statement is not an action. Next action: {nxt}. "
                "Make that tool call now instead of describing it.")
    if kind == "premature-complete":
        un = "; ".join(c["text"] for c in led.unmet()) or "nothing"
        return (f"[sarness-guard] The task is not finished: still to do - {un}. The runtime has no tool evidence for those yet, so "
                f"a summary now would not be accurate. Next action: {nxt}.")
    if kind == "question":
        return ("[sarness-guard] Do not ask the user whether to proceed: the request already authorises it (" + _authorised(led) + "). "
                + approvals + f" Next action: {nxt}.")
    if kind == "duplicate":
        return ("[sarness-guard] That call was not executed because it repeats work whose result is already established "
                f"({detail}). Use the recorded result instead. Next action: {nxt}.")
    if kind == "known-failure":
        return (f"[sarness-guard] That command was not run: it failed before and nothing has changed ({detail}). "
                f"Use a different approach. Next action: {nxt}.")
    if kind == "planning":
        return (f"[sarness-guard] Planning again will not move the task: {detail}. The plan is already recorded. "
                f"Next action: {nxt}. Execute it now with a real tool call.")
    if kind == "systemd-scope":
        return (f"[sarness-guard] That command was not run: {detail} Next action: {nxt}.")
    return f"[sarness-guard] {detail} Next action: {nxt}."


def checkpoint_text(led: Ledger) -> str:
    return render(led, budget=1800, compacted=True)


def stats(led: Ledger) -> dict[str, Any]:
    c = led.d["counters"]
    return {"interventions": c.get("interventions", 0), "ops": led.d["ops_total"], "compactions": len(led.d["compactions"]),
            "phase": led.d["phase"], "rev": led.rev}
