"""Permission policy, enforced in the tool-execution layer before any side effect.

Nothing here reads model output as authority: a verdict depends only on the selected mode, the
tool arguments and the workspace. Shell commands cannot be proven contained by static analysis,
so the command classifier is a conservative heuristic, not an OS sandbox (documented in README).

                     ask   auto   full-access   bypass
  read in workspace   ok    ok        ok          ok
  edit in workspace  ask    ok        ok          ok
  ordinary command   ask    ok        ok          ok
  install / network  ask   ask        ok          ok
  local destructive  ask   ask       ask          ok   (git reset --hard, rm -r <dir>, .git writes excepted)
  outside workspace, credentials, privileged, remote/publish  ->  always asked (never "remembered"); catastrophic -> denied in every mode
"""
from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOW, MEDIUM, HIGH, CRITICAL = 0, 1, 2, 3
RISK_NAMES = {LOW: "low", MEDIUM: "medium", HIGH: "high", CRITICAL: "critical"}


@dataclass
class Verdict:
    kind: str  # allow | ask | deny
    risk: int = LOW
    reason: str = ""
    rememberable: bool = True

    @staticmethod
    def allow() -> Verdict:
        return Verdict("allow")

    @staticmethod
    def ask(risk: int, reason: str, *, rememberable: bool | None = None) -> Verdict:
        return Verdict("ask", risk, reason, risk <= MEDIUM if rememberable is None else rememberable)

    @staticmethod
    def deny(reason: str) -> Verdict:
        return Verdict("deny", CRITICAL, reason, False)


# ------------------------------------------------------------------ sensitive locations
CRED_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".docker"}
SENSITIVE_NAME = re.compile(
    r"^(\.env($|\.(?!example$|sample$|template$|dist$))|\.netrc$|\.npmrc$|\.pypirc$|id_(rsa|ed25519|ecdsa|dsa)|"
    r".*\.(pem|key|p12|pfx|kdbx)$|credentials(\..*)?$|secrets?\.(json|ya?ml|toml)$)", re.I)


def own_config_dirs() -> list[Path]:
    home = Path.home()
    return [home / ".config" / "sarness", home / ".config" / "sarness-cli", home / ".local" / "share" / "sarness-engine"]


def is_credential_store(path: Path) -> bool:
    """Places whose contents are secrets by nature, wherever they are referenced from."""
    if any(part in CRED_DIRS for part in path.parts):
        return True
    return any(path == d or d in path.parents for d in own_config_dirs())


def is_sensitive_name(path: Path) -> bool:
    return any(SENSITIVE_NAME.match(p) for p in path.parts)


def touches_git_dir(path: Path) -> bool:
    return ".git" in path.parts


SYSTEM_DIRS = ("/etc", "/boot", "/root", "/proc", "/sys", "/dev", "/var", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt")


# ------------------------------------------------------------------ command tokenizer
def tokenize(cmd: str) -> tuple[list[list[str]], bool]:
    """Split a shell string into command segments of words (quotes honoured) and report an
    unquoted output redirection. `|` boundaries are kept as ["|"] segments."""
    segs: list[list[str]] = []
    cur: list[str] = []
    word, in_word, redirect = [], False, False
    sq = dq = False
    i, n = 0, len(cmd)

    def end_word() -> None:
        nonlocal word, in_word
        if in_word:
            cur.append("".join(word))
            word, in_word = [], False

    def end_seg() -> None:
        nonlocal cur
        end_word()
        if cur:
            segs.append(cur)
            cur = []

    while i < n:
        c = cmd[i]
        if sq:
            if c == "'":
                sq = False
            else:
                word.append(c)
        elif dq:
            if c == '"':
                dq = False
            elif c == "\\" and i + 1 < n:
                i += 1
                word.append(cmd[i])
            else:
                word.append(c)
        elif c == "'":
            sq = in_word = True
        elif c == '"':
            dq = in_word = True
        elif c == "\\" and i + 1 < n:
            i += 1
            word.append(cmd[i])
            in_word = True
        elif c in " \t":
            end_word()
        elif c in "\n;":
            end_seg()
        elif c in "&|":
            end_seg()
            if i + 1 < n and cmd[i + 1] == c:
                i += 1
            if c == "|":
                segs.append(["|"])
        elif c in "<>":
            redirect |= c == ">"
            end_word()
        else:
            word.append(c)
            in_word = True
        i += 1
    end_seg()
    return segs, redirect


WRAPPERS = {"time", "nohup", "env", "command", "exec", "nice", "setsid", "stdbuf", "xargs"}
SAFE_PROGRAMS = {"ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "whoami", "date", "uname", "file", "stat",
                 "tree", "du", "df", "grep", "rg", "fd", "sort", "uniq", "diff", "basename", "dirname", "realpath",
                 "printf", "true", "false", "test", "[", "id", "hostname", "nproc", "free", "sleep", "cut", "tr", "sed",
                 "awk", "jq", "less", "more", "env"}
GIT_READ = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog", "remote", "config", "grep"}
OK_ABS_PREFIXES = ("/usr/bin", "/usr/local/bin", "/usr/lib", "/usr/share", "/bin", "/lib", "/dev/null", "/dev/stdout",
                   "/dev/stderr", "/dev/stdin", "/tmp")
TEST_RUNNERS = re.compile(r"(^|\s)(pytest|unittest|tox|nox|ruff|mypy|flake8|pylint|black|eslint|tsc|vitest|jest|mocha|"
                          r"npm\s+(run\s+)?(test|build|lint)|pnpm\s+(run\s+)?(test|build|lint)|yarn\s+(run\s+)?(test|build|lint)|"
                          r"cargo\s+(test|build|check|clippy|fmt)|go\s+(test|build|vet)|make(\s+\w+)?|ctest|mvn\s+(test|verify)|"
                          r"gradle(w)?\s+(test|build)|dotnet\s+(test|build)|rspec|phpunit|compileall|py_compile)\b")


@dataclass
class CmdFlags:
    deny: str | None = None
    privileged: bool = False
    remote: str | None = None
    destructive: str | None = None
    install: bool = False
    network: bool = False
    outside: str | None = None
    credential: str | None = None
    sensitive: str | None = None
    obfuscated: str | None = None
    safe: bool = False
    notes: list[str] = field(default_factory=list)


def _base(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def _strip_prefix(words: list[str]) -> list[str]:
    while words:
        w = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            words = words[1:]
        elif w in WRAPPERS:
            words = words[1:]
        elif w == "timeout" and len(words) > 2:
            words = words[2:]
        else:
            break
    return words


def _is_local_url(w: str) -> bool:
    w = re.sub(r"^https?://", "", w)
    return w.startswith(("localhost", "127.0.0.1", "[::1]", "0.0.0.0"))


def _path_like(word: str) -> str | None:
    m = re.match(r"^-[^=]*=(.*)$", word)
    w = m.group(1) if m else word
    if w.startswith(("/", "~")) or w == ".." or ("/" in w and ".." in w.split("/")):
        return w
    return None


def _outside_target(word: str, root: Path) -> tuple[str | None, bool]:
    """(path-if-outside-workspace, is_credential_store)"""
    w = _path_like(word)
    if w is None:
        return None, False
    if any(w == p or w.startswith(p + "/") for p in OK_ABS_PREFIXES):
        return None, False
    expanded = Path(os.path.expanduser(w))
    target = (root / expanded) if not expanded.is_absolute() else expanded
    try:
        resolved = Path(os.path.normpath(target))
        real = resolved.resolve(strict=False)
    except OSError:
        return None, False
    cred = is_credential_store(resolved) or is_credential_store(real)
    if real == root or root in real.parents:
        return None, cred
    if not w.startswith(("/", "~")):
        return w, cred            # '../..' that leaves the project
    parent = real.parent
    if parent == Path("/") and not real.exists():
        return None, cred         # '/api/users' in a URL or regex, not a filesystem path
    if not parent.exists():
        return None, cred
    return w, cred


def classify_command(cmd: str, root: Path, _depth: int = 0) -> CmdFlags:
    f = CmdFlags()
    _classify_into(cmd, root, f, _depth)
    f.safe = (f.safe and f.deny is None and not f.privileged and f.remote is None and f.destructive is None
              and not f.install and not f.network and f.outside is None and f.credential is None
              and f.sensitive is None and f.obfuscated is None)
    return f


def _classify_into(cmd: str, root: Path, f: CmdFlags, depth: int) -> None:
    if depth > 3:
        f.obfuscated = f.obfuscated or "deeply nested shell command"
        return
    if re.search(r":\s*\(\s*\)\s*\{", cmd):
        f.deny = "fork bomb"
    segs, redirect = tokenize(cmd)
    all_safe = bool(segs) and not redirect
    prev_downloader = False
    for seg in segs:
        if seg == ["|"]:
            continue
        w = _strip_prefix(seg)
        if not w:
            continue
        prog = _base(w[0])
        args = w[1:]
        sub = next((a for a in args if not a.startswith("-")), "")
        has = lambda s, a=args: s in a  # noqa: E731
        seg_safe = prog in SAFE_PROGRAMS
        if prog in {"sed", "awk"} and any(a.startswith("-i") for a in args):
            seg_safe = False
        if prog == "find" and not any(x in args for x in ("-delete", "-exec", "-execdir", "-ok")):
            seg_safe = True
        if prog == "git" and sub in GIT_READ:
            seg_safe = True
        if prog == "git" and sub == "branch" and all(a.startswith("-") and a not in {"-D", "-d", "-m", "-M"} for a in args):
            seg_safe = True
        if len(args) == 1 and args[0] in {"--version", "-v", "-V", "version"}:
            seg_safe = True
        all_safe = all_safe and seg_safe

        # catastrophic: refused in every mode
        if prog in {"mkfs", "shutdown", "reboot", "poweroff", "halt", "wipefs", "fdisk", "parted"} or prog.startswith("mkfs."):
            f.deny = f"`{prog}` is never run by the agent"
        if prog == "dd" and any(a.startswith("of=/dev/") for a in args):
            f.deny = "writing to a block device"
        if prog in {"chmod", "chown"} and any(a in {"/", "/*"} for a in args) and ("-R" in args or "-r" in args):
            f.deny = "recursive permission change of the filesystem root"

        if prog in {"sudo", "su", "doas", "pkexec", "runuser"}:
            f.privileged = True
        elif prog in {"ssh", "scp", "sftp", "rsync"} or (prog == "docker" and sub == "push"):
            f.remote = f.remote or f"`{prog}` talks to a remote machine"
        elif prog in {"gh", "glab"}:
            f.remote = f.remote or f"`{prog}` changes remote services"
        elif (prog in {"npm", "pnpm", "yarn", "cargo"} and sub == "publish") or prog == "twine":
            f.remote = f.remote or "publishes a package"
        elif prog == "git" and sub == "push":
            f.remote = f.remote or "git push changes a remote repository"
        elif prog == "git" and sub == "reset" and has("--hard"):
            f.destructive = f.destructive or "git reset --hard discards uncommitted work"
        elif prog == "git" and sub == "clean":
            f.destructive = f.destructive or "git clean deletes untracked files"
        elif prog == "git" and sub in {"checkout", "restore"} and (has(".") or has("--")):
            f.destructive = f.destructive or "discards local modifications"
        elif prog == "git" and sub == "branch" and (has("-D") or has("--delete")):
            f.destructive = f.destructive or "deletes a branch"
        elif prog == "git" and sub in {"clone", "fetch", "pull", "submodule"}:
            f.network = True
        elif prog == "find" and has("-delete"):
            f.destructive = f.destructive or "find -delete"
        elif prog in {"shred", "truncate"}:
            f.destructive = f.destructive or f"`{prog}` destroys file contents"
        elif prog == "eval":
            f.obfuscated = f.obfuscated or "eval hides what will run"
        elif prog in {"chmod", "chown", "chgrp"} and ("-R" in args or "-r" in args):
            f.destructive = f.destructive or f"recursive {prog}"
        elif prog in {"rm", "rmdir", "unlink"}:
            recursive = any(a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a) for a in args) or has("--recursive")
            for t in (a for a in args if not a.startswith("-")):
                if t in {"/", "~", "$HOME", "/*", "~/*", "$HOME/*"}:
                    f.deny = "refusing to delete the filesystem root or home directory"
                elif recursive and (t in {".", "..", "*", "./*", "./"} or ".git" in t or ("*" in t and "/" not in t)):
                    f.destructive = f.destructive or f"recursive delete of `{t}`"
        elif prog in {"bash", "sh", "zsh", "dash", "fish"}:
            if prev_downloader:
                f.privileged = True
                f.remote = f.remote or "pipes downloaded code into a shell"
            if "-c" in args:
                i = args.index("-c")
                if i + 1 < len(args):
                    _classify_into(args[i + 1], root, f, depth + 1)

        installs = {
            "npm": {"i", "install", "ci", "add", "create", "init", "dlx", "up", "update", "upgrade"},
            "pnpm": {"i", "install", "ci", "add", "create", "init", "dlx", "up", "update", "upgrade"},
            "yarn": {"i", "install", "add", "create", "init", "dlx", "up", "upgrade", ""},
            "bun": {"i", "install", "add", "create"},
            "pip": {"install", "download"}, "pip3": {"install", "download"}, "pipenv": {"install"},
            "poetry": {"install", "add", "update"}, "uv": {"add", "sync", "pip", "tool"}, "conda": {"install"},
            "cargo": {"add", "install", "fetch", "update"}, "go": {"get", "install"},
            "gem": {"install"}, "bundle": {"install", "update"}, "composer": {"install", "update", "require"},
            "dotnet": {"add", "restore"},
        }
        if prog in {"npx", "bunx", "pipx", "uvx", "apt", "apt-get", "dpkg", "snap", "brew", "dnf", "yum", "pacman", "zypper", "flatpak"}:
            f.install = True
        elif sub in installs.get(prog, set()) or (prog == "yarn" and not args):
            f.install = True
        elif prog in {"python", "python3"} and has("-m") and (has("pip") or has("ensurepip")) and (has("install") or has("download")):
            f.install = True

        downloader = prog in {"curl", "wget", "http", "https", "ftp", "nc", "ncat", "telnet"}
        if downloader:
            targets = [a for a in args if not a.startswith("-")]
            if not all(_is_local_url(a) or ("." not in a and ":" not in a) for a in targets) or prog in {"nc", "ncat", "telnet"}:
                f.network = True
        prev_downloader = downloader

        for a in args:
            # Credential filenames and system write targets remain boundaries in bypass.
            if is_sensitive_name(Path(a)) or touches_git_dir(Path(a)):
                f.sensitive = f.sensitive or a
            target = (root / os.path.expanduser(a)).resolve(strict=False)
            if is_credential_store(target):
                f.credential = f.credential or a
            if redirect or prog in {"rm", "rmdir", "unlink", "chmod", "chown", "chgrp", "truncate", "shred", "mv", "cp", "tee"}:
                if a.startswith("/") and a not in {"/dev/null", "/dev/stdout", "/dev/stderr"} and any(
                        target == Path(d) or Path(d) in target.parents for d in SYSTEM_DIRS):
                    f.privileged = True
            outside, cred = _outside_target(a, root)
            if outside:
                f.outside = f.outside or outside
            if cred:
                f.credential = f.credential or a
        for inner in _substitutions(cmd):
            if depth < 3:
                _classify_into(inner, root, f, depth + 1)
    f.safe = f.safe or all_safe


def _substitutions(cmd: str) -> list[str]:
    out, i = [], 0
    while i + 1 < len(cmd):
        if cmd[i] == "$" and cmd[i + 1] == "(":
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += (cmd[j] == "(") - (cmd[j] == ")")
                j += 1
            out.append(cmd[i + 2:max(i + 2, j - 1)])
            i = j
        else:
            i += 1
    parts = cmd.split("`")
    out += [p for k, p in enumerate(parts) if k % 2 == 1 and len(parts) > k + 1]
    return out


# ------------------------------------------------------------------ verdicts
MODES = ("ask", "auto", "full-access", "bypass")


class PermissionContextError(ValueError):
    """Permission context must be explicit; never fall back to an interactive mode."""


@dataclass
class EffectivePermissions:
    """One live policy per session, shared by delegates; never serialized in session files."""

    root: Path
    mode: str
    remembered: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve(strict=True)
        self.set_mode(self.mode)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise PermissionContextError(f"Unknown permission mode {mode!r}")
        self.mode = mode
        self.remembered.clear()

    def assess(self, tool: str, args: dict[str, Any], workspace: Path) -> Verdict:
        if workspace != self.root and self.root not in workspace.parents:
            return Verdict.deny("Delegated workspace is outside the authorized workspace")
        return assess(tool, args, workspace, self.mode)

    def environment(self) -> dict[str, str]:
        return {"SARNESS_PERMISSION_CONTEXT": json.dumps({"root": str(self.root), "mode": self.mode})}

    @classmethod
    def for_launch(cls, root: Path, configured_mode: str, *, bypass: bool = False) -> EffectivePermissions:
        # An active launch flag always wins, including when restoring a conversation.
        if bypass:
            return cls(root, "bypass")
        inherited = os.environ.get("SARNESS_PERMISSION_CONTEXT")
        if inherited is not None:
            try:
                data = json.loads(inherited)
                parent = Path(data["root"])
                mode = data["mode"]
                if not parent.is_absolute():
                    raise ValueError("root must be absolute")
                policy = cls(parent, mode)
                if root != policy.root and policy.root not in root.parents:
                    raise ValueError("workspace is outside the inherited permission scope")
                return policy
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise PermissionContextError(f"Invalid inherited permission context: {exc}") from exc
        return cls(root, configured_mode)


ACTIVE_PERMISSIONS: ContextVar[EffectivePermissions | None] = ContextVar("sarness_permissions", default=None)


def verdict_for_command(cmd: str, root: Path, mode: str) -> Verdict:
    if mode not in MODES:
        return Verdict.deny(f"Unknown permission mode {mode!r}")
    f = classify_command(cmd, root)
    if f.deny:
        return Verdict.deny(f"Refused: {f.deny}.")
    if f.credential:
        return Verdict.deny(f"Refused: touches a credential store ({f.credential}). The agent never reads keys or tokens.")
    if f.sensitive:
        return Verdict.ask(HIGH, f"Sensitive file or Git internals: `{f.sensitive}`", rememberable=False)
    if f.privileged:
        return Verdict.ask(CRITICAL, "Runs with elevated privileges or pipes downloaded code into a shell", rememberable=False)
    if f.remote:
        return Verdict.ask(HIGH, f"Remote/outward-facing action: {f.remote}", rememberable=False)
    if f.outside:
        return Verdict.ask(HIGH, f"References `{f.outside}`, outside the workspace", rememberable=False)
    if f.obfuscated and mode != "bypass":
        return Verdict.ask(HIGH, f"Hard to audit: {f.obfuscated}")
    if mode == "ask":
        return Verdict.ask(LOW if f.safe else MEDIUM, "Command execution requires confirmation in Ask mode")
    if f.destructive and mode != "bypass":
        return Verdict.ask(HIGH, f"Destructive: {f.destructive}")
    if mode == "auto":
        if f.install:
            return Verdict.ask(MEDIUM, "Installs dependencies (downloads and may run package scripts)")
        if f.network:
            return Verdict.ask(MEDIUM, "Accesses the network")
    return Verdict.allow()


def resolve_in_workspace(root: Path, rel: str) -> tuple[Path, bool]:
    """(resolved path, inside_workspace). Symlinks are followed before the check."""
    p = Path(os.path.expanduser(rel or "."))
    target = p if p.is_absolute() else root / p
    real = Path(os.path.normpath(target)).resolve(strict=False)
    return real, real == root or root in real.parents


def verdict_for_path(root: Path, rel: str, access: str, mode: str, recursive_dir: bool = False) -> Verdict:
    """access: read | write | delete. Files outside the workspace are not reachable by file tools."""
    real, inside = resolve_in_workspace(root, rel)
    if not inside:
        return Verdict.deny(f"`{rel}` is outside the workspace; file tools cannot reach it (absolute paths, '..' and escaping symlinks are refused)")
    if is_credential_store(real):
        return Verdict.deny("That is a credential store; the agent never reads or writes it")
    if real == root and access != "read":
        return Verdict.deny("The workspace root itself cannot be modified or deleted by a tool call")
    shown = str(real.relative_to(root)) if real != root else "."
    if is_sensitive_name(real.relative_to(root)):
        what = "its contents would be sent to the model server" if access == "read" else "may contain credentials"
        return Verdict.ask(HIGH if access == "read" else MEDIUM, f"`{shown}` looks like a secret file; {what}", rememberable=False)
    if touches_git_dir(real.relative_to(root)):  # .git/config can embed remote credentials
        return Verdict.ask(HIGH, "Modifies Git's internal data (.git)" if access != "read" else "Reads Git internals (.git); use git_* tools instead",
                           rememberable=False)
    if access == "read":
        return Verdict.allow()
    if mode == "ask":
        return Verdict.ask(MEDIUM, f"{'Delete' if access == 'delete' else 'Modify'} `{shown}`")
    if access == "delete" and recursive_dir and mode in {"auto", "full-access"}:
        return Verdict.ask(HIGH, f"Recursively delete the folder `{shown}`")
    return Verdict.allow()


def assess(tool: str, a: dict[str, Any], root: Path, mode: str) -> Verdict:
    s = lambda k: str(a.get(k) or "")  # noqa: E731
    if mode not in MODES:
        return Verdict.deny(f"Unknown permission mode {mode!r}")
    def worst(*vs: Verdict) -> Verdict:
        best = Verdict.allow()
        for v in vs:
            if v.kind == "deny":
                return v
            if v.kind == "ask" and (best.kind == "allow" or v.risk > best.risk):
                best = v
        return best
    if tool == "read_file":
        return verdict_for_path(root, s("path"), "read", mode)
    if tool in {"list_dir", "search", "find_files"}:
        return verdict_for_path(root, s("path") or ".", "read", mode)
    if tool in {"write_file", "append_file", "edit_file"}:
        return verdict_for_path(root, s("path"), "write", mode)
    if tool == "delete_path":
        real, _ = resolve_in_workspace(root, s("path"))
        return verdict_for_path(root, s("path"), "delete", mode, recursive_dir=real.is_dir())
    if tool == "move_path":
        return worst(verdict_for_path(root, s("from"), "delete", mode), verdict_for_path(root, s("to"), "write", mode))
    if tool == "run_command":
        return verdict_for_command(s("command"), root, mode)
    if tool in {"git_status", "git_diff", "git_log", "update_plan"}:
        return Verdict.allow()
    return Verdict.deny(f"No permission policy for tool {tool!r}")


def looks_like_test(command: str) -> bool:
    return bool(TEST_RUNNERS.search(command))
