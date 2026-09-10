"""Convert a canonical Session into another agent's on-disk format.

Strategy: the TARGET agent writes its own envelope. Give `convert` a real (dummy) session of
the target agent as `template`; the writer copies its envelope fields (cwd, version, ids,
flags it doesn't understand) and swaps in the conversation. Without a template the writer
uses minimal defaults that were valid for the versions observed while writing this.

v0.1 ships MESSAGES mode: user and assistant text only. Tool calls/results are folded into
short bracketed notes inside the assistant text; thinking, injected context and system
notices are dropped. Every decision is counted in the FidelityReport; nothing is dropped
silently. Antigravity is source-only (its resumable state is a protobuf SQLite blob).
"""
from __future__ import annotations

import copy as _copy
from collections import Counter
from pathlib import Path

from ..model import Session
from .common import _iso, _read_jsonl, _slug, _uuid7, _write_jsonl          # noqa: F401, re-exported
from .fold import FidelityReport, Turn, _fold_args, banner_is, flatten, folded  # noqa: F401
from .tables import CARRY, CARRY_NATIVE, SOURCE_NOTES, TARGETS, matrix          # noqa: F401
from .native import NATIVE, _native_notice_record, native_copy                   # noqa: F401
from .writers import WRITERS, write_claude, write_codex, write_gemini             # noqa: F401

__all__ = ["FidelityReport", "Turn", "flatten", "folded", "WRITERS", "NATIVE", "native_copy",
           "CARRY", "CARRY_NATIVE", "TARGETS", "SOURCE_NOTES", "matrix", "convert"]


def convert(s: Session, target: str, out_dir: Path, template: Path | None = None, banner: bool = True,
            install: bool = False, redact=None, notice: str | None = None,
            source_path: Path | None = None, workspace: str | None = None,
            extra: list[tuple[Path, str]] | None = None) -> tuple[Path, FidelityReport]:
    """Write `s` in `target`'s on-disk format.

    Same agent in and out → `native_copy` (lossless). Cross-agent → flatten through the canonical
    model. `source_path` is the UNREDACTED original file, needed for the native path."""
    if target not in WRITERS:
        raise ValueError(f"cannot write {target!r}: " + ("Antigravity keeps resumable state in a protobuf SQLite blob; source-only"
                                                          if target == "antigravity" else f"known targets: {', '.join(WRITERS)}"))
    rep = FidelityReport(s.agent, target)
    if target == s.agent and target in NATIVE and source_path is not None:
        counts = Counter()
        for m in s.messages:
            for b in m.blocks:
                counts[b.kind] += 1
        return native_copy(s.agent, Path(source_path), s.id, out_dir, rep, install=install, redact=redact,
                           notice=notice, workspace=workspace, cwd=s.cwd, extra=extra, title=s.title,
                           counts=dict(counts)), rep
    turns = flatten(s, rep, banner=banner)
    if not turns:
        raise ValueError("nothing to convert: session has no user/assistant text")
    if notice:
        turns.append(Turn("user", notice, s.ended or s.started))
        rep.notes.append("import notice appended as the final user message")
    if workspace:
        s = _copy.copy(s)
        s.cwd = workspace
        rep.notes.append(f"re-homed to {workspace}")
    path = WRITERS[target](s, turns, out_dir, template, rep, install, redact)
    return path, rep
