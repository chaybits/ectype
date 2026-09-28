"""Cursor: `~/.config/Cursor/User/globalStorage/state.vscdb`  (SQLite, WAL mode)

The only SQLite-backed store here whose rows are readable JSON:

  table `composerHeaders`   one row per conversation ("composer"): composerId, workspaceId,
                            createdAt / lastUpdatedAt (ms), isArchived, isSubagent,
                            value = {"type":"head", "name", ...}
  table `cursorDiskKV`      key/value rows:
      composerData:<composerId>              the conversation object; `fullConversationHeadersOnly`
                                             is the ORDERED list of {bubbleId, type, createdAt}
      bubbleId:<composerId>:<bubbleId>       one message ("bubble"): type 1 = user, 2 = assistant,
                                             text, thinking {text, signature},
                                             toolFormerData {name, status, params|rawArgs, result, error},
                                             tokenCount {inputTokens, outputTokens}, createdAt (ISO)
      agentKv:blob:<sha>                     content-addressed blobs (terminal output etc.)

An assistant turn is split into several bubbles (one per thinking block, tool call and text
segment), so consecutive type-2 bubbles are merged into ONE assistant Message here, with the
tool result following as a `tool` Message. Tool output is stored inline and untruncated (the
`seq 1 300000` probe left a 1.1 MB result in a single row).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from .base import Adapter, app_config_dir, scratch_dir


class CursorAdapter(Adapter):
    name = "cursor"
    label = "Cursor"
    env_home = "ECTYPE_CURSOR_HOME"
    default_home = app_config_dir("Cursor") + "/globalStorage"

    def _db(self) -> Path:
        return self.home() / "state.vscdb"

    def available(self) -> bool:
        return self._db().exists()

    def _connect(self) -> sqlite3.Connection:
        # read-only URI; WAL side files are honoured automatically
        return sqlite3.connect(self._db().resolve().as_uri() + "?mode=ro", uri=True, timeout=5)

    def discover(self) -> list[SessionRef]:
        out = []
        db = self._db()
        if not db.exists():
            return out
        st = db.stat()
        size, db_mtime = st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        try:
            con = self._connect()
        except sqlite3.Error:
            return out                                # an unreadable store lists nothing and breaks nothing
        try:
            rows = con.execute("select composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, "
                               "isSubagent, value from composerHeaders").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for cid, ws, created, updated, archived, sub, value in rows:
            if cid == "empty-state-draft":
                continue
            title = None
            try:
                head = json.loads(value)
                title = head.get("name") if isinstance(head, dict) else None
            except (TypeError, ValueError):
                pass
            if not title:
                continue                                  # drafts with no content
            out.append(SessionRef(self.name, cid, db, parse_ts(updated or created) or db_mtime,
                                  size, project=ws, title=title))
        return out

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        """Export just this conversation into a temporary single-composer state.vscdb."""
        tmp = scratch_dir("ectype-cursor-") / "state.vscdb"
        src = self._connect()
        try:
            dst = sqlite3.connect(tmp)
            try:
                dst.execute("create table ItemTable (key text primary key, value blob)")
                dst.execute("create table cursorDiskKV (key text primary key, value blob)")
                cols = [c[1] for c in src.execute("pragma table_info(composerHeaders)")]
                dst.execute(f"create table composerHeaders ({', '.join(cols)})")
                for row in src.execute("select * from composerHeaders where composerId=?", (ref.id,)):
                    dst.execute(f"insert into composerHeaders values ({','.join('?' * len(cols))})", row)
                for k, v in src.execute("select key, value from cursorDiskKV where key=? or key like ? or key like ?",
                                        (f"composerData:{ref.id}", f"bubbleId:{ref.id}:%", f"checkpointId:{ref.id}:%")):
                    dst.execute("insert into cursorDiskKV values (?, ?)", (k, v))
                dst.commit()
            finally:
                dst.close()                          # on the failure path too
        finally:
            src.close()
        return [(tmp, "state.vscdb")]

    def load(self, ref: SessionRef) -> Session:
        con = self._connect()
        try:
            row = con.execute("select value from cursorDiskKV where key=?", (f"composerData:{ref.id}",)).fetchone()
            data = json.loads(row[0]) if row else {}
            order = [h.get("bubbleId") if isinstance(h, dict) else h for h in data.get("fullConversationHeadersOnly") or []]
            if not order:                                 # fallback: every bubble of this composer, by time
                order = [k.split(":", 2)[2] for (k,) in con.execute(
                    "select key from cursorDiskKV where key like ?", (f"bubbleId:{ref.id}:%",))]
            # every bubble of this composer in ONE query, then put back into `order`. A query per
            # bubble meant hundreds of index lookups into a database that is hundreds of megabytes.
            raw: dict[str, object] = {}
            for k, v in con.execute("select key, value from cursorDiskKV where key like ?",
                                    (f"bubbleId:{ref.id}:%",)):
                raw[k.split(":", 2)[2]] = v
        finally:
            con.close()                                   # a corrupt row must not leak the handle
        bubbles = []
        for bid in order:
            v = raw.get(bid)
            if v is None:
                continue
            try:
                bubbles.append(json.loads(v))
            except ValueError:
                pass
        if not data.get("fullConversationHeadersOnly"):
            bubbles.sort(key=lambda b: str(b.get("createdAt") or ""))

        model = ((data.get("modelConfig") or {}).get("modelName")) or None
        msgs: list[Message] = []
        cur: list[ContentBlock] = []          # assistant blocks being accumulated
        cur_ts: datetime | None = None
        cur_tok = TokenUsage()

        def flush():
            nonlocal cur, cur_ts, cur_tok
            if cur:
                tok = cur_tok if (cur_tok.input or cur_tok.output) else None
                if tok:
                    tok.total = (tok.input or 0) + (tok.output or 0)
                msgs.append(Message(len(msgs), "assistant", cur_ts, cur, model=model, tokens=tok))
            cur, cur_ts, cur_tok = [], None, TokenUsage()

        for b in bubbles:
            ts = parse_ts(b.get("createdAt"))
            if b.get("type") == 1:
                flush()
                text = b.get("text") or ""
                if text.strip():
                    msgs.append(Message(len(msgs), "user", ts, [ContentBlock("text", text)], id=b.get("bubbleId")))
                continue
            # assistant bubble
            cur_ts = cur_ts or ts
            tc = b.get("tokenCount") or {}
            cur_tok.input = (cur_tok.input or 0) + (tc.get("inputTokens") or 0)
            cur_tok.output = (cur_tok.output or 0) + (tc.get("outputTokens") or 0)
            th = b.get("thinking")
            if isinstance(th, dict) and th.get("text"):
                cur.append(ContentBlock("thinking", th["text"]))
            tf = b.get("toolFormerData")
            if isinstance(tf, dict) and tf.get("name"):
                args = tf.get("params") or tf.get("rawArgs")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                call_id = str(tf.get("toolCallId") or b.get("bubbleId") or "")
                cur.append(ContentBlock("tool_call", name=tf.get("name"), call_id=call_id, args=args,
                                        meta={"tool_code": tf.get("tool")}))
                res = tf.get("result")
                text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False) if res is not None else ""
                if isinstance(res, dict) and isinstance(res.get("contents"), str):
                    text = res["contents"]
                err = str(tf.get("status") or "").lower() == "error" or bool(tf.get("error"))
                if tf.get("error") and not text:
                    text = str(tf["error"])
                flush()
                msgs.append(Message(len(msgs), "tool", ts, [ContentBlock("tool_result", text, call_id=call_id, is_error=err)]))
                continue
            text = b.get("text") or ""
            if text.strip():
                cur.append(ContentBlock("text", text))
        flush()
        return Session(self.name, ref.id, ref.path, msgs, title=data.get("name") or ref.title,
                       project=ref.project, model=model,
                       started=parse_ts(data.get("createdAt")), ended=parse_ts(data.get("lastUpdatedAt")),
                       meta={"mode": data.get("unifiedMode"), "status": data.get("status"),
                             "bubbles": len(bubbles)})
