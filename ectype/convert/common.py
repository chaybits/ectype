"""Helpers shared by the writers and the native copy: timestamps, ids, JSONL in and out, the
Claude Code project slug."""
from __future__ import annotations

import contextvars
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import adapters
from ..jsonl import read_jsonl
from .fold import FidelityReport


# --------------------------------------------------------------------------- helpers
def _iso(ts: datetime | None) -> str:
    ts = ts or datetime.now(timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _uuid7(ts: datetime | None = None) -> str:
    """UUID v7 (Codex uses time-ordered ids)."""
    ms = int((ts or datetime.now(timezone.utc)).timestamp() * 1000)
    rand = secrets.randbits(74)
    n = (ms << 80) | (0x7 << 76) | ((rand >> 62) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=n))


def _read_jsonl(p: Path, rep: "FidelityReport | None" = None) -> list[dict]:
    """The records of a JSONL file. A line that does not parse is not dropped in silence: with a
    report it becomes a note (a template or a native-copy source losing its first record used to
    pass unnoticed; a BOM was enough, because this read strict `utf-8`)."""
    recs, bad = read_jsonl(p)
    if bad and rep is not None:
        rep.notes.append(f"{len(bad)} line(s) of {p.name} could not be parsed and were left out: "
                         + ", ".join(str(n) for n in bad[:10]) + (" …" if len(bad) > 10 else ""))
    return recs


# --------------------------------------------------------------------------- writing into a store
# Every file and folder a writer CREATES goes through the helpers below, which record it in the
# journal `convert()` opens. When the write fails half-way (a record that cannot be encoded, a disk
# that is full, an index that is locked), `convert()` removes what this call created, so the live
# store is left as it was instead of holding a truncated transcript, orphan spill files or an
# unregistered folder. Files are written to `<name>.tmp` and moved into place when complete, so a
# reader (the agent itself) never sees half a file.
_JOURNAL: contextvars.ContextVar[list[Path] | None] = contextvars.ContextVar("ectype_convert_journal", default=None)


def _created(p: Path) -> None:
    j = _JOURNAL.get()
    if j is not None:
        j.append(p)


def _mkdir(d: Path) -> None:
    """`mkdir -p`, recording every folder it had to create (outermost first)."""
    missing = []
    q = d
    while not q.exists():
        missing.append(q)
        if q.parent == q:
            break
        q = q.parent
    d.mkdir(parents=True, exist_ok=True)
    for m in reversed(missing):
        _created(m)


def _write_bytes(p: Path, data: bytes) -> None:
    """Write a new file atomically (temp name, then rename) and record it."""
    _mkdir(p.parent)
    existed = p.exists()
    tmp = p.with_name(p.name + ".ectype-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if not existed:
        _created(p)


def _write_text(p: Path, text: str) -> None:
    _write_bytes(p, text.encode("utf-8"))


def _json_line(r: dict) -> str:
    """One record as a JSON line. A string holding a lone surrogate (a tool output cut in the middle of
    an emoji, stored by the agent as a bare `\\ud83d` escape) cannot be encoded as UTF-8; that record
    falls back to ASCII escapes, which write the surrogate back exactly as it came in."""
    line = json.dumps(r, ensure_ascii=False)
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        line = json.dumps(r, ensure_ascii=True)
    return line


def _write_jsonl(p: Path, recs: list[dict]) -> None:
    _write_text(p, "".join(_json_line(r) + "\n" for r in recs))


def undo(created: list[Path]) -> list[Path]:
    """Remove what a failed write created, newest first; folders only when empty. Returns what is
    still there (a folder something else wrote into meanwhile)."""
    left = []
    for q in reversed(created):
        try:
            if q.is_dir():
                q.rmdir()
            else:
                q.unlink(missing_ok=True)
        except OSError:
            left.append(q)
    return left


def _store(agent: str) -> Path:
    """Where an installed copy goes: the agent's store as `Adapter.home()` resolves it: env var,
    then the path saved in Settings, then the default. The writers used to read the env var alone,
    so a store chosen in the GUI was honoured for reading and ignored for `--install`."""
    return adapters.get(agent).home()


def _no_cwd(rep: FidelityReport) -> str:
    """The working directory to file a session under when the source recorded none. The process
    cwd was used before: from the desktop launcher that is the tool's own source folder, which is
    nobody's project. Home is neutral, and the report says a `--workspace` would be better."""
    rep.notes.append("no working directory recorded: filed under the home directory; pass --workspace / pick one in the GUI to choose")
    return str(Path.home())


def _slug(cwd: str) -> str:
    """Claude Code's project-folder name for a working directory: every non-alphanumeric → `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _gemini_slug(name: str) -> str:
    """Gemini CLI 0.58's folder name for a new project: its `slugify()` over the folder's base name."""
    s = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", name.lower())).strip("-")
    return s or "project"


def gemini_project_dir(home: Path, cwd: str) -> Path:
    """The folder under Gemini's store (`~/.gemini/tmp`) where Gemini CLI looks for `cwd`'s chats.

    Gemini 0.58 maps a working directory in three steps (`getShortId`): the registry
    `~/.gemini/projects.json` (`{"projects": {cwd: short_id}}`) when that folder's `.project_root`
    names the cwd; else any folder whose `.project_root` names it; else a new folder named by its
    slug of the base name, `-1`, `-2` … on a clash. Filing under the lower-cased base name instead
    put a session where Gemini never reads it (a known `my-project` got a second `my_project/`), or
    inside another project's folder. The registry is read, never written: Gemini owns it, and step 2
    finds the folder written here the next time Gemini runs in that directory."""
    def names(d: Path) -> str | None:
        try:
            return (d / ".project_root").read_text(encoding="utf-8").strip()
        except OSError:
            return None
    try:
        reg = json.loads((home.parent / "projects.json").read_text(encoding="utf-8-sig")).get("projects") or {}
        short = reg.get(cwd) if isinstance(reg, dict) else None
        if isinstance(short, str) and short and names(home / short) == cwd:
            return home / short
    except (OSError, ValueError, AttributeError):
        pass
    if home.is_dir():
        for d in sorted(home.iterdir()):
            if d.is_dir() and names(d) == cwd:
                return d
    base = _gemini_slug(Path(cwd).name)
    cand, n = home / base, 0
    while cand.exists() and names(cand) not in (None, cwd):
        n += 1
        cand = home / f"{base}-{n}"
    return cand


def cline_id(ms: int) -> str:
    """The Cline CLI's own session-id shape: `<epoch ms>_<5 lowercase base36 characters>`."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return f"{ms}_{''.join(secrets.choice(alphabet) for _ in range(5))}"


def cline_register(home: Path, meta: dict, rep: "FidelityReport") -> None:
    """Add the row `cline history` and `cline --id` read for a session written under `home`.

    The Cline CLI keeps its session index in `<data>/db/sessions.db` (SQLite) beside the
    `sessions/` folder; a folder alone is invisible to it. Only the columns this version of the
    index has are written, so a newer schema with extra NULLABLE columns still takes the row; a
    required column this code does not know is refused before any file is written
    (`cline_index_problem`). When the database is not there (a fresh store the CLI never opened, or
    an `-o` copy), the report says so instead of creating one: the CLI owns that file."""
    db = home.parent / "db" / "sessions.db"
    if not db.is_file():
        rep.notes.append(f"no index at {db}: the session folder is written, but `cline history` cannot list it until the CLI has created its database")
        return
    row = _cline_row(meta)
    con = sqlite3.connect(db, timeout=5)
    try:
        cols = [c[1] for c in con.execute("pragma table_info(sessions)")]
        present = [c for c in cols if c in row]
        con.execute(f"insert into sessions ({', '.join(present)}) values ({', '.join('?' * len(present))})",
                    [row[c] for c in present])
        con.commit()
    finally:
        con.close()
    rep.notes.append("registered in the store's index db/sessions.db, which is what `cline history` and `cline --id` read")


def cline_index_problem(home: Path) -> str | None:
    """Why an install into this Cline store could not register its index row, or None.

    Asked BEFORE any file is written, so a refusal leaves the store untouched: the index row used to
    be inserted after both session files, and a failing INSERT left an unregistered folder behind."""
    db = home.parent / "db" / "sessions.db"
    if not db.is_file():
        return None                                  # no index yet: the files are written and the report says so
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            info = list(con.execute("pragma table_info(sessions)"))
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"the index {db} could not be read ({e})"
    if not info:
        return f"the index {db} has no `sessions` table (a Cline version this ectype does not know)"
    known = _cline_row({"session_id": "x"})
    required = [c[1] for c in info if c[3] and c[4] is None and not c[5] and c[1] not in known]
    if required:
        return (f"the index {db} requires column(s) {', '.join(required)} that this ectype does not know "
                f"(a newer Cline); nothing was written")
    return None


def _cline_row(meta: dict) -> dict:
    metadata = meta.get("metadata") or {}
    return {"session_id": meta["session_id"], "source": meta.get("source", "cli"), "pid": meta.get("pid", 0),
           "started_at": meta.get("started_at"), "ended_at": meta.get("ended_at"), "exit_code": meta.get("exit_code", 0),
           "status": meta.get("status", "completed"), "status_lock": 0, "interactive": int(bool(meta.get("interactive"))),
           "provider": meta.get("provider") or "cline", "model": meta.get("model") or "unknown",
           "cwd": meta.get("cwd"), "workspace_root": meta.get("workspace_root"), "team_name": meta.get("team_name", ""),
           "enable_tools": int(bool(meta.get("enable_tools", True))), "enable_spawn": int(bool(meta.get("enable_spawn", True))),
           "enable_teams": int(bool(meta.get("enable_teams", True))), "parent_session_id": None, "parent_agent_id": None,
           "agent_id": None, "conversation_id": None, "is_subagent": 0, "prompt": meta.get("prompt", ""),
           "metadata_json": json.dumps(metadata, ensure_ascii=False), "transcript_path": "", "hook_path": "",
           "messages_path": meta.get("messages_path", ""), "updated_at": meta.get("ended_at")}
