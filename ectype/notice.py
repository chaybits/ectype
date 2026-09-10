"""The import notice: a final message telling a model that this conversation was imported, so it
re-checks location, date and time, operating system and tools before acting on anything in it.

Appended (when enabled) as the LAST message of text/markdown/html/json exports and of every
conversion, because the end of the context is what a model reads last. Generic by default; the
detailed form lists one bullet per item the user selected in Settings → Import notice.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from .model import ContentBlock, Message, Session

ITEMS: dict[str, tuple[str, str]] = {          # key -> (label, sentence)
    "paths": ("paths and working directory",
              "File paths and the working directory belong to the machine this was recorded on; check that they exist here before using them."),
    "time": ("date and time",
             "It was recorded on {date}; today's date, time and timezone may differ, so nothing time-based in it can be assumed."),
    "os": ("operating system and tools",
           "The operating system, shell, installed tools and their versions may differ from the original machine."),
    "tools": ("tool results",
              "Tool calls ran on the original machine; the same tools may be missing or behave differently here; re-run them rather than trust the old output."),
    "state": ("current state",
              "Files, repositories and services may have changed since; verify the current state before acting on anything the transcript says was done."),
}
GENERIC = ("Everything environment-specific in it is stale: re-check the working directory and paths, the date and "
           "time, the operating system and which tools are available before acting on anything.")


def build(s: Session, cfg: dict, agent_label: str | None = None) -> str:
    date = s.started.astimezone().date().isoformat() if s.started else "an unknown date"
    head = (f"[Import notice] This conversation was imported from {agent_label or s.agent} session {s.id[:8]} "
            f"(recorded {date}) into a different environment by AI Agent Ectype on {datetime.now().date().isoformat()}.")
    if not cfg.get("detailed"):
        return head + " " + GENERIC
    keys = [k for k in (cfg.get("items") or list(ITEMS)) if k in ITEMS] or list(ITEMS)
    return head + "\n" + "\n".join(f"- {ITEMS[k][1].format(date=date)}" for k in keys)


def message(s: Session, cfg: dict, agent_label: str | None = None) -> Message:
    return Message(index=len(s.messages), role="system", timestamp=datetime.now(timezone.utc),
                   blocks=[ContentBlock("text", build(s, cfg, agent_label))], meta={"notice": True})


def attach(s: Session, cfg: dict, agent_label: str | None = None) -> Session:
    """A copy of `s` with the notice appended as its last message."""
    out = copy.copy(s)
    out.messages = list(s.messages) + [message(s, cfg, agent_label)]
    return out
