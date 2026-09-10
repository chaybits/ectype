"""VS Code Copilot Chat: `<Code>/User/**/chatSessions/<uuid>.jsonl`
(also `globalStorage/emptyWindowChatSessions/` for chats opened without a folder).

A **JSON-PATH PATCH LOG**, the most general mutation format here. Records:

    {"kind": "0", "v": {...}}                 initial full state
    {"kind": "1", "k": [path...], "v": val}   SET state at that path
    {"kind": "2", "k": [path...], "v": [...]} APPEND those items to the array at that path

Path segments are dict keys (str) or list indices (int), e.g. `["requests", 2, "response"]`.
The conversation is `state["requests"]`, one entry per user turn:
`{message: {text}, response: [parts], result, promptTokens, completionTokens, ...}`.
Response parts are `{value: markdown}` for text and `{kind: "toolInvocationSerialized", ...}`
for tool use; VS Code serialises the rendered tool card, not a raw call/result pair.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from .base import Adapter, app_config_dir


def _apply(state, path, value, append: bool):
    """Apply one patch record to the reconstructed state."""
    if not path:
        return value if not append else state
    cur = state
    for seg in path[:-1]:
        if isinstance(seg, int):
            while len(cur) <= seg:
                cur.append({})
            cur = cur[seg]
        else:
            cur = cur.setdefault(seg, {})
    last = path[-1]
    if append:
        if isinstance(last, int):
            while len(cur) <= last:
                cur.append({})
            tgt = cur[last]
        else:
            tgt = cur.setdefault(last, [])
        if isinstance(tgt, list) and isinstance(value, list):
            tgt.extend(value)
        elif isinstance(cur, dict):
            cur[last] = (tgt if isinstance(tgt, list) else []) + list(value or [])
    else:
        if isinstance(last, int):
            while len(cur) <= last:
                cur.append({})
            cur[last] = value
        else:
            cur[last] = value
    return state


class CopilotChatAdapter(Adapter):
    name = "copilot-chat"
    label = "VS Code Copilot Chat"
    env_home = "ECTYPE_VSCODE_HOME"
    default_home = app_config_dir("Code")

    def _files(self) -> list[Path]:
        home = self.home()
        if not home.exists():
            return []
        out = list((home / "globalStorage" / "emptyWindowChatSessions").glob("*.jsonl"))
        ws = home / "workspaceStorage"
        if ws.exists():
            out += list(ws.glob("*/chatSessions/*.jsonl"))
        return sorted(out)

    def available(self) -> bool:
        return bool(self._files())

    @staticmethod
    def _state(path: Path) -> dict:
        state: dict = {}
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = str(r.get("kind"))
                if kind == "0":
                    state = r.get("v") or {}
                elif kind in ("1", "2"):
                    _apply(state, r.get("k") or [], r.get("v"), append=(kind == "2"))
        return state

    def discover(self) -> list[SessionRef]:
        out = []
        for f in self._files():
            st = f.stat()
            title = None
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    for i, line in enumerate(fh):
                        if i > 40:
                            break
                        r = json.loads(line)
                        if r.get("k") == ["customTitle"]:
                            title = r.get("v")
                            break
            except (OSError, ValueError):
                pass
            out.append(SessionRef(self.name, f.stem, f,
                                  datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  st.st_size, project=f.parent.parent.name, title=title))
        return out

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        rel = ref.path.relative_to(self.home()).as_posix() if str(ref.path).startswith(str(self.home())) else ref.path.name
        return [(ref.path, rel)]

    def load(self, ref: SessionRef) -> Session:
        state = self._state(ref.path)
        msgs: list[Message] = []
        model = None
        sel = (state.get("inputState") or {}).get("selectedModel") or {}
        if isinstance(sel, dict):
            model = sel.get("identifier")
        for req in state.get("requests") or []:
            if not isinstance(req, dict):
                continue
            ts = parse_ts(req.get("timestamp"))
            msg = req.get("message") or {}
            text = msg.get("text") if isinstance(msg, dict) else str(msg)
            if text:
                msgs.append(Message(len(msgs), "user", ts, [ContentBlock("text", text)],
                                    id=req.get("requestId")))
            blocks: list[ContentBlock] = []
            for part in req.get("response") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("kind") == "toolInvocationSerialized":
                    inv = part.get("invocationMessage") or {}
                    label = inv.get("value") if isinstance(inv, dict) else str(inv)
                    res = part.get("resultDetails") or part.get("pastTenseMessage") or {}
                    rtext = res.get("value") if isinstance(res, dict) else json.dumps(res, ensure_ascii=False)
                    blocks.append(ContentBlock("tool_call", name=(label or "tool")[:80],
                                               call_id=str(part.get("toolCallId") or ""),
                                               args=part.get("toolSpecificData")))
                    if rtext:
                        blocks.append(ContentBlock("tool_result", str(rtext),
                                                   call_id=str(part.get("toolCallId") or ""),
                                                   is_error=bool(part.get("isError"))))
                elif "value" in part:
                    v = part["value"]
                    if isinstance(v, str) and v.strip():
                        blocks.append(ContentBlock("text", v))
            tok = None
            if req.get("promptTokens") or req.get("completionTokens"):
                tok = TokenUsage(input=req.get("promptTokens"), output=req.get("completionTokens"))
                tok.total = (tok.input or 0) + (tok.output or 0)
            if blocks:
                msgs.append(Message(len(msgs), "assistant", ts, blocks,
                                    id=req.get("requestId"), model=model, tokens=tok))
        started = parse_ts(state.get("creationDate"))
        return Session(self.name, ref.id, ref.path, msgs,
                       title=state.get("customTitle") or ref.title, project=ref.project,
                       model=model, started=started,
                       ended=msgs[-1].timestamp if msgs else started,
                       meta={"responder": state.get("responderUsername"),
                             "location": state.get("initialLocation")})
