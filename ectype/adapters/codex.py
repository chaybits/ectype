"""OpenAI Codex CLI: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local-time>-<uuid>.jsonl

Append-only JSONL of {timestamp, type, payload, ordinal}. Conversation lives in
`response_item` records (message / reasoning / custom_tool_call / custom_tool_call_output).
Reasoning is ENCRYPTED; only `summary` is readable. The model-visible tool output is
truncated (~5 K); the full stdout is in `event_msg:item_completed` (CommandExecution items),
itself capped at 1 MiB (observed: `seq 1 300000` → 1,048,606 chars).
File name uses LOCAL time; record timestamps are UTC. Titles: $CODEX_HOME/session_index.jsonl.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..jsonl import iter_jsonl
from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from .base import Adapter

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


class CodexAdapter(Adapter):
    name = "codex"
    label = "Codex CLI"
    env_home = "CODEX_HOME"
    default_home = "~/.codex"
    writable = True
    native_format = "Codex rollout .jsonl"
    question_tools = frozenset({"request_user_input"})  # the TUI's question pane; its answer comes back as JSON

    def _sessions_dir(self) -> Path:
        return self.home() / "sessions"

    def workspaces(self) -> list[str]:
        """cwd of every rollout, from its session_meta header line: one `readline()` per file, so
        the whole store is cheap to walk and no old workspace drops out of the picker."""
        out: list[str] = []
        for f in sorted(self._sessions_dir().rglob("rollout-*.jsonl"), reverse=True):
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    rec = json.loads(fh.readline() or "{}")
            except (OSError, json.JSONDecodeError):
                continue
            cwd = (rec.get("payload") or {}).get("cwd")
            if cwd and cwd not in out:
                out.append(cwd)
        return sorted(out)

    def available(self) -> bool:
        return self._sessions_dir().exists()

    def _titles(self) -> dict[str, str]:
        idx = self.home() / "session_index.jsonl"
        out: dict[str, str] = {}
        if idx.exists():
            for line in idx.read_text(encoding="utf-8-sig").splitlines():
                try:
                    r = json.loads(line)
                    out[r["id"]] = r.get("thread_name") or out.get(r["id"], "")
                except (json.JSONDecodeError, KeyError):
                    pass
        return out

    @staticmethod
    def _peek_project(f: Path) -> str | None:
        """The basename of the cwd in the `session_meta` header, the file's first line: one
        `readline()` per rollout, so the list's project column is filled at the cost `workspaces()`
        already pays."""
        try:
            with open(f, encoding="utf-8-sig") as fh:
                rec = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            return None
        cwd = (rec.get("payload") or {}).get("cwd") if isinstance(rec, dict) else None
        return Path(cwd).name if cwd else None

    def discover(self) -> list[SessionRef]:
        titles = self._titles()
        out = []
        for f in self._sessions_dir().rglob("rollout-*.jsonl"):
            m = _UUID.search(f.name)
            if not m:
                continue
            sid = m.group(1)
            st = f.stat()
            out.append(SessionRef(self.name, sid, f, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                  st.st_size, project=self._peek_project(f), title=titles.get(sid)))
        return out

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        rel = ref.path.relative_to(self._sessions_dir())
        out = [(ref.path, f"sessions/{rel.as_posix()}")]
        idx = self.home() / "session_index.jsonl"
        if idx.exists():
            out.append((idx, "session_index.jsonl"))
        return out

    def load(self, ref: SessionRef) -> Session:
        msgs: list[Message] = []
        cwd = version = model = None
        started = ended = None
        pending_tokens: TokenUsage | None = None
        full_outputs: list[dict] = []
        bad: list[int] = []
        for _, rec in iter_jsonl(ref.path, bad):
            t, p = rec.get("type"), rec.get("payload")
            if not isinstance(p, dict):           # a record of an unexpected shape is skipped, not fatal
                p = {}
            ts = parse_ts(rec.get("timestamp"))
            if t == "session_meta":
                cwd, version = p.get("cwd"), p.get("cli_version")
                started = parse_ts(p.get("timestamp")) or ts
                continue
            if t == "turn_context":
                model = p.get("model") or model
                continue
            if t == "token_usage_record":
                u = p.get("usage") or {}
                tok = TokenUsage(input=u.get("input_tokens"), output=u.get("output_tokens"),
                                 cached=u.get("cached_input_tokens"),
                                 thinking=u.get("reasoning_output_tokens"), total=u.get("total_tokens"))
                # attach to the most recent assistant message of this turn
                for m in reversed(msgs):
                    if m.role == "assistant":
                        m.tokens = tok
                        break
                continue
            if t == "event_msg" and p.get("type") == "item_completed":
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                if item.get("type") == "CommandExecution":
                    full_outputs.append({"command": item.get("command"), "exit_code": item.get("exit_code"),
                                         "output": item.get("aggregated_output") or item.get("stdout") or ""})
                continue
            if t != "response_item":
                continue
            ended = ts or ended
            pt = p.get("type")
            if pt == "message":
                role = p.get("role")
                text = "".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict))
                meta = {}
                r: str
                if role == "developer":
                    r, meta["env"] = "system", True
                elif role == "user":
                    r = "user"
                    if text.lstrip().startswith("<environment_context>"):
                        meta["env"] = True
                else:
                    r = "assistant"
                msgs.append(Message(len(msgs), r, ts, [ContentBlock("text", text)], id=p.get("id"),
                                    model=model if r == "assistant" else None, meta=meta))
            elif pt == "reasoning":
                summ = "\n".join(s.get("text", "") for s in p.get("summary", []) if isinstance(s, dict))
                b = ContentBlock("thinking", summ, meta={"encrypted": bool(p.get("encrypted_content"))})
                msgs.append(Message(len(msgs), "assistant", ts, [b], id=p.get("id"), model=model))
            elif pt in ("custom_tool_call", "function_call"):
                b = ContentBlock("tool_call", name=p.get("name"), call_id=p.get("call_id"),
                                 args=p.get("input") if pt == "custom_tool_call" else p.get("arguments"))
                msgs.append(Message(len(msgs), "assistant", ts, [b], id=p.get("id"), model=model))
            elif pt in ("custom_tool_call_output", "function_call_output"):
                out = p.get("output")
                if isinstance(out, str):
                    try:
                        parts = json.loads(out)
                        if isinstance(parts, list):
                            out = "".join(x.get("text", "") for x in parts if isinstance(x, dict))
                    except json.JSONDecodeError:
                        pass
                elif isinstance(out, list):
                    out = "".join(x.get("text", "") for x in out if isinstance(x, dict))
                b = ContentBlock("tool_result", out or "", call_id=p.get("call_id"))
                msgs.append(Message(len(msgs), "tool", ts, [b], id=p.get("id")))
        # attach full command outputs to truncated results, in order of occurrence.
        # One pass builds call_id -> name; looking each one up by re-scanning every message made
        # this quadratic, and a Codex rollout runs to thousands of blocks.
        names = {b.call_id: b.name for m in msgs for b in m.blocks
                 if b.kind == "tool_call" and b.call_id}
        results = [b for m in msgs for b in m.blocks if b.kind == "tool_result"]
        fo = iter(full_outputs)
        for b in results:
            if "exec" in (names.get(b.call_id) or ""):
                item = next(fo, None)
                if item is None:
                    break
                b.meta["exit_code"] = item["exit_code"]
                if len(item["output"]) > len(b.text):
                    # D10, as for every other adapter: the full text replaces the stub, so brief mode and
                    # the cap apply to it. It used to sit in meta["full_output"], which no filter touches,
                    # and every names-only json export carried each truncated command's whole output.
                    b.meta["restored_from_output"] = len(item["output"]) - len(b.text)
                    b.text = item["output"]
                if item["exit_code"] not in (0, None):
                    b.is_error = True
        return Session(self.name, ref.id, ref.path, msgs, title=ref.title, project=Path(cwd).name if cwd else None,
                       cwd=cwd, model=model, cli_version=version, started=started, ended=ended,
                       meta={"skipped_lines": len(bad)} if bad else {})
