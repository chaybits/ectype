"""The canonical session model.

Every adapter converts its vendor format INTO these dataclasses; everything downstream
(filters, redaction, rendering, the web UI, conversion) works ONLY on these. No vendor
field name may appear outside `ectype/adapters/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

BlockKind = Literal["text", "thinking", "tool_call", "tool_result", "system", "info", "error", "image"]
Role = Literal["user", "assistant", "system", "tool"]


@dataclass
class TokenUsage:
    """Token accounting as the agent recorded it (None = the agent does not record it)."""
    input: int | None = None
    output: int | None = None
    cached: int | None = None
    thinking: int | None = None
    total: int | None = None


@dataclass
class ContentBlock:
    kind: BlockKind
    text: str = ""
    # tool_call
    name: str | None = None
    call_id: str | None = None
    args: Any = None
    # tool_result
    is_error: bool = False
    truncated: bool = False
    spill_path: Path | None = None     # where the agent kept the FULL output, if anywhere
    # anything vendor-specific worth keeping but not modelling
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Message:
    index: int
    role: Role
    timestamp: datetime | None            # timezone-aware, UTC
    blocks: list[ContentBlock] = field(default_factory=list)
    id: str | None = None
    model: str | None = None
    tokens: TokenUsage | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def texts(self, kinds: tuple[str, ...] = ("text",)) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.kind in kinds and b.text)

    @property
    def is_env(self) -> bool:
        """True for injected environment/system-context messages the user never typed."""
        return bool(self.meta.get("env"))


@dataclass
class SessionRef:
    """Cheap handle produced by discover(); load() turns it into a Session."""
    agent: str
    id: str
    path: Path
    mtime: datetime
    size: int
    project: str | None = None
    title: str | None = None

    @property
    def short(self) -> str:
        return self.id[:8]


@dataclass
class Session:
    agent: str
    id: str
    path: Path
    messages: list[Message]
    title: str | None = None
    project: str | None = None
    cwd: str | None = None
    model: str | None = None
    cli_version: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # ---- convenience -------------------------------------------------------
    def count(self, kind: str) -> int:
        return sum(1 for m in self.messages for b in m.blocks if b.kind == kind)

    def by_role(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.messages:
            out[m.role] = out.get(m.role, 0) + 1
        return out

    @property
    def total_tokens(self) -> int | None:
        """Sum of agent-reported totals, or None if the agent reports nothing."""
        vals = [m.tokens.total for m in self.messages if m.tokens and m.tokens.total is not None]
        return sum(vals) if vals else None

    @property
    def peak_context(self) -> int | None:
        """Largest input+cached any single turn reported: the agent's own context window at that
        turn (system prompt, tool definitions and the conversation as the agent saw it). None when
        the agent records no per-turn input. Semantics are the agent's; treat as agent-reported."""
        vals = [(m.tokens.input or 0) + (m.tokens.cached or 0) for m in self.messages
                if m.tokens and (m.tokens.input or m.tokens.cached)]
        return max(vals) if vals else None


def json_default(x: Any) -> str:
    """`json.dumps(..., default=json_default)` for anything in this model.

    The only non-JSON types the dataclasses hold are datetime and Path. This lived in three
    copies (the CLI's `json` command, the export formats and the web server), which is how two of
    them came to handle Path and one did not."""
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, Path):
        return str(x)
    return str(x)


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 (with Z or offset) or epoch seconds/ms -> aware UTC datetime."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:
                value = value / 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        s = str(value).strip()
        if s.isdigit():                      # epoch as a STRING (Continue, Roo history_item)
            n = int(s)
            return datetime.fromtimestamp(n / 1000 if n > 1e12 else n, tz=timezone.utc)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
