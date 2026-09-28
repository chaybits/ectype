"""Adapter registry: the thirteen built-ins, plus any adapter another package registers.

PLUG-INS. A separate package can provide an adapter without a change here. It names the class in
its own `pyproject.toml`:

    [project.entry-points."ectype.adapters"]
    goose = "ectype_goose:GooseAdapter"

and ectype loads it at start, with the same `discover()` / `load()` contract the built-ins meet
(`ectype.adapters.base.Adapter`; the shapes it returns are `ectype.model.SessionRef` and
`Session`). A plug-in that fails to import, is not an Adapter, or reuses a built-in's name is
reported on stderr and skipped: someone else's mistake must never stop the program from listing
the stores it does know. `PLUGINS` records where each loaded one came from, so `ectype agents`
can say so. A worked example ships in `integrations/adapter-plugin/`.
"""
from __future__ import annotations

import sys

from .. import titles
from ..model import Session

from .aider import AiderAdapter
from .antigravity import AntigravityAdapter
from .base import CATEGORIES, Adapter, mark_questions
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
BUILTIN = frozenset(ADAPTERS)
PLUGINS: dict[str, str] = {}          # adapter name -> "module:attr" of the entry point it came from
ENTRY_POINT_GROUP = "ectype.adapters"


def load_plugins(entry_points=None) -> list[tuple[str, str]]:
    """Register every adapter another installed package announces under ENTRY_POINT_GROUP.

    Args:
        entry_points: the entry points to consider; None = whatever `importlib.metadata` finds
            among the installed packages (tests pass their own).
    Returns:
        [(name, reason)] for every entry point that was NOT loaded: it failed to import, produced
        something that is not an Adapter, or reused a name that is already taken. Each is also
        printed on stderr, because a plug-in the user installed and does not see is a silent
        failure, and a plug-in that breaks the whole listing is a worse one.
    """
    if entry_points is None:
        from importlib.metadata import entry_points as _eps
        try:
            entry_points = list(_eps(group=ENTRY_POINT_GROUP))
        except Exception as e:                        # noqa: BLE001, broken metadata on sys.path must not kill the built-ins
            print(f"ectype: adapter plug-ins could not be listed ({type(e).__name__}: {e}); built-ins only", file=sys.stderr)
            return [(ENTRY_POINT_GROUP, f"{type(e).__name__}: {e}")]
    skipped: list[tuple[str, str]] = []
    for ep in entry_points:
        try:
            obj = ep.load()
            ad = obj() if isinstance(obj, type) else obj
        except Exception as e:                        # noqa: BLE001, the plug-in's error is reported, whatever it is
            skipped.append((ep.name, f"could not be loaded: {type(e).__name__}: {e}"))
            continue
        name = getattr(ad, "name", None)
        if not isinstance(ad, Adapter) or not name or not all(getattr(ad, k, None) for k in ("label", "env_home", "default_home")):
            skipped.append((ep.name, "is not an ectype Adapter with name, label, env_home and default_home"))
            continue
        if name in ADAPTERS:
            skipped.append((ep.name, f"the name {name!r} is already taken by "
                                     + ("a built-in adapter" if name in BUILTIN else f"plug-in {PLUGINS[name]}")))
            continue
        ADAPTERS[name] = ad
        PLUGINS[name] = ep.value
    for name, why in skipped:
        value = next((ep.value for ep in entry_points if ep.name == name), "?")
        print(f"ectype: adapter plug-in {name!r} ({value}) skipped: {why}", file=sys.stderr)
    return skipped


# Discovery at import, like the built-ins above: `ADAPTERS` is read as a plain dict everywhere, so a
# lazy registry would mean a proxy for one dict. The scan is a read of installed metadata (a few
# milliseconds) and can never raise past this line.
load_plugins()


def get(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown agent {name!r}; known: {', '.join(ADAPTERS)}")
    return ADAPTERS[name]


def all_refs(agents: list[str] | None = None, enabled_only: bool = False, skipped: list | None = None):
    """Every session of every available agent, newest first.
    `enabled_only` honours the Settings toggles (what the GUI lists); the CLI shows everything.
    `skipped`, when given, receives `(agent, reason)` for every store that could not be read, so a
    caller whose stderr nobody reads (the MCP server's client) can say so in its answer."""
    refs = []
    for name, a in ADAPTERS.items():
        if agents and name not in agents:
            continue
        if enabled_only and not a.enabled():
            continue
        if not a.available():
            continue
        try:
            refs.extend(a.discover())
        except Exception as e:                        # noqa: BLE001, one store that cannot be read must not hide the other twelve, but it must say so
            print(f"ectype: {name}: the store at {a.home()} could not be read ({type(e).__name__}: {e}); "
                  f"the listing goes on without it", file=sys.stderr)
            if skipped is not None:
                skipped.append((name, f"the store at {a.home()} could not be read ({type(e).__name__}: {e})"))
    local = titles.names()        # read once: this runs over every session in every store
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
    renamed natively needs nothing here: the name is already in its own store. The agent's question
    tool is marked here too, once for every adapter (`Adapter.question_tools`)."""
    ad = ADAPTERS[ref.agent]
    s = ad.load(ref)
    mark_questions(s, getattr(ad, "question_tools", None))
    t = titles.get(ref.agent, ref.id)
    if t:
        s.title = t
    return s


def rename(ref, title: str) -> str:
    """Rename one session. Returns "native" when the agent's own store took the name, "local"
    when ectype kept it on its side (see `ectype.titles` for why those are the only two options).
    An empty title puts the generated name back in both cases."""
    if ADAPTERS[ref.agent].rename(ref, title):
        titles.put(ref.agent, ref.id, "")      # the store owns it now; no stale local copy
        return "native"
    titles.put(ref.agent, ref.id, title)
    return "local"
