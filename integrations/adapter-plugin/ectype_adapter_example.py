"""Example ectype adapter plug-in: a folder of `*.chat.jsonl` files, one line per message.

    {"role": "user" | "assistant", "text": "...", "ts": "2026-01-02T03:04:05Z"}

This is the smallest adapter that does everything an adapter must: say where its store is, list
the sessions cheaply (`discover`), and turn one into the canonical model (`load`). Copy it, rename
it, and replace the two methods with your agent's format. The contract it meets is
`ectype.adapters.base.Adapter`; the shapes it returns are `ectype.model.SessionRef` and `Session`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ectype.adapters.base import TITLE_CHARS, Adapter
from ectype.jsonl import iter_jsonl
from ectype.model import ContentBlock, Message, Session, SessionRef, parse_ts


class ExampleAdapter(Adapter):
    name = "example"                       # the id on the command line: `ectype list -a example`
    label = "Example plug-in"              # what the web UI shows
    env_home = "ECTYPE_EXAMPLE_HOME"       # the env var that overrides the store location
    default_home = "~/.example-agent/chats"  # the store's usual place (~ and %VARS% are expanded)
    category = "chat"                      # "coding" agents are listed by default, "chat" apps are opt-in

    def discover(self) -> list[SessionRef]:
        """One SessionRef per file, without parsing whole transcripts."""
        out: list[SessionRef] = []
        for f in sorted(self.home().glob("*.chat.jsonl")):
            st = f.stat()
            title = None
            for _, rec in iter_jsonl(f):               # the first user line is the title
                if isinstance(rec, dict) and rec.get("role") == "user" and rec.get("text"):
                    title = str(rec["text"]).splitlines()[0][:TITLE_CHARS]   # the list view's cap, shared
                    break
            out.append(SessionRef(self.name, f.name[: -len(".chat.jsonl")], f,
                                  datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), st.st_size,
                                  title=title))
        return out

    def load(self, ref: SessionRef) -> Session:
        """The full parse into the canonical model."""
        msgs: list[Message] = []
        bad: list[int] = []
        for _, rec in iter_jsonl(ref.path, bad):
            if not isinstance(rec, dict) or not rec.get("text"):
                continue
            role = "user" if rec.get("role") == "user" else "assistant"
            msgs.append(Message(len(msgs), role, parse_ts(rec.get("ts")), [ContentBlock("text", str(rec["text"]))]))
        return Session(self.name, ref.id, ref.path, msgs, title=ref.title,
                       started=msgs[0].timestamp if msgs else None, ended=msgs[-1].timestamp if msgs else None,
                       meta={"skipped_lines": len(bad)} if bad else {})
