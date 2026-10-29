"""Terminal-native interactive menus: arrow navigation, live filtering, metadata, and graceful fallback."""
from __future__ import annotations

import sys
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout


class MenuItem:
    def __init__(self, key: str, label: str, description: str = "", metadata: str = "",
                 current: bool = False, disabled: bool = False, disabled_reason: str = "",
                 data: Any = None):
        self.key = key
        self.label = label
        self.description = description
        self.metadata = metadata
        self.current = current
        self.disabled = disabled
        self.disabled_reason = disabled_reason
        self.data = data


class TerminalMenu:
    def __init__(self, title: str, items: list[MenuItem], subtitle: str = "",
                 page_size: int = 10, show_filter: bool = True, empty_message: str = "No matching items"):
        self.title = title
        self.subtitle = subtitle
        self.raw_items = list(items)
        self.page_size = page_size
        self.show_filter = show_filter
        self.empty_message = empty_message
        self.filter_text = ""
        self.selected_index = 0
        self._init_selection()

    def _init_selection(self) -> None:
        for i, item in enumerate(self.raw_items):
            if item.current and not item.disabled:
                self.selected_index = i
                return
        for i, item in enumerate(self.raw_items):
            if not item.disabled:
                self.selected_index = i
                return
        self.selected_index = 0

    @property
    def filtered_items(self) -> list[MenuItem]:
        if not self.filter_text:
            return self.raw_items
        q = self.filter_text.lower()
        return [it for it in self.raw_items if q in it.label.lower() or q in it.description.lower() or q in it.metadata.lower()]

    def run(self, input_pipe: Any = None) -> MenuItem | None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()) and input_pipe is None:
            # Non-interactive fallback: return current item or first non-disabled item
            for item in self.raw_items:
                if item.current and not item.disabled:
                    return item
            for item in self.raw_items:
                if not item.disabled:
                    return item
            return self.raw_items[0] if self.raw_items else None

        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _up(event):  # noqa: ANN001
            items = self.filtered_items
            if not items:
                return
            n = len(items)
            # Find previous non-disabled item
            idx = (self.selected_index - 1) % n
            for _ in range(n):
                if not items[idx].disabled:
                    self.selected_index = idx
                    break
                idx = (idx - 1) % n
            event.app.invalidate()

        @kb.add("down")
        @kb.add("j")
        def _down(event):  # noqa: ANN001
            items = self.filtered_items
            if not items:
                return
            n = len(items)
            # Find next non-disabled item
            idx = (self.selected_index + 1) % n
            for _ in range(n):
                if not items[idx].disabled:
                    self.selected_index = idx
                    break
                idx = (idx + 1) % n
            event.app.invalidate()

        @kb.add("pageup")
        def _pgup(event):  # noqa: ANN001
            items = self.filtered_items
            if not items:
                return
            self.selected_index = max(0, self.selected_index - self.page_size)
            event.app.invalidate()

        @kb.add("pagedown")
        def _pgdn(event):  # noqa: ANN001
            items = self.filtered_items
            if not items:
                return
            self.selected_index = min(len(items) - 1, self.selected_index + self.page_size)
            event.app.invalidate()

        @kb.add("home")
        def _home(event):  # noqa: ANN001
            self.selected_index = 0
            event.app.invalidate()

        @kb.add("end")
        def _end(event):  # noqa: ANN001
            items = self.filtered_items
            if items:
                self.selected_index = len(items) - 1
            event.app.invalidate()

        @kb.add("enter")
        def _enter(event):  # noqa: ANN001
            items = self.filtered_items
            if not items:
                return
            if 0 <= self.selected_index < len(items):
                chosen = items[self.selected_index]
                if not chosen.disabled:
                    event.app.exit(result=chosen)

        @kb.add("escape")
        @kb.add("c-c")
        def _esc(event):  # noqa: ANN001
            event.app.exit(result=None)

        @kb.add("backspace")
        def _bs(event):  # noqa: ANN001
            if self.filter_text:
                self.filter_text = self.filter_text[:-1]
                self.selected_index = 0
                event.app.invalidate()

        @kb.add("<any>")
        def _char(event):  # noqa: ANN001
            char = event.data
            if char.isprintable() and len(char) == 1 and not event.key_sequence[0].key.startswith("c-"):
                self.filter_text += char
                self.selected_index = 0
                event.app.invalidate()

        def render_text():
            res = [("bold", f"{self.title}\n")]
            if self.subtitle:
                res.append(("dim", f"{self.subtitle}\n"))
            if self.show_filter or self.filter_text:
                res.append(("#5fafff", f"Filter: {self.filter_text}█\n"))
            res.append(("", "\n"))

            items = self.filtered_items
            if not items:
                res.append(("#ffaf00", f"  ({self.empty_message})\n"))
            else:
                if self.selected_index >= len(items):
                    self.selected_index = max(0, len(items) - 1)

                start = max(0, min(self.selected_index - self.page_size // 2, len(items) - self.page_size))
                end = min(start + self.page_size, len(items))

                for i in range(start, end):
                    it = items[i]
                    is_sel = (i == self.selected_index)
                    prefix = " ▸ " if is_sel else "   "
                    cur_mark = " (current)" if it.current else ""

                    if it.disabled:
                        res.append(("dim", f"{prefix}{it.label}{cur_mark} [unavailable: {it.disabled_reason}]\n"))
                    elif is_sel:
                        res.append(("#5fafff bold", f"{prefix}{it.label}{cur_mark}"))
                        if it.description:
                            res.append(("#5fafff", f" - {it.description}"))
                        if it.metadata:
                            res.append(("dim", f"  [{it.metadata}]"))
                        res.append(("", "\n"))
                    else:
                        res.append(("", f"{prefix}{it.label}{cur_mark}"))
                        if it.description:
                            res.append(("dim", f" - {it.description}"))
                        if it.metadata:
                            res.append(("dim", f"  [{it.metadata}]"))
                        res.append(("", "\n"))

                if len(items) > self.page_size:
                    res.append(("dim", f"\n  [{self.selected_index + 1}/{len(items)}] ↑/↓ to navigate, Enter to select, Esc to cancel\n"))
                else:
                    res.append(("dim", "\n  ↑/↓ navigate · Enter select · Esc cancel\n"))

            return FormattedText(res)

        app = Application(
            layout=Layout(Window(content=FormattedTextControl(render_text))),
            key_bindings=kb,
            input=input_pipe,
        )
        return app.run()


def select_model(models: list[dict[str, Any]], current_ref: str | None, server_name: str,
                 is_offline: bool = False, input_pipe: Any = None) -> dict[str, Any] | None:
    items = []
    from .modelinfo import capabilities
    for m in models:
        ref = m.get("ref", "")
        name = m.get("name") or ref
        is_cur = (ref == current_ref)
        caps = capabilities(m, server_name)

        meta_parts = []
        if m.get("cached"):
            meta_parts.append("cached - offline")
        if m.get("context_max"):
            meta_parts.append(f"ctx {m['context_max']:,}")
        if m.get("provider"):
            meta_parts.append(m["provider"])

        desc_parts = []
        desc_parts.append(f"tools: {caps.tool_calling}")
        if caps.reasoning:
            desc_parts.append("reasoning: yes")
        if m.get("vision"):
            desc_parts.append("vision: yes")

        disabled = (m.get("supported") is False)
        reason = m.get("unsupported_reason") or "unsupported"

        items.append(MenuItem(
            key=ref,
            label=name,
            description=" · ".join(desc_parts),
            metadata=" · ".join(meta_parts),
            current=is_cur,
            disabled=disabled,
            disabled_reason=reason,
            data=m,
        ))

    sub = f"Server: {server_name}"
    if is_offline:
        sub += " [OFFLINE - showing cached models]"
    sub += " · Type to filter models"

    menu = TerminalMenu(title="Select Model", items=items, subtitle=sub, show_filter=True)
    chosen = menu.run(input_pipe=input_pipe)
    return chosen.data if chosen else None


def select_effort(current_effort: str, caps: Any, input_pipe: Any = None) -> str | None:
    from .modelinfo import EFFORT
    items = [
        MenuItem("low", "Low", "Direct, fast, minimal planning (4,096 tokens/turn, 0.5x steps)",
                 current=(current_effort == "low")),
        MenuItem("medium", "Medium", "Balanced, inspects relevant code and verifies with tests (8,192 tokens/turn, 1.0x steps)",
                 current=(current_effort == "medium")),
        MenuItem("high", "High", "Thorough, creates plans, reads files in full, checks edge cases (16,384 tokens/turn, 1.5x steps)",
                 current=(current_effort == "high")),
        MenuItem("max", "Maximum", "Maximum rigor, full test suite and deep verification (32,768 tokens/turn, 2.0x steps)",
                 current=(current_effort == "max")),
    ]
    model_note = f"Current: {current_effort.title()} · Model thinking toggle: {caps.thinking_toggle}"
    menu = TerminalMenu(title="Reasoning Effort (agent strategy)", items=items,
                        subtitle=model_note, show_filter=False)
    chosen = menu.run(input_pipe=input_pipe)
    return chosen.key if chosen else None


def select_thinking(current_thinking: str, caps: Any, input_pipe: Any = None) -> str | None:
    items = [
        MenuItem("auto", "Auto", "Enable thinking automatically based on effort level (recommended)",
                 current=(current_thinking == "auto")),
        MenuItem("on", "On", "Always enable reasoning thinking text",
                 current=(current_thinking == "on")),
        MenuItem("off", "Off", "Disable reasoning thinking text",
                 current=(current_thinking == "off")),
    ]
    sub = f"Current: {current_thinking} · Model capability: {caps.thinking_toggle}"
    menu = TerminalMenu(title="Thinking Mode", items=items, subtitle=sub, show_filter=False)
    chosen = menu.run(input_pipe=input_pipe)
    return chosen.key if chosen else None


def select_permissions(current_mode: str, input_pipe: Any = None) -> str | None:
    items = [
        MenuItem("ask", "Ask", "Confirm every file change and command execution",
                 current=(current_mode == "ask")),
        MenuItem("auto", "Auto", "Workspace edits + low-risk commands; asks for installs/network/destructive",
                 current=(current_mode == "auto")),
        MenuItem("full-access", "Full Access", "Most local dev actions without prompts; destructive still asks",
                 current=(current_mode == "full-access")),
        MenuItem("bypass", "Bypass", "Unrestricted execution; only OS permissions and authentication apply",
                 current=(current_mode == "bypass")),
    ]
    sub = f"Current mode: {current_mode}"
    menu = TerminalMenu(title="Permission Mode", items=items, subtitle=sub, show_filter=False)
    chosen = menu.run(input_pipe=input_pipe)
    return chosen.key if chosen else None


def select_server(profiles: dict[str, Any], current_server: str | None,
                  is_connected: bool, input_pipe: Any = None) -> str | None:
    items = []
    # Action 1: Reconnect / retry
    conn_str = "Connected" if is_connected else "Offline"
    items.append(MenuItem(
        "action:reconnect",
        f"Reconnect / Test Connection ({current_server})",
        description=f"Status: {conn_str}",
        metadata="action"
    ))

    # Servers list
    for name, p in sorted(profiles.items()):
        is_cur = (name == current_server)
        tgt = (p.get("ssh", {}).get("target") and f"ssh {p['ssh']['target']}") or p.get("url") or "unknown"
        items.append(MenuItem(
            f"server:{name}",
            f"Switch to: {name}",
            description=f"Target: {tgt} ({p.get('transport', 'direct')})",
            current=is_cur,
            metadata="server profile"
        ))

    if is_connected:
        items.append(MenuItem(
            "action:disconnect",
            "Disconnect",
            description="Close current server connection and enter offline mode",
            metadata="action"
        ))

    items.append(MenuItem(
        "action:diagnostics",
        "View Server Diagnostics",
        description="Detailed diagnostic inspection of connection, protocol, and endpoints",
        metadata="action"
    ))

    sub = f"Current Server: {current_server or 'None'} · Status: {conn_str}"
    menu = TerminalMenu(title="Server Management & Diagnostics", items=items, subtitle=sub, show_filter=False)
    chosen = menu.run(input_pipe=input_pipe)
    return chosen.key if chosen else None


def select_session(sessions: list[dict[str, Any]], current_id: str | None = None,
                   input_pipe: Any = None) -> str | None:
    items = []
    for s in sessions:
        sid = s.get("id", "")
        name = s.get("name") or sid[:8]
        status = str(s.get("status", "unknown"))
        ws = s.get("workspace", "")
        model = s.get("model", "")
        is_cur = (sid == current_id)

        items.append(MenuItem(
            key=sid,
            label=name,
            description=f"status: {status} · {ws}",
            metadata=f"model: {model}",
            current=is_cur,
            data=s,
        ))

    menu = TerminalMenu(title="Saved Sessions", items=items,
                        subtitle="Select a session to resume · Type to filter", show_filter=True)
    chosen = menu.run(input_pipe=input_pipe)
    return chosen.key if chosen else None
