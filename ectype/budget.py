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
import os
import threading
from collections import OrderedDict

from . import tokens
from .model import ContentBlock, Message, Session
from .render.text import RenderOptions, render, render_block, render_message
from .transform import Filter, apply

PARTS: list[tuple[str, str]] = [
    ("user", "your messages"),
    ("peers", "other sessions' messages"),   # what another session sent in (Claude Code's peer messages); a toggle
    ("assistant", "assistant text"),
    ("thinking", "thinking"),
    ("tool_calls", "tool calls"),
    ("tool_results", "tool results"),
    ("env", "injected context"),
    ("system", "system messages"),        # the store's own: Aider's console lines, SillyTavern's narrator; always shown
    ("notices", "agent notices"),         # the agent's notes to itself (API errors, refusals, away summaries); a toggle
    ("images", "images"),
    ("notice", "import notice"),
]
ALL = RenderOptions(thinking=True, tool_calls=True, tool_results=True, env=True, system=True, arg_chars=0)
ALL_OPTS = {"thinking": True, "tools": True, "env": True, "notices": True, "peers": True, "cap": 0, "mode": "full"}
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
    """The session as the options select it: kinds, roles, injected context, mode/cap, range, the
    questions put to the user as turns, and the messages other sessions sent in. The two new ones
    default to on when absent, as their settings do."""
    return apply(s, Filter(kinds=kinds_for(opts), env=bool(opts.get("env")), notices=bool(opts.get("notices")), cap=cap_of(opts),
                           brief=mode_of(opts) == "brief", collapse=bool(opts.get("collapse")),
                           questions=bool(opts.get("questions", True)), peers=bool(opts.get("peers", True)),
                           drop_roles=set(opts.get("hide_roles") or []) or None,
                           start=_index(opts, "start"), end=_index(opts, "end")))


def _index(opts: dict, key: str) -> int | None:
    """A message index counted from 0 in the full session, as the Help says. A negative number used
    to count from the end (Python's slice) and a fraction was a 500; both are refused, on every
    surface, because every surface comes through here (audit 2026-09-25, F61)."""
    v = opts.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or v < 0:
        raise ValueError(f"{key} must be a whole number >= 0 (got {v!r})")
    return int(v)


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
    # a split-off answer (transform.questions_as_turns) is no source message, merged or not
    return sum((0 if m.meta.get("split") else 1) + int(m.meta.get("merged", 0)) for m in s.messages if not m.meta.get("notice"))


def _part_of(m: Message, b: ContentBlock) -> str:
    if m.meta.get("notice"):
        return "notice"
    if m.meta.get("peer"):
        return "peers"
    if m.is_env:
        return "env"
    if m.meta.get("agent_notice"):
        return "notices"
    if b.kind == "text":
        return "user" if m.role == "user" else "assistant" if m.role == "assistant" else "system"
    if b.kind == "thinking":
        return "thinking"
    if b.kind == "tool_call":
        return "tool_calls"
    if b.kind == "tool_result":
        return "tool_results"
    if b.kind == "image":
        return "images"
    return "system"


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


def _file_stamp(p) -> tuple | None:
    try:
        st = os.stat(p)
    except (OSError, TypeError, ValueError):
        return None
    return (st.st_mtime_ns, st.st_size)


def everything(s: Session, tool_headers: bool = False, collapse: bool = False, wrap: str = "",
               questions: bool = True) -> dict:
    """Tokens of the whole session with every option on and no cap (cached per session).

    `tool_headers`, `collapse`, `wrap` and `questions` are part of the key: they are presentation
    (how a question is shown, not whether), so the ceiling has to be measured with the same
    presentation the view uses, or the two numbers are not comparable (and `selected` with
    everything on would not equal `everything`)."""
    # the file path is part of the key: the same session id can exist under two project folders
    # with different content (a session resumed from another directory)
    key = (s.agent, s.id, str(s.path), tool_headers, collapse, wrap, questions)
    # the entry validates itself: the long-lived MCP server reloads a session on every call and never
    # invalidated, so a session still being written kept its first ceiling ("selected 344 of 116")
    stamp = (_file_stamp(s.path), len(s.messages), s.title, s.project, s.cwd, s.model)
    with _LOCK:
        hit = _FULL.get(key)
        if hit is not None and hit.get("stamp") == stamp:
            _FULL.move_to_end(key)
            return hit
    full = filtered(s, {**ALL_OPTS, "collapse": collapse, "questions": questions})   # all on, only the layout applied
    tk, n, ch = _tally(full)                                             # the parts as that ceiling holds them
    ro = render_options({"tool_headers": tool_headers, "wrap": wrap, "mode": "full"})
    text = render(full, ro, source=s, with_footer=False)
    # `tail`: the render's last kilobyte, so `breakdown()` can price the import notice at the
    # exact boundary it is appended at (tokens merge across a newline: ".\n" is one token, "o\n" two)
    entry = {"tokens": tokens.count(text), "tail": text[-1000:], "parts": tk,
             "blocks": n, "chars": ch, "messages": len(s.messages), "stamp": stamp}
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
    full = everything(s, bool(opts.get("tool_headers")), bool(opts.get("collapse")), opts.get("wrap") or "",
                      bool(opts.get("questions", True)))
    view = shown if shown is not None else filtered(s, opts)
    shown_n = shown_count(view)
    ro = render_options(opts)
    text = rendered if rendered is not None else render(view, ro, source=s, with_footer=False)
    selected = tokens.count(text)                      # content only; the footer that states it is not counted
    tk, n, ch = _tally(view, ro)                       # the same presentation as the text above
    tools_on = bool(opts.get("tools", True))
    hidden = set(opts.get("hide_roles") or [])
    on = {"user": "user" not in hidden, "peers": bool(opts.get("peers", True)), "assistant": "assistant" not in hidden,
          "thinking": bool(opts.get("thinking")) and "assistant" not in hidden, "tool_calls": tools_on,
          "tool_results": tools_on, "env": bool(opts.get("env")), "system": True,
          "notices": bool(opts.get("notices")), "images": True,
          "notice": n["notice"] > 0}
    parts = []
    for key, label in PARTS:
        if full["blocks"][key] == 0 and n[key] == 0:
            continue                                   # this session has no such part
        # The import notice is not part of the session, so the cached ceiling never holds it. It
        # is a presentation choice, like the tool headers: when the view appends it, the ceiling
        # is the session plus the notice, or `selected` exceeds `everything` on a session smaller
        # than the notice itself (F33: 317 against 232 on a four-message session).
        full_tokens = tk[key] if key == "notice" else full["parts"][key]
        full_blocks = n[key] if key == "notice" else full["blocks"][key]
        full_chars = ch[key] if key == "notice" else full["chars"][key]
        parts.append({"key": key, "label": label, "on": on[key], "tokens": tk[key], "blocks": n[key], "chars": ch[key],
                      "full": full_tokens, "full_blocks": full_blocks, "full_chars": full_chars,
                      # what this element would still cost if it were included in full: the
                      # "remaining" half of the row, so a capped element reads as two numbers
                      "remaining": max(0, full_tokens - tk[key])})
    # The notice as the render adds it: the message with its own header line and the separator
    # before it, priced at the boundary of the FULL render (its cached tail), because token counts
    # are not additive across a newline. Its block tokens alone left the ceiling short by the
    # header (259 against 242 on a two-message session); the Help promises that with everything
    # on, selected equals the ceiling exactly, and this keeps that true to the token.
    notice_cost = 0
    tail = full.get("tail", "")
    for m in view.messages:
        if m.meta.get("notice"):
            nro = dataclasses.replace(ro, stamps="none") if ro.wrap else ro
            r = render_message(m, nro)
            if r:
                # the render is `<everything>\n`; with the notice it is `<everything>\n\n<notice>\n`
                notice_cost += tokens.count(tail[:-1] + "\n\n" + r + "\n") - tokens.count(tail)
    return {
        "selected": selected,
        "everything": full["tokens"] + notice_cost,
        "exact": tokens.is_exact(),
        "messages": shown_n,
        "messages_full": full["messages"],
        "mode": mode_of(opts),
        "cap": cap_of(opts),
        "parts": parts,
        "structure": max(0, selected - sum(tk.values())),
        "context": {"peak": s.peak_context, "cumulative": s.total_tokens},
    }
