"""Compatibility shim: the 0.1.0 entry point lived here."""
from .agent import Session  # noqa: F401
from .cli import main  # noqa: F401
from .context import repaired_history  # noqa: F401
from .engine import Engine, parse_sse  # noqa: F401
from .tools import TOOLS, Workspace, clipped  # noqa: F401
