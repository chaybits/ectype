"""Plain-text rendering of a canonical Session (the extraction.txt successor)."""
from __future__ import annotations

import json
import dataclasses
from dataclasses import dataclass
from datetime import datetime

from .. import tokens
from ..model import ContentBlock, Message, Session


@dataclass
class RenderOptions:
    thinking: bool = False
    tool_calls: bool = True
    tool_results: bool = True
    env: bool = False              # injected system/environment context messages
    system: bool = True            # info/error/system notices
    local_time: bool = True        # False = convert every time to UTC
    show_offset: bool = True       # print the "+0300" in the session header (off = hide timezone)
    stamps: str = "full"           # per-message time: "full" (date+time) | "time" | "none"
    tool_headers: bool = False     # give tool turns their own "[TOOL] <timestamp>" line
    markers: bool = True           # print the "[USER]" / "[ASSISTANT]" line at all
    wrap: str = ""                 # "", or a role: structured formats emit ONE message of that role
    arg_chars: int = 200           # characters of a tool call's arguments to print; 0 = all of them (the `full` mode)


_ALT_CLIP = 60                    # tokens shown of each branch alternative (swipes, versions); a hint, not the text

# What a per-message header costs, measured with cl100k_base on "[ASSISTANT] …":
#   full  "2026-04-18 02:08:51"  18 tokens      time  "02:08:51"  11      none  5
STAMP_FORMATS = {"full": "%Y-%m-%d %H:%M:%S", "time": "%H:%M:%S", "none": ""}
_NO_STAMP = {"full": "---------- --:--:--", "time": "--:--:--"}      # as wide as the stamp it stands in for


def _fmt(ts: datetime | None, local: bool, offset: bool = False, style: str = "full") -> str:
    """A timestamp. The UTC offset is never repeated per message: it is the same all session, it
    costs three tokens a line, and it is what "hide timezone" removes. The session header shows it
    once, unless that rule is on."""
    fmt = STAMP_FORMATS.get(style, STAMP_FORMATS["full"])
    if not fmt:
        return ""
    if ts is None:
        return _NO_STAMP.get(style, _NO_STAMP["full"])
    dt = ts.astimezone() if local else ts
    return dt.strftime(fmt + (" %z" if offset else ""))


def _clip(text: str, cap: int) -> str:
    """Keep the first `cap` tokens and say how many were cut (0 = no cap). Used for branch
    alternatives only; tool results are capped once, in `transform.apply`."""
    if not cap:
        return text
    kept, cut, _ = tokens.truncate(text, cap)
    return kept.rstrip() + f" …[+{cut:,} tokens]" if cut else text


def _clip_chars(text: str, cap: int) -> str:
    """Cut at `cap` characters, and only when that saves tokens: an argument of 201-215 characters
    once cost MORE clipped than whole (the marker is longer than what it removes), which let a
    custom view count more than *max context* (D6). The cap's own guard in `transform` is the same."""
    if cap and len(text) > cap:
        clipped = text[:cap].rstrip() + f" …[+{len(text) - cap:,} chars]"
        if tokens.count(clipped) < tokens.count(text):
            return clipped
    return text


def _args(a, limit: int = 200) -> str:
    """A call's arguments as one line, cut at `limit` characters (0 = never cut).

    The cut is the `custom` mode's economy (an `Edit` patch or a long command runs to kilobytes),
    and `full` mode passes 0, because "every argument" has to mean every argument."""
    if a is None:
        return ""
    if isinstance(a, str):
        return _clip_chars(a, limit)
    try:
        return _clip_chars(json.dumps(a, ensure_ascii=False), limit)
    except TypeError:
        return _clip_chars(str(a), limit)


def call_label(b: ContentBlock, arg_chars: int = 200) -> str:
    """`name(args…)`, or the bare name when there is nothing to show.

    Every format renders a tool call through this, so "names only" cannot be honoured by one
    formatter and forgotten by the next. In brief mode `Filter` has already dropped `args`, so
    there is no mode flag to check here; an empty argument list simply prints as the name.
    `arg_chars` is `RenderOptions.arg_chars`: 0 in `full` mode, 200 otherwise."""
    a = _args(b.args, arg_chars)
    return f"{b.name or '?'}({a})" if a else f"{b.name or '?'}"


def _result_flags(b: ContentBlock) -> list[str]:
    """Provenance of a tool result: did the agent truncate it, did we recover the full text from
    a sidecar spill file, was it inferred rather than recorded."""
    flags = []
    if b.is_error:
        flags.append("ERROR")
    if b.truncated:
        flags.append("truncated by agent")
    if b.meta.get("restored_from_spill"):
        flags.append("full output restored from the agent's spill file")
    elif b.meta.get("restored_from_output"):
        flags.append("full output restored from the agent's command record")
    elif b.spill_path:
        flags.append(f"full: {b.spill_path}")
    if b.meta.get("inferred"):
        flags.append("inferred")
    return flags


def render_block(b: ContentBlock, o: RenderOptions) -> str | None:
    if b.kind == "text":
        alts = b.meta.get("alternatives")
        if alts:
            sw = b.meta.get("swipe", "")
            extra = "".join(f"\n  ↺ alternative: {_clip(a.strip(), _ALT_CLIP)}" for a in alts)
            return f"{b.text}\n  [branch {sw}]{extra}"
        return b.text
    if b.kind == "thinking":
        if not o.thinking:
            return None
        tag = " (encrypted, summary only)" if b.meta.get("encrypted") else ""
        return f"  ▸ thinking{tag}: {b.text if b.text else '(empty)'}"     # the tool-output mode does not touch it
    if b.kind == "tool_call":
        if not o.tool_calls:
            return None
        # brief: the name alone. Arguments are the expensive half of a call, and `Filter` has
        # already removed them, so this is the same call site for every mode.
        return f"  ● {call_label(b, o.arg_chars)}"
    if b.kind == "tool_result":
        if not o.tool_results:
            return None
        flags = _result_flags(b)
        if b.meta.get("brief") is not None:
            # Names only: the result is not mentioned. `↳ 800 chars` cost 8 tokens per call and, as the
            # user put it, "adds nothing to the session". A failure is still a different fact from
            # "it ran", so that one word stays.
            return "  ↳ error" if b.is_error else None
        f = f" [{', '.join(flags)}]" if flags else ""
        # the Filter has already capped the text (and marked it `…[+n tokens cut]`); the renderer
        # prints what it is given, so one cap exists and one marker
        return f"  ↳ result {b.chars:,} chars{f}: {b.text.strip()}"
    if b.kind in ("system", "info", "error"):
        return f"  ⚑ {b.kind}: {b.text}" if o.system else None
    if b.kind == "image":
        return "  [image]"
    return None


def render_message(m: Message, o: RenderOptions, prev_model: str | None = None,
                   tool_header: bool | None = None) -> str | None:
    """One message. Returns None when the options leave it with nothing to show.

    A TOOL turn gets no header of its own unless asked for: it is the result of the call in the
    turn above it, its timestamp is within a second of that turn, and the header line costs more
    tokens (21) than the line it introduces. Its lines simply continue the previous turn.
    `tool_header` overrides that per message; `render()` sets it when there is no turn above to
    continue, because a transcript needs at least one actor."""
    if m.is_env and not o.env:
        return None
    parts = [p for p in (render_block(b, o) for b in m.blocks) if p]
    if not parts:
        return None
    headed = o.tool_headers if tool_header is None else tool_header
    if not o.markers or (m.role == "tool" and not headed):
        return "\n".join(parts)
    stamp = _fmt(m.timestamp, o.local_time, style=o.stamps)
    head = f"[{label(m).upper()}]" + (f" {stamp}" if stamp else "")
    peer = m.meta.get("peer")
    if isinstance(peer, dict):
        # who sent it: the reader must never take another session's words for the user's
        head += ("  " if stamp else " ") + "from " + (peer.get("name") or (peer.get("from") or "")[:8] or "another session")
    if m.meta.get("wrapped"):
        head += f"  (the whole transcript as one message: {m.meta['wrapped']:,} turns)"
    if m.model and m.role == "assistant" and m.model != prev_model:
        head += ("  " if stamp else " ") + m.model    # only when it changes; the header names the rest
    return head + "\n" + "\n".join(parts)


def label(m: Message) -> str:
    """The actor a turn is printed under: its role, or `peer` for a message another session sent."""
    return "peer" if m.meta.get("peer") else m.role


_LABELS = ("user", "assistant", "peer", "tool", "system")


def _turns(s: Session, o: RenderOptions) -> tuple[list[str], dict]:
    """The rendered turns, and what they hold: the turns by the label printed over them, and the tool
    calls and thinking blocks actually printed. One loop for both, so the counts cannot disagree with
    the text they describe."""
    body: list[str] = []
    labels: dict[str, int] = {}
    calls = thinking = 0
    prev_model = None
    above = None                          # the actor of the last headed turn: a tool turn continues only an assistant or tool turn
    for m in s.messages:
        # a tool turn continues the ASSISTANT turn above it (the one that made the calls). Above a
        # user turn (the assistant hidden) or with nothing above, it carries its own [TOOL] header:
        # continuing the user's turn showed the user running the assistant's tools
        cont = m.role == "tool" and not o.tool_headers and bool(body) and above in ("assistant", "tool")
        r = render_message(m, o, prev_model, tool_header=not cont)
        if r is None:
            continue
        calls += sum(1 for b in m.blocks if b.kind == "tool_call" and render_block(b, o) is not None)
        thinking += sum(1 for b in m.blocks if b.kind == "thinking" and render_block(b, o) is not None)
        if m.role == "assistant" and m.model:
            prev_model = m.model
        if cont:
            body[-1] += "\n" + r        # a tool result continues the turn that called it
        else:
            body.append(r)
            above = m.role
            if not m.meta.get("notice"):
                # the import notice is appended to the session, not part of it: counting it made a
                # view with the notice print a longer header than the ceiling, which never holds it (F33)
                labels[label(m)] = labels.get(label(m), 0) + 1
    return body, {"labels": labels, "tool_calls": calls, "thinking": thinking}


def header(s: Session, o: RenderOptions, counts: dict | None = None) -> list[str]:
    """Header lines. The counts are those of the turns the render prints (`counts`, from `_turns`),
    nothing else: since 2026-09-27 they describe the view, not the whole session. A header that said
    `user 22, tool 172` over a view showing three turns sent a model looking for eighteen missing
    messages, and the user's rule is that a model reading an import need not learn what was left
    out at all. Tool calls and thinking blocks are named only when the view prints some.

    There is deliberately no line summarising what the view left out. It used to print
    `shown: 847 of 981 messages (thinking off, …)`, and it grew the more you filtered, so on a
    small session the header could cost more than the filtering saved, and a filtered export could
    count MORE tokens than the unfiltered ceiling it is measured against. Counting the view keeps that
    property: a view prints no more turns than the ceiling (D6). The GUI still shows "847 of 981
    messages" beside the budget, for the person choosing the options."""
    c = counts if counts is not None else _turns(s, o)[1]
    got = c["labels"]
    parts = [f"{k} {got[k]}" for k in _LABELS if got.get(k)] + [f"{k} {v}" for k, v in got.items() if k not in _LABELS and v]
    line = f"messages: {sum(got.values())}" + (f" ({', '.join(parts)})" if parts else "")
    if c["tool_calls"]:
        line += f"    tool calls: {c['tool_calls']}"
    if c["thinking"]:
        line += f"    thinking blocks: {c['thinking']}"
    return [
        f"=== {s.agent} · {s.id} ===",
        f"title:    {s.title or '-'}",
        f"project:  {s.project or '-'}    cwd: {s.cwd or '-'}",
        f"model:    {s.model or '-'}    cli: {s.cli_version or '-'}",
        f"started:  {_fmt(s.started, o.local_time, offset=o.show_offset)}    ended: {_fmt(s.ended, o.local_time, offset=o.show_offset)}",
        line,
    ]


def footer(n: int) -> str:
    return f"\n--- {n:,} tokens ({'tiktoken' if tokens.is_exact() else 'estimated, chars/4'}) ---\n"


def wrap_session(s: Session, o: RenderOptions, source: Session | None = None,
                 with_header: bool = False) -> Session:
    """The whole transcript as ONE message of `o.wrap`'s role.

    For a structured format this is the difference between handing a model 800 message records and
    handing it one message that happens to contain a conversation; for text it is one `[USER]`
    turn holding the conversation. Nothing is dropped: the text is the same render, the inner
    turns keep their role lines as content. `with_header` puts the session header inside the
    message too (structured formats, whose envelope does not print it); text formats print the
    header once, outside."""
    body = render(s, dataclasses.replace(o, wrap=""), source=source, with_footer=False,
                  with_header=with_header)
    m = Message(0, o.wrap, s.ended or s.started, [ContentBlock("text", body.strip("\n"))],
                meta={"wrapped": len(s.messages)})
    return dataclasses.replace(s, messages=[m])


def render(s: Session, o: RenderOptions | None = None, source: Session | None = None,
           with_footer: bool = True, with_header: bool = True) -> str:
    """The transcript; the footer states the token count of everything above it.

    `source` (the unfiltered session) is accepted and passed through for the callers that still send
    it; the header no longer reads it, because it counts the view (see `header`)."""
    o = o or RenderOptions()
    counts = None
    if o.wrap:
        # one message holding the conversation; the header is printed once, outside it, and counts
        # the turns inside it. The single outer turn carries no timestamp (the inner turns lost
        # theirs for the same reason)
        counts = _turns(s, dataclasses.replace(o, wrap=""))[1]
        s = wrap_session(s, o, source, with_header=False)
        o = dataclasses.replace(o, wrap="", stamps="none")
    body, own = _turns(s, o)
    lines = (header(s, o, counts or own) + [""]) if with_header else []
    text = "\n".join(lines) + "\n\n".join(body) + "\n"
    return text + footer(tokens.count(text)) if with_footer else text
