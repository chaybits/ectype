"""Copies of the files ectype is about to disturb, and of any session you ask for.

The README used to say "point it at data you can afford to lose", which is advice, not a feature.
This is the feature. Two ways in:

* **Automatic, before a write into a live store.** Installing a converted session mostly *adds* a
  file, which is safe, but not always: the Codex writer appends to the store's own
  `session_index.jsonl`, and `--install` then offers the rollout to `codex migrate-rollouts
  --apply`, which rewrites `thread_history.sqlite`. Those two already exist and belong to Codex,
  so they are copied first, every time, unless `--no-backup` says otherwise.
* **On request, for a whole session.** `ectype backup <id>` copies a transcript and every sidecar
  file that belongs to it, so you can experiment and put it back.

Each backup is one timestamped folder holding a `manifest.json` that records where every file came
from, which is what makes `--restore` possible without guessing. Files are stored under flat
numbered names so two files of the same name cannot collide.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from . import settings

MANIFEST = "manifest.json"


def root() -> Path:
    """`backups/` beside the settings file, so ECTYPE_CONFIG moves both together."""
    return settings.config_path().parent / "backups"


def _slug(why: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in why.lower())
    return "-".join(x for x in keep.split("-") if x)[:40] or "backup"


def save(paths, why: str, agent: str | None = None) -> Path | None:
    """Copy every path that exists into a new timestamped folder. None when none of them did.

    Returning None rather than an empty folder matters: "nothing needed saving" and "a backup was
    taken" are different facts, and the caller reports them differently."""
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
        shutil.copy2(p, folder / as_name)
        files.append({"from": str(p), "as": as_name, "bytes": p.stat().st_size})
    (folder / MANIFEST).write_text(json.dumps(
        {"created": datetime.now().astimezone().isoformat(timespec="seconds"),
         "why": why, "agent": agent, "files": files}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return folder


def at_risk(target: str, home: Path) -> list[Path]:
    """The files an `--install` of `target` may modify, as opposed to create.

    Deliberately short, and it should stay that way: a writer that adds a new session file puts
    nothing at risk, and pretending otherwise would make every install produce a pointless copy.
    Claude Code and Gemini CLI write a new file (Gemini's `.project_root` only when it is absent).
    Codex is the exception."""
    if target == "codex":
        return [home / "session_index.jsonl", home / "thread_history.sqlite"]
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
        out.append(data)
    return out


def restore(name: str, dry_run: bool = False) -> list[tuple[Path, bool]]:
    """Put a backup back where it came from. [(destination, written)].

    `dry_run` reports what would happen and touches nothing, which is the only honest default for
    a command whose whole job is to overwrite files that are there now."""
    folder = root() / name
    m = folder / MANIFEST
    if not m.is_file():
        raise FileNotFoundError(f"no backup named {name!r} under {root()}")
    data = json.loads(m.read_text(encoding="utf-8"))
    done = []
    for f in data.get("files", []):
        dest, src = Path(f["from"]), folder / f["as"]
        if not src.is_file():
            done.append((dest, False))
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        done.append((dest, True))
    return done
