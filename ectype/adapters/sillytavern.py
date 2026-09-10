"""SillyTavern: <data>/<user>/chats/<Character>/<name>.jsonl  (+ "group chats/*.jsonl")

Line 0 is a header {user_name, character_name, create_date, chat_metadata}; every later line is
one message {name, is_user, is_system, send_date, mes, extra}. A regenerated/edited reply also
carries `swipes[]` + `swipe_id`, SillyTavern's branching. The shown text is `swipes[swipe_id]`;
the alternatives are kept as `alternatives` on the block's meta so nothing is lost.

`send_date` is LOCAL and human-formatted ("September 04, 2026 01:17PM"); there is no timezone in
the file, so it is parsed as local time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..jsonl import read_jsonl
from ..model import ContentBlock, Message, Session, SessionRef
from .base import Adapter

_FMTS = ("%B %d, %Y %I:%M%p", "%B %d, %Y %H:%M", "%Y-%m-%d@%Hh%Mm%Ss")


def _ts(value) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    for f in _FMTS:
        try:
            return datetime.strptime(value.strip(), f).astimezone().astimezone(timezone.utc)
        except ValueError:
            continue
    return None


class SillyTavernAdapter(Adapter):
    name = "sillytavern"
    category = "chat"
    label = "SillyTavern"
    env_home = "ECTYPE_SILLYTAVERN_HOME"
    default_home = "~/SillyTavern/data"          # the usual checkout location; set ECTYPE_SILLYTAVERN_HOME or Settings otherwise

    def _user_dirs(self) -> list[Path]:
        home = self.home()
        return [d for d in sorted(home.iterdir()) if d.is_dir() and (d / "chats").is_dir()] if home.exists() else []

    def available(self) -> bool:
        return bool(self._user_dirs())

    def discover(self) -> list[SessionRef]:
        out = []
        for udir in self._user_dirs():
            for root, label in ((udir / "chats", None), (udir / "group chats", "group")):
                if not root.is_dir():
                    continue
                files = root.glob("*/*.jsonl") if label is None else root.glob("*.jsonl")
                for f in files:
                    st = f.stat()
                    char = f.parent.name if label is None else "(group)"
                    out.append(SessionRef(self.name, f"{char}/{f.stem}", f,
                                          datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                          st.st_size, project=char, title=f.stem))
        return out

    def find(self, id_prefix: str) -> list[SessionRef]:
        p = id_prefix.lower()
        return [r for r in self.discover() if r.id.lower().startswith(p) or p in r.id.lower()]

    def load(self, ref: SessionRef) -> Session:
        recs, bad = read_jsonl(ref.path)
        header = recs[0] if recs and "character_name" in recs[0] else {}
        body = recs[1:] if header else recs
        msgs: list[Message] = []
        for r in body:
            text = r.get("mes", "") or ""
            swipes = r.get("swipes") or []
            meta = {}
            if swipes:
                idx = r.get("swipe_id", 0)
                if 0 <= idx < len(swipes):
                    text = swipes[idx]
                meta["alternatives"] = [s for i, s in enumerate(swipes) if i != r.get("swipe_id", 0)]
                meta["swipe"] = f"{r.get('swipe_id', 0) + 1}/{len(swipes)}"
            role = "user" if r.get("is_user") else ("system" if r.get("is_system") else "assistant")
            kind = "system" if role == "system" else "text"
            msgs.append(Message(len(msgs), role, _ts(r.get("send_date")),
                                [ContentBlock(kind, text, meta=meta)],
                                meta={"name": r.get("name")}))
        return Session(self.name, ref.id, ref.path, msgs,
                       title=header.get("character_name") or ref.title, project=ref.project,
                       started=_ts(header.get("create_date")) or (msgs[0].timestamp if msgs else None),
                       ended=msgs[-1].timestamp if msgs else None,
                       meta={"user_name": header.get("user_name"), "chat_metadata": header.get("chat_metadata"),
                             **({"skipped_lines": len(bad)} if bad else {})})
