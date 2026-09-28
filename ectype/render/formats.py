"""Export formats: one registry, used by both the CLI (`export --format`) and the web UI.

    text      the plain render (extraction.txt successor)
    markdown  a heading per turn, fenced tool output; pastes into issues, notes, blog posts
    html      one standalone page: role colours, collapsible tool output and thinking
    json      the canonical model, pretty-printed
    jsonl     one JSON object per message, a session header first; greps and streams
    csv       one row per message (index, role, timestamp, model, kinds, chars, text); spreadsheets

Every formatter takes the (already filtered, possibly redacted) session plus RenderOptions;
`source` is the unfiltered session so headers can say "showing 685 of 924 messages".
"""
from __future__ import annotations

import csv
import dataclasses
import html as _html
import io
import json
import re
from .. import tokens
from ..model import ContentBlock, Message, Session, json_default as _default
from .text import RenderOptions, _args, _fmt, _result_flags, call_label, render, wrap_session

_ROLE = {"user": "User", "assistant": "Assistant", "tool": "Tool", "system": "System"}


def _label(m: Message) -> str:
    peer = m.meta.get("peer")
    if isinstance(peer, dict):     # another session's message, named, never filed under the user
        return "Message from " + (peer.get("name") or (peer.get("from") or "")[:8] or "another session")
    return "Injected context" if m.is_env else _ROLE.get(m.role, m.role)


def _flags(b: ContentBlock) -> list[str]:
    f = _result_flags(b)
    if b.meta.get("capped"):
        f.append("capped")
    return f


def _fence(text: str) -> str:
    n = 3
    while "`" * n in text:
        n += 1
    return "`" * n


def _stamp(fn):
    return f"{tokens.count(fn):,} tokens ({'tiktoken' if tokens.is_exact() else 'estimated, chars/4'})"


# ------------------------------------------------------------------------------------ text/json
def to_text(s: Session, o: RenderOptions, source: Session | None = None) -> str:
    return render(s, o, source=source)


def to_json(s: Session, o: RenderOptions, source: Session | None = None) -> str:
    return json.dumps(dataclasses.asdict(s), ensure_ascii=False, indent=1, default=_default) + "\n"


def to_jsonl(s: Session, o: RenderOptions, source: Session | None = None) -> str:
    src = source or s
    head = {"type": "session", "agent": s.agent, "id": s.id, "title": s.title, "project": s.project,
            "cwd": s.cwd, "model": s.model, "cli_version": s.cli_version,
            "started": _default(s.started) if s.started else None, "ended": _default(s.ended) if s.ended else None,
            "messages": len(s.messages), "messages_in_source": len(src.messages)}
    lines = [json.dumps(head, ensure_ascii=False, default=_default)]
    for m in s.messages:
        d = dataclasses.asdict(m)
        d["type"] = "message"
        lines.append(json.dumps(d, ensure_ascii=False, default=_default))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------ csv
def _msg_text(m: Message, arg_chars: int) -> str:
    parts = []
    for b in m.blocks:
        if b.kind == "tool_call":
            parts.append(call_label(b, arg_chars))
        elif b.kind == "image":
            parts.append("[image]")
        elif b.text:
            parts.append(b.text)
    return "\n\n".join(parts)


_UNSAFE_NAME = re.compile(r"[^\w.\- ()]+")        # file names: word chars (any script), dot, dash, space, parens


def export_name(raw: str | None, agent: str, sid: str, fmt: str) -> str:
    """A file name for an export: the typed name, else `<agent>_<id8>`, BOTH sanitised (a SillyTavern
    id carries a `/`, an Aider id is a folder name), bounded so it stays a legal name. One helper for
    the CLI's `-o <folder>` and the web UI (audit 2026-09-25, F53 and F83)."""
    base = _UNSAFE_NAME.sub("_", ((raw or "").strip() or f"{agent}_{sid[:8]}")).strip(" .")[:150] or "export"
    ext = extension(fmt)
    return base if base.lower().endswith("." + ext) else f"{base}.{ext}"


def _cell(text: str) -> str:
    """A text cell a spreadsheet will not evaluate: `=`, `+`, `-`, `@` (and a leading tab or CR) make
    it a formula, so a transcript line `=HYPERLINK(…)` became a live link and a markdown list opened as
    #NAME?. The apostrophe is the conventional escape (OWASP CSV injection); only the text column."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(s: Session, o: RenderOptions, source: Session | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["index", "role", "timestamp", "model", "kinds", "chars", "text"])
    for m in s.messages:
        kinds = "+".join(dict.fromkeys(b.kind for b in m.blocks))
        w.writerow([m.index, "peer" if m.meta.get("peer") else "env" if m.is_env else m.role, m.timestamp.isoformat() if m.timestamp else "",
                    m.model or "", kinds, sum(b.chars for b in m.blocks), _cell(_msg_text(m, o.arg_chars))])
    return "\ufeff" + buf.getvalue()        # BOM so spreadsheets open UTF-8 (non-ASCII text) correctly


# ------------------------------------------------------------------------------------ markdown
def _md_block(b: ContentBlock, o: RenderOptions) -> str | None:
    if b.kind == "text":
        alts = b.meta.get("alternatives")
        if alts:
            return b.text + f"\n\n*[branch {b.meta.get('swipe', '')}]*" + "".join(f"\n\n> alternative: {a.strip()}" for a in alts)
        return b.text
    if b.kind == "thinking":
        if not o.thinking:
            return None
        tag = " (encrypted, summary only)" if b.meta.get("encrypted") else ""
        body = (b.text or "(empty)").replace("\n", "\n> ")
        return f"> **thinking{tag}:** {body}"
    if b.kind == "tool_call":
        if not o.tool_calls:
            return None
        a = _args(b.args, o.arg_chars).replace("`", "ˋ")
        return f"**▶ {b.name or '?'}** `{a}`" if a else f"**▶ {b.name or '?'}**"
    if b.kind == "tool_result":
        if not o.tool_results:
            return None
        f = _flags(b)
        if b.meta.get("brief") is not None:
            # names only, as in the text render: no result line and no size, a failure flagged
            return "*error*" if b.is_error else None
        fence = _fence(b.text)
        head = f"result · {b.chars:,} chars" + (f" · {', '.join(f)}" if f else "")
        return f"*{head}*\n\n{fence}text\n{b.text.rstrip()}\n{fence}"
    if b.kind in ("system", "info", "error"):
        return f"> ⚑ **{b.kind}:** {b.text}" if o.system else None
    if b.kind == "image":
        return "*[image]*"
    return None


def to_markdown(s: Session, o: RenderOptions, source: Session | None = None) -> str:
    src = source or s
    out = [f"# {s.title or s.id}", "",
           f"- **agent:** {s.agent} · **id:** `{s.id}`",
           f"- **project:** {s.project or '-'} · **cwd:** `{s.cwd or '-'}`",
           f"- **model:** {s.model or '-'} · **cli:** {s.cli_version or '-'}",
           f"- **started:** {_fmt(s.started, o.local_time)} · **ended:** {_fmt(s.ended, o.local_time)}",
           f"- **messages:** {len(src.messages)}"]
    out.append("")
    for m in s.messages:
        if m.is_env and not o.env:
            continue
        body = [p for p in (_md_block(b, o) for b in m.blocks) if p]
        if not body:
            continue
        head = f"### {_label(m)}" + (f" · {_fmt(m.timestamp, o.local_time, style=o.stamps)}" if o.stamps != "none" else "")
        if m.model and m.role == "assistant":
            head += f" · {m.model}"
        if m.meta.get("wrapped"):        # one message holding the transcript: fence it so the lines survive
            head += f" · the whole transcript as one message ({m.meta['wrapped']:,} turns)"
            f = _fence(body[0])
            body = [f"{f}text\n{body[0]}\n{f}"]
        out += [head, "", "\n\n".join(body), ""]
    text = "\n".join(out)
    return text + f"\n---\n*{_stamp(text)}*\n"


# ------------------------------------------------------------------------------------ html
_CSS = """
:root{--bg:#fbfbfc;--fg:#1d2024;--dim:#6b7280;--line:#e3e6ea;--user:#1d6f42;--asst:#1f5fbf;--tool:#6b5d3a;--env:#a04a00;--err:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e6e6;--dim:#9aa0a8;--line:#2b2f37;--user:#7fd39a;--asst:#8db8ff;--tool:#d8c28a;--env:#f0a35e;--err:#ff8a80}}
body{margin:0 auto;max-width:960px;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;color:var(--dim);font-size:13px}header dt{font-weight:600}header dd{margin:0}
section{border-top:1px solid var(--line);padding:12px 0}h2{font-size:13px;margin:0 0 6px;font-weight:600}h2 time,h2 .model{color:var(--dim);font-weight:400;margin-left:8px}
.user h2 .role{color:var(--user)}.assistant h2 .role{color:var(--asst)}.tool h2 .role{color:var(--tool)}.env h2 .role{color:var(--env)}
pre{white-space:pre-wrap;word-break:break-word;margin:6px 0;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace}
.txt{white-space:pre-wrap;word-break:break-word;margin:4px 0}
details{margin:6px 0;border-left:3px solid var(--line);padding-left:10px}summary{cursor:pointer;color:var(--dim);font-size:13px}
.call{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;color:var(--tool)}.err summary{color:var(--err)}
.note{color:var(--dim);font-size:13px}footer{color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:10px;margin-top:20px}
"""


def _html_block(b: ContentBlock, o: RenderOptions) -> str | None:
    e = _html.escape
    if b.kind == "text":
        extra = ""
        alts = b.meta.get("alternatives")
        if alts:
            extra = f"<div class=note>[branch {e(str(b.meta.get('swipe', '')))}]</div>" + "".join(
                f"<details><summary>alternative</summary><div class=txt>{e(a.strip())}</div></details>" for a in alts)
        return f"<div class=txt>{e(b.text)}</div>{extra}"
    if b.kind == "thinking":
        if not o.thinking:
            return None
        tag = " (encrypted, summary only)" if b.meta.get("encrypted") else ""
        return f"<details class=think><summary>thinking{e(tag)} · {b.chars:,} chars</summary><pre>{e(b.text or '(empty)')}</pre></details>"
    if b.kind == "tool_call":
        return f"<div class=call>▶ {e(call_label(b, o.arg_chars))}</div>" if o.tool_calls else None
    if b.kind == "tool_result":
        if not o.tool_results:
            return None
        f = _flags(b)
        cls = " err" if b.is_error else ""
        if b.meta.get("brief") is not None:
            return "<div class='note err'>error</div>" if b.is_error else None
        head = f"result · {b.chars:,} chars" + (f" · {e(', '.join(f))}" if f else "")
        return f"<details class='res{cls}'><summary>{head}</summary><pre>{e(b.text.rstrip())}</pre></details>"
    if b.kind in ("system", "info", "error"):
        return f"<div class=note>⚑ {e(b.kind)}: {e(b.text)}</div>" if o.system else None
    if b.kind == "image":
        return "<div class=note>[image]</div>"
    return None


def to_html(s: Session, o: RenderOptions, source: Session | None = None) -> str:
    e = _html.escape
    src = source or s
    title = e(s.title or s.id)
    rows = [("agent", s.agent), ("id", s.id), ("project", s.project or "-"), ("cwd", s.cwd or "-"),
            ("model", s.model or "-"), ("cli", s.cli_version or "-"),
            ("started", _fmt(s.started, o.local_time)), ("ended", _fmt(s.ended, o.local_time)),
            ("messages", f"{len(src.messages)}")]
    dl = "".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in rows)
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{title}</title><style>{_CSS}</style></head><body><header><h1>{title}</h1><dl>{dl}</dl></header>"]
    for m in s.messages:
        if m.is_env and not o.env:
            continue
        body = [p for p in (_html_block(b, o) for b in m.blocks) if p]
        if not body:
            continue
        cls = "peer" if m.meta.get("peer") else "env" if m.is_env else m.role
        model = f"<span class=model>{e(m.model)}</span>" if m.model and m.role == "assistant" else ""
        if m.meta.get("wrapped"):
            model = f"<span class=model>the whole transcript as one message ({m.meta['wrapped']:,} turns)</span>"
        out.append(f"<section class='msg {cls}'><h2><span class=role>{e(_label(m))}</span>"
                   + (f"<time>{e(_fmt(m.timestamp, o.local_time, style=o.stamps))}</time>" if o.stamps != "none" else "")
                   + f"{model}</h2>{''.join(body)}</section>")
    page = "".join(out)
    return page + f"<footer>{e(_stamp(page))} · exported by AI Agent Ectype</footer></body></html>\n"


# ------------------------------------------------------------------------------------ registry
FORMATS: dict[str, tuple[str, str, object]] = {
    "text": ("txt", "text/plain; charset=utf-8", to_text),
    "markdown": ("md", "text/markdown; charset=utf-8", to_markdown),
    "html": ("html", "text/html; charset=utf-8", to_html),
    "json": ("json", "application/json; charset=utf-8", to_json),
    "jsonl": ("jsonl", "application/x-ndjson; charset=utf-8", to_jsonl),
    "csv": ("csv", "text/csv; charset=utf-8", to_csv),
}


def export(s: Session, fmt: str, o: RenderOptions, source: Session | None = None) -> str:
    if fmt not in FORMATS:
        raise KeyError(f"unknown format {fmt!r}; known: {', '.join(FORMATS)}")
    if o.wrap:
        # every format: the structured ones keep the session header inside the one message (their
        # envelope does not print it); text, markdown and html print it once, outside
        s = wrap_session(s, o, source, with_header=fmt in ("json", "jsonl", "csv"))
        o = dataclasses.replace(o, wrap="", stamps="none")
    return FORMATS[fmt][2](s, o, source)


def extension(fmt: str) -> str:
    return FORMATS[fmt][0]


def mime(fmt: str) -> str:
    return FORMATS[fmt][1]
