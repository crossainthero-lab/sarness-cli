"""What a model/engine pair really supports, effort strategies and budgets.

Facts come only from the engine: `/v1/agent/models` (context limits, tools: null = unverified),
`/v1/models` (capabilities.reasoning) and `/v1/llm/status`. Engine 0.4.0 forwards exactly one
reasoning control to the model server - the boolean `thinking` (chat-template `enable_thinking`) -
and only for local models. There is NO native low/medium/high reasoning-effort parameter, so the
effort levels below are agent-side strategies and are always labelled as such.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import state_dir

# effort -> (answer tokens/turn, extra allowance when thinking is on, step multiplier, thinking when 'auto', guidance)
EFFORT = {
    "low": dict(answer=4096, think_extra=0, steps=0.5, auto_thinking=False,
                guidance="Be direct. Explore only what is needed, skip planning for small tasks, make the smallest correct change, "
                         "and run one quick check."),
    "medium": dict(answer=8192, think_extra=4096, steps=1.0, auto_thinking=None,
                   guidance="Work normally: inspect the relevant code, make focused changes, and verify with tests or a build."),
    "high": dict(answer=16384, think_extra=8192, steps=1.5, auto_thinking=True,
                 guidance="Be thorough. Call update_plan before editing, read the relevant files in full, consider edge cases, run the "
                          "tests, and review your own diff before finishing."),
    "max": dict(answer=32768, think_extra=16384, steps=2.0, auto_thinking=True,
                guidance="Maximum rigor. Plan with update_plan, investigate before editing, handle edge cases and failure modes, run the "
                         "full test suite, then re-read the final diff against every requirement and fix any gap before you answer."),
}
MIN_PROMPT_FRACTION = 0.55   # the prompt side always keeps at least this share of the context window


@dataclass
class Capabilities:
    ref: str
    local: bool
    reasoning: bool | None          # registry says the model can think; None = not reported
    tools_reported: bool | None     # engine's own field (None = unverified, never assumed true)
    tools_probed: bool | None       # result of a past local probe, if any
    ctx_max: int | None
    ctx_default: int | None
    notes: list[str] = field(default_factory=list)

    @property
    def thinking_toggle(self) -> str:
        if not self.local:
            return "unsupported (remote endpoint model: engine does not forward it)"
        if self.reasoning is True:
            return "supported (enable_thinking)"
        if self.reasoning is False:
            return "not applicable (model not reported as reasoning-capable)"
        return "unknown (engine did not report reasoning capability)"

    @property
    def tool_calling(self) -> str:
        if self.tools_probed is True:
            return "verified by probe"
        if self.tools_probed is False:
            return "FAILED probe"
        if self.tools_reported is True:
            return "reported supported"
        return "unverified (engine reports none)"


def capabilities(model: dict[str, Any], server: str = "") -> Capabilities:
    probed = _probe_cache().get(f"{server}|{model.get('ref')}")
    return Capabilities(ref=model.get("ref", ""), local=bool(model.get("local")),
                        reasoning=model.get("reasoning"), tools_reported=model.get("tools"),
                        tools_probed=probed.get("tools") if probed else None,
                        ctx_max=model.get("context_max"), ctx_default=model.get("default_ctx"))


def _probe_path():
    return state_dir() / "capabilities.json"


def _probe_cache() -> dict[str, Any]:
    try:
        return json.loads(_probe_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record_probe(server: str, ref: str, tools_ok: bool, detail: str = "") -> None:
    from .config import atomic_json
    import time
    cache = _probe_cache()
    cache[f"{server}|{ref}"] = {"tools": tools_ok, "detail": detail[:200], "at": time.time()}
    atomic_json(_probe_path(), cache)


def record_tool_failure(server: str, ref: str, detail: str) -> None:
    """The model proved it cannot do structured tool calls in a real run."""
    record_probe(server, ref, False, detail)


def _models_cache_path():
    return state_dir() / "models_cache.json"


def _models_cache() -> dict[str, Any]:
    try:
        return json.loads(_models_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def cache_models(server: str, models: list[dict[str, Any]]) -> None:
    from .config import atomic_json
    import time
    if not server or not models:
        return
    cache = _models_cache()
    # Save clean model dicts without circular references or transient error messages
    clean = [{k: v for k, v in m.items() if k != "cached"} for m in models]
    cache[server] = {"models": clean, "at": time.time()}
    atomic_json(_models_cache_path(), cache)


def load_cached_models(server: str) -> list[dict[str, Any]]:
    cache = _models_cache()
    entry = cache.get(server) or {}
    models = entry.get("models") or []
    out = []
    for m in models:
        cm = dict(m)
        cm["cached"] = True
        out.append(cm)
    return out


@dataclass
class TurnPlan:
    max_tokens: int
    thinking: bool | None           # None = do not send the parameter
    thinking_note: str
    ctx: int                        # context window used for budgeting
    notes: list[str] = field(default_factory=list)


def plan_turn(caps: Capabilities, effort: str, thinking_setting: str, ctx: int, user_max: int | None,
              *, force_no_thinking: bool = False, boost: float = 1.0) -> TurnPlan:
    prof = EFFORT[effort]
    notes: list[str] = []
    thinking: bool | None
    if force_no_thinking:
        thinking, why = (False if caps.local else None), "thinking disabled for this turn after reasoning exhausted the output budget"
    elif thinking_setting == "on":
        thinking, why = (True if caps.local else None), "thinking: on (requested)"
    elif thinking_setting == "off":
        thinking, why = (False if caps.local else None), "thinking: off (requested)"
    elif caps.local and caps.reasoning is True and prof["auto_thinking"] is not None:
        thinking, why = prof["auto_thinking"], f"thinking: {'on' if prof['auto_thinking'] else 'off'} (from effort={effort})"
    else:
        thinking, why = None, "thinking: model default"
    if not caps.local and thinking_setting != "auto":
        why = "thinking: unsupported for remote endpoint models (ignored)"
    elif thinking_setting in ("on", "off") and caps.local and caps.reasoning is not True:
        why += "; model not reported reasoning-capable, may have no effect"
    answer = user_max or prof["answer"]
    want = int(answer * boost) + (prof["think_extra"] if thinking else 0)
    # Generation budget and context are budgeted separately: output never takes more than
    # (1 - MIN_PROMPT_FRACTION) of the window, so a long answer can't starve the prompt.
    cap = int(ctx * (1 - MIN_PROMPT_FRACTION))
    if want > cap:
        notes.append(f"output budget {want} reduced to {cap} to keep {int(MIN_PROMPT_FRACTION * 100)}% of the {ctx}-token window for the prompt")
        want = cap
    return TurnPlan(max_tokens=max(512, want), thinking=thinking, thinking_note=why, ctx=ctx, notes=notes)


def effort_report(caps: Capabilities, effort: str, thinking_setting: str, ctx: int, user_max: int | None) -> list[str]:
    plan = plan_turn(caps, effort, thinking_setting, ctx, user_max)
    p = EFFORT[effort]
    return [
        f"effort: {effort}  (agent-side strategy; the engine has no native reasoning-effort parameter)",
        f"  per-turn output budget: {plan.max_tokens} tokens (answer {user_max or p['answer']} + thinking allowance {p['think_extra'] if plan.thinking else 0})",
        f"  step limit multiplier: x{p['steps']}",
        f"  {plan.thinking_note}; model toggle: {caps.thinking_toggle}",
        f"  planning/verification guidance: {p['guidance']}",
        *[f"  note: {n}" for n in plan.notes],
    ]
