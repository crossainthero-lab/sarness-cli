"""What a shell command is, from its text alone: SSH target and remote command, systemd scope, whether it runs tests,
deploys, verifies a deployment, or changes files. Nothing here executes anything; it only reads command strings, and
the answers feed the execution ledger (what actually happened) and the loop and continuation checks."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from ..policy import TEST_RUNNERS

_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n|\|)\s*")
_TARGET = r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9][A-Za-z0-9_.-]*)"
# "sshfylume@192.168.20.193": the model dropped the space after the program name (seen in a real Trinodal session).
_GLUED = re.compile(r"^(\s*(?:sudo\s+)?)(ssh|scp|sftp)(?=[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9])")
_EXIT = re.compile(r"(?:^|\n)\s*Exited with code (-?\d+)\s*$")
_SSH_OPTS_WITH_ARG = {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R",
                      "-S", "-W", "-w"}
_DEPLOY = re.compile(
    r"(?:^|[\s/])(?:rsync|scp)\b|(?:^|[\s/])(?:\./)?[\w./-]*deploy[\w.-]*(?:\.sh)?\b|"
    r"\bsystemctl\s+(?:--user\s+)?(?:restart|start|reload|enable|daemon-reload)\b|\bservice\s+\S+\s+(?:restart|start|reload)\b|"
    r"\bdocker\s+(?:compose\s+)?(?:up|restart|build)\b|\bkubectl\s+(?:apply|rollout|set\s+image)\b|\bpm2\s+(?:restart|reload|start)\b|"
    r"\bhelm\s+(?:upgrade|install)\b|\bansible-playbook\b|\bfly\s+deploy\b|\bgit\s+push\b")
_VERIFY = re.compile(
    r"\bsystemctl\s+(?:--user\s+)?(?:is-active|is-failed|status|show)\b|\bcurl\b|\bwget\b|\bhttp(?:ie)?\b|\bnc\s+-z\b|"
    r"\bss\s+-[a-z]*l|\bnetstat\b|\bdocker\s+(?:ps|logs|inspect)\b|\bjournalctl\b|\bpgrep\b|\bpidof\b|\bdiff\s+-[a-z]*r|"
    r"\b(?:sha256sum|md5sum|cksum)\b|\bkubectl\s+(?:get|rollout\s+status)\b|\bhealth\b|\bsmoke\b")
_REMOTE_MUTATE = re.compile(r"\bsed\s+-[a-z]*i|\btee\b|(?<![0-9&>])>{1,2}\s*[^&\s/][^\s]*|\bmv\b|\bcp\b|\brm\b|\bpatch\b|"
                            r"\bgit\s+(?:apply|commit|checkout|reset|merge|rebase|stash|cherry-pick|restore)\b|\binstall\b\s+-")
_COMMIT = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")
_READ_ONLY_PROGRAMS = {"ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "whoami", "date", "uname", "file", "stat",
                       "find", "grep", "rg", "tree", "du", "df", "ps", "ss", "env", "id", "hostname", "free", "uptime",
                       "readlink", "realpath", "basename", "dirname", "sort", "uniq", "cut", "tr", "awk", "jq", "less"}
_READ_ONLY_GIT = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame", "describe", "remote", "config",
                  "grep", "shortlog", "tag"}
_SYSTEMCTL_STATE = {"status", "is-active", "is-failed", "show"}
_RSYNC_REMOTE = re.compile(r"(?:^|\s)(?:[\w.-]+@)?(\[[0-9a-fA-F:]+\]|[A-Za-z0-9][\w.-]*):(?![0-9]+(?:/|$))[~/\w.${}\"'-]")
_SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "show", "list-units", "list-unit-files", "cat",
                   "list-timers", "list-sockets"}


@dataclass
class Cmd:
    raw: str
    kind: str = "other"            # read | mutate | test | deploy | verify | commit | ssh-session | other
    host: str | None = None        # SSH target ("user@host") of the outermost ssh invocation
    remote: str | None = None      # the command run over SSH, unquoted
    systemctl: dict | None = None  # {"verbs": [...], "units": [...], "scope": "user" | "system"}
    dest: str | None = None        # remote host an rsync/scp copies to or from
    notes: list[str] = field(default_factory=list)

    @property
    def effective(self) -> str:
        """The command that really runs where the work happens (the remote command for ssh, else the whole line)."""
        return self.remote if self.remote is not None else self.raw


def parse_exit(result_text: str) -> int | None:
    """Exit status of a finished shell tool call, when its output carries one ("Exited with code 127"). A call that
    reports no failure is a success; a call whose text says it was interrupted, timed out or never ran is unknown."""
    if not isinstance(result_text, str):
        return None
    match = _EXIT.search(result_text)
    if match:
        return int(match.group(1))
    native = re.match(r"\s*exit(?: code:?)?\s+(-?\d+)\b(?:;\s*timeout:\s*(True|False))?", result_text)
    if native:                                      # the Python runtime's own result header
        return None if native.group(2) == "True" else int(native.group(1))
    head = result_text[:300].lower()
    if any(w in head for w in ("not executed", "interrupted by the user", "timed out", "timeout", "killed", "denied:",
                               "permission error", "tool error")):
        return None
    return 0


def split_simple(cmd: str) -> list[str]:
    """Simple commands of a compound line (outside quotes it splits on && || ; | newline)."""
    parts, buf, quote = [], "", ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf += ch
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                buf += cmd[i + 1]
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf += ch
        else:
            m = _SPLIT.match(cmd, i) if ch in "&|;\n" else None
            if m and m.end() > i:
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""
                i = m.end()
                continue
            buf += ch
        i += 1
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def fix_glued_ssh(cmd: str) -> tuple[str, str] | None:
    """Repair the one unambiguous construction error: `sshuser@host ...` -> `ssh user@host ...` (also scp, sftp).
    Returns (fixed command, explanation) or None. Nothing else is rewritten."""
    fixed, n = _GLUED.subn(lambda m: f"{m.group(1)}{m.group(2)} ", cmd, count=1)
    if n and fixed != cmd:
        glued = cmd.strip().split()[0]
        return fixed, f"`{glued}` has no space after the program name; ran `{fixed.strip().split()[0]} {fixed.strip().split()[1]}`"
    # the glued form can also follow a separator: `cd x && sshuser@host ...`
    pieces = split_simple(cmd)
    if len(pieces) > 1:
        changed = False
        for piece in pieces:
            if _GLUED.match(piece):
                new = _GLUED.sub(lambda m: f"{m.group(1)}{m.group(2)} ", piece, count=1)
                fixed = fixed.replace(piece, new, 1) if fixed != cmd else cmd.replace(piece, new, 1)
                changed = True
        if changed and fixed != cmd:
            return fixed, "a `sshuser@host` command was missing the space after `ssh`"
    return None


def parse_ssh(cmd: str) -> tuple[str, str | None] | None:
    """(target, remote command) for the first `ssh` invocation in a command line, else None. Understands options with
    arguments (-o X, -i key, -p port, -J jump). The remote command is the words after the target joined and unquoted."""
    for piece in split_simple(cmd):
        words = _words(piece)
        while words and (words[0] in ("sudo", "time", "nohup", "env", "command", "exec") or "=" in words[0].split("/")[0]):
            words = words[1:]
        if not words or words[0].rsplit("/", 1)[-1] != "ssh":
            continue
        i, target = 1, None
        while i < len(words):
            w = words[i]
            if w.startswith("-") and len(w) > 1:
                i += 2 if w in _SSH_OPTS_WITH_ARG else 1
                continue
            target = w
            i += 1
            break
        if not target or not re.fullmatch(_TARGET, target):
            continue
        rest = words[i:]
        if len(rest) == 1:
            return target, rest[0]
        return target, (" ".join(shlex.quote(r) if re.search(r"\s", r) else r for r in rest) if rest else None)
    return None


def host_of(target: str | None) -> str | None:
    """Bare host of an ssh target (user@host -> host)."""
    return target.rsplit("@", 1)[-1].strip("[]") if target else None


def parse_systemctl(cmd: str) -> dict | None:
    """{"verbs", "units", "scope"} for systemctl commands in a line (scope is "user" only when --user is given)."""
    verbs: list[str] = []
    units: list[str] = []
    scope = "system"
    seen = False
    for piece in split_simple(cmd):
        words = _words(piece)
        while words and words[0] in ("sudo", "time", "env", "command", "exec"):
            words = words[1:]
        if not words or words[0].rsplit("/", 1)[-1] != "systemctl":
            continue
        seen = True
        args = [w for w in words[1:]]
        if "--user" in args:
            scope = "user"
        positional = [a for a in args if not a.startswith("-") and re.fullmatch(r"[A-Za-z0-9_.@:\\-]+", a)]
        if positional:
            verbs.append(positional[0])
            units += [u for u in positional[1:] if not u.startswith("/") and not re.fullmatch(r"\d*[<>].*", u)]
    return {"verbs": verbs, "units": units, "scope": scope} if seen else None


def command_kind(effective: str) -> str:
    """test | deploy | commit | verify | mutate | read | other for what the command does where it runs."""
    pieces = split_simple(effective) or [effective]
    kinds: set[str] = set()
    for piece in pieces:
        words = _words(piece)
        while words and (words[0] in ("sudo", "time", "env", "nohup", "command", "exec", "nice") or re.match(r"\w+=", words[0])):
            words = words[1:]
        prog = words[0].rsplit("/", 1)[-1] if words else ""
        text = " ".join(words)
        if _COMMIT.search(text):
            kinds.add("commit")
        elif prog == "ssh":
            continue                                            # classified by its remote command
        elif TEST_RUNNERS.search(" " + text) and prog not in {"echo", "cat", "grep", "ls"} and not text.startswith("make deploy"):
            kinds.add("test")
        elif _DEPLOY.search(text) and prog not in {"echo", "cat", "grep", "ls", "head", "tail", "diff"}:
            kinds.add("deploy")
        elif prog == "systemctl":
            info = parse_systemctl(text) or {"verbs": []}
            if all(v in _SYSTEMCTL_STATE for v in info["verbs"]):
                kinds.add("verify")                             # asks whether a service is up: evidence a deployment worked
            else:
                kinds.add("read" if all(v in _SYSTEMCTL_READ for v in info["verbs"]) else "deploy")
        elif _VERIFY.search(text) and prog not in {"sed", "tee"}:
            kinds.add("verify")
        elif prog == "git":
            sub = next((w for w in words[1:] if not w.startswith("-") and w not in ("-C",)), "")
            kinds.add("read" if sub in _READ_ONLY_GIT else "mutate")
        elif prog in _READ_ONLY_PROGRAMS and not _REMOTE_MUTATE.search(text):
            kinds.add("read")
        elif _REMOTE_MUTATE.search(text) or prog in {"pip", "pip3", "npm", "make", "touch", "mkdir", "chmod", "chown", "ln",
                                                     "uv", "pnpm", "yarn", "apt", "apt-get", "sed", "python", "python3", "node"}:
            kinds.add("mutate" if prog not in {"python", "python3", "node"} or _REMOTE_MUTATE.search(text) else "other")
        else:
            kinds.add("other")
    for k in ("commit", "deploy", "test", "mutate", "verify", "read", "other"):
        if k in kinds:
            return k
    return "other"


def classify(cmd: str) -> Cmd:
    """Everything the ledger needs to know about one shell command line."""
    info = Cmd(raw=cmd)
    ssh = parse_ssh(cmd)
    if ssh:
        info.host, info.remote = ssh
        if info.remote is None:
            info.kind = "ssh-session"
            return info
    info.systemctl = parse_systemctl(info.effective)
    info.kind = command_kind(info.effective)
    if not ssh and re.search(r"\b(?:rsync|scp)\b", cmd):
        m = _RSYNC_REMOTE.search(cmd.split("rsync", 1)[-1] if "rsync" in cmd else cmd.split("scp", 1)[-1])
        info.dest = m.group(1) if m else None
    if ssh and info.kind == "other":
        info.kind = "read" if not _REMOTE_MUTATE.search(info.effective) else "mutate"
    # a plain ssh command that only reaches the host is still a read of that host
    return info
