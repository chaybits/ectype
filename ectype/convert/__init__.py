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
from .common import _JOURNAL, _iso, _read_jsonl, _slug, _uuid7, _write_jsonl, undo   # noqa: F401, re-exported
from .fold import FidelityReport, Turn, _fold_args, banner_is, flatten, folded  # noqa: F401
from .tables import CARRY, CARRY_NATIVE, SOURCE_NOTES, TARGETS, matrix          # noqa: F401
from .native import NATIVE, _native_notice_record, native_copy                   # noqa: F401
from .writers import WRITERS, write_claude, write_codex, write_gemini             # noqa: F401

__all__ = ["FidelityReport", "Turn", "flatten", "folded", "WRITERS", "NATIVE", "native_copy",
           "CARRY", "CARRY_NATIVE", "TARGETS", "SOURCE_NOTES", "matrix", "convert"]


def convert(s: Session, target: str, out_dir: Path, template: Path | None = None, banner: bool = True,
            install: bool = False, redact=None, notice: str | None = None,
            source_path: Path | None = None, workspace: str | None = None,
            extra: list[tuple[Path, str]] | None = None, backup: bool = True) -> tuple[Path, FidelityReport]:
    """Write `s` in `target`'s on-disk format.

    Same agent in and out → `native_copy` (lossless). Cross-agent → flatten through the canonical
    model. `source_path` is the UNREDACTED original file, needed for the native path.

    `install` writes into the target's live store. Before it does, every store file the install
    would MODIFY (as opposed to create) is copied by `ectype.backup`, unless `backup` is False.
    That copy lives here, not in the command line, so the web UI and the MCP server take it too:
    it used to be the CLI's alone, and the README promised it for every install."""
    if target not in WRITERS:
        raise ValueError(f"cannot write {target!r}: " + ("Antigravity keeps resumable state in a protobuf SQLite blob; source-only"
                                                          if target == "antigravity" else f"known targets: {', '.join(WRITERS)}"))
    rep = FidelityReport(s.agent, target)
    folder = None
    if install and backup:
        from .. import adapters, backup as bk
        risky = bk.at_risk(target, adapters.get(target).home())
        present = [p for p in risky if p.is_file()]        # at_risk names what an install MAY touch; count what exists
        folder = bk.save(present, f"install-into-{target}", agent=target)
        if folder:
            rep.notes.append(f"backed up {len(present)} store file(s) first: {folder}  (put back with: ectype backup --restore {folder.name})")
    if install and redact:
        rep.notes.append("installed with redaction on: the copy is filed under the placeholder path, so it lists and "
                         "resumes under that path, not from the real project directory")
    # Every file this call creates is journaled (common._JOURNAL); a failure removes them again, so the
    # live store is never left holding half a session, and the error still names the backup.
    created: list[Path] = []
    token = _JOURNAL.set(created)
    try:
        path = _convert(s, target, out_dir, template, banner, install, redact, notice, source_path, workspace, extra, rep)
    except Exception as e:
        left = undo(created)
        _JOURNAL.reset(token)
        where = "the store" if install else str(out_dir)
        msg = (f"{e}" if isinstance(e, ValueError) and not created else
               f"writing into {where} failed ({type(e).__name__}: {e}); "
               + (f"{len(created) - len(left)} file(s) it had created were removed" if created else "nothing had been written")
               + (f", {len(left)} could not be ({', '.join(str(q) for q in left)})" if left else ""))
        if folder:
            msg += f"; the backup taken first is {folder.name}"
        raise ValueError(msg) from e
    _JOURNAL.reset(token)
    return path, rep


def _convert(s: Session, target: str, out_dir: Path, template: Path | None, banner: bool, install: bool, redact,
             notice: str | None, source_path: Path | None, workspace: str | None,
             extra: list[tuple[Path, str]] | None, rep: FidelityReport) -> Path:
    if target == s.agent and target in NATIVE and source_path is not None:
        counts = Counter()
        for m in s.messages:
            for b in m.blocks:
                counts[b.kind] += 1
        return native_copy(s.agent, Path(source_path), s.id, out_dir, rep, install=install, redact=redact,
                           notice=notice, workspace=workspace, cwd=s.cwd, extra=extra, title=s.title,
                           counts=dict(counts))
    turns = flatten(s, rep, banner=banner)
    if not any(not banner_is(t) for t in turns):
        # the banner alone is not a conversation: this guard never fired with the banner on (the
        # default), so a session with no text was installed as a banner-only thread
        raise ValueError("nothing to convert: session has no user/assistant text")
    # a turn with no timestamp takes the previous turn's, so the target sees the order the source had
    # (writers used to stamp it with the wall clock or the session start: "re-stamped monotonically")
    prev = s.started
    for t in turns:
        if t.timestamp is None:
            t.timestamp = prev
        else:
            prev = t.timestamp
    if notice:
        turns.append(Turn("user", notice, s.ended or prev or s.started))
        rep.notes.append("import notice appended as the final user message")
    if workspace:
        s = _copy.copy(s)
        s.cwd = workspace
        rep.notes.append(f"re-homed to {workspace}")
    return WRITERS[target](s, turns, out_dir, template, rep, install, redact)
