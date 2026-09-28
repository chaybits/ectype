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

The one part an agent wrote rather than ectype extracted is the closing summary: when an
assistant turn carries text between a pair of markers (`%%SUMMARY%%` unless Settings → Summary
says otherwise), that text is quoted as a section of its own, because it is the agent's own
account of what mattered and what comes next, which no extraction can produce. Every such block is
quoted, in order. It is still taken from the transcript, never generated. Only the assistant's turns
count: a user turn that pastes an earlier session's summary carries the same markers and is not this
session's. `ectype summary-rule` writes the paragraph that asks an agent for one.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Sequence

from .model import Session

# Bumped when the summary's shape changes, so `summarize --all` redoes the stored ones once
# (the ledger records the format each summary was written in). 2: the closing-summary section.
# 3 (audit 2026-09-25): `when:` in local time with its offset, the keyword block uncapped and
# wrapped, the closing summary found by marker lines first and ordered by position.
# 4 (2026-09-27): every closing summary quoted, not only the last; the user's answers to the agent's
# questions under `answered:`; asks never taken from another session's message or injected context.
FORMAT = 4

# Tool arguments are vendor-shaped, so these are the keys that in practice name a file, a command
# or a search across the agents ectype reads. Unknown shapes simply contribute nothing.
_FILE_KEYS = ("file_path", "filePath", "path", "file", "notebook_path", "target_file", "absolute_path")
_CMD_KEYS = ("command", "cmd", "script", "shell_command")
_QUERY_KEYS = ("pattern", "query", "regex", "search", "url")
# tools whose `path` is a folder searched, not a file touched (Grep's `path` was listed as a file)
_SEARCH_TOOLS = {"grep", "glob", "ls", "search", "list_directory", "search_file_content", "find", "codebase_search"}
# Codex's apply_patch carries the patch as text; the files it touches are named on these lines
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
# the line that says what failed is usually the last one: a traceback opens with a banner
_BANNER = re.compile(r"^(Traceback \(most recent call last\)|=+.*=+|-+|Exit code \d+)\s*:?\s*$")
# A word starts with a letter of any script and runs over letters, digits and the characters that
# glue file names and flags together. The ASCII-only class it replaced dropped every Turkish word
# (ç ş ğ ı ö ü) from the keyword line, so nothing asked in Turkish was ever greppable.
_WORD = re.compile(r"[^\W\d_][\w.+-]{2,}")
_STOP = {"the", "and", "for", "you", "that", "this", "with", "not", "but", "are", "can", "has",
         "have", "was", "were", "from", "into", "out", "there", "what", "when", "which",
         "let", "get", "set", "run", "use", "using", "one", "two", "now", "any", "all", "its",
         "his", "her", "they", "them", "then", "than", "some", "here", "would", "could", "should",
         # Turkish function words, so a Turkish ask does not fill the line with "için" and "ile"
         "bir", "için", "ile", "ama", "gibi", "daha", "çok", "var", "yok", "olan", "bunu", "şey"}


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


def _error_line(text: str) -> str:
    """The line of a failed output that says what failed: the first, unless it is a banner (a
    traceback's opening line, a pytest rule), then the last non-empty one."""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return "(no message)"
    return lines[-1] if len(lines) > 1 and _BANNER.match(lines[0].strip()) else lines[0]


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def markers(override: Sequence[str] | None = None) -> list[str]:
    """The closing-summary markers to look for: the caller's, else Settings → Summary (one default
    for every surface, D14). An empty list switches the section off."""
    if override is not None:
        return [m.strip() for m in override if m and m.strip()]
    from . import settings
    return list(settings.load()["summary"]["markers"])


_FENCE = re.compile(r"^[ \t]*(```|~~~)", re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges inside ``` / ~~~ code fences (an unclosed fence runs to the end)."""
    spans, start = [], None
    for m in _FENCE.finditer(text):
        if start is None:
            start = m.start()
        else:
            end = text.find("\n", m.end())
            spans.append((start, len(text) if end < 0 else end))
            start = None
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _pairs(text: str, occ: list[tuple[int, int]]) -> list[tuple[int, str]]:
    """(position, enclosed text) for occurrences paired in order: 1st with 2nd, 3rd with 4th, …"""
    out = []
    for i in range(0, len(occ) - 1, 2):
        inner = text[occ[i][1]:occ[i + 1][0]].strip()
        if inner:
            out.append((occ[i][0], inner))
    return out


def closing_summaries(s: Session, marks: Sequence[str] | None = None) -> list[tuple[str, str]]:
    """Every block the assistant wrote between a pair of the same marker, in session order, as
    (marker, text).

    A marker is a literal string, not a pattern. The rule the markers come from says "between two
    marker lines", so the search is tiered, and the first tier that finds anything in the session
    wins:

    1. markers standing alone on their line, outside code fences;
    2. markers anywhere in a line, outside code fences and outside `inline code`;
    3. markers standing alone on their line inside a code fence.

    Pairing every occurrence strictly in order (the first version) let one quoted mention, an agent
    explaining the rule ("I close with the -*-summary-*- block"), shift every pair: a fragment was
    quoted as the agent's summary and the real one dropped. Results are ordered by position in the
    session, so "the last" is the last written whatever the order of the markers in Settings.
    Notices are skipped as everywhere else, and user turns are never read (module docstring).
    """
    marks = markers(marks)
    if not marks:
        return []
    tiers: list[list[tuple[tuple[int, int, int], str, str]]] = [[], [], []]
    for mi, m in enumerate(s.messages):
        if m.role != "assistant" or m.meta.get("notice"):
            continue
        for bi, b in enumerate(m.blocks):
            if b.kind != "text" or not b.text:
                continue
            text = b.text
            fences = _fenced_spans(text)
            codes = [(x.start(), x.end()) for x in _INLINE_CODE.finditer(text)]
            for mk in marks:
                alone_out, alone_in, inline = [], [], []
                pos = 0
                for line in text.splitlines(keepends=True):
                    if line.strip() == mk:
                        (alone_in if _inside(pos, fences) else alone_out).append((pos, pos + len(line)))
                    pos += len(line)
                i = text.find(mk)
                while i >= 0:
                    if not _inside(i, fences) and not _inside(i, codes):
                        inline.append((i, i + len(mk)))
                    i = text.find(mk, i + len(mk))
                for tier, occ in ((0, alone_out), (1, inline), (2, alone_in)):
                    for at, inner in _pairs(text, occ):
                        tiers[tier].append(((mi, bi, at), mk, inner))
    for found in tiers:
        if found:
            return [(mk, inner) for _, mk, inner in sorted(found, key=lambda x: x[0])]
    return []


def summarize(s: Session, max_asks: int = 8, max_items: int = 12,
              marks: Sequence[str] | None = None) -> str:
    """One block of text: what happened in this session, extracted, never generated.

    `marks` overrides the closing-summary markers for this call (the command line's `--marker`);
    None means Settings → Summary.
    """
    from .transform import answer_pairs, answer_text, question_text
    closing = closing_summaries(s, marks)
    asks: list[str] = []
    answered: list[str] = []
    asked_by_call: dict = {}                    # call id -> the question it put, for an answer that does not name it
    tools: Counter = Counter()
    files: list[str] = []
    cmds: list[str] = []
    queries: list[str] = []
    errors: list[str] = []

    for m in s.messages:
        if m.meta.get("notice"):
            continue
        for b in m.blocks:
            if b.kind == "tool_call" and b.meta.get("question"):
                # the agent's question to the user: its answer is recorded below, the call is no tool use
                asked_by_call[b.call_id] = question_text(b.args).split("\n", 1)[0]
                continue
            if b.kind == "tool_result" and b.meta.get("answer"):
                pairs = answer_pairs(b) or [(asked_by_call.get(b.call_id, "?"), answer_text(b))]
                for q, a in pairs:
                    line = f"{_clip(q, 80)} → {_clip(a, 80)}"
                    if line not in answered:
                        answered.append(line)
                continue
            if b.kind == "text" and m.role == "user" and not m.is_env and not m.meta.get("peer"):
                # the user's own words only: injected context and another session's message are not asks
                t = b.text.strip()
                # skip the injected wrappers agents put in user turns
                # `[Image: …` is an attachment the agent wrote into the turn, not something asked
                if t and not t.startswith(("<", "Caveat:", "[Request interrupted", "[Image:")):
                    asks.append(_clip(t.splitlines()[0], 100))
            elif b.kind == "tool_call":
                tools[b.name or "?"] += 1
                a = _args(b)
                search_tool = (b.name or "").lower() in _SEARCH_TOOLS
                file_keys = tuple(k for k in _FILE_KEYS if k != "path") if search_tool else _FILE_KEYS
                for bucket, keys in ((files, file_keys), (cmds, _CMD_KEYS), (queries, _QUERY_KEYS)):
                    v = _first(a, keys)
                    if v and v not in bucket:
                        bucket.append(_clip(v, 120))
                cmd = a.get("command") or a.get("cmd")
                if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
                    # Codex's `shell` passes argv: ["bash", "-lc", "pytest -q"] ran `pytest -q`
                    line = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-c", "-lc") else " ".join(cmd)
                    if line and _clip(line, 120) not in cmds:
                        cmds.append(_clip(line, 120))
                raw = b.args if isinstance(b.args, str) else (a.get("input") if isinstance(a.get("input"), str) else "")
                for fpath in _PATCH_FILE.findall(raw or ""):
                    if _clip(fpath, 120) not in files:
                        files.append(_clip(fpath, 120))
            elif b.kind == "tool_result" and b.is_error:
                line = _clip(_error_line(b.text or ""), 100)
                if line not in errors:
                    errors.append(line)
            elif b.kind == "error":
                # the agent's own error notices (an API error, a refusal): as much an error as a
                # failed tool call, and the one place a "why did this session stall" answer lives
                line = _clip((b.text or "").strip().splitlines()[0] if (b.text or "").strip() else "(no message)", 100)
                tag = b.meta.get("subtype")
                line = f"[{tag}] {line}" if tag else line
                if line not in errors:
                    errors.append(line)

    out = [f"{s.agent} · {s.id}"]
    if s.title:
        out.append(f"title: {s.title}")
    # local time, with the offset once, like the render: in UTC a session begun between midnight and
    # 03:00 here was dated the day before, and `search <that date>` missed it
    st = s.started.astimezone() if s.started else None
    en = s.ended.astimezone() if s.ended else None
    when = f"{st:%Y-%m-%d %H:%M}" if st else "?"
    if en and st and en.date() != st.date():
        when += f" to {en:%Y-%m-%d %H:%M}"
    if st:
        when += f" {st:%z}"
    out.append(f"when: {when}   messages: {len(s.messages)}   tool calls: {sum(tools.values())}"
               + (f"   project: {s.project}" if s.project else ""))
    for i, (mk, text) in enumerate(closing, 1):
        # the agent's own words come first: they are the part a reader wants, and the part `search`
        # is most likely to hit. Every block, in the order written (the user, 2026-09-27: "if there
        # are multiple summaries, consider them both"); a session resumed later adds its own
        which = f" {i} of {len(closing)}" if len(closing) > 1 else ""
        out.append("")
        out.append(f"closing summary{which} (the agent's own, between {mk} markers):")
        out += [f"  {ln}" if ln.strip() else "" for ln in text.splitlines()]
    if asks:
        out.append("")
        out.append("asked:")
        out += [f"  - {a}" for a in asks[:max_asks]]
        if len(asks) > max_asks:
            out.append(f"  … and {len(asks) - max_asks} more")
    if answered:
        # the user's decisions, from the agent's question tool: "what did I choose about X" is a search
        out.append("")
        out.append("answered:")
        out += [f"  - {a}" for a in answered[:max_items]]
        if len(answered) > max_items:
            out.append(f"  … and {len(answered) - max_items} more")
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
    out += _keywords(asks + answered, tools, files, cmds, queries)
    return "\n".join(out) + "\n"


def _keywords(asks, tools, files, cmds, queries, width: int = 100) -> list[str]:
    """Lowercase, deduplicated, ordered by where it came from: these lines exist for `grep`.

    Every word, wrapped at `width` characters. It was one line cut at 60 words, so a word from a
    later ask was nowhere in the summary and `search` could not find the session."""
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
    lines, cur = [], "keywords:"
    for w in seen:
        if len(cur) + 1 + len(w) > width and cur != "keywords:":     # a line never starts empty
            lines.append(cur)
            cur = "  " + w
        else:
            cur += " " + w
    lines.append(cur)
    return lines
