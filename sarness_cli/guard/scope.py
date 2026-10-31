"""What the user actually asked for and authorised.

The scope is read from the user's own messages only. Tool results, repository instruction files, compaction summaries
and messages the runtime wrote itself ("[sarness] ...", "<conversation-checkpoint>", "The following shell command was
executed by the user") never widen it, so text planted in a file or a command's output cannot grant the agent more."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Messages that sit in the user role but were not typed by the user.
SYNTHETIC_PREFIXES = ("[sarness]", "<conversation-checkpoint>", "<system-reminder>", "The following shell command was executed",
                      "The server restarted while you were working", "<summary>", "[sarness-guard]",
                      "You MUST summarize the conversation above", "Update the existing checkpoint in the conversation above",
                      "The previous response did not fill in the required summary template")

ACTION_WORDS: dict[str, re.Pattern[str]] = {
    "ssh": re.compile(r"(?i)\b(?:ssh|log\s*(?:in)?to|connect(?:ing)?\s+to|remote(?:ly)?|on\s+(?:the|my)\s+(?:server|spark|box|host|machine))\b"),
    "inspect": re.compile(r"(?i)\b(?:inspect|investigat\w+|look\s+(?:at|into|through)|check|find|diagnos\w+|review|analy[sz]e|audit|explore|"
                          r"read|understand|examine|trace|debug|identify|locate|reproduce|survey|study)\b"),
    "edit": re.compile(r"(?i)\b(?:fix|implement\w*|chang\w+|edit|modif\w+|add|updat\w+|refactor|writ\w+|creat\w+|correct\w*|patch|"
                       r"repair|develop|build|rewrite|rename|remove|extend|introduce|make\s+(?:it|the|them|sure))\b"),
    "test": re.compile(r"(?i)\b(?:tests?|pytest|unit\s*tests?|verify|validat\w+|run\s+the\s+suite|ci|lint|type-?check)\b"),
    "deploy": re.compile(r"(?i)\b(?:deploy\w*|ship|release|roll\s*out|publish|go\s+live|restart|rsync|push\s+(?:it|this|the\s+\w+)?\s*to\s+(?:the\s+)?(?:server|spark|prod\w*))\b"),
    "commit": re.compile(r"(?i)\bcommit\b"),
}
NEGATION = re.compile(r"(?i)(?:\bdo\s+not\b|\bdon'?t\b|\bnever\b|\bwithout\b|\bno\s+need\s+to\b|\bnot\s+to\b|\bavoid\b|\bskip\b|\bstop\s+short\s+of\b)"
                      r"(?:\s+\w+){0,4}\s*$")
AUTONOMOUS = re.compile(
    r"(?i)\b(?:proceed\s+autonomously|autonomous(?:ly)?|without\s+(?:asking|confirmation|checking\s+in|stopping|prompting)|"
    r"don'?t\s+ask|do\s+not\s+ask|no\s+need\s+to\s+ask|stop\s+asking|just\s+do\s+it|go\s+ahead|you\s+have\s+(?:my\s+)?(?:permission|"
    r"authori[sz]ation)|i\s+authori[sz]e|authori[sz]ed|end[-\s]to[-\s]end|until\s+(?:it'?s|it\s+is|everything\s+is)\s+(?:done|complete|working)|"
    r"don'?t\s+stop|keep\s+going|finish\s+(?:it|the\s+task|everything))\b")
READ_ONLY = re.compile(r"(?i)\b(?:read[-\s]only|do\s+not\s+(?:change|modify|edit|write)\s+anything|don'?t\s+(?:change|modify|edit|touch)\s+anything|"
                       r"just\s+(?:look|inspect|explain|tell|report|review)|only\s+(?:inspect|look|report|explain|review|read)|"
                       r"explain\b|what\s+does|how\s+does|why\s+(?:does|is|did)|summari[sz]e|describe)\b")
HOST = re.compile(r"\b(?:[A-Za-z_][\w.-]*@)?(?:(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z][\w-]*(?:\.[A-Za-z0-9][\w-]*)+)(?::\d{2,5})?\b")
USER_AT = re.compile(r"\b[A-Za-z_][\w.-]*@[A-Za-z0-9][\w.-]*\b")
ALIAS = re.compile(r"(?i)\b(?:ssh|connect|log\s*in|login|remote)\s*(?:in)?(?:to|into|onto)?\s+(?:to\s+|into\s+)?(?:my\s+|the\s+|our\s+)?([A-Za-z][\w-]{2,})\b")
PATHLIKE = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/[\w.~/@-]{2,}")
CONTINUE_ONLY = re.compile(r"(?is)^\s*(?:yes|yep|yeah|y|ok(?:ay)?|sure|continue|go\s+on|go\s+ahead|proceed|carry\s+on|do\s+it|please\s+continue|"
                           r"yes[,!.\s]+(?:please\s+)?(?:continue|go\s+ahead|proceed|do\s+it)(?:\s+on)?|continue\s+on|keep\s+going|resume)\W*$")
_STOPWORDS = {"the", "server", "host", "machine", "box", "remote", "it", "this", "that", "my", "our", "a", "an", "to", "into",
              "and", "then", "there", "here", "via", "using"}


# The terminal flattens an over-long history into one user-role message made of transcript lines when it asks for a summary:
# "[N older exchanges omitted]" and "[User]: ...", "[Assistant]: ...", "[Tool result]: ..." blocks. Tool output inside it must
# never be read as something the user said.
_TRANSCRIPT = re.compile(r"^\[(?:\d+ older exchanges? omitted|User\]:|Assistant|Tool (?:result|error)\]:|Shell\]:|Synthetic context\]:|"
                         r"Skill activated)")


def is_real_user_text(text: str) -> bool:
    t = text.lstrip()
    return bool(t) and not any(t.startswith(p) for p in SYNTHETIC_PREFIXES) and not _TRANSCRIPT.match(t)


def is_continue_only(text: str) -> bool:
    return bool(CONTINUE_ONLY.match(text or ""))


@dataclass
class Scope:
    actions: list[str] = field(default_factory=list)       # subset of ssh inspect edit test deploy commit
    hosts: list[str] = field(default_factory=list)         # explicit ssh targets and addresses named by the user
    aliases: list[str] = field(default_factory=list)       # named machines ("my Spark")
    paths: list[str] = field(default_factory=list)
    autonomous: bool = False
    read_only: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> Scope:
        d = d or {}
        return cls(actions=list(d.get("actions") or []), hosts=list(d.get("hosts") or []), aliases=list(d.get("aliases") or []),
                   paths=list(d.get("paths") or []), autonomous=bool(d.get("autonomous")), read_only=bool(d.get("read_only")))

    def mentions_host(self, text: str) -> bool:
        low = text.lower()
        return any(h.lower().split("@")[-1] in low for h in self.hosts) or any(a.lower() in low for a in self.aliases)

    def names_target(self) -> bool:
        return bool(self.hosts or self.aliases)

    def allows(self, action: str) -> bool:
        """True when the user's request covers `action`, directly or through an explicit instruction to just carry on."""
        if action in self.actions:
            return True
        if not self.autonomous:
            return False
        # An instruction to proceed autonomously covers the routine steps of the job, not new kinds of risk.
        return action in {"inspect", "ssh"} or (action in {"edit", "test"} and bool(set(self.actions) & {"edit", "test", "deploy"}))


def extract(text: str) -> Scope:
    """Scope from one user request."""
    sc = Scope()
    for name, pat in ACTION_WORDS.items():
        for m in pat.finditer(text):
            if NEGATION.search(text[max(0, m.start() - 32):m.start()]):
                continue
            if name not in sc.actions:
                sc.actions.append(name)
            break
    for m in HOST.finditer(text):
        h = m.group(0).rstrip(".,;:")
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", h) or "@" in h or re.search(r"[A-Za-z]\.[A-Za-z]{2,}$", h) and "/" not in h:
            if not re.search(r"\.(?:py|js|ts|tsx|json|md|txt|toml|yaml|yml|html|css|sh|cfg|ini|log|rs|go|java|c|h|cpp|rb|lock)$", h, re.I):
                if h not in sc.hosts:
                    sc.hosts.append(h)
    for m in USER_AT.finditer(text):
        if m.group(0) not in sc.hosts:
            sc.hosts.append(m.group(0))
    for m in ALIAS.finditer(text):
        a = m.group(1)
        if a.lower() not in _STOPWORDS and a not in sc.aliases and a not in sc.hosts:
            sc.aliases.append(a)
    sc.paths = list(dict.fromkeys(m.group(0) for m in PATHLIKE.finditer(text)))[:12]
    if sc.hosts or sc.aliases:
        if "ssh" not in sc.actions and re.search(r"(?i)\b(?:ssh|connect|log\s*in|remote)\b", text):
            sc.actions.append("ssh")
    sc.autonomous = bool(AUTONOMOUS.search(text))
    changing = set(sc.actions) & {"edit", "deploy", "commit"}
    sc.read_only = bool(READ_ONLY.search(text)) and not changing or (not changing and "test" not in sc.actions and bool(sc.actions)
                                                                      and set(sc.actions) <= {"inspect", "ssh"})
    return sc
