"""Token budget: what a render costs, part by part.

`breakdown(session, opts)` attributes the tokens of a render to parts (your messages, assistant
text, thinking, tool calls, tool results, injected context, notices, images) plus "structure"
(headers, timestamps, separators). `everything(session)` is the same with every option on and
no cap, cached per session. The web UI shows both, so the cost of each toggle and of the cap
is visible before anything is exported.

Counting is done on the rendered form of each block (what a reader or a model would actually
see), so the parts add up to the render, not on raw block text.
"""
from __future__ import annotations

import dataclasses
import threading
from collections import OrderedDict

from . import tokens
from .model import ContentBlock, Message, Session
from .render.text import RenderOptions, render, render_block
from .transform import Filter, apply

PARTS: list[tuple[str, str]] = [
    ("user", "your messages"),
    ("assistant", "assistant text"),
    ("thinking", "thinking"),
    ("tool_calls", "tool calls"),
    ("tool_results", "tool results"),
    ("env", "injected context"),
    ("notices", "notices"),
    ("images", "images"),
    ("notice", "import notice"),
]
ALL = RenderOptions(thinking=True, tool_calls=True, tool_results=True, env=True, system=True, arg_chars=0)
ALL_OPTS = {"thinking": True, "tools": True, "env": True, "cap": 0, "mode": "full"}
MODES = {
    "brief": "names only: the tool's name alone, with no arguments, no output and no sizes; a failed call is flagged",
    "custom": "the first N tokens of each result, N set in Settings → View, and a call's arguments cut at 200 characters (N = 0 cuts nothing)",
    "full": "every argument and every byte of output",
}

# The ceiling of a session, keyed on (agent, id, presentation). Bounded, because the GUI is a
# long-running process and a browse session touches an unbounded number of transcripts; and
# invalidated by `invalidate()` when the file behind a session changed on disk, because the key
# says nothing about content; without that, a session an agent is still writing kept its first
# ceiling while `selected` grew past it, and the two headline numbers contradicted each other.
_FULL: "OrderedDict[tuple, dict]" = OrderedDict()
_FULL_MAX = 64
_LOCK = threading.Lock()         # the web server renders on request threads; the OrderedDict is not thread-safe


def invalidate(agent: str | None = None, sid: str | None = None, path: str | None = None) -> None:
    """Forget the cached ceiling of one session (or all of them when called with no arguments).
    `path` narrows it to one file when the same id exists under two project folders."""
    with _LOCK:
        if agent is None:
            _FULL.clear()
            return
        for key in [k for k in _FULL if k[0] == agent and k[1] == sid and (path is None or k[2] == path)]:
            del _FULL[key]


def kinds_for(opts: dict) -> set[str]:
    kinds = {"text", "system", "info", "error", "image"}
    if opts.get("thinking"):
        kinds.add("thinking")
    if opts.get("tools", True):
        kinds |= {"tool_call", "tool_result"}
    return kinds


def mode_of(opts: dict) -> str:
    m = opts.get("mode") or "custom"
    return m if m in MODES else "custom"


def cap_of(opts: dict) -> int:
    """The cap the options actually apply: only `custom` mode caps anything."""
    return int(opts.get("cap") or 0) if mode_of(opts) == "custom" else 0


def filtered(s: Session, opts: dict) -> Session:
    """The session as the options select it: kinds, roles, injected context, mode/cap, range."""
    return apply(s, Filter(kinds=kinds_for(opts), env=bool(opts.get("env")), cap=cap_of(opts),
                           brief=mode_of(opts) == "brief", collapse=bool(opts.get("collapse")),
                           drop_roles=set(opts.get("hide_roles") or []) or None,
                           start=opts.get("start"), end=opts.get("end")))


def render_options(opts: dict, local_time: bool = True, show_offset: bool = True) -> RenderOptions:
    """RenderOptions for a view: everything on, because `filtered()` has already removed whatever
    the options exclude. Only presentation choices belong here."""
    wrap = opts.get("wrap") or ""
    return dataclasses.replace(
        ALL, local_time=local_time, show_offset=show_offset,
        tool_headers=bool(opts.get("tool_headers")),
        # one message cannot carry 800 timestamps meaningfully, so wrapping drops them
        stamps="none" if wrap else (opts.get("stamps") or "full"),
        markers=bool(opts.get("markers", True)), wrap=wrap,
        # a cap of 0 means nothing is cut (results or arguments), so `full`, and `custom` with the cap
        # at 0, print every argument; a real cap also cuts a call's arguments at 200 characters
        arg_chars=0 if cap_of(opts) == 0 else 200)


def shown_count(s: Session) -> int:
    """How many of the source's messages a view shows: the originals behind a merged turn count,
    the appended import notice does not. So the GUI's "981 of 981 messages" stays true with
    merging on, instead of collapsing to the number of merged turns."""
    return sum(1 + int(m.meta.get("merged", 0)) for m in s.messages if not m.meta.get("notice"))


def _part_of(m: Message, b: ContentBlock) -> str:
    if m.meta.get("notice"):
        return "notice"
    if m.is_env:
        return "env"
    if b.kind == "text":
        return "user" if m.role == "user" else "assistant" if m.role == "assistant" else "notices"
    if b.kind == "thinking":
        return "thinking"
    if b.kind == "tool_call":
        return "tool_calls"
    if b.kind == "tool_result":
        return "tool_results"
    if b.kind == "image":
        return "images"
    return "notices"


def _tally(s: Session, o: RenderOptions = ALL) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Tokens, blocks and chars per part, counted on each block AS THE VIEW RENDERS IT (`o`): the
    parts must add up to the render, and a view that cuts a call's arguments renders less than the
    ceiling does."""
    tk = {k: 0 for k, _ in PARTS}
    n = {k: 0 for k, _ in PARTS}
    ch = {k: 0 for k, _ in PARTS}
    for m in s.messages:
        for b in m.blocks:
            r = render_block(b, o)
            if r is None:
                continue
            p = _part_of(m, b)
            tk[p] += tokens.count(r)
            n[p] += 1
            ch[p] += len(r)
    return tk, n, ch


def everything(s: Session, tool_headers: bool = False, collapse: bool = False, wrap: str = "") -> dict:
    """Tokens of the whole session with every option on and no cap (cached per session).

    `tool_headers` and `collapse` are part of the key: they are presentation, so the ceiling has
    to be measured with the same presentation the view uses, or the two numbers are not
    comparable (and `selected` with everything on would not equal `everything`)."""
    # the file path is part of the key: the same session id can exist under two project folders
    # with different content (a session resumed from another directory)
    key = (s.agent, s.id, str(s.path), tool_headers, collapse, wrap)
    with _LOCK:
        hit = _FULL.get(key)
        if hit is not None:
            _FULL.move_to_end(key)
            return hit
    tk, n, ch = _tally(s)
    ro = render_options({"tool_headers": tool_headers, "wrap": wrap, "mode": "full"})
    full = filtered(s, {**ALL_OPTS, "collapse": collapse})               # all on, only the layout applied
    entry = {"tokens": tokens.count(render(full, ro, source=s, with_footer=False)), "parts": tk,
             "blocks": n, "chars": ch, "messages": len(s.messages)}
    with _LOCK:
        _FULL[key] = entry
        _FULL.move_to_end(key)
        while len(_FULL) > _FULL_MAX:
            _FULL.popitem(last=False)
    return entry


def breakdown(s: Session, opts: dict, shown: Session | None = None, rendered: str | None = None) -> dict:
    """Per-part token accounting of the render `opts` select.

    `shown`/`rendered` let the caller pass the exact session/text it displays (e.g. after
    redaction) so the numbers match the screen; otherwise they are computed here."""
    full = everything(s, bool(opts.get("tool_headers")), bool(opts.get("collapse")), opts.get("wrap") or "")
    view = shown if shown is not None else filtered(s, opts)
    shown_n = shown_count(view)
    ro = render_options(opts)
    text = rendered if rendered is not None else render(view, ro, source=s, with_footer=False)
    selected = tokens.count(text)                      # content only; the footer that states it is not counted
    tk, n, ch = _tally(view, ro)                       # the same presentation as the text above
    tools_on = bool(opts.get("tools", True))
    hidden = set(opts.get("hide_roles") or [])
    on = {"user": "user" not in hidden, "assistant": "assistant" not in hidden,
          "thinking": bool(opts.get("thinking")) and "assistant" not in hidden, "tool_calls": tools_on,
          "tool_results": tools_on, "env": bool(opts.get("env")), "notices": True, "images": True,
          "notice": n["notice"] > 0}
    parts = []
    for key, label in PARTS:
        if full["blocks"][key] == 0 and n[key] == 0:
            continue                                   # this session has no such part
        parts.append({"key": key, "label": label, "on": on[key], "tokens": tk[key], "blocks": n[key], "chars": ch[key],
                      "full": full["parts"][key], "full_blocks": full["blocks"][key], "full_chars": full["chars"][key],
                      # what this element would still cost if it were included in full: the
                      # "remaining" half of the row, so a capped element reads as two numbers
                      "remaining": max(0, full["parts"][key] - tk[key])})
    return {
        "selected": selected,
        "everything": full["tokens"],
        "exact": tokens.is_exact(),
        "messages": shown_n,
        "messages_full": full["messages"],
        "mode": mode_of(opts),
        "cap": cap_of(opts),
        "parts": parts,
        "structure": max(0, selected - sum(tk.values())),
        "context": {"peak": s.peak_context, "cumulative": s.total_tokens},
    }
