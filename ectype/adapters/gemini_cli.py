"""Gemini CLI: ~/.gemini/tmp/<project>/chats/session-<local-time>-<id8>.jsonl

A MUTATION LOG, not a message list:
  line 0            {sessionId, projectHash, startTime, lastUpdated, kind}
  {"$set": {...}}   patch of the session object; when it carries `messages` it is a FULL
                    snapshot of the array at that moment
  {id, type, ...}   a message; the SAME id can be re-emitted with more data (tool call
                    finished); the LAST record for an id wins
Tool calls + results are embedded in the gemini message (`toolCalls[]`). Outputs over the
limit are truncated inline and the full text is written to
<project>/tool-outputs/session-<id>/<callId>.txt.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..jsonl import iter_jsonl
from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from .base import TITLE_CHARS, Adapter

# to the end of the line, not to the first space: a spill path under a home with a space in it
# must still resolve (see the Claude Code adapter's note on the same pattern)
_SPILL = re.compile(r"For full output see:[ \t]*([^\r\n<]+?)[ \t]*(?:<|\r?\n|$)")


class GeminiCliAdapter(Adapter):
    name = "gemini-cli"
    label = "Gemini CLI"
    env_home = "ECTYPE_GEMINI_HOME"
    default_home = "~/.gemini/tmp"
    writable = True
    native_format = "Gemini CLI chat .jsonl"
    question_tools = frozenset({"ask_user"})           # ASK_USER_TOOL_NAME in the CLI's bundle (0.58)

    def workspaces(self) -> list[str]:
        """Each project dir records its real path in `.project_root`."""
        out: list[str] = []
        try:
            projects = sorted(self.home().iterdir())
        except OSError:
            return out
        for proj in projects:
            root = proj / ".project_root"
            if not root.is_file():
                continue
            try:
                cwd = root.read_text(encoding="utf-8-sig").strip()
            except OSError:
                continue
            if cwd and cwd not in out:
                out.append(cwd)
        return sorted(out)

    def discover(self) -> list[SessionRef]:
        out = []
        for proj in sorted(self.home().iterdir()):
            chats = proj / "chats"
            if not chats.is_dir():
                continue
            root = proj / ".project_root"
            label = Path(root.read_text(encoding="utf-8-sig").strip()).name if root.exists() else proj.name
            for f in chats.glob("session-*.jsonl"):
                st = f.stat()
                sid, title = self._peek(f)              # one open for both, not two
                out.append(SessionRef(self.name, sid or f.stem, f,
                                      datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), st.st_size, project=label,
                                      title=title))
        return out

    @staticmethod
    def _peek(f: Path, max_lines: int = 40) -> tuple[str | None, str | None]:
        """(sessionId, first real user prompt), scanning at most a few lines.

        Both come out of the same handful of lines, and `discover()` used to open the file once
        for each: twice per session, for every session in every project."""
        sid = title = None
        try:
            with open(f, encoding="utf-8-sig") as fh:
                for i, line in enumerate(fh):
                    if i >= max_lines:
                        break
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(r, dict):
                        continue
                    if i == 0:
                        sid = r.get("sessionId") or sid
                    if title is not None or len(line) > 200_000:
                        continue                       # id found on line 0; a huge line is a snapshot
                    setv = r.get("$set")
                    cands = (setv.get("messages") or []) if isinstance(setv, dict) else [r]
                    for m in cands:
                        if isinstance(m, dict) and m.get("type") == "user":
                            t = GeminiCliAdapter._parts(m.get("content")).strip()
                            if t and not t.startswith("<session_context>"):
                                title = t.splitlines()[0][:TITLE_CHARS]
                                break
        except OSError:
            pass
        return sid, title

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        proj = ref.path.parent.parent
        p = proj.name
        out = [(ref.path, f"{p}/chats/{ref.path.name}")]
        root = proj / ".project_root"
        if root.exists():
            out.append((root, f"{p}/.project_root"))
        spill = proj / "tool-outputs" / f"session-{ref.id}"
        for f in sorted(spill.glob("*")) if spill.exists() else []:
            out.append((f, f"{p}/tool-outputs/session-{ref.id}/{f.name}"))
        return out

    def load(self, ref: SessionRef) -> Session:
        header: dict = {}
        by_id: dict[str, dict] = {}
        order: list[str] = []
        bad: list[int] = []
        for i, rec in iter_jsonl(ref.path, bad):
            if i == 1 and "sessionId" in rec:        # line numbers are 1-based
                header = rec
                continue
            if "$set" in rec:
                s = rec["$set"]
                if isinstance(s.get("messages"), list):   # full snapshot → replace
                    by_id, order = {}, []
                    for m in s["messages"]:
                        self._upsert(by_id, order, m)
                if "lastUpdated" in s:
                    header["lastUpdated"] = s["lastUpdated"]
                continue
            if "id" in rec and "type" in rec:
                self._upsert(by_id, order, rec)
        spill_dir = ref.path.parent.parent / "tool-outputs" / f"session-{ref.id}"
        msgs: list[Message] = []
        model = None
        cwd = None
        root = ref.path.parent.parent / ".project_root"
        if root.exists():
            cwd = root.read_text(encoding="utf-8-sig").strip()
        for mid in order:
            r = by_id[mid]
            ts = parse_ts(r.get("timestamp"))
            t = r.get("type")
            if t == "user":
                text = self._parts(r.get("content"))
                meta = {"env": True} if text.lstrip().startswith("<session_context>") else {}
                msgs.append(Message(len(msgs), "user", ts, [ContentBlock("text", text)], id=mid, meta=meta))
            elif t == "gemini":
                model = r.get("model") or model
                blocks: list[ContentBlock] = []
                for th in r.get("thoughts") or []:
                    subj, desc = th.get("subject", ""), th.get("description", "")
                    blocks.append(ContentBlock("thinking", f"{subj}\n{desc}".strip()))
                text = self._parts(r.get("content"))
                if text:
                    blocks.append(ContentBlock("text", text))
                results: list[ContentBlock] = []
                for tc in r.get("toolCalls") or []:
                    blocks.append(ContentBlock("tool_call", name=tc.get("name"), call_id=tc.get("id"), args=tc.get("args"),
                                               meta={"display_name": tc.get("displayName")}))
                    results.append(self._result(tc, spill_dir))
                tk = r.get("tokens") or {}
                tok = TokenUsage(input=tk.get("input"), output=tk.get("output"), cached=tk.get("cached"),
                                 thinking=tk.get("thoughts"), total=tk.get("total")) if tk else None
                msgs.append(Message(len(msgs), "assistant", ts, blocks, id=mid, model=r.get("model"), tokens=tok))
                if results:
                    msgs.append(Message(len(msgs), "tool", ts, results, id=f"{mid}#results"))
            elif t in ("info", "error"):
                # the CLI's own notices (an API error, a cancelled tool): agent notices, like Claude
                # Code's `system` records, so the same toggle governs them (ARCHITECTURE D19)
                text = r.get("content") if isinstance(r.get("content"), str) else self._parts(r.get("content"))
                msgs.append(Message(len(msgs), "system", ts, [ContentBlock(t, text, meta={"subtype": t})], id=mid,
                                    meta={"agent_notice": t}))
        return Session(self.name, ref.id, ref.path, msgs, title=self._title(msgs), project=ref.project, cwd=cwd,
                       model=model, started=parse_ts(header.get("startTime")), ended=parse_ts(header.get("lastUpdated")),
                       meta={"project_hash": header.get("projectHash"), "kind": header.get("kind"),
                             **({"skipped_lines": len(bad)} if bad else {})})

    @staticmethod
    def _upsert(by_id: dict, order: list, m: dict) -> None:
        if not isinstance(m, dict):                # one malformed entry in a snapshot must not sink the session
            return
        mid = m.get("id")
        if not mid:
            return
        if mid not in by_id:
            order.append(mid)
        by_id[mid] = m

    @staticmethod
    def _parts(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return "" if content is None else str(content)

    @staticmethod
    def _result(tc: dict, spill_dir: Path) -> ContentBlock:
        text, err = "", tc.get("status") == "error"
        res = tc.get("result")
        if isinstance(res, list):
            for part in res:
                fr = (part or {}).get("functionResponse", {}) if isinstance(part, dict) else {}
                resp = fr.get("response") or {}
                if "error" in resp:
                    text += str(resp["error"]); err = True
                elif "output" in resp:
                    text += str(resp["output"])
        elif isinstance(res, str):
            text = res
        if not text and isinstance(tc.get("resultDisplay"), str):
            text = tc["resultDisplay"]
        b = ContentBlock("tool_result", text, call_id=tc.get("id"), is_error=err)
        m = _SPILL.search(text)
        if m:
            p = Path(m.group(1))
            b.spill_path = p if p.exists() else (spill_dir / p.name if (spill_dir / p.name).exists() else p)
            # Read the full output back in, the same policy as Claude Code's tool-results: the
            # transcript keeps a 40 KB head and a pointer, and the pointer alone left `full` mode
            # short by the rest (one spill here is 2 MB against 40 KB inline). `replace`, not
            # strict: one invalid byte must not kill every export of the session.
            if b.spill_path.is_file():
                try:
                    full = b.spill_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    b.truncated = True                  # pointed somewhere we cannot read
                    return b
                b.meta["restored_from_spill"] = len(full) - len(text)
                b.text = full
            else:
                b.truncated = True
        return b

    @staticmethod
    def _title(msgs: list[Message]) -> str | None:
        for m in msgs:
            if m.role == "user" and not m.is_env:
                return m.texts().strip().splitlines()[0][:TITLE_CHARS] if m.texts().strip() else None
        return None
