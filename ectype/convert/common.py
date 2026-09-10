"""Helpers shared by the writers and the native copy: timestamps, ids, JSONL in and out, the
Claude Code project slug."""
from __future__ import annotations

import json
import re
import secrets
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


def _write_jsonl(p: Path, recs: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


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
