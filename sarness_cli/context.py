"""History budgeting and compaction that never orphans a tool message."""
from __future__ import annotations

import json
from typing import Any

from .tools import clipped


def request_chars(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    return len(json.dumps(messages, ensure_ascii=False)) + (len(json.dumps(tools)) if tools else 0)


def _round_end(h: list[dict[str, Any]], start: int) -> int:
    """Index just past the message group starting at `start` (an assistant+its tool results stay together)."""
    end = start + 1
    if h[start].get("role") == "assistant" and h[start].get("tool_calls"):
        while end < len(h) and h[end].get("role") == "tool":
            end += 1
    return end


def repaired_history(history: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Model-visible view within `budget` characters: system + first request are kept, old tool output is
    shortened first, then whole completed rounds are dropped. The input list is not modified."""
    h = json.loads(json.dumps(history))
    if not h or h[0].get("role") != "system":
        raise ValueError("Missing system message")
    size = lambda: len(json.dumps(h, ensure_ascii=False))  # noqa: E731
    protected = max(2, len(h) - 10)
    for i in range(2, protected):
        if size() <= budget:
            break
        if h[i].get("role") == "tool" and len(h[i].get("content", "")) > 500:
            h[i]["content"] = "[old tool output compacted; re-read the file or rerun the command if needed]"
    if size() > budget:
        for m in h[2:]:
            if m.get("role") == "tool" and len(m.get("content", "")) > 1200:
                m["content"] = clipped(m["content"], 1200)
    # Drop whole completed rounds, oldest first; keep at least the latest round when the window is tiny.
    for floor in (12, 6):
        while size() > budget and len(h) > floor:
            if h[2].get("role") == "tool":
                raise RuntimeError("Cannot compact an orphaned tool reply")
            del h[2:_round_end(h, 2)]
    for limit in (600, 300):   # last resort: shorten what remains instead of failing
        if size() <= budget:
            break
        for m in h[2:]:
            if m.get("role") == "tool" and len(m.get("content", "")) > limit:
                m["content"] = clipped(m["content"], limit)
            elif m.get("role") == "assistant" and len(m.get("content") or "") > limit:
                m["content"] = clipped(m["content"], limit)
    if size() > budget:
        raise RuntimeError("Task context is too large even after compaction; the model's context window is too small for this agent. "
                           "Use /clear or choose a model with a larger window.")
    return h


def compact_in_place(messages: list[dict[str, Any]], target_chars: int, digest: str) -> list[dict[str, Any]]:
    """Permanently drop the oldest completed rounds (returned for archiving) and leave a digest note,
    so the stored transcript cannot grow without bound."""
    removed: list[dict[str, Any]] = []
    keep_tail = 6
    # Messages [0]=system, [1]=original request. Only drop rounds that end before the protected tail.
    while request_chars(messages) > target_chars and len(messages) > keep_tail + 3:
        start = 2
        if messages[start].get("content", "").startswith("[sarness] Earlier steps were compacted") and len(messages) > start + 1:
            start = 3  # keep the digest where it is; fold older rounds into it
        if messages[start].get("role") == "tool":
            break  # malformed; leave to repaired_history
        end = _round_end(messages, start)
        if end > len(messages) - keep_tail:
            break
        removed += messages[start:end]
        del messages[start:end]
    if removed:
        note = {"role": "user", "content": "[sarness] Earlier steps were compacted to save context. " + digest}
        if messages[2].get("content", "").startswith("[sarness] Earlier steps were compacted") if len(messages) > 2 else False:
            messages[2] = note
        else:
            messages.insert(2, note)
    return removed
