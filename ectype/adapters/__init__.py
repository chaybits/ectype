"""Adapter registry."""
from __future__ import annotations

from .. import titles
from ..model import Session

from .aider import AiderAdapter
from .antigravity import AntigravityAdapter
from .base import CATEGORIES, Adapter
from .claude_code import ClaudeCodeAdapter
from .cline import ClineAdapter
from .codex import CodexAdapter
from .continue_dev import ContinueAdapter
from .copilot_chat import CopilotChatAdapter
from .cursor import CursorAdapter
from .gemini_cli import GeminiCliAdapter
from .lmstudio import LMStudioAdapter
from .open_webui import OpenWebUIAdapter
from .roo_code import RooCodeAdapter
from .sillytavern import SillyTavernAdapter

ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (
        ClaudeCodeAdapter(), CodexAdapter(), GeminiCliAdapter(), AntigravityAdapter(),
        CopilotChatAdapter(), CursorAdapter(), ClineAdapter(), RooCodeAdapter(), ContinueAdapter(),
        AiderAdapter(), LMStudioAdapter(), OpenWebUIAdapter(), SillyTavernAdapter(),
    )
}


def get(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown agent {name!r}; known: {', '.join(ADAPTERS)}")
    return ADAPTERS[name]


def all_refs(agents: list[str] | None = None, enabled_only: bool = False):
    """Every session of every available agent, newest first.
    `enabled_only` honours the Settings toggles (what the GUI lists); the CLI shows everything."""
    refs = []
    for name, a in ADAPTERS.items():
        if agents and name not in agents:
            continue
        if enabled_only and not a.enabled():
            continue
        if a.available():
            refs.extend(a.discover())
    local = titles.all()          # read once: this runs over every session in every store
    if local:
        for r in refs:
            t = local.get(f"{r.agent}:{r.id}")
            if t:
                r.title = t
    return sorted(refs, key=lambda r: r.mtime, reverse=True)


def load(ref) -> Session:
    """The session behind a ref, with any local rename applied.

    Every caller goes through here rather than `ADAPTERS[ref.agent].load(ref)`, so a session
    renamed in the UI reads the same in a render, an export and a conversion. An adapter that
    renamed natively needs nothing here: the name is already in its own store."""
    s = ADAPTERS[ref.agent].load(ref)
    t = titles.get(ref.agent, ref.id)
    if t:
        s.title = t
    return s


def rename(ref, title: str) -> str:
    """Rename one session. Returns "native" when the agent's own store took the name, "local"
    when ectype kept it on its side (see `ectype.titles` for why those are the only two options).
    An empty title puts the generated name back in both cases."""
    if ADAPTERS[ref.agent].rename(ref, title):
        titles.set(ref.agent, ref.id, "")      # the store owns it now; no stale local copy
        return "native"
    titles.set(ref.agent, ref.id, title)
    return "local"
