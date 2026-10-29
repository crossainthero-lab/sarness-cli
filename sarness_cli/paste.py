"""Collapsed paste manager for Sarness CLI.

Intercepts large or multiline pastes via bracketed paste, collapses them into a compact
indicator token in the composer, preserves the original full text internally, and allows
expanding, inspecting, editing (via Ctrl-E) or removing before sending.
"""
from __future__ import annotations

import re
from typing import Any

from prompt_toolkit.lexers import Lexer


PASTE_TOKEN_RE = re.compile(r"\[▸ Pasted text · [0-9,]+ characters · [0-9,]+ lines #P(?P<id>[0-9]+)\]")


class PasteLexer(Lexer):
    """Styles [▸ Pasted text · ... #P...] in blue while keeping other text normal."""

    def lex_document(self, document: Any) -> Any:
        def get_line(lineno: int) -> list[tuple[str, str]]:
            line = document.lines[lineno]
            if "[▸ Pasted text ·" not in line:
                return [("", line)]
            tokens: list[tuple[str, str]] = []
            last_idx = 0
            for m in PASTE_TOKEN_RE.finditer(line):
                start, end = m.span()
                if start > last_idx:
                    tokens.append(("", line[last_idx:start]))
                tokens.append(("class:pasted-block", line[start:end]))
                last_idx = end
            if last_idx < len(line):
                tokens.append(("", line[last_idx:]))
            return tokens

        return get_line



class PasteManager:
    """Manages collapsed bracketed pastes in the prompt buffer."""

    def __init__(self, char_threshold: int = 200, line_threshold: int = 4):
        self.char_threshold = char_threshold
        self.line_threshold = line_threshold
        self._pastes: dict[int, str] = {}
        self._counter: int = 0

    def should_collapse(self, text: str) -> bool:
        """True if the pasted text exceeds size or newline thresholds."""
        lines = text.count("\n") + (1 if text else 0)
        return len(text) >= self.char_threshold or lines >= self.line_threshold

    def register(self, text: str) -> str:
        """Store the full raw text and return the compact token string."""
        self._counter += 1
        pid = self._counter
        self._pastes[pid] = text
        chars = len(text)
        lines = text.count("\n") + (1 if text else 0)
        return f"[▸ Pasted text · {chars:,} characters · {lines:,} lines #P{pid}]"

    def get(self, pid: int) -> str | None:
        return self._pastes.get(pid)

    def expand(self, text: str) -> str:
        """Replace all collapsed paste tokens in text with their stored original text."""
        if not text or "#P" not in text:
            return text

        def _repl(m: re.Match) -> str:
            pid = int(m.group("id"))
            return self._pastes.get(pid, m.group(0))

        return PASTE_TOKEN_RE.sub(_repl, text)

    def has_tokens(self, text: str) -> bool:
        return bool(PASTE_TOKEN_RE.search(text))

    def expand_buffer(self, buffer: Any) -> bool:
        """Expand all paste tokens currently present in a prompt_toolkit buffer in-place."""
        current_text = buffer.text
        if not self.has_tokens(current_text):
            return False
        expanded = self.expand(current_text)
        if expanded != current_text:
            buffer.text = expanded
            buffer.cursor_position = len(expanded)
            return True
        return False
