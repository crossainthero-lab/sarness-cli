"""Tool definitions and the local executor. Every side effect passes through `policy.assess`."""
from __future__ import annotations

import ast
import contextlib
import difflib
import fnmatch
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import policy

MAX_WRITE = 250_000          # chars in one write_file call that arrived intact (the model is told a much smaller chunk size)
MAX_APPEND = 250_000
IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__", ".next", ".pytest_cache", ".mypy_cache"}
PLACEHOLDER = re.compile(
    r"(?im)^\s*(?:#|//|/\*|<!--|--)?\s*\.\.\.\s*(?:rest|remaining|existing|other|previous|unchanged|same)\b.*$"
    r"|^\s*(?:#|//)\s*\.\.\.\s*$"
    r"|\((?:rest|remainder) of (?:the )?(?:file|code|function)[^)]*\)"
    r"|\[(?:rest|remaining) (?:of )?(?:the )?(?:file|code)[^\]]*\]")
SECRET_ENV = re.compile(r"(TOKEN|PASSWORD|PASSWD|API_?KEY|SECRET|PRIVATE_KEY|CREDENTIAL|SESSION)", re.I)
TOKEN_PATTERNS = re.compile(r"(Bearer\s+[A-Za-z0-9._\-]{16,}|sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16})")


def clipped(text: str, max_chars: int = 12000, hint: str = "use smaller reads") -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}\n... [{len(text) - max_chars} characters omitted; {hint}] ...\n{text[-tail:]}"


def _prop(kind: str, desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "description": desc, **extra}


def _tool(name: str, description: str, required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}}


S = lambda d: _prop("string", d)    # noqa: E731
I = lambda d: _prop("integer", d)   # noqa: E731

TOOLS: list[dict[str, Any]] = [
    _tool("list_dir", "List directory entries; bypass includes protected and ignored paths.", [], path=S("Folder (default .; absolute/outside paths supported in bypass)")),
    _tool("read_file", "Read a text file with line numbers. Use offset/limit for large files.", ["path"],
          path=S("Relative path"), offset=I("First line number (default 1)"), limit=I("Number of lines (default 150, max 400)")),
    _tool("search", "Regex search across files (respects .gitignore in normal modes; includes protected/ignored files in bypass). Bounded results.", ["pattern"],
          pattern=S("Python regular expression"), path=S("Relative folder or file (default .)")),
    _tool("find_files", "Find files by glob pattern, e.g. '*.py' or 'src/**/test_*.py'.", ["pattern"], pattern=S("Glob pattern")),
    _tool("write_file", "Create or replace a whole file. Keep each call to a few thousand characters (the system prompt gives the exact limit); for larger files write the first part "
          "then add the rest with append_file. Never use placeholders such as '... rest unchanged'.", ["path", "content"],
          path=S("Relative path"), content=S("Complete contents of this file (or its first part)")),
    _tool("append_file", "Append the next chunk to a file. Only applies if the file's current length equals expected_length, so a "
          "retry can never duplicate or corrupt content.", ["path", "content", "expected_length"],
          path=S("Relative path"), content=S("Next chunk"), expected_length=I("Exact current length of the file in characters")),
    _tool("edit_file", "Replace an exact, unique substring of an existing file. Preferred for changes to existing files.",
          ["path", "old_string", "new_string"], path=S("Relative path"), old_string=S("Original text, occurring exactly once"),
          new_string=S("Replacement text"), replace_all=_prop("boolean", "Replace every occurrence instead of requiring uniqueness")),
    _tool("delete_path", "Delete a file or folder recursively. Bypass permits any OS-accessible path.", ["path"], path=S("Path")),
    _tool("move_path", "Move or rename a file or folder. Bypass permits outside-workspace paths.", ["from", "to"], **{"from": S("Source"), "to": S("Destination")}),
    _tool("run_command", "Run a shell command LOCALLY in the workspace (not on the GPU server). Returns the real exit code and bounded output; "
          "the full log is kept on disk. Stdin is closed; use noninteractive options (e.g. sudo -n). Bypass permits arbitrary commands, including SSH.", ["command"],
          command=S("Shell command"), timeout_s=I("Seconds (default 120, max 600)")),
    _tool("git_status", "Show git status and branch.", []),
    _tool("git_diff", "Show the uncommitted git diff (untracked files listed separately).", []),
    _tool("git_log", "Show recent commits.", [], count=I("How many (default 10, max 50)")),
    _tool("update_plan", "Record or update your task checklist (shown to the user). Call early for multi-step work and keep it current.",
          ["items"], items=_prop("array", "Checklist", items={"type": "object", "properties": {
              "text": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "done"]}},
              "required": ["text", "status"]})),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
MUTATING = {"write_file", "append_file", "edit_file", "delete_path", "move_path"}


class ToolArgError(ValueError):
    pass


def validate_args(name: str, args: Any) -> dict[str, Any]:
    """Strict, schema-driven validation with lossless coercions (numeric strings -> int, 'true' -> bool)."""
    if name not in TOOL_NAMES:
        raise ToolArgError(f"Unknown tool {name}. Available: {', '.join(sorted(TOOL_NAMES))}")
    if not isinstance(args, dict):
        raise ToolArgError("Tool arguments must be a JSON object")
    schema = next(t["function"]["parameters"] for t in TOOLS if t["function"]["name"] == name)
    props = schema["properties"]
    extra = set(args) - set(props)
    if extra:
        raise ToolArgError(f"Unknown argument(s) for {name}: {', '.join(sorted(extra))}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise ToolArgError(f"Missing required argument(s) for {name}: {', '.join(missing)}")
    out: dict[str, Any] = {}
    for key, value in args.items():
        kind = props[key]["type"]
        if kind == "string":
            if not isinstance(value, str):
                raise ToolArgError(f"{key} must be a string")
        elif kind == "integer":
            if isinstance(value, bool):
                raise ToolArgError(f"{key} must be an integer")
            if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
                value = int(value)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise ToolArgError(f"{key} must be an integer")
        elif kind == "boolean":
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                value = value.lower() == "true"
            if not isinstance(value, bool):
                raise ToolArgError(f"{key} must be true or false")
        elif kind == "array":
            if not isinstance(value, list):
                raise ToolArgError(f"{key} must be an array")
        out[key] = value
    return out


def unified_diff(old: str, new: str, name: str, context: int = 3) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{name}", f"b/{name}", n=context))


def diff_stats(old: str, new: str) -> tuple[int, int]:
    add = rem = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            add += 1
        elif line.startswith("- "):
            rem += 1
    return add, rem


def syntax_note(path: Path, text: str) -> str:
    try:
        if path.suffix == ".py":
            ast.parse(text)
        elif path.suffix == ".json":
            json.loads(text)
        else:
            return ""
    except SyntaxError as exc:
        return f"\nWARNING: Python syntax error at line {exc.lineno}: {exc.msg} (ignore only if you are still appending chunks)"
    except json.JSONDecodeError as exc:
        return f"\nWARNING: invalid JSON at line {exc.lineno}: {exc.msg} (ignore only if you are still appending chunks)"
    return "\nSyntax check: OK"


def _atomic_write(p: Path, text: str, mode_from: Path | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sarness-", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode_from is not None and mode_from.exists():
            os.chmod(tmp, mode_from.stat().st_mode & 0o777)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


Confirmer = Callable[[str], Any]  # returns truthy to allow; the string "always" also remembers the exact action


class Workspace:
    def __init__(self, path: Path, mode: str | None = None, confirmer: Confirmer | None = None, *, command_timeout: int = 120,
                 log_dir: Path | None = None, result_cap: int = 12000, redact: list[str] | None = None,
                 permissions: policy.EffectivePermissions | None = None):
        self.root = Path(path).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be an existing directory")
        active = policy.ACTIVE_PERMISSIONS.get()
        inherited = active if active and active.mode == "bypass" else permissions or active
        if mode is not None and not (inherited and inherited.mode == "bypass"):
            if permissions is not None:
                raise policy.PermissionContextError("Pass either a permission policy or an explicit mode, not both")
            inherited = policy.EffectivePermissions(self.root, mode)
        if inherited is None:
            raise policy.PermissionContextError("Missing permission context: supply a mode or the parent session policy")
        if inherited.mode != "bypass" and self.root != inherited.root and inherited.root not in self.root.parents:
            raise policy.PermissionContextError("Workspace is outside the authorized permission scope")
        self.permissions = inherited
        self.confirm = confirmer or self._confirm
        self.command_timeout = command_timeout
        self.log_dir = log_dir
        self.result_cap = result_cap
        self.redact = [r for r in (redact or []) if r]
        self.remembered = self.permissions.remembered
        self.plan: list[dict[str, str]] = []
        self.last: dict[str, Any] = {}
        self._cmd_n = 0
        self._delegate_n = 0

    @property
    def mode(self) -> str:
        return self.permissions.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self.permissions.set_mode(value)
        self.remembered.clear()

    def delegate(self, path: Path | None = None) -> Workspace:
        """Share the session policy; bypass delegates may use any existing directory."""
        self._delegate_n += 1
        child_logs = self.log_dir / "delegates" / str(self._delegate_n) if self.log_dir else None
        return Workspace(path or self.root, confirmer=self.confirm, permissions=self.permissions,
                         command_timeout=self.command_timeout, log_dir=child_logs,
                         result_cap=self.result_cap, redact=self.redact)

    @staticmethod
    def _confirm(message: str) -> bool:
        import sys
        if not sys.stdin.isatty():
            return False
        return input(f"\n{message}\nApprove? [y/N] ").strip().lower() in {"y", "yes"}

    # ------------------------------------------------------------------ gating
    def resolve(self, rel: str, write: bool = False) -> Path:
        real, inside = policy.resolve_in_workspace(self.root, rel)
        if self.mode == "bypass":
            return real
        if not inside:
            raise policy.PolicyDenied("Path crosses the workspace boundary (absolute paths, '..' and symlinks that leave the project are refused)")
        if write and real == self.root:
            raise policy.PolicyDenied("Cannot overwrite the workspace root")
        return real

    def _gate(self, name: str, args: dict[str, Any], detail: str, extra: str = "") -> None:
        # Authoritative execution boundary: no downstream permission handler or
        # classifier can turn bypass into an approval or a policy refusal.
        if self.mode == "bypass":
            self.last["permission"] = {"mode": "bypass", "verdict": "allow", "reason": ""}
            return
        verdict = self.permissions.assess(name, args, self.root)
        self.last["permission"] = {"mode": self.mode, "verdict": verdict.kind, "reason": verdict.reason}
        if verdict.kind == "deny":
            raise policy.PolicyDenied(verdict.reason)
        if verdict.kind == "allow":
            return
        sig = json.dumps([str(self.root), name, args], sort_keys=True, ensure_ascii=False)
        if verdict.rememberable and sig in self.remembered:
            return
        # Only routine edits get previews. Sensitive files must not be read to render an approval.
        if verdict.rememberable and name in {"write_file", "edit_file"}:
            p = self.resolve(args["path"])
            if p.is_file():
                old = self._read_text(p)
                new = args["content"] if name == "write_file" else old.replace(
                    args["old_string"], args["new_string"], -1 if args.get("replace_all") else 1)
                extra = clipped(unified_diff(old, new, args["path"]), 3000, "diff truncated")
        risk = policy.RISK_NAMES[verdict.risk]
        message = f"[{risk.upper()}] {name}: {detail}\n  why asked: {verdict.reason}" + (f"\n{extra}" if extra else "")
        answer = self.confirm(message)
        if not answer:
            raise policy.PolicyDenied("Action denied by user")
        if answer == "always" and verdict.rememberable:
            self.remembered.add(sig)

    # ------------------------------------------------------------------ files
    def _shown_path(self, path: Path) -> str:
        return os.path.relpath(path, self.root)

    def _candidates(self, base: Path) -> list[Path]:
        files: list[Path] = []
        if self.mode == "bypass":
            # Include Git internals, ignored files and synthetic sensitive paths.
            # No normal-mode discovery filter is an access boundary in bypass.
            for dirpath, _, names in os.walk(base):
                files.extend(Path(dirpath) / n for n in names)
                if len(files) > 20000:
                    break
            return files[:20000]
        with contextlib.suppress(Exception):
            res = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=base,
                                 capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                files = [base / n for n in res.stdout.split("\0") if n]
        if not files:
            for dirpath, dirs, names in os.walk(base):
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                files += [Path(dirpath) / n for n in names]
                if len(files) > 20000:
                    break
        return [f for f in files if not policy.is_sensitive_name(f.relative_to(self.root))
                and ".git" not in f.relative_to(self.root).parts
                and self.permissions.assess("read_file", {"path": str(f)}, self.root).kind == "allow"][:20000]

    def _read_text(self, p: Path) -> str:
        if not p.is_file():
            raise FileNotFoundError(f"{self._shown_path(p)} is not a file")
        if p.stat().st_size > 4_000_000:
            raise ValueError("File over 4MB; use search or read a range of a smaller file")
        data = p.read_bytes()
        if b"\0" in data[:8192]:
            raise ValueError("Binary file; not readable as text")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"File is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc

    def execute(self, name: str, args: dict[str, Any]) -> str:
        args = validate_args(name, args)
        self.last = {"tool": name}
        # Every registered tool is authorized before its handler can read or mutate anything.
        detail = args.get("command") or args.get("path") or args.get("from") or name
        self._gate(name, args, str(detail))
        token = policy.ACTIVE_PERMISSIONS.set(self.permissions)
        try:
            handler = getattr(self, f"_t_{name}")
            return handler(args)
        finally:
            policy.ACTIVE_PERMISSIONS.reset(token)

    def _t_list_dir(self, a: dict[str, Any]) -> str:
        root = self.resolve(a.get("path", "."))
        if not root.is_dir():
            raise FileNotFoundError(f"{a.get('path', '.')} is not a directory")
        entries = []
        for p in sorted(root.iterdir(), key=lambda v: (not v.is_dir(), v.name)):
            if self.mode != "bypass" and (p.name in IGNORE_DIRS or policy.is_sensitive_name(Path(p.name))):
                continue
            with contextlib.suppress(PermissionError):
                self.resolve(self._shown_path(p))
                entries.append(("dir " if p.is_dir() else "file") + " " + self._shown_path(p))
            if len(entries) >= 250:
                entries.append("... more entries omitted")
                break
        return "\n".join(entries) or "[empty]"

    def _t_read_file(self, a: dict[str, Any]) -> str:
        p = self.resolve(a["path"])
        if not p.is_file():
            raise FileNotFoundError(f"{self._shown_path(p)} is not a file")
        with p.open("rb") as raw:
            if b"\0" in raw.read(8192):
                raise ValueError("Binary file; not readable as text")
        first = max(1, a.get("offset", 1))
        limit = min(400, max(1, a.get("limit", 150)))
        selected = []
        count = 0
        # Iterate rather than load the entire file. Even a multi-megabyte log is readable.
        with p.open("r", encoding="utf-8") as src:
            for count, line in enumerate(src, 1):
                if first <= count < first + limit:
                    selected.append(f"{count}: {line.rstrip(chr(10)).rstrip(chr(13))}")
        last = min(count, first + limit - 1)
        more = f"\n[{count - last} more lines; call read_file with offset={last + 1}]" if count > last else ""
        return clipped(f"{a['path']} lines {first}-{last}/{count}\n" + "\n".join(selected), self.result_cap) + more

    def _t_search(self, a: dict[str, Any]) -> str:
        try:
            regex = re.compile(a["pattern"])
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        base = self.resolve(a.get("path", "."))
        files = [base] if base.is_file() else self._candidates(base)
        results: list[str] = []
        for f in files:
            try:
                if not f.is_file():
                    continue
                # Stream text; silently skipping >500KB files hid matches in real projects.
                with f.open("r", encoding="utf-8", errors="replace") as src:
                    for i, line in enumerate(src, 1):
                        if regex.search(line):
                            results.append(f"{self._shown_path(f)}:{i}: {line[:300]}")
                            if len(results) >= 100:
                                return clipped("\n".join(results) + "\n[first 100 matches]", self.result_cap)

            except (OSError, UnicodeError):
                continue
        return clipped("\n".join(results) or "No matches", self.result_cap)

    def _t_find_files(self, a: dict[str, Any]) -> str:
        pat = a["pattern"]
        hits = [str(f.relative_to(self.root)) for f in self._candidates(self.root)
                if fnmatch.fnmatch(str(f.relative_to(self.root)), pat) or fnmatch.fnmatch(f.name, pat)]
        return clipped("\n".join(sorted(hits)[:300]) or "No files match", self.result_cap)

    def _check_placeholders(self, content: str) -> None:
        m = PLACEHOLDER.search(content)
        if m:
            line = content[:m.start()].count("\n") + 1
            raise ValueError(f"Refusing to write: line {line} looks like an elision placeholder ({m.group(0).strip()[:60]!r}). "
                             "Write the real content: use edit_file for changes, or write_file + append_file in chunks.")

    def _t_write_file(self, a: dict[str, Any]) -> str:
        p = self.resolve(a["path"], write=True)
        content = a["content"]
        if len(content) > MAX_WRITE:
            raise ValueError(f"Content is {len(content)} characters; write the first part (a few thousand characters), then use append_file for the rest")
        self._check_placeholders(content)
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        if old == content:
            return f"{a['path']} already has identical content ({len(content)} chars); nothing changed"
        diff = unified_diff(old or "", content, a["path"]) if old is not None else ""
        _atomic_write(p, content, p)
        add, rem = diff_stats(old or "", content)
        self.last.update(path=a["path"], diff=diff, added=add, removed=rem, created=old is None)
        return (f"{'Created' if old is None else 'Overwrote'} {a['path']}: {len(content)} chars, +{add} -{rem} lines" +
                syntax_note(p, content))

    def _t_append_file(self, a: dict[str, Any]) -> str:
        p = self.resolve(a["path"], write=True)
        extra = a["content"]
        if len(extra) > MAX_APPEND:
            raise ValueError(f"Append no more than {MAX_APPEND} characters at a time")
        self._check_placeholders(extra)
        if not p.is_file():
            raise FileNotFoundError("Create the file with write_file first")
        existing = p.read_text(encoding="utf-8")
        expected = a["expected_length"]
        if len(existing) != expected:
            if len(existing) == expected + len(extra) and existing.endswith(extra):
                return f"Already applied: {a['path']} is {len(existing)} chars and ends with this chunk (idempotent retry, nothing changed)"
            raise ValueError(f"Append position mismatch: expected {expected} chars, actual {len(existing)}; re-read the file")
        _atomic_write(p, existing + extra, p)
        self.last.update(path=a["path"], added=extra.count("\n") + 1, removed=0)
        return f"Appended {len(extra)} chars to {a['path']}; length now {len(existing) + len(extra)}"

    def _t_edit_file(self, a: dict[str, Any]) -> str:
        p = self.resolve(a["path"], write=True)
        old, new = a["old_string"], a["new_string"]
        if not old:
            raise ValueError("old_string must not be empty")
        self._check_placeholders(new)
        content = self._read_text(p)
        n = content.count(old)
        if n == 0:
            if new and new in content:
                return f"{a['path']} already contains the new text; nothing changed"
            raise ValueError("old_string was not found; read the file again and copy the text exactly")
        if n != 1 and not a.get("replace_all"):
            raise ValueError(f"edit_file needs exactly 1 occurrence; found {n} (add context or pass replace_all=true)")
        replaced = content.replace(old, new) if a.get("replace_all") else content.replace(old, new, 1)
        diff = unified_diff(content, replaced, a["path"])
        _atomic_write(p, replaced, p)
        add, rem = diff_stats(content, replaced)
        self.last.update(path=a["path"], diff=diff, added=add, removed=rem)
        return f"Edited {a['path']}: {n if a.get('replace_all') else 1} replacement(s), +{add} -{rem} lines" + syntax_note(p, replaced)

    def _t_delete_path(self, a: dict[str, Any]) -> str:
        p = self.resolve(a["path"], write=True)
        if not p.exists() and not p.is_symlink():
            return f"{a['path']} does not exist; nothing to delete"
        if p.is_dir() and not p.is_symlink():
            import shutil
            shutil.rmtree(p)
        else:
            p.unlink()
        self.last.update(path=a["path"], removed=1)
        return f"Deleted {a['path']}"

    def _t_move_path(self, a: dict[str, Any]) -> str:
        src, dst = self.resolve(a["from"], write=True), self.resolve(a["to"], write=True)
        if not src.exists():
            raise FileNotFoundError(f"{a['from']} does not exist")
        if dst.exists():
            raise FileExistsError(f"{a['to']} already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        self.last.update(path=a["to"])
        return f"Moved {a['from']} -> {a['to']}"

    # ------------------------------------------------------------------ git
    def _git(self, *cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *cmd], cwd=self.root, capture_output=True, text=True, timeout=30)

    def _t_git_status(self, a: dict[str, Any]) -> str:
        r = self._git("status", "--short", "--branch")
        return clipped(f"exit {r.returncode}\n{r.stdout}{r.stderr}", self.result_cap)

    def _t_git_diff(self, a: dict[str, Any]) -> str:
        r = self._git("diff", "--no-ext-diff", "--no-color")
        out = r.stdout + r.stderr
        if r.returncode == 0:
            out += "\nUntracked files:\n" + self._git("ls-files", "--others", "--exclude-standard").stdout
        return clipped(f"exit {r.returncode}\n{out}", self.result_cap)

    def _t_git_log(self, a: dict[str, Any]) -> str:
        r = self._git("log", f"-{min(50, max(1, a.get('count', 10)))}", "--oneline", "--decorate")
        return clipped(f"exit {r.returncode}\n{r.stdout}{r.stderr}", self.result_cap)

    # ------------------------------------------------------------------ plan
    def _t_update_plan(self, a: dict[str, Any]) -> str:
        items = []
        for it in a["items"][:30]:
            if not isinstance(it, dict) or not isinstance(it.get("text"), str) or it.get("status") not in {"todo", "doing", "done"}:
                raise ValueError("Each plan item needs text (string) and status (todo|doing|done)")
            items.append({"text": it["text"][:200], "status": it["status"]})
        self.plan = items
        self.last["plan"] = items
        return "Plan recorded:\n" + "\n".join(f"[{i['status']}] {i['text']}" for i in items)

    # ------------------------------------------------------------------ commands
    def _scrub(self, text: str) -> str:
        for secret in self.redact:
            text = text.replace(secret, "[REDACTED]")
        return TOKEN_PATTERNS.sub("[REDACTED]", text)

    def _t_run_command(self, a: dict[str, Any]) -> str:
        command = a["command"]
        if not command.strip():
            raise ValueError("Empty command")
        timeout = min(600, max(1, a.get("timeout_s", self.command_timeout)))
        env = dict(os.environ) if self.mode == "bypass" else {
            k: v for k, v in os.environ.items() if not SECRET_ENV.search(k) and not k.startswith("SARNESS_")}
        # A same-size edit inside one second would otherwise be masked by a stale .pyc (mtime has 1 s granularity).
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(self.permissions.environment())
        self._cmd_n += 1
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            logf = open(self.log_dir / f"cmd-{self._cmd_n:03d}.log", "w+b")  # noqa: SIM115
            log_name = logf.name
        else:
            logf = tempfile.TemporaryFile()  # noqa: SIM115
            log_name = None
        cap_bytes = 50 * 1024 * 1024
        timed_out = over_cap = False
        started = time.monotonic()
        with logf:
            proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=self.root, stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
            try:
                while True:
                    try:
                        proc.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() - started > timeout:
                            timed_out = True
                        elif logf.tell() > cap_bytes:
                            over_cap = True
                        else:
                            continue
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                        break
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
            size = logf.tell()
            logf.seek(0)
            if size <= 10000:
                text = logf.read().decode("utf-8", "replace")
            else:
                first = logf.read(4000)
                logf.seek(size - 6000)
                text = first.decode("utf-8", "replace") + f"\n...[{size - 10000} bytes omitted]...\n" + logf.read().decode("utf-8", "replace")
        elapsed = time.monotonic() - started
        text = self._scrub(text)
        self.last.update(exit_code=proc.returncode, timed_out=timed_out, command=command, elapsed=elapsed)
        flags = (" TIMED OUT and was killed." if timed_out else "") + (" Output exceeded 50MB; killed." if over_cap else "")
        head = f"exit code: {proc.returncode}; timeout: {timed_out}; seconds: {elapsed:.1f}; output bytes: {size}"
        tail = f"\nfull log: {log_name}" if log_name and size > 10000 else ""
        return clipped(f"{head}{flags}\n{text}{tail}", self.result_cap + 500, "output trimmed")
