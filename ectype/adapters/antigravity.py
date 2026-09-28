"""Antigravity CLI (agy): ~/.gemini/antigravity-cli/brain/<conversation-id>/

  .system_generated/logs/transcript_full.jsonl   step log (source of truth)
  .system_generated/logs/transcript.jsonl        same steps, long fields truncated
  .system_generated/steps/<step_index>/output.txt tool output spill (~8 K cap, tail kept)
  ../conversation_summaries.db                    title / step_count (SQLite; may lag)
  ../conversations/<id>.db                        trajectory_metadata_blob holds the workspace as a file:// URI
  ../history.jsonl                                {conversationId, workspace} per interactive prompt
  ../cli.log                                      'Initializing CLI store manager for workspace X' … 'Created conversation <id>'
                                                  the only place a HEADLESS (`agy -p`) run's cwd is written; the log is short and rotates

Steps: {step_index (STRING), source, type, status, created_at (UTC Z), content?, thinking?, tool_calls?}
  USER_INPUT        content wrapped in <USER_REQUEST>…</USER_REQUEST><ADDITIONAL_METADATA>…
  PLANNER_RESPONSE  assistant text and/or tool_calls [{name, args}]; NO call ids
  GENERIC           a tool result: "Created At/Completed At" preamble + output, or empty with
                    the output only in steps/<n>/output.txt
  SYSTEM_MESSAGE
A FAILED tool call leaves NO step at all; the only evidence is a gap in step_index.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..jsonl import read_jsonl
from ..model import ContentBlock, Message, Session, SessionRef, parse_ts
from .base import TITLE_CHARS, Adapter

_REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_PREAMBLE = re.compile(r"^(?:Created At: .*\n|Completed At: .*\n)+\n?")
_TRUNC = re.compile(r"<truncated \d+ lines?>")
_MODEL = re.compile(r"changed setting `Model Selection` from .*? to (.+?)\.(?:\s|$)", re.S)
_FILEURI = re.compile(rb"file://(/[^\x00-\x1f\x7f]+?)(?=[\x00-\x1f\x7f]|$)")
_LOG_WS = re.compile(r"Initializing CLI store manager for workspace (\S+)")
_LOG_CONV = re.compile(r"Created conversation ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _stamp(p: Path) -> tuple[float, int] | None:
    """(mtime, size) of a file, or None when it is not there: the cheap fingerprint a memo is keyed on."""
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _file_uri(blob) -> str | None:
    """The first `file:///path` inside a text or binary column, as a path."""
    if not blob:
        return None
    m = _FILEURI.search(blob.encode("utf-8") if isinstance(blob, str) else blob)
    return m.group(1).decode("utf-8", "replace") if m else None


class AntigravityAdapter(Adapter):
    name = "antigravity"
    label = "Antigravity CLI"
    env_home = "ECTYPE_ANTIGRAVITY_HOME"
    default_home = "~/.gemini/antigravity-cli"

    def _brain(self) -> Path:
        return self.home() / "brain"

    def available(self) -> bool:
        return self._brain().exists()

    def _summaries(self) -> dict[str, tuple[str | None, str | None]]:
        """conversation id -> (title, workspace), in ONE read of conversation_summaries.db.

        Title and workspace used to be two methods, and `discover()` called the workspace one per
        conversation, so listing N sessions opened the same SQLite file N+1 times. Deliberately
        NOT memoised on the instance: the adapters are singletons, and a GUI left running would
        then never see a conversation started after it launched."""
        db = self.home() / "conversation_summaries.db"
        if not db.exists():
            return {}
        try:
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            return {}
        try:
            rows = con.execute("select conversation_id, title, workspace_uris from conversation_summaries").fetchall()
        except sqlite3.Error:
            return {}
        finally:
            con.close()                              # on the failure path too
        return {r[0]: (r[1] or None, _file_uri(r[2])) for r in rows}

    def _workspace(self, sid: str) -> str | None:
        """conversation_summaries.workspace_uris: present only for conversations opened in a
        workspace; headless `agy -p` runs from another directory record no working directory at all."""
        return self._summaries().get(sid, (None, None))[1]

    def _side_workspaces(self) -> dict[str, str]:
        """conversation id -> workspace from history.jsonl (interactive prompts) and cli.log (every launch,
        headless included). The transcript itself never records a working directory, so for an
        `agy -p` run these two files are the only evidence; cli.log is small and rotates, so old
        headless runs stay unknown. Memoised on the (mtime, size) of both files; the adapters are
        singletons, so a memo with no key would stop a running GUI from ever seeing a conversation
        started after launch (the rule `_summaries` states)."""
        log = self.home() / "cli.log"
        hist = self.home() / "history.jsonl"
        stamp = (_stamp(log), _stamp(hist))
        cached = getattr(self, "_side_ws", None)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        out: dict[str, str] = {}
        if log.exists():
            ws = None
            try:
                for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                    m = _LOG_WS.search(line)
                    if m:
                        ws = m.group(1)
                        continue
                    m = _LOG_CONV.search(line)
                    if m and ws:
                        out.setdefault(m.group(1), ws)
            except OSError:
                pass
        if hist.exists():
            try:
                for line in hist.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("conversationId") and d.get("workspace"):
                        out[str(d["conversationId"])] = str(d["workspace"])     # last prompt wins
            except OSError:
                pass
        self._side_ws = (stamp, out)
        return out

    def _cwd(self, sid: str, summaries: dict | None = None) -> str | None:
        ws = (summaries.get(sid, (None, None))[1] if summaries is not None else self._workspace(sid))
        if ws:
            return ws
        db = self.home() / "conversations" / f"{sid}.db"
        if not db.exists():
            return self._side_workspaces().get(sid)
        try:
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            return self._side_workspaces().get(sid)
        try:
            row = con.execute("select data from trajectory_metadata_blob").fetchone()
        except sqlite3.Error:
            return self._side_workspaces().get(sid)
        finally:
            con.close()
        return _file_uri(row[0] if row else None) or self._side_workspaces().get(sid)

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        sysgen = ref.path.parent.parent
        base = f"brain/{ref.id}/.system_generated"
        out = [(ref.path, f"{base}/logs/transcript_full.jsonl")]
        compact = sysgen / "logs" / "transcript.jsonl"
        if compact.exists():
            out.append((compact, f"{base}/logs/transcript.jsonl"))
        for f in sorted((sysgen / "steps").glob("*/output.txt"), key=lambda p: int(p.parent.name)):
            out.append((f, f"{base}/steps/{f.parent.name}/output.txt"))
        return out

    def discover(self) -> list[SessionRef]:
        summaries = self._summaries()                 # one read for every title AND workspace
        out = []
        for d in self._brain().iterdir():
            f = d / ".system_generated" / "logs" / "transcript_full.jsonl"
            if not f.exists():
                continue
            st = f.stat()
            title, cwd = summaries.get(d.name, (None, None))
            if not cwd:                               # only then pay for the per-conversation .db
                cwd = self._cwd(d.name, summaries)
            out.append(SessionRef(self.name, d.name, f, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  st.st_size, title=title, project=Path(cwd).name if cwd else None))
        return out

    def load(self, ref: SessionRef) -> Session:
        steps_dir = ref.path.parent.parent / "steps"
        steps, bad = read_jsonl(ref.path)
        steps.sort(key=lambda s: int(s.get("step_index", 0)))
        msgs: list[Message] = []
        model = None
        if steps:
            mm = _MODEL.search(steps[0].get("content") or "")
            if mm:
                model = mm.group(1).strip()
        last_calls: list[ContentBlock] = []      # tool_calls of the last PLANNER_RESPONSE, awaiting results
        prev_idx = None
        for s in steps:
            idx = int(s.get("step_index", 0))
            ts = parse_ts(s.get("created_at"))
            t = s.get("type")
            # gap => a step was never written => the pending tool call failed
            if prev_idx is not None and idx > prev_idx + 1 and last_calls:
                missing = ", ".join(str(i) for i in range(prev_idx + 1, idx))
                blk = ContentBlock("tool_result", f"[no result recorded: step {missing} missing; the tool call most likely failed]",
                                   call_id=last_calls[0].call_id, is_error=True, meta={"inferred": True})
                msgs.append(Message(len(msgs), "tool", ts, [blk], id=f"step-{missing}", meta={"inferred": True}))
                last_calls = []
            prev_idx = idx
            content = s.get("content") or ""
            if t == "USER_INPUT":
                m = _REQ.search(content)
                text = m.group(1) if m else content
                msgs.append(Message(len(msgs), "user", ts, [ContentBlock("text", text)], id=f"step-{idx}",
                                    meta={"raw_wrapper": bool(m)}))
                last_calls = []
            elif t == "PLANNER_RESPONSE":
                blocks: list[ContentBlock] = []
                if s.get("thinking"):
                    blocks.append(ContentBlock("thinking", s["thinking"]))
                if content:
                    blocks.append(ContentBlock("text", content))
                last_calls = []
                for i, tc in enumerate(s.get("tool_calls") or []):
                    b = ContentBlock("tool_call", name=tc.get("name"), call_id=f"agy-{idx}-{i}", args=tc.get("args"))
                    blocks.append(b)
                    last_calls.append(b)
                msgs.append(Message(len(msgs), "assistant", ts, blocks, id=f"step-{idx}", model=model))
            elif t == "GENERIC":
                text = _PREAMBLE.sub("", content)
                spill = steps_dir / str(idx) / "output.txt"
                if not text.strip() and spill.exists():
                    text = spill.read_text(encoding="utf-8-sig", errors="replace")
                b = ContentBlock("tool_result", text, call_id=last_calls[0].call_id if last_calls else None,
                                 truncated=bool(_TRUNC.search(text)), spill_path=spill if spill.exists() else None)
                if last_calls:
                    last_calls.pop(0)
                msgs.append(Message(len(msgs), "tool", ts, [b], id=f"step-{idx}"))
            elif t == "SYSTEM_MESSAGE":
                # the CLI's own note to itself (a model switch, a setting): an agent notice, so the
                # same toggle governs it as Claude Code's `system` records (ARCHITECTURE D19)
                msgs.append(Message(len(msgs), "system", ts, [ContentBlock("system", content, meta={"subtype": "system_message"})],
                                    id=f"step-{idx}", meta={"agent_notice": "system_message"}))
        title = ref.title
        if not title:
            for m in msgs:
                if m.role == "user" and m.texts().strip():
                    title = m.texts().strip().splitlines()[0][:TITLE_CHARS]
                    break
        cwd = self._cwd(ref.id)
        return Session(self.name, ref.id, ref.path, msgs, title=title, project=ref.project or (Path(cwd).name if cwd else None),
                       cwd=cwd, model=model, started=msgs[0].timestamp if msgs else None,
                       ended=msgs[-1].timestamp if msgs else None,
                       meta={"steps": len(steps), **({"skipped_lines": len(bad)} if bad else {})})
