"""LM Studio: `~/.lmstudio/conversations/<epoch>.conversation.json`

One JSON file per conversation. Every message is `{versions: [...], currentlySelected: n}`,
**branching by regeneration**, like SillyTavern's swipes. A user version is `type: singleStep`
with `content: [{type: "text", text}]`. An assistant version is `type: multiStep` with `steps[]`:

    contentBlock             `style`/`prefix` marks reasoning -> thinking, else the answer
    requestConfirmToolCall   an MCP tool CALL: `request` = {callId, pluginIdentifier, name,
                             parameters}, `response.result.type` = allow | deny
    toolStatus               that call's OUTCOME only ({toolCallSucceeded, timeMs}); LM Studio
                             does NOT store the tool's output text anywhere in the file
    debugInfoBlock           ignored

(`toolCallRequestBlock` / `toolCallResultBlock` are also handled, for older files.)

Model name lives in `senderInfo.senderName`; token count in the file-level `tokenCount`.
Timestamps exist only at file level (`createdAt`, `userLastMessagedAt`,
`assistantLastMessagedAt`); individual messages carry none.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage
from .base import Adapter


def _ts(ms) -> datetime | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000 if ms > 1e12 else ms, tz=timezone.utc)


class LMStudioAdapter(Adapter):
    name = "lmstudio"
    category = "chat"
    label = "LM Studio"
    env_home = "ECTYPE_LMSTUDIO_HOME"
    default_home = "~/.lmstudio/conversations"

    def discover(self) -> list[SessionRef]:
        out = []
        for f in sorted(self.home().glob("*.conversation.json")):
            st = f.stat()
            title = None
            try:
                # cheap: only the head of the file, never the whole conversation
                with open(f, encoding="utf-8-sig") as fh:
                    head = fh.read(4096)
                i = head.find('"name"')
                if i >= 0:
                    j = head.index(":", i) + 1
                    while j < len(head) and head[j] in " \t\r\n":
                        j += 1
                    # decode the string value itself: cutting at the next comma lost every title
                    # that contained one ("Hello, world" peeked as no title at all)
                    val, _ = json.JSONDecoder().raw_decode(head, j)
                    title = val if isinstance(val, str) and val else None
            except (OSError, ValueError):
                pass
            out.append(SessionRef(self.name, f.name.split(".")[0], f,
                                  datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  st.st_size, title=title))
        return out

    def load(self, ref: SessionRef) -> Session:
        d = json.loads(ref.path.read_text(encoding="utf-8-sig"))
        msgs: list[Message] = []
        model = None
        for m in d.get("messages", []):
            versions = m.get("versions") or []
            if not versions:
                continue
            sel = m.get("currentlySelected", 0)
            v = versions[sel] if 0 <= sel < len(versions) else versions[0]
            role = v.get("role", "assistant")
            blocks: list[ContentBlock] = []
            if v.get("type") == "singleStep" or role == "user":
                text = "".join(c.get("text", "") for c in v.get("content") or [] if isinstance(c, dict))
                if text:
                    blocks.append(ContentBlock("text", text))
            else:
                model = (v.get("senderInfo") or {}).get("senderName") or model
                for step in v.get("steps") or []:
                    st = step.get("type")
                    text = "".join(c.get("text", "") for c in step.get("content") or [] if isinstance(c, dict))
                    if st == "contentBlock":
                        style = str((step.get("style") or {}).get("type", "")).lower()
                        is_think = "think" in style or "reason" in style or bool(step.get("prefix"))
                        if text:
                            blocks.append(ContentBlock("thinking" if is_think else "text", text))
                    elif st == "requestConfirmToolCall":
                        # LM Studio records an MCP call as a confirmation request; `request`
                        # holds the call, `response.result.type` is allow/deny.
                        req = step.get("request") or {}
                        resp = ((step.get("response") or {}).get("result") or {})
                        denied = resp.get("type") == "deny"
                        blocks.append(ContentBlock(
                            "tool_call", name=req.get("name"), call_id=str(req.get("callId") or ""),
                            args=req.get("parameters"),
                            meta={"plugin": req.get("pluginIdentifier"), "denied": denied}))
                    elif st == "toolStatus":
                        # the OUTCOME of a call; LM Studio does not store the tool's output text
                        stat = ((step.get("statusState") or {}).get("status") or {})
                        kind = str(stat.get("type") or "")
                        ok = "Succeeded" in kind
                        ms_ = stat.get("timeMs")
                        label = kind or "unknown"
                        if ms_ is not None:
                            label += f", {ms_} ms"
                        blocks.append(ContentBlock(
                            "tool_result", f"[{label}]",
                            call_id=str(step.get("callId") or ""), is_error=not ok,
                            meta={"no_output_stored": True}))
                    elif st in ("toolCallRequestBlock", "toolCallResultBlock"):
                        req = step.get("toolCallRequest") or {}
                        if st == "toolCallRequestBlock":
                            blocks.append(ContentBlock("tool_call", name=req.get("name"),
                                                       call_id=str(req.get("id") or ""),
                                                       args=req.get("arguments")))
                        else:
                            blocks.append(ContentBlock("tool_result",
                                                       text or json.dumps(step.get("content"), ensure_ascii=False),
                                                       call_id=str(step.get("toolCallId") or "")))
            if not blocks:
                continue
            if len(versions) > 1:
                blocks[0].meta["alternatives"] = [f"(version {i + 1} not shown)"
                                                  for i in range(len(versions)) if i != sel]
                blocks[0].meta["swipe"] = f"{sel + 1}/{len(versions)}"
            msgs.append(Message(len(msgs), "user" if role == "user" else "assistant", None, blocks))

        total = d.get("tokenCount")
        if msgs and isinstance(total, int):
            msgs[-1].tokens = TokenUsage(total=total)
        started = _ts(d.get("createdAt"))
        ended = _ts(d.get("assistantLastMessagedAt") or d.get("userLastMessagedAt"))
        if msgs:
            msgs[0].timestamp = started
            msgs[-1].timestamp = ended
        lum = d.get("lastUsedModel")
        return Session(self.name, ref.id, ref.path, msgs, title=d.get("name") or ref.title,
                       model=(lum.get("identifier") if isinstance(lum, dict) else None) or model,
                       started=started, ended=ended,
                       meta={"plugins": d.get("plugins"), "pinned": d.get("pinned")})
