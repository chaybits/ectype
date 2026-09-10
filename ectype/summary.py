"""A summary of a session that costs nothing to produce.

No model, no API key, no network: everything below is extracted from the transcript itself. That
is the whole design decision. A summariser that needs a key will be run once and never again,
and the useful part of a summary is mostly mechanical anyway: what was asked, which tools ran,
which files were touched, what failed. A model-written paragraph is an enrichment somebody else
can add on top, not the mechanism.

The last line is a lowercase keyword block. It is there to be matched by a regex across thousands
of summaries, not to be read, and it is what makes "have I dealt with this before?" answerable
without opening a single transcript.

Whatever view is passed in is what gets summarised, so the same filters that shape an export
shape this: a summary of messages 40 to 80 is a summary of messages 40 to 80.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .model import Session

# Tool arguments are vendor-shaped, so these are the keys that in practice name a file, a command
# or a search across the agents ectype reads. Unknown shapes simply contribute nothing.
_FILE_KEYS = ("file_path", "filePath", "path", "file", "notebook_path", "target_file", "absolute_path")
_CMD_KEYS = ("command", "cmd", "script", "shell_command")
_QUERY_KEYS = ("pattern", "query", "regex", "search", "url")
_WORD = re.compile(r"[a-z][a-z0-9_.+-]{2,}")
_STOP = {"the", "and", "for", "you", "that", "this", "with", "not", "but", "are", "can", "has",
         "have", "was", "were", "from", "into", "out", "there", "what", "when", "which",
         "let", "get", "set", "run", "use", "using", "one", "two", "now", "any", "all", "its",
         "his", "her", "they", "them", "then", "than", "some", "here", "would", "could", "should"}


def _args(block) -> dict:
    a = block.args
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return {}
    return a if isinstance(a, dict) else {}


def _first(d: dict, keys) -> Any:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize(s: Session, max_asks: int = 8, max_items: int = 12) -> str:
    """One block of text: what happened in this session, extracted, never generated."""
    asks: list[str] = []
    tools: Counter = Counter()
    files: list[str] = []
    cmds: list[str] = []
    queries: list[str] = []
    errors: list[str] = []

    for m in s.messages:
        if m.meta.get("notice"):
            continue
        for b in m.blocks:
            if b.kind == "text" and m.role == "user":
                t = b.text.strip()
                # skip the injected wrappers agents put in user turns
                # `[Image: …` is an attachment the agent wrote into the turn, not something asked
                if t and not t.startswith(("<", "Caveat:", "[Request interrupted", "[Image:")):
                    asks.append(_clip(t.splitlines()[0], 100))
            elif b.kind == "tool_call":
                tools[b.name or "?"] += 1
                a = _args(b)
                for bucket, keys in ((files, _FILE_KEYS), (cmds, _CMD_KEYS), (queries, _QUERY_KEYS)):
                    v = _first(a, keys)
                    if v and v not in bucket:
                        bucket.append(_clip(v, 120))
            elif b.kind == "tool_result" and b.is_error:
                line = _clip((b.text or "").strip().splitlines()[0] if (b.text or "").strip() else "(no message)", 100)
                if line not in errors:
                    errors.append(line)

    out = [f"{s.agent} · {s.id}"]
    if s.title:
        out.append(f"title: {s.title}")
    when = f"{s.started:%Y-%m-%d %H:%M}" if s.started else "?"
    if s.ended and s.started and s.ended.date() != s.started.date():
        when += f" to {s.ended:%Y-%m-%d %H:%M}"
    out.append(f"when: {when}   messages: {len(s.messages)}   tool calls: {sum(tools.values())}"
               + (f"   project: {s.project}" if s.project else ""))
    if asks:
        out.append("")
        out.append("asked:")
        out += [f"  - {a}" for a in asks[:max_asks]]
        if len(asks) > max_asks:
            out.append(f"  … and {len(asks) - max_asks} more")
    if tools:
        out.append("")
        out.append("tools: " + ", ".join(f"{n}×{c}" for n, c in tools.most_common()))
    for label, bucket in (("files", files), ("commands", cmds), ("searched", queries), ("errors", errors)):
        if bucket:
            out.append("")
            out.append(f"{label}:")
            out += [f"  - {x}" for x in bucket[:max_items]]
            if len(bucket) > max_items:
                out.append(f"  … and {len(bucket) - max_items} more")
    out.append("")
    out.append("keywords: " + _keywords(asks, tools, files, cmds, queries))
    return "\n".join(out) + "\n"


def _keywords(asks, tools, files, cmds, queries) -> str:
    """Lowercase, deduplicated, ordered by where it came from: this line exists for `grep`."""
    seen: list[str] = []
    def add(text: str) -> None:
        for w in _WORD.findall(text.lower()):
            if w not in _STOP and w not in seen:
                seen.append(w)
    for a in asks:
        add(a)
    for n in tools:
        add(n)
    for f in files:
        add(f.rsplit("/", 1)[-1])
    for c in cmds:
        add(c.split()[0] if c.split() else "")
    for q in queries:
        add(q)
    return " ".join(seen[:60])
