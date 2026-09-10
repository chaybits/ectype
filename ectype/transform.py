"""Filters and redaction: canonical Session in, canonical Session out.

Filter: keep only some block kinds, hide roles, drop injected context, cap tool results at N
TOKENS, mention-only tool output, merge back-to-back turns, slice a message range.

Redaction has two modes:
  * model-level  (Redactor.session)   for renders/exports of the canonical model
  * raw-text     (Redactor.text/file) for FIXTURES: the agent's own file, byte-for-byte except
    the replaced strings, so the format under test is untouched.
Default rules are computed from the running machine (home dir, username, hostname) plus generic
patterns (e-mails, API keys, optionally IP addresses). Every rule can be switched off (Settings →
Redaction) and every match is recorded, so the UI can show exactly what was replaced.
Nothing personal is hard-coded.
"""
from __future__ import annotations

import copy
import getpass
import os
import re
import socket
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import tokens
from .model import Message, Session


# ----------------------------------------------------------------------------- filters
@dataclass
class Filter:
    drop_roles: set[str] | None = None       # hide these roles' text and thinking; the tool calls a hidden turn made stay, filed under the tool turn
    kinds: set[str] | None = None            # keep only these block kinds
    env: bool = False                        # keep injected environment/system messages
    cap: int = 0                             # keep the first N TOKENS of each tool_result (0 = off); thinking is never capped
    brief: bool = False                      # tool calls by name only, results not included; thinking is not touched
    collapse: bool = False                   # merge back-to-back turns of the same actor into one; a tool result rides with the assistant turn that called it
    start: int | None = None                 # message index slice [start, end)
    end: int | None = None


def apply(s: Session, f: Filter) -> Session:
    out = copy.copy(s)
    msgs: list[Message] = []
    for m in s.messages[f.start:f.end]:
        if m.is_env and not f.env:
            continue
        hidden = bool(f.drop_roles and m.role in f.drop_roles)
        blocks = []
        for b in m.blocks:
            if f.kinds and b.kind not in f.kinds:
                continue
            if hidden and b.kind != "tool_call":
                continue        # a hidden turn keeps only the calls it made: "what tools have been called"
            if f.brief and b.kind == "tool_call":
                # BRIEF: the NAME only. Arguments are the expensive half of a call (a single
                # Bash command or file patch runs to hundreds of characters), and the model does
                # not need them to know the call happened.
                # The arguments are REMOVED here, not merely left unprinted by one renderer: this
                # filter is the single place that decides what a view contains, so every format
                # (and every conversion, which folds `args` into bracketed notes) sees the same
                # session. Marking the block and leaving `args` in place shipped the whole command
                # line in markdown, html, csv, json and jsonl while the header said "by name only".
                b = copy.copy(b)
                b.args = None
                b.meta = {**b.meta, "brief": True}
            elif f.brief and b.kind == "tool_result":
                # BRIEF: the result is not printed at all, the user's call: its size "adds nothing to
                # the session". Only a failure is still flagged. Empty results are marked too, so they
                # do not fall through to the normal `↳ result 0 chars:` line.
                b = copy.copy(b)
                b.meta = {**b.meta, "brief": len(b.text)}
                b.text = ""
            elif f.cap and b.kind == "tool_result" and b.text:
                kept, cut, total = tokens.truncate(b.text, f.cap)
                if cut:
                    capped = kept.rstrip() + f" …[+{cut:,} tokens cut]"
                    # A block barely over the cap can cost MORE once the "…[+n tokens cut]" note is
                    # added. Capping is an economy measure, so it only applies when it actually saves.
                    if tokens.count(capped) < total:
                        b = copy.copy(b)
                        b.text = capped
                        b.meta = {**b.meta, "capped": cut}
            blocks.append(b)
        if not blocks:
            continue
        nm = copy.copy(m)
        nm.blocks = blocks
        nm.index = len(msgs)
        if hidden:
            # the assistant's words are hidden, its activity is not: file the calls under the tool turn
            nm.role = "tool"
            nm.meta = {**m.meta, "calls_only": True}
        msgs.append(nm)
    if f.collapse:
        msgs = collapse(msgs)
    out.messages = msgs
    return out


def collapse(msgs: list[Message]) -> list[Message]:
    """Merge back-to-back messages of the same actor into one message.

    A tool result counts as part of the assistant turn that called it, so a run of
    call → result → call → result … becomes ONE assistant message with one header instead of one
    header per call; on a tool-heavy session most of the headers, and most of the cost of a
    names-only render. Nothing is dropped: every block is kept, in order. The merged message keeps
    the first timestamp, id and model; `meta["merged"]` says how many messages went into it.
    Injected context and the import notice are never merged."""
    out: list[Message] = []
    for m in msgs:
        prev = out[-1] if out else None
        joinable = (prev is not None and not m.is_env and not prev.is_env
                    and not m.meta.get("notice") and not prev.meta.get("notice")
                    and (m.role == prev.role or (m.role == "tool" and prev.role == "assistant")))
        if joinable:
            prev.blocks = prev.blocks + m.blocks
            prev.meta = {**prev.meta, "merged": prev.meta.get("merged", 0) + 1}
            if not prev.model and m.model:
                prev.model = m.model
            continue
        nm = copy.copy(m)
        nm.index = len(out)
        out.append(nm)
    return out


# ----------------------------------------------------------------------------- redaction
# publish-safe:path-placeholders: every path shape in this section is the redactor's own REPLACEMENT
# template (/run/media/user/, C:\Users\user) or a pattern built from the running user at runtime;
# none is a real path from any machine.
# name -> (label, on by default). The order is the order rules are applied.
RULES: dict[str, tuple[str, bool]] = {
    "home": ("home directory", True),
    "media": ("removable-media path (/run/media/<user>/)", True),
    "username": ("username", True),
    "hostname": ("hostname", True),
    "email": ("e-mail addresses", True),
    "keys": ("API keys and tokens", True),
    "ip": ("IP addresses", False),
}
# Bounded quantifiers on purpose: an unbounded `[...]+@` is O(n²) on a long run of matching
# characters with no "@" (Cursor stores ~1 MB base64 `toolCallBinary` blobs; one such run
# made the original pattern effectively never finish).
# (?<!\\): in RAW JSON text a match must not start on the letter of an escape: "\\n323…@x" once
# swallowed the n of \\n and left a bare backslash, which made the copied record invalid JSON.
_EMAIL = re.compile(r"(?<!\\)[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")
_KEYS = [
    re.compile(r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
]
_IP = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")


@dataclass
class Rule:
    pattern: re.Pattern
    repl: str
    name: str


@dataclass
class Redactor:
    rules: list[Rule] = field(default_factory=list)
    hits: dict[str, int] = field(default_factory=dict)
    matches: dict[str, Counter] = field(default_factory=dict)      # name -> Counter(matched text)

    @classmethod
    def defaults(cls, extra: list[str] | None = None, machine: bool = True,
                 options: dict[str, bool] | None = None) -> "Redactor":
        """Rules from the running machine + generic patterns. `options` toggles rules by name
        (see RULES); `machine=False` switches off home/media/username/hostname at once."""
        on = {k: v[1] for k, v in RULES.items()}
        on.update({k: bool(v) for k, v in (options or {}).items() if k in on})
        if not machine:
            on.update(home=False, media=False, username=False, hostname=False)
        r = cls()
        home = str(Path.home())
        user = getpass.getuser()
        host = socket.gethostname()
        if on["home"]:
            if os.name == "nt":
                # C:\Users\name plus its JSON-escaped (C:\\Users\\name) and forward-slash spellings
                r.add_literal(home, r"C:\Users\user", "home")
                r.add_literal(home.replace("\\", "\\\\"), r"C:\\Users\\user", "home")
                r.add_literal(home.replace("\\", "/"), "C:/Users/user", "home")
            else:
                r.add_literal(home, "/home/user", "home")
        if on["media"] and os.name != "nt":
            r.add(rf"/run/media/{re.escape(user)}/", "/run/media/user/", "media")
        if on["username"]:
            if len(user) > 3:
                # the username right after a JSON escape (\\n, \\t, \\r) is still the username: raw-text
                # redaction sees escapes, not newlines, and the alnum boundary alone let those through
                r.add(rf"(?:(?<![A-Za-z0-9_])|(?<=\\[ntr])){re.escape(user)}(?![A-Za-z0-9_])", "user", "username")
            else:
                # `me`, `ali`, `bob`: an ordinary word as often as a name. Replace it only where it sits
                # in a path; the hostname rule has had the same length guard all along
                r.add(rf"(?<=[/\\]){re.escape(user)}(?![A-Za-z0-9_])", "user", "username")
        if on["hostname"] and host and len(host) > 3:
            r.add_literal(host, "host", "hostname")
        if on["email"]:
            r.add_pattern(_EMAIL, "user@example.com", "email")
        if on["keys"]:
            for k in _KEYS:
                r.add_pattern(k, "[REDACTED_KEY]", "keys")
        if on["ip"]:
            r.add_pattern(_IP, "[IP]", "ip")
        for term in extra or []:
            if "=" in term:
                t, rep = term.split("=", 1)
            else:
                t, rep = term, "[REDACTED]"
            r.add_literal(t.strip(), rep.strip(), f"term:{t.strip()}")
        return r

    def add_pattern(self, pattern: re.Pattern, repl: str, name: str) -> None:
        self.rules.append(Rule(pattern, repl, name))
        self.hits.setdefault(name, 0)
        self.matches.setdefault(name, Counter())

    def add(self, pattern: str, repl: str, name: str | None = None) -> None:
        self.add_pattern(re.compile(pattern), repl, name or pattern)

    def add_literal(self, text: str, repl: str, name: str | None = None) -> None:
        if text:
            self.add(re.escape(text), repl, name or text)

    def text(self, s: str) -> str:
        if not s:
            return s
        for rule in self.rules:
            if rule.pattern is _EMAIL and "@" not in s:      # cheap prefilter; most blobs have no e-mail at all
                continue
            counter = self.matches[rule.name]

            # callable replacement: the text is inserted VERBATIM. A string would be parsed as a
            # regex template, and Windows replacements like C:\Users\user then die on "\U".
            def _sub(m, _r=rule.repl, _c=counter):
                _c[m.group(0)] += 1
                return _r
            s, n = rule.pattern.subn(_sub, s)
            if n:
                self.hits[rule.name] += n
        return s

    def session(self, s: Session) -> Session:
        out = copy.copy(s)
        out.title = self.text(s.title or "") or None
        out.cwd = self.text(s.cwd or "") or None
        out.project = self.text(s.project or "") or None
        out.path = Path(self.text(str(s.path)))
        # `meta` is where adapters keep vendor extras: Claude Code's `source_file` and `subagents`,
        # Aider's `history_file`, all absolute paths under the home directory. A redacted `json`
        # export carried them verbatim next to a redacted `path`.
        out.meta = _walk(s.meta, self.text)
        msgs = []
        for m in s.messages:
            nm = copy.copy(m)
            nm.meta = _walk(m.meta, self.text)
            nm.blocks = []
            for b in m.blocks:
                nb = copy.copy(b)
                nb.text = self.text(b.text)
                if isinstance(b.args, str):
                    nb.args = self.text(b.args)
                elif b.args is not None:
                    nb.args = _walk(b.args, self.text)
                if b.spill_path:
                    nb.spill_path = Path(self.text(str(b.spill_path)))
                if b.meta:
                    nb.meta = _walk(b.meta, self.text)
                nm.blocks.append(nb)
            msgs.append(nm)
        out.messages = msgs
        return out

    def file(self, src: Path, dst: Path) -> int:
        """Raw-text redaction copy. Returns bytes written.
        SQLite files are redacted row by row (text/JSON values) so they stay valid databases."""
        with src.open("rb") as fh:
            magic = fh.read(16)
        if src.suffix in (".vscdb", ".db", ".sqlite") or magic.startswith(b"SQLite format 3"):
            import shutil, sqlite3
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            con = sqlite3.connect(dst)
            for (t,) in con.execute("select name from sqlite_master where type='table'").fetchall():
                cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
                rows = con.execute(f"select rowid, * from {t}").fetchall()
                for row in rows:
                    rid, vals = row[0], list(row[1:])
                    changed = False
                    for i, v in enumerate(vals):
                        if isinstance(v, (str, bytes)):
                            txt = v.decode("utf-8", "surrogateescape") if isinstance(v, bytes) else v
                            red = self.text(txt)
                            if red != txt:
                                vals[i] = red.encode("utf-8", "surrogateescape") if isinstance(v, bytes) else red
                                changed = True
                    if changed:
                        con.execute(f"update {t} set {', '.join(c + '=?' for c in cols)} where rowid=?", (*vals, rid))
            con.commit(); con.execute("vacuum"); con.close()
            return dst.stat().st_size
        data = src.read_text(encoding="utf-8", errors="surrogateescape")
        red = self.text(data)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(red, encoding="utf-8", errors="surrogateescape")
        return len(red.encode("utf-8", errors="surrogateescape"))

    def report(self) -> str:
        parts = [f"{k}×{v}" for k, v in self.hits.items() if v]
        return ", ".join(parts) if parts else "nothing matched"

    def details(self, limit: int = 60) -> list[dict]:
        """One entry per rule: what it matched (distinct strings with counts) and the replacement."""
        out = []
        for name in self.hits:
            rules = [r for r in self.rules if r.name == name]
            label = RULES[name][0] if name in RULES else (f"custom term {name[5:]!r}" if name.startswith("term:") else name)
            out.append({"name": name, "label": label, "replacement": ", ".join(dict.fromkeys(r.repl for r in rules)),
                        "hits": self.hits[name],
                        "matches": [[t, c] for t, c in self.matches[name].most_common(limit)]})
        return out


def _walk(obj, fn):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    return obj
