"""The lossy half: flatten a canonical Session into user/assistant turns, counting every decision.

Used by every cross-agent writer and by `folded()` (the same fold written as a generic format).
"""
from __future__ import annotations

import copy as _copy
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..model import ContentBlock, Message, Session


# --------------------------------------------------------------------------- fidelity
@dataclass
class FidelityReport:
    source: str
    target: str
    kept: Counter = field(default_factory=Counter)      # user_text, assistant_text
    folded: Counter = field(default_factory=Counter)    # tool_call, tool_result
    dropped: Counter = field(default_factory=Counter)   # thinking, env, system, info, error, image
    merged: int = 0                                     # consecutive same-role messages merged
    notes: list[str] = field(default_factory=list)
    # the id the written session carries, set by every writer and by the native copy. The file
    # name is not it: a Codex rollout is `rollout-<time>-<uuid>.jsonl`, and a caller that took the
    # file's stem for the id asked Codex to resume a file name.
    new_id: str | None = None

    def render(self) -> str:
        def row(c: Counter) -> str:
            return ", ".join(f"{k} {v}" for k, v in c.items()) or "-"
        return "\n".join([
            f"# Fidelity report: {self.source} → {self.target}",
            f"kept:    {row(self.kept)}",
            f"folded:  {row(self.folded)}   (tool activity summarised inside assistant text)",
            f"dropped: {row(self.dropped)}",
            f"merged:  {self.merged} consecutive same-role messages",
            *[f"note:    {n}" for n in self.notes],
        ]) + "\n"


# --------------------------------------------------------------------------- flattening
@dataclass
class Turn:
    role: str            # user | assistant
    text: str
    timestamp: datetime | None


def _fold_args(args) -> str:
    if args is None:
        return ""
    s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= 120 else s[:117] + "..."


def flatten(s: Session, rep: FidelityReport, banner: bool = True, excerpt: int = 200) -> list[Turn]:
    turns: list[Turn] = []
    if banner:
        turns.append(Turn("user", f"[Imported from {s.agent} session {s.id} by AI Agent Ectype on "
                                  f"{datetime.now(timezone.utc).date()}. Tool activity appears as bracketed notes. "
                                  f"Continue the conversation from where it left off.]", s.started))
    for m in s.messages:
        if m.is_env:
            rep.dropped["env"] += 1
            continue
        if m.role == "system":
            rep.dropped["system"] += 1
            continue
        parts: list[str] = []
        for b in m.blocks:
            if b.kind == "text":
                if b.text.strip():
                    parts.append(b.text.strip())
                    rep.kept[f"{m.role}_text" if m.role in ("user", "assistant") else m.role] += 1
            elif b.kind == "thinking":
                rep.dropped["thinking"] += 1
            elif b.kind == "tool_call":
                parts.append(f"[tool call: {b.name}({_fold_args(b.args)})]")
                rep.folded["tool_call"] += 1
            elif b.kind == "tool_result":
                t = b.text.strip().replace("\n", " ")
                if len(t) > excerpt:
                    t = t[:excerpt] + f"... (+{len(b.text) - excerpt:,} chars)"
                flag = " ERROR" if b.is_error else ""
                if b.meta.get("brief") is not None:        # a names-only view hid it; it was not empty
                    parts.append(f"[tool result{flag}: not included in this view]")
                else:
                    parts.append(f"[tool result{flag}: {t}]" if t else f"[tool result{flag}: (empty)]")
                rep.folded["tool_result"] += 1
            elif b.kind in ("system", "info", "error"):
                rep.dropped[b.kind] += 1
            elif b.kind == "image":
                rep.dropped["image"] += 1
        if not parts:
            continue
        role = "assistant" if m.role in ("assistant", "tool") else "user"
        text = "\n".join(parts)
        if turns and turns[-1].role == role and not banner_is(turns[-1]):
            turns[-1].text += "\n\n" + text
            rep.merged += 1
        else:
            turns.append(Turn(role, text, m.timestamp))
    return turns


def banner_is(t: Turn) -> bool:
    return t.text.startswith("[Imported from ")


def folded(s: Session, target: str, rep: FidelityReport, banner: bool = True, notice: str | None = None) -> Session:
    """The conversation exactly as a conversion to `target` would write it, as a canonical Session.

    Same fold as `convert()`: user and assistant text verbatim, tool activity as bracketed notes,
    thinking and injected context dropped, the import banner first and the notice last. The
    difference is what happens next: instead of the target's file envelope, this goes through a
    generic format, a 'Codex-style text' you feed to the agent yourself."""
    turns = flatten(s, rep, banner=banner)
    if notice:
        turns.append(Turn("user", notice, s.ended or s.started))
        rep.notes.append("import notice appended as the final user message")
    out = _copy.copy(s)
    out.messages = [Message(i, t.role, t.timestamp, [ContentBlock("text", t.text)], meta={"folded": True})
                    for i, t in enumerate(turns)]
    out.meta = {**s.meta, "folded_for": target}
    return out
