"""Continue: `~/.continue/sessions/<uuid>.json` (+ `sessions.json` index)

  `sessions.json`  `[{sessionId, title, dateCreated, workspaceDirectory, messageCount}]`
  `<uuid>.json`    `{sessionId, title, workspaceDirectory, mode, chatModelTitle, history: [...]}`

Each `history` item is one turn:
`{message: {role, content}, contextItems, editorState, promptLogs, toolCallStates}`
`content` is a string OR a list of `{type: "text", text}` parts. Tool use lives in
`toolCallStates: [{status, toolCall: {id, function: {name, arguments}}, output}]`; the call and
its result are the SAME object, unlike every other agent here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, parse_ts
from .base import Adapter


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


class ContinueAdapter(Adapter):
    name = "continue"
    label = "Continue"
    env_home = "ECTYPE_CONTINUE_HOME"
    default_home = "~/.continue/sessions"

    def _index(self) -> dict[str, dict]:
        f = self.home() / "sessions.json"
        if not f.exists():
            return {}
        try:
            return {s["sessionId"]: s for s in json.loads(f.read_text(encoding="utf-8-sig"))}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def discover(self) -> list[SessionRef]:
        home = self.home()
        if not home.exists():
            return []
        idx = self._index()
        out = []
        for f in sorted(home.glob("*.json")):
            if f.name == "sessions.json":
                continue
            st = f.stat()
            meta = idx.get(f.stem, {})
            out.append(SessionRef(self.name, f.stem, f,
                                  parse_ts(meta.get("dateCreated")) or datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  st.st_size,
                                  project=Path(str(meta.get("workspaceDirectory") or "").replace("file://", "")).name or None,
                                  title=meta.get("title")))
        return out

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        out = [(ref.path, ref.path.name)]
        idx = self.home() / "sessions.json"
        if idx.exists():
            out.append((idx, "sessions.json"))
        return out

    def load(self, ref: SessionRef) -> Session:
        d = json.loads(ref.path.read_text(encoding="utf-8-sig"))
        model = d.get("chatModelTitle")
        msgs: list[Message] = []
        for item in d.get("history") or []:
            m = item.get("message") or {}
            role = m.get("role", "assistant")
            blocks: list[ContentBlock] = []
            text = _text(m.get("content"))
            if text.strip():
                blocks.append(ContentBlock("thinking" if role == "thinking" else "text", text))
            for tcs in item.get("toolCallStates") or []:
                if not isinstance(tcs, dict):
                    continue
                call = tcs.get("toolCall") or {}
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                blocks.append(ContentBlock("tool_call", name=fn.get("name"),
                                           call_id=str(call.get("id") or ""), args=args))
                out = tcs.get("output")
                if out is not None:
                    if isinstance(out, list):
                        out = "\n".join(o.get("content", "") if isinstance(o, dict) else str(o) for o in out)
                    status = str(tcs.get("status") or "")
                    blocks.append(ContentBlock("tool_result", str(out),
                                               call_id=str(call.get("id") or ""),
                                               is_error=status.lower() in ("errored", "error", "failed")))
            if not blocks:
                continue
            if role not in ("user", "assistant", "system", "tool"):
                role = "assistant"
            msgs.append(Message(len(msgs), role, None, blocks,
                                model=model if role == "assistant" else None))
        idx = self._index().get(ref.id, {})
        started = parse_ts(idx.get("dateCreated"))
        if msgs:
            msgs[0].timestamp = started
        wd = str(d.get("workspaceDirectory") or "").replace("file://", "")
        return Session(self.name, ref.id, ref.path, msgs, title=d.get("title") or ref.title,
                       project=Path(wd).name or None, cwd=wd or None, model=model,
                       started=started, ended=None, meta={"mode": d.get("mode")})
