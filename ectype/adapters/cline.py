"""Cline: `~/.cline/data/sessions/<id>/<id>.json` + `<id>.messages.json`

(Cline moved its store out of the VS Code extension's globalStorage into `~/.cline`; only
`cline_mcp_settings.json` remains under `globalStorage/saoudrizwan.claude-dev/`.)

  `<id>.json`           session metadata: session_id, model, provider, cwd, workspace_root,
                        started_at / ended_at (ISO), status, exit_code, prompt, enable_tools
  `<id>.messages.json`  `{"messages": [...]}`, Anthropic-shaped blocks:
                        `{id, role, ts, content: [{type: text|thinking|tool_use|tool_result}],
                          modelInfo, metrics: {inputTokens, outputTokens, cacheReadTokens}}`

User text arrives wrapped in `<user_input mode="act">…</user_input>`, which is unwrapped here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from ._anthropic import result_text
from .base import Adapter

_INPUT = re.compile(r"<user_input[^>]*>\s*(.*?)\s*</user_input>", re.S)


class ClineAdapter(Adapter):
    name = "cline"
    label = "Cline"
    env_home = "ECTYPE_CLINE_HOME"
    default_home = "~/.cline/data/sessions"

    def discover(self) -> list[SessionRef]:
        home = self.home()
        if not home.exists():
            return []
        out = []
        for d in sorted(home.iterdir()):
            meta_f = d / f"{d.name}.json"
            if not meta_f.exists():
                continue
            st = meta_f.stat()
            try:
                meta = json.loads(meta_f.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            prompt = (meta.get("prompt") or "").strip().splitlines()
            msgs_f = d / f"{d.name}.messages.json"
            size = msgs_f.stat().st_size if msgs_f.exists() else st.st_size
            out.append(SessionRef(self.name, d.name, meta_f,
                                  parse_ts(meta.get("ended_at")) or datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  size,
                                  project=Path(meta.get("workspace_root") or meta.get("cwd") or ".").name,
                                  title=(prompt[0][:80] if prompt else None)))
        return out

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        d = ref.path.parent
        return [(f, f"{d.name}/{f.name}") for f in sorted(d.iterdir()) if f.is_file()]

    def load(self, ref: SessionRef) -> Session:
        meta = json.loads(ref.path.read_text(encoding="utf-8-sig"))
        msgs_f = ref.path.parent / f"{ref.id}.messages.json"
        raw = {}
        if msgs_f.exists():
            raw = json.loads(msgs_f.read_text(encoding="utf-8-sig"))
        items = raw.get("messages") if isinstance(raw, dict) else raw
        msgs: list[Message] = []
        for m in items or []:
            blocks: list[ContentBlock] = []
            content = m.get("content")
            if isinstance(content, str):
                blocks.append(ContentBlock("text", content))
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    text = b.get("text", "")
                    mi = _INPUT.search(text)
                    blocks.append(ContentBlock("text", mi.group(1) if mi else text))
                elif t == "thinking":
                    blocks.append(ContentBlock("thinking", b.get("thinking", "")))
                elif t == "tool_use":
                    blocks.append(ContentBlock("tool_call", name=b.get("name"),
                                               call_id=b.get("id"), args=b.get("input")))
                elif t == "tool_result":
                    blocks.append(ContentBlock("tool_result", result_text(b.get("content")),
                                               call_id=b.get("tool_use_id"),
                                               is_error=bool(b.get("is_error"))))
            if not blocks:
                continue
            role = m.get("role", "assistant")
            if role == "user" and all(b.kind == "tool_result" for b in blocks):
                role = "tool"
            met = m.get("metrics") or {}
            tok = None
            if met:
                tok = TokenUsage(input=met.get("inputTokens"), output=met.get("outputTokens"),
                                 cached=met.get("cacheReadTokens"))
                tok.total = sum(v for v in (tok.input, tok.output, tok.cached) if v)
            mi = m.get("modelInfo") or {}
            msgs.append(Message(len(msgs), role, parse_ts(m.get("ts")), blocks, id=m.get("id"),
                                model=mi.get("id") if isinstance(mi, dict) else None, tokens=tok))
        return Session(self.name, ref.id, ref.path, msgs, title=ref.title, project=ref.project,
                       cwd=meta.get("workspace_root") or meta.get("cwd"), model=meta.get("model"),
                       cli_version=str(meta.get("version") or ""),
                       started=parse_ts(meta.get("started_at")), ended=parse_ts(meta.get("ended_at")),
                       meta={"provider": meta.get("provider"), "status": meta.get("status"),
                             "source": meta.get("source"), "enable_tools": meta.get("enable_tools")})
