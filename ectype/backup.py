"""Copies of the files ectype is about to disturb, and of any session you ask for.

The README used to say "point it at data you can afford to lose", which is advice, not a feature.
This is the feature. Two ways in:

* **Automatic, before a write into a live store.** Installing a converted session mostly *adds* a
  file, which is safe, but not always: the Codex writer appends to the store's own
  `session_index.jsonl`, and `--install` then offers the rollout to `codex migrate-rollouts
  --apply`, which rewrites its SQLite databases (`thread_history_<n>.sqlite`, `state_<n>.sqlite`
  in 0.153); a Cline install inserts a row into `db/sessions.db`. Those files already exist and
  belong to the agent, so they are copied first, every time, unless `--no-backup` says otherwise.
  A SQLite database is copied through SQLite itself (`backup()`), never as a plain file: these run
  in WAL mode, and a plain copy of the main file misses every committed row still in the `-wal`.
* **On request, for a whole session.** `ectype backup <id>` copies a transcript and every sidecar
  file that belongs to it, so you can experiment and put it back.

Each backup is one timestamped folder holding a `manifest.json` that records where every file came
from, which is what makes `--restore` possible without guessing. Files are stored under flat
numbered names so two files of the same name cannot collide.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import settings

MANIFEST = "manifest.json"


def root() -> Path:
    """`backups/` beside the settings file, so ECTYPE_CONFIG moves both together."""
    return settings.config_path().parent / "backups"


def _slug(why: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in why.lower())
    return "-".join(x for x in keep.split("-") if x)[:40] or "backup"


def _is_sqlite(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite_copy(src: Path, dst: Path) -> None:
    """A consistent copy of a live database, WAL frames included, taken under SQLite's own locks.
    A database locked by the running agent makes this wait (5 s) and then fail, which fails closed."""
    a = sqlite3.connect(src, timeout=5)
    try:
        b = sqlite3.connect(dst, timeout=5)
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()


def save(paths, why: str, agent: str | None = None, prune_after: bool = True,
         modes: dict | None = None) -> Path | None:
    """Copy every path that exists into a new timestamped folder. None when none of them did.

    Returning None rather than an empty folder matters: "nothing needed saving" and "a backup was
    taken" are different facts, and the caller reports them differently. `prune_after=False` skips
    the retention pass: a restore takes its safety copy that way, because pruning then could delete
    the very backup being restored. `modes` maps a path to how `restore()` puts it back: `replace`
    (the default), `merge` (a store index several sessions share: the saved entries are added back,
    the others kept), `keep` (a file several sessions share that cannot be merged, such as an Aider
    history) or `export` (a scratch copy of one chat, not the live store): the last two are kept in
    the backup and never written over the store."""
    real = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            real.append(p)
    if not real:
        return None
    folder = root() / f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(why)}"
    n = 0
    while folder.exists():                      # two backups in the same second
        n += 1
        folder = root() / f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(why)}-{n}"
    folder.mkdir(parents=True)
    files = []
    for i, p in enumerate(real, 1):
        as_name = f"{i:03d}-{p.name}"
        entry = {"from": str(p), "as": as_name, "bytes": p.stat().st_size}
        mode = (modes or {}).get(p) or (modes or {}).get(str(p))
        if mode and mode != "replace":
            entry["restore"] = mode
        if _is_sqlite(p):
            _sqlite_copy(p, folder / as_name)
            entry["kind"] = "sqlite"
        else:
            shutil.copy2(p, folder / as_name)
        files.append(entry)
    (folder / MANIFEST).write_text(json.dumps(
        {"created": datetime.now().astimezone().isoformat(timespec="seconds"),
         "why": why, "agent": agent, "files": files}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if prune_after:
        prune()                                 # the retention the user set applies as new copies arrive
    return folder


def size_of(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def _created(d: Path) -> datetime | None:
    """A backup's creation time from its manifest, aware. A naive stamp (a hand-edited or copied-in
    manifest) is read as local time: comparing it with an aware cutoff used to raise TypeError from
    every prune, which aborted every later install while `keep_days` was set."""
    try:
        c = datetime.fromisoformat(json.loads((d / MANIFEST).read_text(encoding="utf-8"))["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return c.astimezone() if c.tzinfo is None else c


def prune(keep_last: int | None = None, keep_days: int | None = None, protect=()) -> list[Path]:
    """Delete backup folders beyond the retention in Settings → Backups (or the values given):
    the oldest past `keep_last`, and any older than `keep_days`. 0 means no limit, which is the
    default, so nothing is ever deleted unless the user asked for it. Returns what was removed.
    The newest folder is never removed by `keep_days` alone: a rule that empties the store the
    day after the last install would defeat the store. Folders in `protect` are never removed.
    Age is the manifest's `created`, the name only a tiebreak: two backups in one second sort by
    their reason, which put a fresh safety copy before the backup it was protecting."""
    cfg = settings.load()["backup"]
    keep_last = cfg["keep_last"] if keep_last is None else keep_last
    keep_days = cfg["keep_days"] if keep_days is None else keep_days
    if not root().is_dir() or (keep_last <= 0 and keep_days <= 0):
        return []
    keep = {Path(p).resolve() for p in protect}
    stamped = [(_created(d), d) for d in root().iterdir() if d.is_dir() and (d / MANIFEST).is_file()]
    floor = datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
    folders = [d for c, d in sorted(stamped, key=lambda cd: (cd[0] or floor, cd[1].name))]
    created = {d: c for c, d in stamped}
    doomed: list[Path] = []
    if keep_last > 0 and len(folders) > keep_last:
        doomed += folders[: len(folders) - keep_last]
    if keep_days > 0:
        cutoff = datetime.now().astimezone() - timedelta(days=keep_days)
        for d in folders[:-1]:                                  # never the newest
            c = created.get(d)
            if c is not None and c < cutoff and d not in doomed:
                doomed.append(d)
    doomed = [d for d in doomed if d.resolve() not in keep]
    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)
    return doomed


def at_risk(target: str, home: Path) -> list[Path]:
    """The files an `--install` of `target` may modify, as opposed to create.

    Deliberately short, and it should stay that way: a writer that adds a new session file puts
    nothing at risk, and pretending otherwise would make every install produce a pointless copy.
    Claude Code and Gemini CLI write a new file (Gemini's `.project_root` only when it is absent).
    Codex and Cline are the exceptions. Codex's database names carry a schema number that changes
    between versions (0.153: `thread_history_1.sqlite`, `state_5.sqlite`); a fixed name once backed
    up nothing, because that file did not exist."""
    if target == "codex":
        dbs = sorted(home.glob("thread_history*.sqlite")) + sorted(home.glob("state_*.sqlite"))
        return [home / "session_index.jsonl", *dbs]
    if target == "cline":
        return [home.parent / "db" / "sessions.db"]       # the index `cline history` reads; the writer inserts one row
    return []


def listing() -> list[dict]:
    """Every backup, newest first, with its manifest read back."""
    out = []
    if not root().is_dir():
        return out
    for d in sorted(root().iterdir(), reverse=True):
        m = d / MANIFEST
        if not m.is_file():
            continue
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except ValueError:
            continue
        data["name"] = d.name
        data["path"] = str(d)
        data["bytes"] = size_of(d)
        out.append(data)
    return out


class RestoreResult(list):
    """`[(destination, written)]`, plus `safety`: the backup taken of what the restore overwrote
    (None on a dry run, or when nothing was there to overwrite), and `skipped`: `(destination,
    reason)` for entries that are kept in the backup and not written back (an export copy, a file
    several sessions share)."""
    safety: Path | None = None

    def __init__(self, *a):
        super().__init__(*a)
        self.skipped: list[tuple[Path, str]] = []


def _index_key(rec) -> str | None:
    if isinstance(rec, dict):
        for k in ("id", "sessionId", "session_id"):
            if isinstance(rec.get(k), str):
                return rec[k]
    return None


def _merge_index(src: Path, dest: Path) -> None:
    """Put a shared index's saved entries back without dropping the ones added since: a one-session
    restore used to replace Codex's `session_index.jsonl` (or Continue's `sessions.json`) whole,
    losing every session indexed after the backup (the 2026-09-26 ideas round)."""
    saved_text = src.read_text(encoding="utf-8-sig")
    live_text = dest.read_text(encoding="utf-8-sig") if dest.exists() else ""
    if dest.suffix == ".jsonl":
        live = [l for l in live_text.splitlines() if l.strip()]
        keys = set()
        for l in live:
            try:
                keys.add(_index_key(json.loads(l)) or l)
            except ValueError:
                keys.add(l)
        add = []
        for l in (x for x in saved_text.splitlines() if x.strip()):
            try:
                k = _index_key(json.loads(l)) or l
            except ValueError:
                k = l
            if k not in keys:
                add.append(l)
        out = "".join(x + "\n" for x in live + add)
    else:
        live = json.loads(live_text) if live_text.strip() else []
        saved = json.loads(saved_text)
        if not isinstance(live, list) or not isinstance(saved, list):
            raise ValueError(f"{dest.name}: not a list of entries, cannot merge")
        keys = {_index_key(x) for x in live}
        out = json.dumps(live + [x for x in saved if _index_key(x) not in keys], ensure_ascii=False, indent=2) + "\n"
    tmp = dest.with_name(dest.name + ".ectype-restore")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, dest)


def restore(name: str, dry_run: bool = False) -> RestoreResult:
    """Put a backup back where it came from. [(destination, written)].

    `dry_run` reports what would happen and touches nothing, which is the only honest default for
    a command whose whole job is to overwrite files that are there now. A real restore first saves
    the files it is about to overwrite as a backup of its own (`before-restore-<name>`), so a
    restore is itself reversible with another restore."""
    folder = root() / name
    m = folder / MANIFEST
    if not m.is_file():
        raise FileNotFoundError(f"no backup named {name!r} under {root()}")
    data = json.loads(m.read_text(encoding="utf-8"))
    done = RestoreResult()
    if not dry_run:
        # no retention pass here: it ran before the copy below and could delete the folder being
        # restored, or the safety copy it had just named as the way back (audit 2026-09-25 F03)
        done.safety = save([Path(f["from"]) for f in data.get("files", []) if (folder / f["as"]).is_file()],
                           f"before-restore-{name}", agent=data.get("agent"), prune_after=False)
    for f in data.get("files", []):
        dest, src = Path(f["from"]), folder / f["as"]
        mode = f.get("restore", "replace")
        if mode in ("keep", "export"):
            # an export copy of one chat, or a file several sessions share that cannot be merged:
            # writing it back would put a one-chat database over the store, or roll back every other
            # session in that file; it stays in the backup, where it can be read
            done.skipped.append((dest, "an export copy of this chat, not the live store" if mode == "export"
                                 else "a file other sessions share; writing it back would roll them back"))
            continue
        if not src.is_file():
            done.append((dest, False))
            continue
        if not dry_run and mode == "merge":
            dest.parent.mkdir(parents=True, exist_ok=True)
            _merge_index(src, dest)
            done.append((dest, True))
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f.get("kind") == "sqlite" or (_is_sqlite(src) and dest.exists() and _is_sqlite(dest)):
                # through SQLite, so the live database's WAL is rewritten with it; a plain copy of the
                # main file under a live -wal grafts old pages onto new frames and corrupts the index
                _sqlite_copy(src, dest)
            else:
                tmp = dest.with_name(dest.name + ".ectype-restore")
                shutil.copy2(src, tmp)
                os.replace(tmp, dest)
        done.append((dest, True))
    if not dry_run:
        prune(protect=[folder, *([done.safety] if done.safety else [])])
    return done
