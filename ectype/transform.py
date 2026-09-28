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
import json
import os
import re
import socket
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from . import tokens
from .model import ContentBlock, Message, Session


# ----------------------------------------------------------------------------- filters
@dataclass
class Filter:
    drop_roles: set[str] | None = None       # hide these roles' text and thinking; the tool calls a hidden turn made stay, filed under the tool turn
    kinds: set[str] | None = None            # keep only these block kinds
    env: bool = False                        # keep injected environment/system messages
    notices: bool = False                    # keep the agent's own notices (API errors, refusals, away summaries): meta["agent_notice"]
    cap: int = 0                             # keep the first N TOKENS of each tool_result (0 = off); thinking is never capped
    brief: bool = False                      # tool calls by name only, results not included; thinking is not touched
    collapse: bool = False                   # merge back-to-back turns of the same actor into one; a tool result rides with the assistant turn that called it
    questions: bool = True                   # a question the agent put to the user, and the answer, as the turns they are (see questions_as_turns)
    peers: bool = True                       # keep the messages other sessions sent in: meta["peer"]
    start: int | None = None                 # message index slice [start, end)
    end: int | None = None


def apply(s: Session, f: Filter) -> Session:
    out = copy.copy(s)
    msgs: list[Message] = []
    # sliced before the questions become turns: `start` and `end` count in the full session
    source = s.messages[f.start:f.end]
    if f.questions:
        source = questions_as_turns(source)
    for m in source:
        if m.is_env and not f.env:
            continue
        if m.meta.get("agent_notice") and not f.notices:
            continue
        if m.meta.get("peer") and not f.peers:
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
                # measured on the text as the renderer prints it (stripped): trailing padding once made
                # a result that fit the cap carry a "…[+n tokens cut]" note for a cut of nothing visible
                kept, cut, total = tokens.truncate(b.text.rstrip(), f.cap)
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
                    # another session's message is never folded into the user's turn, nor into a
                    # different session's: each keeps the label that says who wrote it
                    and m.meta.get("peer") == prev.meta.get("peer")
                    and (m.role == prev.role or (m.role == "tool" and prev.role == "assistant"))
                    # a merged run shows one model; merging across a switch hid the second one (D4:
                    # "the model name only when it changes")
                    and (not m.model or not prev.model or m.model == prev.model))
        if joinable:
            prev.blocks = prev.blocks + m.blocks
            # a split-off answer adds no source message to the count (budget.shown_count)
            prev.meta = {**prev.meta, "merged": prev.meta.get("merged", 0) + (0 if m.meta.get("split") else 1)}
            if not prev.model and m.model:
                prev.model = m.model
            continue
        nm = copy.copy(m)
        nm.index = len(out)
        out.append(nm)
    return out


# ----------------------------------------------------------------------------- questions put to the user
def questions_as_turns(msgs: list[Message]) -> list[Message]:
    """A question the agent put to the user, and the user's answer, as the turns they are.

    The adapters mark the calls to their agent's question tool (`meta["question"]`) and the results
    that answer them (`meta["answer"]`; `adapters.base.mark_questions`). Here the call becomes text in
    the assistant's turn and the result becomes a user turn right after the turn that carried it. A
    view with tool calls off then still shows what was asked and what the user decided, and a view
    with them on shows no tool at all for it: the answer is the user's words, not tool output.
    Unmarked messages pass through untouched, so a session with no question costs one scan."""
    out: list[Message] = []
    for m in msgs:
        if not any(b.meta.get("question") or b.meta.get("answer") for b in m.blocks):
            out.append(m)
            continue
        keep: list[ContentBlock] = []
        answers: list[ContentBlock] = []
        for b in m.blocks:
            if b.kind == "tool_call" and b.meta.get("question"):
                keep.append(ContentBlock("text", question_text(b.args), meta={"question": True}))
            elif b.kind == "tool_result" and b.meta.get("answer"):
                answers.append(ContentBlock("text", answer_text(b), meta={"answer": True}))
            else:
                keep.append(b)
        if keep:
            nm = copy.copy(m)
            nm.blocks = keep
            out.append(nm)
        if answers:
            # `split`: the answer came out of a message that stays, so it is not one of the source's
            # messages (the "N of M messages" count skips it); otherwise it stands in for the original
            out.append(Message(m.index, "user", m.timestamp, answers, id=m.id,
                               meta={"answer": True, **({"split": True} if keep else {})}))
    return out


def _json_or(v):
    """`v` parsed, when it is a string holding JSON; `v` itself otherwise."""
    if isinstance(v, str) and v.strip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _options(v) -> list[str]:
    """The choices offered with a question, as `label: description` lines. Agents send them as a list
    of objects (Claude Code, Gemini CLI, Codex), a list of strings, a JSON string of one (Cline), or
    `<suggest>` items (Roo Code)."""
    v = _json_or(v)
    if isinstance(v, str):
        found = re.findall(r"<suggest[^>]*>(.*?)</suggest>", v, re.S)
        return [x.strip() for x in found if x.strip()] or ([v.strip()] if v.strip() else [])
    out = []
    for o in v if isinstance(v, list) else []:
        if isinstance(o, dict):
            label = str(o.get("label") or o.get("text") or o.get("value") or "").strip()
            desc = str(o.get("description") or "").strip()
            if label:
                out.append(f"{label}: {desc}" if desc else label)
        elif isinstance(o, str) and o.strip():
            out.append(o.strip())
    return out


def question_text(args) -> str:
    """What a question tool asked: each question, then its choices, one per line."""
    a = _json_or(args)
    if not isinstance(a, dict):
        return str(a or "").strip()
    qs = a.get("questions")
    items = qs if isinstance(qs, list) and qs else [a]
    parts = []
    for q in items:
        if not isinstance(q, dict):
            parts.append(str(q).strip())
            continue
        line = str(q.get("question") or q.get("text") or q.get("prompt") or "").strip()
        opts = _options(q.get("options") if q.get("options") is not None else q.get("follow_up"))
        if opts:
            line += "".join(f"\n  - {o}" for o in opts)
            if q.get("multiSelect") or q.get("multi_select") or q.get("multiple"):
                line += "\n  (more than one may be chosen)"
        parts.append(line)
    return "\n\n".join(p for p in parts if p)


# `"question"="answer"` pairs, as Claude Code's AskUserQuestion result states them in its text
_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"')
_ANSWER_TAG = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.S)       # Cline and Roo Code


def _strings(v) -> list[str]:
    """Every non-empty string inside a JSON value, in order (a Codex answer object nests them)."""
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, dict):
        return [x for val in v.values() for x in _strings(val)]
    if isinstance(v, list):
        return [x for val in v for x in _strings(val)]
    return [str(v)] if v is not None else []


def answer_pairs(b: ContentBlock) -> list[tuple[str, str]]:
    """(question, answer) pairs a question tool's result holds, when it names the questions; [] when
    the result is only the answer (then `answer_text` is the whole of it)."""
    pairs = b.meta.get("answers")
    if isinstance(pairs, list) and pairs:
        return [(str(p[0]), str(p[1])) for p in pairs if isinstance(p, (list, tuple)) and len(p) == 2]
    return [(q, a) for q, a in _PAIR.findall(b.text or "")]


def answer_text(b: ContentBlock) -> str:
    """The user's reply in a question tool's result, without the tool's wording around it: the
    structured answers when the adapter kept them, else what the text or JSON holds, else the text."""
    pairs = answer_pairs(b)
    if pairs:
        return pairs[0][1] if len(pairs) == 1 else "\n".join(f"{q} → {a}" for q, a in pairs)
    t = (b.text or "").strip()
    m = _ANSWER_TAG.search(t)
    if m:
        return m.group(1).strip()
    data = _json_or(t)
    if isinstance(data, dict) and "answers" in data:
        found = _strings(data["answers"])
        if found:
            return "\n".join(found)
    m = re.match(r"(?:the )?user answered:\s*(.+)", t, re.I | re.S)
    return m.group(1).strip() if m else t


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
    # left boundary: without it `risk-assessment-framework` and `task-refactor-…` became keys; the
    # prefixes that end in `sk-` (Cerebras `csk-`, `gsk-` styles) are listed on their own
    re.compile(r"(?<![A-Za-z0-9])(?:c|g)?sk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),                      # GitHub fine-grained tokens
    re.compile(r"(?<![A-Za-z0-9])gsk_[A-Za-z0-9]{40,}"),              # Groq
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"),               # Hugging Face
    re.compile(r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{20,}"),  # Stripe
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),    # AWS access key ids
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWTs
]
# NAME_API_KEY=value, NAME_TOKEN: value …: the name stays (it says what was there), the value goes.
_KEY_ASSIGN = re.compile(r"\b([A-Z][A-Z0-9_]*(?:API_KEY|APIKEY|TOKEN|SECRET|PASSWORD))(\s*[=:]\s*)([\"']?)(?!\[REDACTED)([^\s\"']{8,})\3")
# account and host names that are ordinary words on stock images and CI runners: a rule for them
# would eat `ubuntu-latest` and `Kubuntu`; they identify nobody, so they are matched only in paths
# (usernames) or not at all (hostnames)
_GENERIC_NAMES = {"ubuntu", "runner", "admin", "administrator", "vagrant", "user", "root", "pi", "debian",
                  "fedora", "localhost", "raspberrypi", "docker", "codespace", "builder", "ec2-user"}
_IP = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")


@dataclass
class Rule:
    pattern: re.Pattern
    repl: str
    name: str
    fn: Callable[[re.Match], str] | None = None     # a replacement built from the match (keeps a name, drops a value)


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
        try:
            user = getpass.getuser()
        except (OSError, KeyError):
            # no login name in the environment and no passwd entry for the uid (a container run
            # with an arbitrary uid): the username and media rules have nothing to match, and a
            # redaction must not crash for want of a name
            user = ""
        host = socket.gethostname()
        if on["home"]:
            if os.name == "nt":
                # C:\Users\name plus its JSON-escaped (C:\\Users\\name) and forward-slash spellings
                r.add_literal(home, r"C:\Users\user", "home")
                r.add_literal(home.replace("\\", "\\\\"), r"C:\\Users\\user", "home")
                r.add_literal(home.replace("\\", "/"), "C:/Users/user", "home")
                spellings = [(home, r"C:\Users\user"), (home.replace("\\", "/"), "C:/Users/user")]
            else:
                r.add_literal(home, "/home/user", "home")
                spellings = [(home, "/home/user")]
            # percent-encoded, as in VS Code workspace URIs and query strings (`folder=%2Fhome%2F…`);
            # the hex digits of an escape may be either case
            for real, placeholder in spellings:
                r.add_pattern(re.compile(re.escape(quote(real, safe="")), re.I), quote(placeholder, safe=""), "home")
        if on["media"] and os.name != "nt" and user:
            r.add(rf"/run/media/{re.escape(user)}/", "/run/media/user/", "media")
            r.add_pattern(re.compile(re.escape(quote(f"/run/media/{user}/", safe="")), re.I),
                          quote("/run/media/user/", safe=""), "media")
        if on["username"] and user:
            if len(user) > 3 and user.lower() not in _GENERIC_NAMES:
                # the username right after a JSON escape (\\n, \\t, \\r) is still the username: raw-text
                # redaction sees escapes, not newlines, and the alnum boundary alone let those through.
                # Any case (`Zorbax's laptop`, `ZORBAX-PC`), and `_` counts as a boundary: it joins
                # identifiers (`zorbax_backup.tar`), where the name is still the name
                r.add_pattern(re.compile(rf"(?:(?<![A-Za-z0-9])|(?<=\\[ntr])){re.escape(user)}(?![A-Za-z0-9])", re.I),
                              "user", "username")
            else:
                # `me`, `ali`, `bob`, or a stock account (`ubuntu`, `runner`): an ordinary word as often as a
                # name. Replace it only where it sits in a path; the hostname rule has the same guard
                r.add(rf"(?<=[/\\]){re.escape(user)}(?![A-Za-z0-9_])", "user", "username")
        if on["hostname"] and host and len(host) > 3 and host.lower() not in _GENERIC_NAMES:
            # bounded like the username: an unbounded literal replaced the host inside longer words
            r.add(rf"(?<![A-Za-z0-9]){re.escape(host)}(?![A-Za-z0-9])", "host", "hostname")
        if on["email"]:
            r.add_pattern(_EMAIL, "user@example.com", "email")
        if on["keys"]:
            for k in _KEYS:
                r.add_pattern(k, "[REDACTED_KEY]", "keys")
            r.rules.append(Rule(_KEY_ASSIGN, "NAME=[REDACTED_KEY]", "keys",
                                fn=lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED_KEY]{m.group(3)}"))
        if on["ip"]:
            r.add_pattern(_IP, "[IP]", "ip")
        for term in extra or []:
            if "=" in term:
                t, rep = term.split("=", 1)
            else:
                t, rep = term, "[REDACTED]"
            t, rep = t.strip(), rep.strip()
            if not t:
                continue
            # any case (`IŞIKÇI` for `Işıkçı`), and also as the \\u escapes a JSON writer with ASCII
            # output leaves in tool results and raw records (the 2026-09-26 ideas round)
            r.add_pattern(re.compile(re.escape(t), re.I), rep, f"term:{t}")
            esc = json.dumps(t, ensure_ascii=True)[1:-1]
            if esc != t:
                r.add_pattern(re.compile(re.escape(esc), re.I), json.dumps(rep, ensure_ascii=True)[1:-1], f"term:{t}")
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
            def _sub(m, _r=rule.repl, _c=counter, _f=rule.fn):
                _c[m.group(0)] += 1
                return _f(m) if _f else _r
            s, n = rule.pattern.subn(_sub, s)
            if n:
                self.hits[rule.name] += n
        return s

    def session(self, s: Session) -> Session:
        out = copy.copy(s)
        # the id too: an Aider id is the history's folder name, which is the username for a history
        # kept in the home folder (uuids match no rule and pass through unchanged)
        out.id = self.text(s.id) if s.id else s.id
        # the model name too: a custom term the user typed must go everywhere it appears, and the
        # header and every model-change line print it (found in the Chrome pass of the third audit)
        out.model = self.text(s.model) if s.model else s.model
        out.cli_version = self.text(s.cli_version) if s.cli_version else s.cli_version
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
            nm.id = self.text(m.id) if isinstance(m.id, str) and m.id else m.id
            nm.model = self.text(m.model) if m.model else m.model
            nm.meta = _walk(m.meta, self.text)
            nm.blocks = []
            for b in m.blocks:
                nb = copy.copy(b)
                nb.text = self.text(b.text)
                if b.name:
                    nb.name = self.text(b.name)          # an MCP tool name carries its server's name
                if isinstance(b.call_id, str) and b.call_id:
                    nb.call_id = self.text(b.call_id)
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

            def q(name: str) -> str:
                # a table or column called `group`, `order` or `user` is legal SQL only when quoted
                return '"' + name.replace('"', '""') + '"'
            con = sqlite3.connect(dst)
            ok = False
            try:
                for (t,) in con.execute("select name from sqlite_master where type='table'").fetchall():
                    info = con.execute(f"pragma table_info({q(t)})").fetchall()
                    cols = [c[1] for c in info]
                    try:                                   # a WITHOUT ROWID table has no rowid: its primary key names a row instead
                        con.execute(f"select rowid from {q(t)} limit 1").fetchall()
                        key = ["rowid"]
                    except sqlite3.OperationalError:
                        key = [q(c[1]) for c in sorted((c for c in info if c[5]), key=lambda c: c[5])]
                    if not key:
                        continue
                    rows = con.execute(f"select {', '.join(key)}, {', '.join(q(c) for c in cols)} from {q(t)}").fetchall()
                    n = len(key)
                    for row in rows:
                        ident, vals = row[:n], list(row[n:])
                        changed = False
                        for i, v in enumerate(vals):
                            if isinstance(v, (str, bytes)):
                                txt = v.decode("utf-8", "surrogateescape") if isinstance(v, bytes) else v
                                red = self.text(txt)
                                if red != txt:
                                    vals[i] = red.encode("utf-8", "surrogateescape") if isinstance(v, bytes) else red
                                    changed = True
                        if changed:
                            con.execute(f"update {q(t)} set {', '.join(q(c) + '=?' for c in cols)} "
                                        f"where {' and '.join(k + '=?' for k in key)}", (*vals, *ident))
                con.commit()
                con.execute("vacuum")
                ok = True
            finally:
                con.close()                                # on the failure path too
                if not ok:
                    dst.unlink(missing_ok=True)            # never leave a half-redacted copy of a store behind
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
                        "matches": [[t, c] for t, c in self.matches[name].most_common(limit)],
                        # how many distinct values there were: the list is the first `limit`, and the
                        # page used to show a cut list as if it were all of them
                        "distinct": len(self.matches[name])})
        return out


def _walk(obj, fn):
    """Every string inside a nested structure, keys included: a path used as a dict key, a tuple or a
    `Path` (a plug-in's meta, D18) is serialised later exactly like a value."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk(x, fn) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(x, fn) for x in obj)
    if isinstance(obj, dict):
        return {(fn(k) if isinstance(k, str) else k): _walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, Path):
        return Path(fn(str(obj)))
    return obj
