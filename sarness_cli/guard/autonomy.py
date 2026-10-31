"""Reading a model turn that ends without acting: is it asking permission it already has, announcing work it did not do,
declaring victory early, or raising a genuine blocker?

Wording alone never decides. A question counts as redundant only when the action it asks about is covered by the
user's own request (scope), is not a new kind of risk, and no real obstacle is named in the same breath."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .scope import Scope

# the last paragraph of the turn is what the user is left looking at
_ASKS = re.compile(
    r"(?i)(?:\b(?:should|shall|can|could|may|might)\s+(?:i|we)\b|\b(?:do|would)\s+you\s+(?:want|like)\s+(?:me\s+)?to\b|\bwant\s+me\s+to\b|"
    r"\bwould\s+you\s+like\b|\bready\s+(?:for\s+me\s+)?to\s+(?:begin|start|proceed|continue|go)\b|\blet\s+me\s+know\s+(?:if|when|whether)\b|"
    r"\b(?:is\s+(?:it|that)|are\s+you)\s+(?:ok(?:ay)?|fine|alright|good)\b|\bokay\s+to\b|\bok\s+to\b|\bhow\s+would\s+you\s+like\s+to\s+proceed\b|"
    r"\bwhat\s+would\s+you\s+like\s+(?:me\s+)?to\s+do\b|\bwhich\s+(?:step|option|phase)\s+(?:should|would)\b|\bconfirm\b[^.?!]*\b(?:proceed|continue|go\s+ahead|begin)\b|"
    r"\bproceed\s*\?|\bgo\s+ahead\s*\?|\bshall\s+we\b|\bif\s+you(?:'d|\s+would)?\s+(?:like|want|prefer)\b|\bi\s+(?:will\s+)?wait\s+for\s+your\b|"
    r"\bawait(?:ing)?\s+your\s+(?:go|confirm|approval|instruction|direction)\w*)")
_BLOCKER = re.compile(
    r"(?i)\b(?:password|passphrase|credential|api[-\s]?key|token|private\s+key|permission\s+denied|access\s+denied|authenticat\w+|"
    r"unable\s+to|cannot|can'?t\s+(?:reach|connect|access|find)|do(?:es)?n'?t\s+(?:exist|have\s+access)|not\s+(?:found|reachable|installed|available)|"
    r"unclear|ambiguous|which\s+(?:one|of|branch|server|host|file|directory|version)|multiple\s+(?:options|candidates|servers|hosts|versions)|"
    r"two\s+(?:options|ways|approaches)|trade-?off|(?:want|need)\s+to\s+know|please\s+(?:provide|specify|tell|clarify)|what\s+is\s+the\s+(?:ip|address|host|path|name))")
_RISK = re.compile(r"(?i)\b(?:delete|remove|drop|wipe|erase|destroy|overwrite|truncate|force[-\s]?push|reset\s+--hard|rm\s+-r|production|prod\b|"
                   r"sudo|root\s+access|format|reboot|shut\s*down|uninstall|migrate\s+the\s+(?:database|data)|spend|purchase|pay)\b")
_ANNOUNCE = re.compile(
    r"(?i)\b(?:let\s+me|i(?:'ll|\s+will)|i(?:'m|\s+am)\s+(?:going\s+to|about\s+to|now)|i\s+(?:can|could)\s+now|next[,\s]+(?:i(?:'ll|\s+will)|we(?:'ll|\s+will))|"
    r"now\s+(?:i(?:'ll|\s+will)|let(?:'s|\s+us))|let(?:'s|\s+us)|starting\s+(?:with|now)|(?:time|ready)\s+to)\s+(?:now\s+|first\s+|just\s+)?"
    r"(?:start|begin|proceed|run|execute|test|check|inspect|read|write|edit|change|install|build|launch|create|update|fix|fetch|verify|deploy|"
    r"open|load|implement|connect|ssh|look|examine|investigate|apply|restart|rsync|commit|move\s+(?:on|to)|get\s+(?:started|going)|dig|dive|review|"
    r"proceed|make|add|modify|refactor|continue)\b")
_DONE = re.compile(r"(?i)\b(?:all\s+(?:done|set|complete)|(?:task|work|implementation|fix|deployment)\s+(?:is\s+)?(?:now\s+)?(?:complete[d]?|done|finished)|"
                   r"(?:successfully|have\s+(?:now\s+)?)\s*(?:deployed|implemented|fixed|completed|finished)|summary\s+of\s+(?:what|the\s+work|changes)|"
                   r"here(?:'s|\s+is)\s+(?:a\s+|the\s+)?(?:summary|recap|overview)|that\s+completes|everything\s+(?:is\s+)?(?:working|in\s+place|done)|"
                   r"in\s+summary|to\s+summari[sz]e)\b")
_CATEGORY = [
    ("deploy", re.compile(r"(?i)\b(?:deploy\w*|ship|release|roll\s*out|restart|rsync|push\s+(?:it|this|the\s+\w+)\s+to)\b")),
    ("test", re.compile(r"(?i)\b(?:tests?|pytest|verify|validate|lint|run\s+the\s+suite)\b")),
    ("ssh", re.compile(r"(?i)\b(?:ssh|connect(?:ion)?\s+to|log\s*in(?:to)?|remote)\b")),
    ("edit", re.compile(r"(?i)\b(?:implement\w*|fix|edit|modif\w+|chang\w+|writ\w+|creat\w+|updat\w+|refactor|add(?:ing)?|build|patch|correct)\b")),
    ("commit", re.compile(r"(?i)\bcommit\b")),
    ("inspect", re.compile(r"(?i)\b(?:inspect|check|look|read|examine|investigate|review|explore|begin|start|proceed|continue|phase|step)\b")),
]


@dataclass
class Reading:
    kind: str                 # confirmation | announced | premature-complete | blocker | clarification | final | none
    category: str | None = None
    reason: str = ""


def tail_of(text: str, n: int = 700) -> str:
    return (text or "").strip()[-n:]


def asked_category(text: str) -> str | None:
    t = tail_of(text, 500)
    for name, pat in _CATEGORY:
        if pat.search(t):
            return name
    return None


def asks_permission(text: str) -> bool:
    t = tail_of(text, 600)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", t)
    last = " ".join(s for s in sentences[-3:] if s)
    return bool(_ASKS.search(last))


def announces_action(text: str) -> bool:
    """Closing text that says work is about to happen ("I'll deploy now") without it having happened. An offer that
    waits for the user ("if you'd like, I'll ...") is a question, not an announcement."""
    t = tail_of(text, 400)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", t) if s]
    last = " ".join(sentences[-2:])
    return bool(_ANNOUNCE.search(last)) and not _ASKS.search(last)


def reads_as_final(text: str) -> bool:
    return bool(_DONE.search(text or "")) or (len((text or "").strip()) > 500 and not asks_permission(text))


def question_tool_is_proceed_prompt(args: dict) -> bool:
    """A `question` tool call whose options are just go/no-go or 'how would you like to proceed' choices."""
    qs = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(qs, list) or not qs:
        return False
    for q in qs:
        if not isinstance(q, dict):
            return False
        prompt = str(q.get("question") or "") + " " + str(q.get("header") or "")
        labels = " ".join(str(o.get("label") or "") + " " + str(o.get("description") or "")[:80]
                          for o in (q.get("options") or []) if isinstance(o, dict))
        if _BLOCKER.search(prompt) or _RISK.search(prompt):
            return False
        proceed = re.search(r"(?i)\b(?:proceed|begin|start|continue|next\s+step|go\s+ahead|how\s+would\s+you\s+like|what\s+(?:next|would\s+you)|ready|confirm|approve|should\s+i|shall\s+i|want\s+me)\b", prompt)
        optioned = re.search(r"(?i)\b(?:begin|start|proceed|continue|yes|go\s+ahead|inspect\s+first|review\s+(?:the\s+)?plan|do\s+it|approve|something\s+else)\b", labels)
        if not (proceed and optioned):
            return False
    return True


def read_turn(text: str, scope: Scope, *, known_hosts: set[str] | None = None, blockers: list[str] | None = None,
              objective: str = "") -> Reading:
    """Classify a turn that made no tool call."""
    text = (text or "").strip()
    if not text:
        return Reading("none")
    asks = asks_permission(text)
    tail = tail_of(text)
    if blockers:
        return Reading("blocker", reason="the ledger records a genuine blocker")
    if asks:
        cat = asked_category(text)
        if _BLOCKER.search(tail) or _RISK.search(tail):
            return Reading("clarification", cat, "names a real obstacle, a choice, or a risky action")
        # a target the user never named is a real clarification, not hesitation
        named = set(re.findall(r"\b(?:[\w.-]+@)?(?:\d{1,3}\.){3}\d{1,3}\b|\b[\w.-]+@[\w.-]+\b", tail))
        allowed = (known_hosts or set()) | set(scope.hosts)
        if named and not all(any(n.split("@")[-1] in a for a in allowed) for n in named):
            return Reading("clarification", cat, "names a target the user did not")
        if cat and scope.allows(cat):
            return Reading("confirmation", cat, "asks permission for an action the request already covers")
        if scope.autonomous and cat in {"inspect", "ssh", "edit", "test"}:
            return Reading("confirmation", cat, "the user said to proceed without asking")
        if cat == "inspect" and scope.actions and scope.allows("inspect"):
            return Reading("confirmation", cat, "asks whether to begin")
        return Reading("clarification", cat, "asks about something the request does not cover")
    if announces_action(text):
        cat = asked_category(text)
        if scope.read_only and cat in {"edit", "deploy", "commit"}:
            return Reading("final", cat, "read-only request")
        return Reading("announced", cat, "announced an action without performing it")
    if reads_as_final(text):
        return Reading("premature-complete", None, "reads as a final report")
    return Reading("final")
