"""Roo Code: `<Code>/User/globalStorage/rooveterinaryinc.roo-cline/tasks/<uuid>/`

Three files per task, and they hold DIFFERENT things:

  `history_item.json`            metadata: task (title), ts, tokensIn/Out, workspace, mode, size
  `api_conversation_history.json` the real conversation, Anthropic-shaped
                                  `[{role, content: [{type: text|thinking|tool_use|tool_result}]}]`
  `ui_messages.json`             the UI event log (`say`/`ask` records: api_req_started,
                                  user_feedback, completion_result, tool, error); used here only
                                  for the session's end time and the error count; every entry of the
                                  conversation file carries its own `ts`

Roo (a Cline fork) wraps user text in `<user_message>` / `<task>` and appends an
`<environment_details>` block that is marked as injected context, not typed by the user.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from ._anthropic import result_text
from .base import TITLE_CHARS, Adapter, app_config_dir

_WRAP = re.compile(r"<(?:user_message|task|feedback)>\s*(.*?)\s*</(?:user_message|task|feedback)>", re.S)
_ENV = re.compile(r"<environment_details>.*?</environment_details>", re.S)
# a result that IS an error message, not one that merely starts with the letters ("errors.py")
_ERR = re.compile(r"\s*(?:\[?error\]?\s*[:\-\u2014]|error\s+(?:executing|running|reading|writing)\b|ENOENT\b)", re.I)


class RooCodeAdapter(Adapter):
    name = "roo-code"
    label = "Roo Code"
    env_home = "ECTYPE_ROO_HOME"
    default_home = app_config_dir("Code") + "/globalStorage/rooveterinaryinc.roo-cline/tasks"
    question_tools = frozenset({"ask_followup_question"})   # inherited from Cline, options as <suggest> items

    def discover(self) -> list[SessionRef]:
        home = self.home()
        if not home.exists():
            return []
        out = []
        for d in sorted(home.iterdir()):
            api = d / "api_conversation_history.json"
            if not api.exists():
                continue
            st = api.stat()
            title = None
            ts = None
            hi = d / "history_item.json"
            if hi.exists():
                try:
                    h = json.loads(hi.read_text(encoding="utf-8-sig"))
                    if isinstance(h, dict):          # any other shape: no title, and the store keeps listing
                        task = h.get("task")
                        title = task.strip().splitlines()[:1] if isinstance(task, str) else []
                        title = title[0][:TITLE_CHARS] if title else None
                        ts = parse_ts(h.get("ts"))
                except (OSError, ValueError):
                    pass
            out.append(SessionRef(self.name, d.name, api,
                                  ts or datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  st.st_size, title=title))
        return out

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        d = ref.path.parent
        return [(f, f"{d.name}/{f.name}") for f in sorted(d.iterdir()) if f.is_file()] + \
               [(f, f"{d.name}/{f.relative_to(d).as_posix()}") for f in sorted(d.rglob("*")) if f.is_file() and f.parent != d]

    def load(self, ref: SessionRef) -> Session:
        d = ref.path.parent
        conv = json.loads(ref.path.read_text(encoding="utf-8-sig"))
        hist = {}
        hi = d / "history_item.json"
        if hi.exists():
            try:
                hist = json.loads(hi.read_text(encoding="utf-8-sig"))
            except ValueError:
                pass
        # ui_messages gives per-turn timestamps and surfaces errors the API history hides
        stamps: list[datetime] = []
        errors = 0
        ui = d / "ui_messages.json"
        if ui.exists():
            try:
                for u in json.loads(ui.read_text(encoding="utf-8-sig")):
                    t = parse_ts(u.get("ts"))
                    if t:
                        stamps.append(t)
                    if u.get("say") == "error":
                        errors += 1
            except ValueError:
                pass
        msgs: list[Message] = []
        for i, m in enumerate(conv):
            blocks: list[ContentBlock] = []
            env_only = False
            content = m.get("content")
            if isinstance(content, str):
                blocks.append(ContentBlock("text", content))
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    text = b.get("text", "")
                    if _ENV.fullmatch(text.strip()):
                        env_only = True
                        continue
                    text = _ENV.sub("", text).strip()
                    w = _WRAP.search(text)
                    if w:
                        text = w.group(1)
                    if text:
                        blocks.append(ContentBlock("text", text))
                elif t == "thinking":
                    blocks.append(ContentBlock("thinking", b.get("thinking", "")))
                elif t == "tool_use":
                    blocks.append(ContentBlock("tool_call", name=b.get("name"),
                                               call_id=b.get("id"), args=b.get("input")))
                elif t == "tool_result":
                    c = result_text(b.get("content"))
                    blocks.append(ContentBlock("tool_result", c, call_id=b.get("tool_use_id"),
                                               is_error=bool(b.get("is_error")) or bool(_ERR.match(c))))
            if not blocks:
                continue
            role = m.get("role", "assistant")
            if role == "user" and all(b.kind == "tool_result" for b in blocks):
                role = "tool"
            # each conversation entry carries its own `ts`; the UI log has more events than there
            # are entries, so pairing the two by position gave most messages an unrelated time
            ts = parse_ts(m.get("ts"))
            msgs.append(Message(len(msgs), role, ts, blocks,
                                meta={"env": True} if env_only else {}))
        tok = None
        if hist.get("tokensIn") or hist.get("tokensOut"):
            tok = TokenUsage(input=hist.get("tokensIn"), output=hist.get("tokensOut"),
                             cached=hist.get("cacheReads"))
            tok.total = sum(v for v in (tok.input, tok.output) if v)
            if msgs:
                msgs[-1].tokens = tok
        return Session(self.name, ref.id, ref.path, msgs, title=ref.title,
                       project=Path(hist.get("workspace") or ".").name,
                       cwd=hist.get("workspace"), model=hist.get("apiConfigName"),
                       started=parse_ts(hist.get("ts")) or (stamps[0] if stamps else None),
                       ended=stamps[-1] if stamps else None,
                       meta={"mode": hist.get("mode"), "ui_errors": errors,
                             "total_cost": hist.get("totalCost")})
