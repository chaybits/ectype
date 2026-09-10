"""What a conversion does with each element, per source → target pair, and the verified targets:
everything the GUI's conversion table shows."""
from __future__ import annotations

from .native import NATIVE

# What a conversion does with each element. This depends on the PAIR: a session exported to its
# own agent is copied, not converted, so nothing is folded or dropped.
CARRY: list[tuple[str, str, str]] = [           # (element, fate, detail)
    ("your messages", "carried", "verbatim"),
    ("assistant text", "carried", "verbatim; consecutive same-role turns are merged into one"),
    ("tool calls", "folded", "become a bracketed note inside the assistant text: [tool call: name(args…)], arguments cut at 120 characters"),
    ("tool results", "folded", "first 200 characters as [tool result: …]; errors flagged ERROR"),
    ("thinking", "dropped", "no agent accepts foreign reasoning (Codex encrypts even its own)"),
    ("injected context", "dropped", "harness-specific; the target injects its own"),
    ("notices", "dropped", "system / info / error blocks"),
    ("images", "dropped", "not carried"),
    ("timestamps", "carried", "kept where the target stores them; re-stamped monotonically where it needs order"),
    ("cwd and project", "carried", "from the source; the model name comes from the target (template)"),
    ("import notice", "added", "a final message telling the model the session was imported (Settings → Import notice)"),
]
# Same agent in and out: the file already is the target format.
CARRY_NATIVE: list[tuple[str, str, str]] = [
    ("your messages", "carried", "byte-for-byte"),
    ("assistant text", "carried", "byte-for-byte, every turn separate"),
    ("tool calls", "carried", "real tool_use records, arguments intact"),
    ("tool results", "carried", "real tool_result records, full output"),
    ("thinking", "carried", "kept exactly as the agent stored it"),
    ("injected context", "carried", "kept"),
    ("notices", "carried", "kept"),
    ("images", "carried", "kept"),
    ("timestamps", "carried", "original values"),
    ("cwd and project", "carried", "the source's, or the workspace you pick"),
    ("sidecar files", "carried", "spill files the transcript points at are copied along and repointed"),
    ("session id", "changed", "a fresh id so the copy cannot collide with the original"),
    ("import notice", "added", "a final message telling the model the session was imported (Settings → Import notice)"),
]
TARGETS: dict[str, dict] = {
    # verified = when the live resume was last checked; ectype = this tool's version on that day
    "claude-code": {"label": "Claude Code", "verified": "2026-09-04", "version": "2.1", "ectype": "0.1.0",
                    "resume": "claude --resume <id>  (run from the source's cwd)",
                    "notes": "lands in ~/.claude/projects/<cwd-slug>/; a --template session gives a version-exact envelope"},
    "codex": {"label": "Codex CLI", "verified": "2026-09-04", "version": "0.153", "ectype": "0.1.0",
              "resume": "codex resume <id>",
              "notes": "writes the legacy session_meta header Codex reads directly; the modern paginated header loads but resumes empty"},
    "gemini-cli": {"label": "Gemini CLI", "verified": "2026-09-04", "version": "0.58", "ectype": "0.1.0",
                   "resume": "gemini --resume latest  (from the same project directory)",
                   "notes": "projectHash = sha256(cwd) unless a --template from that project is given; Gemini resumes by index, not id"},
}
SOURCE_NOTES: dict[str, str] = {
    "codex": "its reasoning is encrypted at the source; even the original file holds only summaries",
    "antigravity": "a failed tool call leaves no step; its result is inferred and flagged. Antigravity is source-only (resumable state lives in a protobuf SQLite blob)",
    "lmstudio": "LM Studio never stores tool output; results fold as (empty)",
    "aider": "console output is recorded as system messages, which a conversion drops; only user and assistant text carries",
    "gemini-cli": "big tool outputs live in tool-outputs/ spill files and are read from there",
    "copilot-chat": "terminal output may be missing where VS Code let the scrollback drop it",
    "roo-code": "per-message timestamps are the conversation file's own; the UI log supplies only the session end and the error count",
    "sillytavern": "only the selected swipe of each message is carried; alternatives are dropped",
    "open-webui": "only the current branch of the message tree is carried",
}


def matrix(source: str | None = None, target: str | None = None) -> dict:
    """Everything the GUI's conversion table shows, for one source → target pair.

    The pair matters: `claude-code → claude-code` is a copy, not a conversion, so every row says
    carried. Only a CROSS-agent pair goes through the canonical model and loses anything."""
    native = bool(source and target and source == target and source in NATIVE)
    rows = CARRY_NATIVE if native else CARRY
    return {"carry": [{"element": e, "fate": f, "detail": d} for e, f, d in rows],
            "native": native, "source": source, "target": target,
            "targets": TARGETS, "source_notes": SOURCE_NOTES,
            "native_agents": sorted(NATIVE),
            "summary": ("Same agent in and out: the file already is this format, so it is copied "
                        "record for record. Nothing is folded and nothing is dropped."
                        if native else
                        "Different agents: the conversation is normalised into the canonical model and "
                        "rewritten in the target's format, which has no place for foreign tool records "
                        "or reasoning. What that costs is listed below."),
            "disclaimer": "Verified on the dates and versions listed by installing a converted session and resuming it in the "
                          "real agent. Every format here is an undocumented internal of its agent and can change with any "
                          "release; treat this table as last known, not guaranteed."}
