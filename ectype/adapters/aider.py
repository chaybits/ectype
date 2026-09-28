"""Aider: `.aider.chat.history.md` in each repo (plus `.aider.input.history`).

MARKDOWN, not JSON; the only such format so far. Structure:

    # aider chat started at 2026-09-04 14:21:14     <- session boundary
    > console/system output line                     <- '> ' prefix
    #### the user's message                          <- '#### ' prefix
    assistant reply text (plain lines)
    > Tokens: 719 sent, 745 received.                <- usage, also '> '

One file holds MANY sessions, split by the `# aider chat started at` headings; each becomes its
own Session (id = `<dirname>#<n>`). Timestamps exist only on that heading and are LOCAL time, so
every message in a run inherits the run's start time.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage
from .base import TITLE_CHARS, Adapter

_START = re.compile(r"^#\s*aider chat started at\s+(.+?)\s*$")
_USER = re.compile(r"^####\s?(.*)$")
_SYS = re.compile(r"^>\s?(.*)$")
_TOKENS = re.compile(r"^Tokens:\s*([\d.k]+)\s*sent,\s*([\d.k]+)\s*received", re.I)
_MODEL = re.compile(r"^Model:\s*(\S+)")


def _n(v: str) -> int | None:
    v = v.strip().lower()
    try:
        return int(float(v[:-1]) * 1000) if v.endswith("k") else int(float(v))
    except ValueError:
        return None


class AiderAdapter(Adapter):
    name = "aider"
    label = "Aider"
    env_home = "ECTYPE_AIDER_HOME"
    default_home = "~"

    def _files(self) -> list[Path]:
        roots = [self.home()]
        env = os.environ.get("ECTYPE_AIDER_DIRS")
        if env:
            roots = [Path(os.path.expanduser(p)) for p in env.split(os.pathsep) if p]
        out: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            f = root / ".aider.chat.history.md"
            if f.exists():
                out.append(f)
            try:                       # shallow scan: project dirs one level down
                for d in root.iterdir():
                    if d.is_dir() and not d.name.startswith("."):
                        c = d / ".aider.chat.history.md"
                        if c.exists():
                            out.append(c)
            except (PermissionError, OSError):
                pass
        return out

    def available(self) -> bool:
        return bool(self._files())

    @staticmethod
    def _split(path: Path) -> list[tuple[datetime | None, list[str]]]:
        runs: list[tuple[datetime | None, list[str]]] = []
        cur: list[str] = []
        ts: datetime | None = None
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            m = _START.match(line)
            if m:
                if cur:
                    runs.append((ts, cur))
                cur = []
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone().astimezone(timezone.utc)
                except ValueError:
                    ts = None
            else:
                cur.append(line)
        if cur:
            runs.append((ts, cur))
        return runs

    def discover(self) -> list[SessionRef]:
        out = []
        for f in self._files():
            st = f.stat()
            runs = self._split(f)
            for i, (ts, body) in enumerate(runs):
                title = None
                had_user = had_reply = False
                for line in body:                       # one pass: title, and whether anything was said
                    mu = _USER.match(line)
                    if mu:
                        had_user = True
                        if title is None and mu.group(1).strip():
                            title = mu.group(1)
                    elif line.strip() and not _SYS.match(line):
                        had_reply = True
                # skip runs with no conversation at all (aider writes a heading even for a run
                # that only printed console noise); they would load as an empty Session
                if not had_user and not had_reply:
                    continue
                out.append(SessionRef(self.name, f"{f.parent.name}#{i}", f,
                                      ts or datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                      len("\n".join(body).encode()), project=f.parent.name,
                                      title=(title or "").strip()[:TITLE_CHARS] or None))
        return out

    def find(self, id_prefix: str) -> list[SessionRef]:
        p = id_prefix.lower()
        return [r for r in self.discover() if r.id.lower().startswith(p) or p in r.id.lower()]

    def load(self, ref: SessionRef) -> Session:
        idx = int(ref.id.rsplit("#", 1)[1])
        ts, body = self._split(ref.path)[idx]
        msgs: list[Message] = []
        model = None
        buf: list[str] = []
        mode = None        # 'user' | 'assistant' | 'system'

        def flush():
            nonlocal buf, mode
            text = "\n".join(buf).strip()
            buf = []
            if not text or mode is None:
                return
            role = {"user": "user", "assistant": "assistant", "system": "system"}[mode]
            kind = "system" if mode == "system" else "text"
            msgs.append(Message(len(msgs), role, ts, [ContentBlock(kind, text)]))

        for line in body:
            mu, ms = _USER.match(line), _SYS.match(line)
            if mu:
                flush(); mode = "user"; buf = [mu.group(1)]
            elif ms:
                content = ms.group(1)
                mm = _MODEL.match(content)
                if mm:
                    model = mm.group(1)
                mt = _TOKENS.match(content)
                if mt and msgs:
                    msgs[-1].tokens = TokenUsage(input=_n(mt.group(1)), output=_n(mt.group(2)))
                    continue
                if mode != "system":
                    flush(); mode = "system"
                buf.append(content)
            else:
                if mode != "assistant" and line.strip():
                    flush(); mode = "assistant"
                if mode == "assistant" or line.strip():
                    buf.append(line)
        flush()
        return Session(self.name, ref.id, ref.path, msgs, title=ref.title, project=ref.project,
                       cwd=str(ref.path.parent), model=model, started=ts,
                       ended=ts, meta={"history_file": str(ref.path)})
