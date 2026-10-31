"""The execution ledger: durable, structured task state kept outside the conversation.

It is filled only from tool calls the runtime actually observed (a call, its arguments and its result as they appear in
the message history), never from what the model says about them. It survives compaction, restarts and resume because
it lives in its own file, and both agent runtimes read and write the same versioned format (`sarness.ledger/1`):

    objective   the user's request and what it authorises            facts      verified discoveries and where from
    ops         commands/reads/edits that really ran, with exit codes   files      what was changed, with content hashes
    tests       test runs and the workspace revision they ran against   deploys    deployments and whether they were verified
    failures    commands that failed, with the remedy once one is known env        classifications learned (systemd scope, ...)
    blockers    genuine obstacles (authentication, missing host)        interventions  every time the guard stepped in
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import config
from . import classify as C
from . import scope as S

SCHEMA = "sarness.ledger/1"
MAX_OPS = 300
MAX_FACTS = 60
MAX_SEEN = 2000
MAX_INTERVENTIONS = 200

# canonical tool kinds, across both runtimes (the terminal's tools and the Python runtime's tools)
READ_TOOLS = {"read", "read_file", "list_dir", "glob", "grep", "search", "find_files", "git_status", "git_diff", "git_log",
              "webfetch", "websearch", "skill"}
WRITE_TOOLS = {"write", "edit", "write_file", "append_file", "edit_file", "delete_path", "move_path", "apply_patch"}
SHELL_TOOLS = {"shell", "run_command", "bash"}
PATH_KEYS = ("path", "filePath", "file_path", "from", "to")


def _sha(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


def file_hash(path: str | Path, cap: int = 4_000_000) -> tuple[str, int] | None:
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > cap:
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16], p.stat().st_size
    except OSError:
        return None


def ledger_dir() -> Path:
    d = config.state_dir() / "ledgers"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:120] or "default"


_NOISE = re.compile(r"\s+2>&1|\s+2>/dev/null|\s+--no-pager|\s+-n\s*\d+\b|\s*\|\s*(?:head|tail)(?:\s+-\w*\d+|\s+-n\s*\d+)?\s*$")


def normalize_command(cmd: str) -> str:
    """Stable form of a command for 'is this the same thing again': separators, redirections, pagers and `head`/`tail`
    trimming are dropped; the simple commands inside a compound line are sorted so reordering them is the same command."""
    info = C.classify(cmd)
    host = C.host_of(info.host)
    body = info.effective
    parts = []
    for piece in C.split_simple(body) or [body]:
        words = C._words(piece)
        if words and words[0] in ("echo", "printf", "head", "tail"):
            continue                                  # separators, banners and output trimming
        piece = _NOISE.sub("", piece).strip()
        piece = re.sub(r"\s+", " ", piece)
        if piece and piece not in parts:
            parts.append(piece)
    norm = "; ".join(sorted(parts)) or re.sub(r"\s+", " ", body).strip()
    return f"{host}: {norm}" if host else norm


PLAN_TOOLS = {"update_plan", "todowrite", "todoread"}


def fingerprint(tool: str, args: dict[str, Any]) -> str:
    if tool in SHELL_TOOLS:
        return "sh:" + _sha(normalize_command(str(args.get("command") or "")))
    path = next((str(args[k]) for k in PATH_KEYS if args.get(k)), "")
    if tool in READ_TOOLS:                 # reads are the same read whatever incidental options are set
        keep = {k: args[k] for k in ("pattern", "include", "query", "url", "offset", "limit", "id") if k in args}
    else:                                  # anything else is identified by all of its arguments
        keep = {k: v for k, v in args.items() if k not in PATH_KEYS}
    return f"{tool}:" + _sha(path + json.dumps(keep, sort_keys=True, ensure_ascii=False, default=str))


def _first_line(text: str, n: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("Exited with code"):
            return line[:n]
    return ""


def _summary(kind: str, tool: str, args: dict, text: str) -> str:
    if kind == "test":
        m = re.findall(r"(\d+)\s+(passed|failed|error|errors|skipped)", text)
        if m:
            return ", ".join(f"{n} {w}" for n, w in m[:4])
        for line in reversed((text or "").splitlines()):
            if re.search(r"(?i)\b(ok|pass|fail|error|success)", line):
                return line.strip()[:160]
    if tool in {"read", "read_file"}:
        m = re.search(r"lines? (\d+)-(\d+)", text or "")
        return f"{m.group(0)}" if m else _first_line(text, 80)
    return _first_line(text)


class Ledger:
    def __init__(self, key: str, *, runtime: str = "tui", path: Path | None = None):
        self.key = key
        self.path = path or ledger_dir() / f"{safe_key(key)}.json"
        self.d: dict[str, Any] = {
            "schema": SCHEMA, "key": key, "runtime": runtime, "created": time.time(), "updated": time.time(),
            "parent": None, "workspace": None,
            "objective": {"text": "", "set_at": None, "scope": S.Scope().to_dict(), "requests": []},
            "phase": "discovery", "rev": 0, "ops_total": 0, "ops": [], "facts": [], "files": {}, "tests": [], "deploys": [],
            "failures": {}, "env": {"systemd_scope": {}, "deploy_kind": {}, "units": {}}, "blockers": [], "interventions": [],
            "compactions": [], "counters": {}, "seen": [], "fp_index": [], "pending": {},
        }

    # ------------------------------------------------------------------ persistence
    @classmethod
    def open(cls, key: str, *, runtime: str = "tui") -> Ledger:
        led = cls(key, runtime=runtime)
        try:
            data = json.loads(led.path.read_text("utf-8"))
            if isinstance(data, dict) and data.get("schema") == SCHEMA:
                led.d.update(data)
                led.d["runtime"] = data.get("runtime") or runtime
        except (OSError, ValueError):
            pass
        return led

    def save(self) -> None:
        self.d["updated"] = time.time()
        self.d["ops"] = self.d["ops"][-MAX_OPS:]
        self.d["facts"] = self.d["facts"][-MAX_FACTS:]
        self.d["seen"] = self.d["seen"][-MAX_SEEN:]
        self.d["interventions"] = self.d["interventions"][-MAX_INTERVENTIONS:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(prefix=".ledger-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=1, default=str)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass                                   # the ledger must never take the agent down

    # ------------------------------------------------------------------ small accessors
    @property
    def scope(self) -> S.Scope:
        return S.Scope.from_dict(self.d["objective"].get("scope"))

    @property
    def objective(self) -> str:
        return self.d["objective"].get("text") or ""

    @property
    def rev(self) -> int:
        return int(self.d["rev"])

    def counter(self, name: str, delta: int = 0, *, reset: bool = False) -> int:
        c = self.d["counters"]
        if reset:
            c[name] = 0
        c[name] = int(c.get(name, 0)) + delta
        return c[name]

    def intervene(self, kind: str, detail: str, **extra: Any) -> None:
        self.d["interventions"].append({"t": round(time.time(), 1), "kind": kind, "detail": detail[:300], "rev": self.rev, **extra})
        self.counter("interventions:" + kind, 1)
        self.counter("interventions", 1)

    def interventions(self, kind: str | None = None) -> list[dict]:
        return [i for i in self.d["interventions"] if kind is None or i["kind"] == kind]

    # ------------------------------------------------------------------ the request
    def set_request(self, text: str) -> bool:
        """Record a genuine user message. A bare "yes, continue" keeps the standing objective; any other message of
        substance becomes the current one (the previous ones stay in `requests`). Returns True when the objective changed."""
        text = (text or "").strip()
        if not text or not S.is_real_user_text(text):
            return False
        obj = self.d["objective"]
        reqs = obj["requests"]
        if reqs and reqs[-1]["sha"] == _sha(text):
            return False
        reqs.append({"sha": _sha(text), "text": text[:4000], "t": round(time.time(), 1)})
        del reqs[:-12]
        # a new instruction from the user lifts a stop the guard itself put in place
        self.d["blockers"] = [b for b in self.d["blockers"] if not str(b.get("key", "")).startswith("guard:")]
        self.counter("nudges", reset=True)
        self.counter("premature", reset=True)
        self.counter("stalled_compactions", reset=True)
        if S.is_continue_only(text):
            obj["scope"] = S.Scope.from_dict(obj.get("scope")).to_dict()
            self.counter("user_continues", 1)
            # the user said to carry on: that is itself an instruction to stop asking
            sc = S.Scope.from_dict(obj.get("scope"))
            sc.autonomous = True
            obj["scope"] = sc.to_dict()
            return False
        if len(text) < 12 and self.objective:
            return False
        obj["text"] = text[:12000]
        obj["set_at"] = round(time.time(), 1)
        obj["scope"] = S.extract(text).to_dict()
        self.d["phase"] = "discovery"
        self.counter("nudges", reset=True)
        return True

    # ------------------------------------------------------------------ evidence
    def ingest(self, messages: list[dict[str, Any]], *, workspace: str | None = None) -> list[dict]:
        """Add every finished tool call in `messages` that has not been seen. Returns the new ops."""
        if workspace and not self.d.get("workspace"):
            self.d["workspace"] = workspace
        calls: dict[str, tuple[str, dict]] = {}
        new_ops = []
        seen = set(self.d["seen"])
        for m in messages:
            role = m.get("role")
            if role == "assistant":
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    raw = fn.get("arguments")
                    try:
                        args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
                    except ValueError:
                        args = {}
                    calls[str(c.get("id"))] = (str(fn.get("name") or ""), args if isinstance(args, dict) else {})
            elif role == "tool":
                cid = str(m.get("tool_call_id"))
                if cid not in calls:
                    continue
                name, args = calls[cid]
                content = m.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
                sid = f"{cid}:{_sha(name + json.dumps(args, sort_keys=True, default=str), 6)}:{_sha(text, 6)}"
                if sid in seen:
                    continue
                seen.add(sid)
                self.d["seen"].append(sid)
                self.d["pending"].pop(cid, None)
                if text.lstrip().startswith(("NOT EXECUTED", "DENIED:")):
                    self.counter("not_executed", 1)        # refused or denied: it did not run, so it is not evidence of anything
                    continue
                op = self.record(name, args, text)
                if op:
                    new_ops.append(op)
        return new_ops

    # ------------------------------------------------------------------ calls forwarded to the terminal whose results have not been seen
    def note_pending(self, calls: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Calls the model made and the runtime forwarded for execution: (id, tool, args). Their results come back in a later
        request, or, when that request is a compaction, only as text inside the checkpoint."""
        for cid, name, args in calls:
            self.d["pending"][str(cid)] = {"name": name, "args": args, "t": round(time.time(), 1), "fp": fingerprint(name, args)}
        for k in list(self.d["pending"])[:-60]:
            del self.d["pending"][k]

    def settle_from_recent(self, recent: str) -> int:
        """Results the terminal kept as text in a checkpoint's recent part. Only calls this ledger saw the model make are
        resolved (matched by tool and exact arguments), so text cannot add operations that never happened."""
        n = 0
        blocks = _RECENT_CALL.split(recent or "")
        # split() with a capturing group alternates: [before, name, args, body, name, args, body, ...]
        for i in range(1, len(blocks) - 2, 3):
            name, raw_args, body = blocks[i], blocks[i + 1], blocks[i + 2]
            try:
                args = json.loads(raw_args)
            except ValueError:
                continue
            m = re.match(r"\[Tool (?:result|error)\]: ?(.*)", body.lstrip("\n"), re.S)
            if not m:
                continue
            text = _RECENT_NEXT.split(m.group(1))[0].rstrip("\n")
            for cid, p in list(self.d["pending"].items()):
                if p["name"] == name and json.dumps(p["args"], sort_keys=True) == json.dumps(args, sort_keys=True):
                    del self.d["pending"][cid]
                    if self.record(name, p["args"], text):
                        n += 1
                    break
        return n

    def settle_lost(self) -> int:
        """Pending calls whose results never reappeared anywhere (the history was compacted): they ran, but what they
        returned is gone. Record them as executed with an unknown result so they are neither forgotten nor presumed good."""
        n = 0
        for cid, p in list(self.d["pending"].items()):
            del self.d["pending"][cid]
            op = self.record(p["name"], p["args"], "[result not retained after compaction]", ok=None)
            if op:
                op["exit"], op["lost"] = None, True
                op["summary"] = "ran; its result was not kept after compaction"
                n += 1
        return n

    def record(self, tool: str, args: dict[str, Any], text: str, *, ok: bool | None = None) -> dict | None:
        """Account for one finished tool call. `ok` overrides the success inferred from the result text."""
        if tool == "question":
            return None
        kind, host, cmd_text, info = "other", None, "", None
        exit_code: int | None = None
        if tool in SHELL_TOOLS:
            cmd_text = str(args.get("command") or "")
            info = C.classify(cmd_text)
            kind, host = info.kind, C.host_of(info.host) or info.dest
            exit_code = C.parse_exit(text)
        elif tool in WRITE_TOOLS:
            kind = "mutate"
            cmd_text = next((str(args[k]) for k in PATH_KEYS if args.get(k)), "")
            exit_code = 0 if re.search(r"(?i)^(?:error|failed|could not|no such|cannot|invalid|denied|tool error|not executed)", (text or "").strip()) is None else 1
        elif tool in READ_TOOLS:
            kind = "read"
            cmd_text = next((str(args[k]) for k in (*PATH_KEYS, "pattern", "query", "url", "id") if args.get(k)), "") or tool
            exit_code = 0 if not re.match(r"(?i)\s*(?:error|tool error|not executed|file not found)", text or "") else 1
        else:
            cmd_text = json.dumps(args, ensure_ascii=False, default=str)[:200]
            exit_code = 0 if text and not re.match(r"(?i)\s*(?:error|tool error|not executed)", text) else None
        if ok is not None:
            exit_code = 0 if ok else (exit_code or 1)
        op = {"n": self.d["ops_total"] + 1, "tool": tool, "kind": kind, "fp": fingerprint(tool, args), "cmd": cmd_text[:400],
              "host": host, "exit": exit_code, "digest": _sha(text or ""), "rev": self.rev, "t": round(time.time(), 1),
              "summary": _summary(kind, tool, args, text or ""), "bytes": len(text or "")}
        self.d["ops_total"] += 1
        self.d["ops"].append(op)
        if tool in PLAN_TOOLS:
            op["kind"] = kind = "plan"
        if op["fp"] not in self.d["fp_index"]:
            self.d["fp_index"].append(op["fp"])           # something not done before: that is progress, not a loop
            del self.d["fp_index"][:-3000]
        succeeded = exit_code == 0
        # ---- state transitions, from evidence only
        if kind == "mutate" and exit_code != 1 and (tool in WRITE_TOOLS or (info is not None and not _is_remote_deploy(info))):
            self.d["rev"] += 1
            op["rev"] = self.d["rev"]
            if tool in WRITE_TOOLS and succeeded:
                self._note_file(args, op)
            if exit_code is None:
                op["uncertain"] = True
        if kind == "commit" and succeeded:
            self.d.setdefault("commits", []).append({"cmd": cmd_text[:200], "rev": self.rev, "t": op["t"]})
        if kind == "test":
            self.d["tests"].append({"cmd": cmd_text[:300], "exit": exit_code, "rev": self.rev, "host": host, "t": op["t"],
                                    "summary": op["summary"]})
        if kind == "deploy":
            self.d["deploys"].append({"cmd": cmd_text[:300], "exit": exit_code, "rev": self.rev, "host": host, "t": op["t"],
                                      "verified": False, "verified_by": None, "n": op["n"]})
        if kind in {"verify", "read"} and succeeded and self.d["deploys"]:
            self._maybe_verify_deploy(op)
        if kind == "read" and succeeded and tool in READ_TOOLS | SHELL_TOOLS:
            self.note_text_evidence(text or "", op)
        if info is not None:
            self._learn(op, info, text or "", exit_code)
        self._learn_failure(op, tool, args, text or "")
        self._advance_phase()
        return op

    def _note_file(self, args: dict, op: dict) -> None:
        path = next((str(args[k]) for k in ("path", "filePath", "file_path", "to") if args.get(k)), "")
        if not path:
            return
        entry = self.d["files"].setdefault(path, {"edits": 0})
        entry["edits"] += 1
        entry["rev"] = self.rev
        entry["t"] = op["t"]
        base = self.d.get("workspace") or ""
        h = file_hash(path if os.path.isabs(path) else os.path.join(base, path))
        if h:
            entry["sha"], entry["size"] = h

    def _maybe_verify_deploy(self, op: dict) -> None:
        for dep in reversed(self.d["deploys"]):
            if dep["exit"] == 0 and not dep["verified"] and op["n"] > dep["n"]:
                if op["kind"] == "verify" or (dep.get("host") and op.get("host") == dep.get("host")):
                    dep["verified"], dep["verified_by"] = True, op["cmd"][:200]
                    return

    def _learn(self, op: dict, info: C.Cmd, text: str, exit_code: int | None) -> None:
        host = C.host_of(info.host) or "local"
        low = text.lower()
        sc = info.systemctl
        if sc:
            units = sc["units"]
            if sc["scope"] == "user":
                found = "loaded:" in low or exit_code in (0, 3) and "could not be found" not in low and "failed to connect" not in low
                if found and units:
                    self.d["env"]["systemd_scope"][host] = "user"
                    for u in units:
                        self.d["env"]["units"][f"{host}:{u}"] = "user"
                    self.add_fact(f"systemd:{host}", f"On {host} the service is managed with `systemctl --user` "
                                  f"({', '.join(units)}); querying the system scope reports 'could not be found'.",
                                  op, host=host)
            elif exit_code == 4 and "could not be found" in low and units:
                self.add_fact(f"systemd-system-miss:{host}:{','.join(units)}",
                              f"On {host}, `systemctl {' '.join(sc['verbs'][:1])} {' '.join(units)}` (system scope) reports the unit "
                              "could not be found; it is not a system service. Check `systemctl --user` before concluding it does not exist.",
                              op, host=host)
        if "not a git repository" in low:
            where = _path_in(info.effective) or "the directory"
            rs = any("rsync" in (o.get("cmd") or "") for o in self.d["ops"]) or "rsync" in self.objective.lower() or \
                self._seen_rsync_text()
            kind = "rsync-deployment" if rs else "deployment-copy"
            self.d["env"]["deploy_kind"][f"{host}:{where}"] = kind
            self.add_fact(f"deploy-kind:{host}:{where}",
                          f"{where} on {host} has no .git: it is a {'rsync ' if rs else ''}deployment copy, not a broken source repository. "
                          "The source repository is the local workspace; do not try to repair or `git init` the copy.", op, host=host)
        if re.search(r"(?i)permission denied \((?:publickey|password|keyboard)", text) or "host key verification failed" in low:
            self.add_blocker(f"authentication to {host} failed: {_first_line(text, 120)}", op, key=f"auth:{host}")
        if re.search(r"(?i)connection (?:timed out|refused)|no route to host|could not resolve hostname", text) and info.host:
            self.add_blocker(f"cannot reach {host}: {_first_line(text, 120)}", op, key=f"reach:{host}")

    def _seen_rsync_text(self) -> bool:
        return any(f.get("key", "").startswith("saw-rsync") for f in self.d["facts"])

    def note_text_evidence(self, text: str, source: dict) -> None:
        """Facts that come from reading files (deploy scripts), e.g. that a project deploys with rsync."""
        if re.search(r"\brsync\b", text or "") and re.search(r"(?i)deploy", text or ""):
            self.add_fact("saw-rsync", "The project's deploy script uses rsync: deployments are file copies, not git checkouts.", source)

    def _learn_failure(self, op: dict, tool: str, args: dict, text: str) -> None:
        fails = self.d["failures"]
        fp = op["fp"]
        if op["kind"] == "test" or op["exit"] in (0, None):
            if op["exit"] == 0 and fp in fails:
                fails[fp]["resolved_at_rev"] = self.rev
            if op["exit"] == 0:
                self._learn_remedy(op)
            return
        entry = fails.setdefault(fp, {"cmd": op["cmd"][:300], "count": 0, "error": "", "rev": self.rev, "remedy": None})
        entry["count"] += 1
        entry["rev"] = self.rev
        entry["exit"] = op["exit"]
        entry["error"] = _first_line(text, 200)
        entry["last_op"] = op["n"]
        entry.pop("resolved_at_rev", None)

    def _learn_remedy(self, op: dict) -> None:
        """A success that closely resembles the most recent failure is that failure's verified correction."""
        if op["tool"] not in SHELL_TOOLS:
            return
        for fp, f in reversed(list(self.d["failures"].items())):
            if f.get("remedy") or f.get("last_op", 0) < op["n"] - 6 or fp == op["fp"]:
                continue
            a, b = set(re.findall(r"[\w@./~-]+", f["cmd"])), set(re.findall(r"[\w@./~-]+", op["cmd"]))
            glued = re.sub(r"\s", "", f["cmd"].split(" ", 1)[0])
            if a and b and len(a & b) / len(a | b) >= 0.5 or (glued and glued in op["cmd"].replace(" ", "", 1)):
                f["remedy"] = op["cmd"][:300]
                return

    # ------------------------------------------------------------------ facts and blockers
    def add_fact(self, key: str, text: str, source: dict | None, *, host: str | None = None) -> None:
        src = {"op": source.get("n"), "tool": source.get("tool"), "cmd": (source.get("cmd") or "")[:140], "exit": source.get("exit")} \
            if source else {}
        for f in self.d["facts"]:
            if f.get("key") == key:
                if f["text"] != text:
                    f.update(text=text, source=src, t=round(time.time(), 1), current=True)
                return
        self.d["facts"].append({"key": key, "text": text, "source": src, "host": host, "workspace": self.d.get("workspace"),
                                "t": round(time.time(), 1), "current": True})

    def add_blocker(self, text: str, source: dict | None = None, *, key: str | None = None) -> None:
        for b in self.d["blockers"]:
            if b.get("key") == (key or text):
                return
        self.d["blockers"].append({"key": key or text, "text": text, "t": round(time.time(), 1),
                                   "op": (source or {}).get("n")})

    def clear_blocker(self, key_prefix: str) -> None:
        self.d["blockers"] = [b for b in self.d["blockers"] if not str(b.get("key", "")).startswith(key_prefix)]

    def invalidate_facts(self) -> None:
        """A source change makes workspace facts about earlier file contents stale (host facts stay)."""
        for f in self.d["facts"]:
            if f.get("key", "").startswith("file:"):
                f["current"] = False

    # ------------------------------------------------------------------ progress, criteria, next action
    def criteria(self) -> list[dict]:
        sc = self.scope
        ops = self.d["ops"]
        out: list[dict] = []
        if sc.read_only and not (set(sc.actions) & {"edit", "deploy", "commit"}):
            return [{"id": "answer", "text": "answer the question from what was inspected", "met": bool(ops), "evidence": None}]
        if "ssh" in sc.actions or sc.hosts:
            ssh_ok = next((o for o in ops if o.get("host") and o["exit"] == 0), None)
            out.append({"id": "ssh", "text": "reach the named server over ssh", "met": ssh_ok is not None,
                        "evidence": ssh_ok and ssh_ok["cmd"][:100]})
        if "inspect" in sc.actions or not set(sc.actions) & {"edit", "test", "deploy"}:
            out.append({"id": "inspect", "text": "inspect the project", "met": any(o["kind"] in {"read", "verify"} and o["exit"] == 0 for o in ops),
                        "evidence": None})
        if "edit" in sc.actions:
            out.append({"id": "edit", "text": "change the source to implement the fix", "met": bool(self.d["files"]) or
                        any(o["kind"] == "mutate" and o["exit"] in (0, None) for o in ops), "evidence": sorted(self.d["files"])[:4]})
        if "test" in sc.actions or "deploy" in sc.actions:
            fresh = [t for t in self.d["tests"] if t["exit"] == 0 and t["rev"] == self.rev]
            out.append({"id": "test", "text": "run the tests against the changed code" if self.d["files"] else "run the tests",
                        "met": bool(fresh) and (bool(self.d["files"]) or "edit" not in sc.actions), "evidence": fresh[-1]["cmd"][:100] if fresh else None})
        if "deploy" in sc.actions:
            deps = [d for d in self.d["deploys"] if d["exit"] == 0 and d["rev"] == self.rev]
            tested_first = any(t["exit"] == 0 and t["rev"] == self.rev for t in self.d["tests"]) or "test" not in sc.actions
            out.append({"id": "deploy", "text": "deploy the tested change", "met": bool(deps) and tested_first,
                        "evidence": deps[-1]["cmd"][:100] if deps else None})
            out.append({"id": "verify-deploy", "text": "independently verify the deployment", "met": any(d["verified"] for d in deps),
                        "evidence": next((d["verified_by"] for d in deps if d["verified"]), None)})
        if "commit" in sc.actions:
            out.append({"id": "commit", "text": "commit the change", "met": any(c["rev"] == self.rev for c in self.d.get("commits", [])),
                        "evidence": None})
        return out

    def unmet(self) -> list[dict]:
        return [c for c in self.criteria() if not c["met"]]

    def _advance_phase(self) -> None:
        crit = {c["id"]: c["met"] for c in self.criteria()}
        if self.d["blockers"] and not any(o["exit"] == 0 for o in self.d["ops"][-3:]):
            phase = "blocked"
        elif not crit or all(crit.values()):
            phase = "done" if self.d["ops"] else "discovery"
        elif "verify-deploy" in crit and crit.get("deploy"):
            phase = "verifying"
        elif crit.get("test") is False and (crit.get("edit", True)):
            phase = "testing" if crit.get("edit", True) and self.d["files"] else "implementing" if "edit" in crit else "testing"
        elif crit.get("deploy") is False and crit.get("test", True):
            phase = "deploying"
        elif crit.get("edit") is False and crit.get("inspect", True):
            phase = "implementing"
        else:
            phase = "discovery"
        self.d["phase"] = phase

    def next_action(self) -> str:
        sc = self.scope
        un = self.unmet()
        if self.d["blockers"] and not un:
            return "report the blocker(s) listed above"
        if not un:
            return "give the final report, citing the evidence above"
        first = un[0]
        files = ", ".join(sorted(self.d["files"])[:4])
        host = (sc.hosts or sc.aliases or [""])[0]
        steps = {
            "ssh": f"connect to {host or 'the named server'} with a plain `ssh user@host 'command'` (a space after ssh) and run one read-only command",
            "inspect": "inspect only what is not already established above, then move on to the change",
            "answer": "answer the user's question from what was inspected",
            "edit": "make the code change now (write/edit tools); the investigation above is enough to start",
            "test": f"run the test suite against the changed code ({files})" if files else "run the test suite",
            "deploy": "deploy the tested change using the project's own deploy method",
            "verify-deploy": "verify the deployment independently (service status, health check or file comparison on the target)",
            "commit": "commit the change",
        }
        return steps.get(first["id"], first["text"])

    def progress_marker(self) -> tuple:
        """Changes whenever real progress is made (new criterion met or a source/deploy/test operation happened)."""
        return (self.rev, len(self.d["tests"]), len(self.d["deploys"]), sum(1 for c in self.criteria() if c["met"]),
                sum(1 for d in self.d["deploys"] if d["verified"]), len(self.d["fp_index"]))

    def plan_streak(self) -> int:
        """Consecutive planning-tool calls at the end of the history (planning without doing anything)."""
        n = 0
        for o in reversed(self.d["ops"]):
            if o["kind"] != "plan":
                break
            n += 1
        return n

    # ------------------------------------------------------------------ repeats
    def find_repeat(self, tool: str, args: dict[str, Any], visible: set[str] | None = None) -> dict | None:
        """The earlier op this call would repeat for no reason, if any: an identical read-only/diagnostic call whose
        result cannot have changed (no source change, deploy or other mutation since), or a test run with nothing new.
        `visible` is the set of call fingerprints whose results the model can still see; re-reading a file or search
        whose result was compacted away is legitimate (the model needs the text to work on it), so those are not repeats."""
        fp = fingerprint(tool, args)
        if visible is not None and tool in READ_TOOLS and fp not in visible:
            return None
        kind = "other"
        if tool in SHELL_TOOLS:
            kind = C.classify(str(args.get("command") or "")).kind
        elif tool in READ_TOOLS:
            kind = "read"
        elif tool in WRITE_TOOLS:
            return None
        if kind not in {"read", "verify", "test", "ssh-session"} and tool in SHELL_TOOLS:
            return None
        for op in reversed(self.d["ops"]):
            if op["fp"] != fp:
                continue
            if op["exit"] is None:
                return None
            state_since = [o for o in self.d["ops"] if o["n"] > op["n"] and (o["kind"] in {"mutate", "deploy", "commit"})]
            if kind in {"read", "ssh-session"} and tool in READ_TOOLS and tool in {"read", "read_file"}:
                path = next((str(args[k]) for k in PATH_KEYS if args.get(k)), "")
                if path in self.d["files"] and self.d["files"][path].get("rev", 0) > op["rev"]:
                    return None                 # the file was edited since it was read
            if state_since and kind != "ssh-session":
                return None
            if kind == "verify" and any(o["kind"] == "deploy" for o in self.d["ops"] if o["n"] > op["n"]):
                return None
            return op
        return None

    def repeat_count(self, fp: str, delta: int = 0) -> int:
        return self.counter("repeat:" + fp, delta)

    def reset_repeat_counters(self) -> None:
        """Real new work happened: the refusal budgets of earlier repeated proposals start again."""
        c = self.d["counters"]
        for k in [k for k in c if k.startswith(("repeat:", "scope:", "uncertain:", "plan-streak"))]:
            del c[k]

    def is_new_work(self, tool: str, args: dict[str, Any]) -> bool:
        return fingerprint(tool, args) not in self.d["fp_index"]

    def known_failure(self, tool: str, args: dict[str, Any]) -> dict | None:
        if tool not in SHELL_TOOLS:
            return None
        f = self.d["failures"].get(fingerprint(tool, args))
        if f and f["count"] >= 1 and "resolved_at_rev" not in f and f.get("rev") == self.rev and f.get("exit") not in (None,):
            return f
        return None

    def recent_fps(self, n: int = 12) -> list[str]:
        return [o["fp"] for o in self.d["ops"][-n:]]

    def cycle(self) -> list[str] | None:
        """A repeating sequence of operations with no state change between its repeats (A B A B ..., A B C A B C ...)."""
        fps = self.recent_fps(18)
        for size in (1, 2, 3, 4):
            need = size * 3 if size == 1 else size * 2
            if len(fps) >= need:
                tail = fps[-need:]
                unit = tail[-size:]
                if all(tail[i] == unit[i % size] for i in range(need)) and len(set(unit)) == size:
                    ops = self.d["ops"][-need:]
                    if not any(o["kind"] in {"mutate", "deploy", "commit"} or o["kind"] == "test" and size > 1 for o in ops):
                        return unit
        return None

    def checkpoint(self, reason: str) -> None:
        self.d["counters"]["checkpoint_reason"] = reason[:200]
        self.d["phase"] = "blocked" if self.unmet() else self.d["phase"]
        self.save()


_RECENT_CALL = re.compile(r"(?m)^\[Assistant tool call\]: (\w+)\((\{.*\})\)$")
_RECENT_NEXT = re.compile(r"\n(?=\[(?:Assistant(?: tool call| reasoning)?|User|Tool result|Tool error|Shell|Synthetic context)\]: )")


def _is_remote_deploy(info: C.Cmd) -> bool:
    return info.kind == "deploy"


def _path_in(command: str) -> str | None:
    m = re.search(r"cd\s+([~\w./-]+)", command) or re.search(r"(?:^|\s)(~?/[\w./-]+)", command)
    return m.group(1) if m else None
