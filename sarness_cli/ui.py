"""Terminal rendering: streaming text, spinner, tool lines, coloured diffs, approval prompts.

Assistant text goes to stdout; status chrome goes to stderr, so `sarness run ... > out.txt` captures
only the answer. Colour follows NO_COLOR / isatty."""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time
from typing import Any


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def dim(self, s: str) -> str: return self._c("90", s)
    def bold(self, s: str) -> str: return self._c("1", s)
    def red(self, s: str) -> str: return self._c("31", s)
    def green(self, s: str) -> str: return self._c("32", s)
    def amber(self, s: str) -> str: return self._c("38;5;214", s)
    def yellow(self, s: str) -> str: return self._c("38;5;214", s)
    def blue(self, s: str) -> str: return self._c("38;5;39", s)
    def cyan(self, s: str) -> str: return self._c("38;5;39", s)
    def magenta(self, s: str) -> str: return self._c("38;5;39", s)
    def white(self, s: str) -> str: return self._c("97", s)
    def muted(self, s: str) -> str: return self._c("90", s)


def make_style(stream: Any = None) -> Style:
    stream = stream or sys.stderr
    return Style(bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, style: Style, enabled: bool):
        self.style, self.enabled = style, enabled and sys.stderr.isatty()
        self.label = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        self._width = 0

    def start(self, label: str) -> None:
        self.label = label
        if not self.enabled or self._thread:
            return
        self._stop.clear()
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.wait(0.1):
                break
            text = f" {frame} {self.label} {time.monotonic() - self._start:.0f}s"
            self._width = max(self._width, len(text))
            sys.stderr.write("\r" + self.style.dim(text) + "\033[K")
            sys.stderr.flush()

    def stop(self) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join()
            self._thread = None
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def short(value: Any, n: int = 90) -> str:
    s = str(value).replace("\n", "⏎")
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_args(name: str, args: dict[str, Any]) -> str:
    if name == "run_command":
        return short(args.get("command", ""), 110)
    if name in {"write_file", "append_file"}:
        return f"{args.get('path')} ({len(str(args.get('content', '')))} chars)"
    if name == "edit_file":
        return str(args.get("path"))
    if name == "update_plan":
        return f"{len(args.get('items') or [])} items"
    return short(", ".join(f"{k}={v}" for k, v in args.items()), 110)


class Console:
    def __init__(self, show_reasoning: bool = False, verbose: bool = False, input_fn: Any = None):
        self.st = make_style()
        self.out_st = make_style(sys.stdout)
        self.spin = Spinner(self.st, True)
        self.show_reasoning = show_reasoning
        self.verbose = verbose
        self._reasoned = 0
        self._text_started = False
        self.input_fn = input_fn or input

    # ---- helpers
    def err(self, s: str = "", end: str = "\n") -> None:
        sys.stderr.write(s + end)
        sys.stderr.flush()

    # ---- agent hooks
    def turn_start(self, step: int, max_steps: int, info: str = "") -> None:
        self._reasoned = 0
        self._text_started = False
        self.spin.start("waiting for the model")
        if self.verbose:
            self.err(self.st.dim(f"· turn {step}/{max_steps} · {info}"))

    def reasoning(self, chunk: str) -> None:
        self._reasoned += len(chunk)
        if self.show_reasoning:
            self.spin.stop()
            sys.stderr.write(self.st.dim(chunk))
            sys.stderr.flush()
        else:
            self.spin.label = f"thinking ({self._reasoned} chars)"

    def text(self, chunk: str) -> None:
        if not self._text_started:
            self.spin.stop()
            if self.show_reasoning and self._reasoned:
                self.err()
            self._text_started = True
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def tool_progress(self, index: int, name: str) -> None:
        self.spin.label = f"model is writing a call to {name}"

    def tool_start(self, name: str, args: dict[str, Any]) -> None:
        self.spin.stop()
        self.err(self.st.cyan(f"  ▸ {name}") + " " + self.st.dim(summarize_args(name, args)))

    def tool_end(self, name: str, args: dict[str, Any], result: str, info: dict[str, Any], ok: bool) -> None:
        if not ok:
            self.err(self.st.red("  ✗ ") + short(result, 200))
            return
        if name == "run_command" and "exit_code" in info:
            code = info["exit_code"]
            mark = self.st.green("  ✓ exit 0") if code == 0 else self.st.red(f"  ✗ exit {code}")
            took = self.st.dim("(%.1fs)" % info.get("elapsed", 0))
            self.err(f"{mark} {took}" + (self.st.red("  TIMED OUT") if info.get("timed_out") else ""))
            if code != 0 or self.verbose:
                body = result.split("\n", 1)[1] if "\n" in result else ""
                for line in body.strip().splitlines()[-12:]:
                    self.err(self.st.dim("    │ " + short(line, 160)))
        elif info.get("diff"):
            self.err(self.st.green(f"  ✓ {info.get('path')}") + self.st.dim(f"  +{info.get('added', 0)} -{info.get('removed', 0)}"))
            self.print_diff(info["diff"], limit=30)
        elif name in {"append_file", "delete_path", "move_path"}:
            self.err(self.st.green("  ✓ ") + short(result, 160))
        elif self.verbose:
            self.err(self.st.dim("  ← " + short(result, 200)))

    def print_diff(self, diff: str, limit: int = 40) -> None:
        lines = diff.splitlines()
        for line in lines[:limit]:
            if line.startswith("+++") or line.startswith("---"):
                continue
            color = self.st.green if line.startswith("+") else self.st.red if line.startswith("-") else self.st.cyan if line.startswith("@@") else self.st.dim
            self.err("    " + color(short(line, 160)))
        if len(lines) > limit:
            self.err(self.st.dim(f"    … {len(lines) - limit} more diff lines (use /diff)"))

    def notice(self, message: str, level: str = "info") -> None:
        self.spin.stop()
        color = {"warn": self.st.yellow, "error": self.st.red, "dim": self.st.dim}.get(level, lambda s: s)
        self.err(color(("! " if level in {"warn", "error"} else "· ") + message))

    def turn_end(self, usage: dict[str, Any] | None, timings: dict[str, Any] | None, finish: str | None) -> None:
        self.spin.stop()
        if self.verbose and usage:
            tps = (timings or {}).get("predicted_per_second")
            self.err(self.st.dim(f"· tokens in {usage.get('prompt_tokens')} out {usage.get('completion_tokens')}" +
                                 (f" · {tps:.1f} tok/s" if tps else "") + f" · finish {finish}"))

    def plan(self, items: list[dict[str, str]]) -> None:
        self.spin.stop()
        marks = {"todo": "☐", "doing": "◐", "done": "☑"}
        self.err(self.st.bold("  Plan"))
        for i in items:
            line = f"   {marks.get(i['status'], '·')} {i['text']}"
            self.err(self.st.dim(line) if i["status"] == "done" else line)

    def summary(self, lines: list[str]) -> None:
        self.spin.stop()
        self.err(self.st.bold("\n── run summary (from real tool results) ──"))
        for line in lines:
            color = self.st.red if "FAILED" in line or "NOT RUN" in line else self.st.green if "exited 0" in line else self.st.dim
            self.err("  " + color(line))

    # ---- approvals
    def confirm(self, message: str) -> Any:
        """Ask the user. Returns False, True or "always" (remember this exact action for the session)."""
        self.spin.stop()
        if not sys.stdin.isatty():
            self.err(self.st.red("✗ approval needed but stdin is not a terminal; denied: ") + message.splitlines()[0])
            return False
        bar = self.st.amber("┃ ")
        self.err()
        for line in message.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                line = (self.st.green if line.startswith("+") else self.st.red)(line)
            self.err(bar + line)
        try:
            ans = self.input_fn(self.st.amber("┃ ") + "Allow? [y]es / [N]o / [a]lways for this exact action: ").strip().lower()
        except EOFError:
            self.err()
            return False
        except KeyboardInterrupt:      # Ctrl-C means "stop the task", not merely "no"
            self.err()
            raise
        if ans in {"a", "always"}:
            return "always"
        return ans in {"y", "yes"}


def warning_bypass(st: Style) -> str:
    return st.amber(st.bold("⚠ Bypass Permissions skips routine approvals (edits, ordinary commands, local destructive actions).")) + st.dim(
        "\n  Protected credentials, catastrophic commands, privileged / remote / publishing / out-of-workspace actions are refused without prompting.")


def banner_bypass(st: Style) -> str:
    return st.amber(st.bold("⚠ BYPASS PERMISSIONS is ON for this session: routine approvals are skipped.")) + st.dim(
        "\n  Protected credentials, catastrophic commands, privileged / remote / publishing / out-of-workspace actions are refused without prompting.")
