"""Open WebUI: `<DATA_DIR>/webui.db` (SQLite), table `chat`

Columns: id, user_id, title, created_at / updated_at (epoch s), archived, pinned, meta (JSON),
current_message_id, chat (JSON). The `chat` JSON is the frontend's own object:

    {title, models[], params, tags[], timestamp,
     messages: [...],                            flat list = the CURRENT branch
     history: {currentId, messages: {id: {...}}}} a TREE: every node has parentId + childrenIds

Regenerating an answer or editing a prompt creates a sibling under the same parent, so the tree
is the branching structure and `messages[]` is just the path to `currentId`. This adapter walks
the tree from `currentId` back to the root (so it agrees with what the UI shows) and records
the other children as alternatives. Assistant nodes carry `usage` (prompt/completion tokens) and
`output` (the raw backend response); the model is per message (`model`/`modelName`).
The backend fills assistant nodes asynchronously; `done: false` with empty content means the
generation never finished. Chats driven through the API get assistant nodes upserted WITHOUT
`parentId`, so the tree can be broken while `messages[]` is intact; the adapter uses whichever
gives the longer branch.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from .base import Adapter, scratch_dir


class OpenWebUIAdapter(Adapter):
    name = "open-webui"
    category = "chat"
    label = "Open WebUI"
    env_home = "ECTYPE_OPENWEBUI_HOME"
    default_home = "~/.open-webui"           # DATA_DIR; the pip default is <site-packages>/open_webui/data

    def _db(self) -> Path:
        return self.home() / "webui.db"

    def available(self) -> bool:
        return self._db().exists()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db().resolve().as_uri() + "?mode=ro", uri=True, timeout=5)

    def discover(self) -> list[SessionRef]:
        db = self._db()
        if not db.exists():
            return []
        try:
            con = self._connect()
        except sqlite3.Error:
            return []                                 # an unreadable store lists nothing and breaks nothing
        try:
            rows = con.execute("select id, title, created_at, updated_at, length(chat) from chat "
                               "order by updated_at desc").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        return [SessionRef(self.name, cid, db,
                           parse_ts(upd or cre) or datetime.fromtimestamp(db.stat().st_mtime, tz=timezone.utc),
                           size or 0, title=title) for cid, title, cre, upd, size in rows]

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        """Export just this chat into a small webui.db with the same `chat` (and `user`) schema."""
        tmp = scratch_dir("ectype-owui-") / "webui.db"
        src = self._connect()
        try:
            dst = sqlite3.connect(tmp)
            try:
                for t in ("chat", "user"):
                    sql = src.execute("select sql from sqlite_master where type='table' and name=?", (t,)).fetchone()
                    if sql:
                        dst.execute(sql[0])
                cols = [c[1] for c in src.execute("pragma table_info(chat)")]
                for row in src.execute("select * from chat where id=?", (ref.id,)):
                    dst.execute(f"insert into chat values ({','.join('?' * len(cols))})", row)
                dst.commit()
            finally:
                dst.close()                          # on the failure path too
        finally:
            src.close()
        return [(tmp, "webui.db")]

    def load(self, ref: SessionRef) -> Session:
        con = self._connect()
        row = con.execute("select title, created_at, updated_at, chat, meta, current_message_id from chat where id=?",
                          (ref.id,)).fetchone()
        con.close()
        if not row:
            return Session(self.name, ref.id, ref.path, [], title=ref.title)
        title, created, updated, chat_json, meta_json, current = row
        chat = json.loads(chat_json) if chat_json else {}
        tree = (chat.get("history") or {}).get("messages") or {}
        head = current or (chat.get("history") or {}).get("currentId")
        # walk the current branch root-wards, then reverse
        # walk by KEY: the backend stores nodes without an "id" field (the dict key is the id)
        path: list[dict] = []
        seen: set[str] = set()
        key = head
        while key and key in tree and key not in seen:
            seen.add(key)
            node = dict(tree[key]); node.setdefault("id", key); path.append(node)
            key = node.get("parentId")
        path.reverse()
        # The backend's own upserts (API-driven chats) drop `parentId` on assistant nodes, which
        # truncates the walk; the UI keeps `messages[]` = current branch, so trust whichever is longer.
        flat = chat.get("messages") or []
        if len(flat) > len(path):
            # order from the flat list, DATA from the tree node (the list is the client's stale copy;
            # the backend upserts content/usage/output into the tree only)
            path = [{**m, **tree.get(m.get("id"), {}), "id": m.get("id")} for m in flat]
        msgs: list[Message] = []
        for n in path:
            role = n.get("role", "assistant")
            text = n.get("content") or ""
            blocks: list[ContentBlock] = []
            # `output` = the backend's raw response parts, in the order they happened. Reasoning
            # AND tool activity live ONLY here: a tool-enabled turn writes no message-level
            # `tool_calls` at all, which is why the pair below is read from `output` (verified
            # 2026-09-10 against a real tool run on Open WebUI 0.11.3: a `function_call` part
            # carrying name + arguments, then a `function_call_output` part with the same
            # `call_id`).
            for part in n.get("output") or []:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "reasoning":
                    rt = "".join(c.get("text", "") for c in part.get("content") or [] if isinstance(c, dict))
                    if rt.strip():
                        blocks.append(ContentBlock("thinking", rt.strip(), meta={"tags": (part.get("start_tag"), part.get("end_tag"))}))
                elif kind == "function_call":
                    blocks.append(ContentBlock("tool_call", name=part.get("name"),
                                               call_id=str(part.get("call_id") or part.get("id") or ""),
                                               args=part.get("arguments")))
                elif kind == "function_call_output":
                    out = part.get("output")
                    if isinstance(out, list):        # [{"type": "input_text", "text": …}, …]
                        texts = [c.get("text", "") for c in out if isinstance(c, dict) and "text" in c]
                        out = "\n".join(texts) if len(texts) == len(out) else json.dumps(out, ensure_ascii=False)
                    blocks.append(ContentBlock("tool_result",
                                               out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                                               call_id=str(part.get("call_id") or ""),
                                               # anything but "completed" is a failed call; the
                                               # part carries no error text of its own
                                               is_error=part.get("status") not in (None, "completed")))
            if text.strip():
                blocks.append(ContentBlock("text", text))
            if role == "assistant" and not text.strip():
                blocks.append(ContentBlock("error" if n.get("error") else "info",
                                           str(n.get("error") or ("(generation did not finish)" if n.get("done") is False else "(empty)"))))
            # tool traces, if the frontend stored any
            for tc in n.get("tool_calls") or n.get("toolCalls") or []:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                blocks.append(ContentBlock("tool_call", name=fn.get("name") or tc.get("name"),
                                           call_id=str(tc.get("id") or ""), args=fn.get("arguments")))
            for src in n.get("sources") or []:
                if isinstance(src, dict):
                    blocks.append(ContentBlock("tool_result", json.dumps(src, ensure_ascii=False),
                                               meta={"kind": "source"}))
            if not blocks:
                continue
            # siblings under the same parent = alternatives (regenerations / edits)
            parent = tree.get(n.get("parentId")) if n.get("parentId") else None
            if parent is not None and "childrenIds" not in parent:
                parent = None
            sibs = [s for s in (parent.get("childrenIds") if parent else []) if s != n.get("id")] if parent else []
            if sibs:
                blocks[0].meta["alternatives"] = [f"(sibling {s[:8]} not shown)" for s in sibs]
                kids = parent["childrenIds"] if parent else []
                # an upserted node can name a parent whose childrenIds never listed it (the tree is
                # kept by the frontend, the node by the backend): position 1, not a crash
                pos = kids.index(n["id"]) + 1 if n.get("id") in kids else 1
                blocks[0].meta["swipe"] = f"{pos}/{len(sibs) + 1}"
            u = n.get("usage") or {}
            tok = TokenUsage(input=u.get("prompt_tokens"), output=u.get("completion_tokens"),
                             total=u.get("total_tokens")) if u else None
            msgs.append(Message(len(msgs), "user" if role == "user" else ("system" if role == "system" else "assistant"),
                                parse_ts(n.get("timestamp")), blocks, id=n.get("id"),
                                model=n.get("model") if role == "assistant" else None, tokens=tok,
                                meta={"done": n.get("done")} if role == "assistant" else {}))
        models = chat.get("models") or []
        return Session(self.name, ref.id, ref.path, msgs, title=title or chat.get("title") or ref.title,
                       model=models[0] if models else None,
                       started=parse_ts(created), ended=parse_ts(updated),
                       meta={"models": models, "params": chat.get("params"), "tags": chat.get("tags"),
                             "branches": sum(1 for n in tree.values() if len(n.get("childrenIds") or []) > 1),
                             "meta": json.loads(meta_json) if meta_json else None})
