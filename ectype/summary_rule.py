"""The paragraph that asks an agent for a closing summary, kept in the instruction files the user picks.

ectype quotes an agent's closing summary (`summary.closing_summaries`) only when the agent wrote one,
and an agent writes one only when its instructions ask for it. `ectype summary-rule` (and Settings ->
Summary in the web UI) writes that request into the files the user chooses: an agent's global
instruction file (Claude Code's CLAUDE.md, Codex's AGENTS.md, Gemini CLI's GEMINI.md) or any file
given by path. It is never written anywhere by default.

The paragraph sits inside a block ectype owns, between two HTML comment lines, and nothing outside the
block is ever changed. The first line records a hash of the paragraph, which gives the same states as
the slash command's files (`ectype.skill`):

    none          the file exists and holds no rule
    hand-written  no block, but the file already names a closing-summary marker: someone asks in their
                  own words, so ectype adds no second rule unless forced
    ours          the block holds exactly the paragraph for today's markers
    stale         untouched since ectype wrote it, for other markers: `install` updates it
    edited        changed by hand: refused unless forced (the file is backed up first either way)

Every file is backed up (`ectype backup`) before it is changed, and a file left empty by `remove`
held only the block and goes with it.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from . import settings

BEGIN = "<!-- ectype summary-rule: begin"
END = "<!-- ectype summary-rule: end -->"
# the whole block, its recorded hash, and the paragraph between the two lines
_BLOCK = re.compile(r"<!-- ectype summary-rule: begin(?: \(sha256 ([0-9a-f]{12})\))? -->\n(.*?)\n<!-- ectype summary-rule: end -->\n?", re.S)

# where each agent reads its global instructions, and the variable that moves it (as in `ectype.skill`)
AGENTS: dict[str, dict] = {
    "claude-code": {"label": "Claude Code", "dir": "~/.claude", "env": ("CLAUDE_CONFIG_DIR", ""), "file": "CLAUDE.md"},
    "codex": {"label": "Codex", "dir": "~/.codex", "env": ("CODEX_HOME", ""), "file": "AGENTS.md"},
    "gemini-cli": {"label": "Gemini CLI", "dir": "~/.gemini", "env": ("GEMINI_CLI_HOME", ".gemini"), "file": "GEMINI.md"},
}


def path(agent: str) -> Path:
    """The agent's global instruction file: under its own config variable when that is set."""
    if agent not in AGENTS:
        raise KeyError(f"no instruction file known for {agent!r}; known: {', '.join(AGENTS)}")
    spec = AGENTS[agent]
    var, sub = spec["env"]
    if os.environ.get(var):
        base = Path(os.path.expanduser(os.path.expandvars(os.environ[var]))) / sub
    else:
        base = Path(os.path.expanduser(spec["dir"]))
    return base / spec["file"]


def _markers() -> list[str]:
    from .summary import markers
    return markers()


def text(marks: list[str] | None = None) -> str:
    """The paragraph, naming the first marker in Settings -> Summary (or `marks`)."""
    marks = _markers() if marks is None else marks
    if not marks:
        raise ValueError("Settings -> Summary lists no marker, so there is no closing summary to ask for")
    return (f"At the end of a session, and whenever your context is close to full, finish your reply with a "
            f"summary of the whole session placed between two lines that each hold only {marks[0]}, outside any "
            f"code fence: what was asked, what was done and decided, what is still open, and what the next "
            f"session needs to know. ectype (AI Agent Ectype) reads that block as the session's closing summary.")


def _digest(paragraph: str) -> str:
    return hashlib.sha256(paragraph.encode("utf-8")).hexdigest()[:12]


def block(marks: list[str] | None = None) -> str:
    """The block as it is written: the begin line with the paragraph's hash, the paragraph, the end line."""
    p = text(marks)
    return f"{BEGIN} (sha256 {_digest(p)}) -->\n{p}\n{END}"


def state(f: Path) -> str:
    """`absent`, `none`, `hand-written`, `ours`, `stale` or `edited` (see the module docstring)."""
    f = Path(f)
    if not f.is_file():
        return "absent"
    content = f.read_text(encoding="utf-8", errors="replace")
    m = _BLOCK.search(content)
    if m:
        recorded, inner = m.group(1), m.group(2)
        try:
            if inner == text():
                return "ours"
        except ValueError:
            pass                                 # no marker set: nothing is current, the block is judged by its hash
        return "stale" if recorded and _digest(inner) == recorded else "edited"
    return "hand-written" if any(mk in content for mk in _markers()) else "none"


def _write(f: Path, content: str) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, f)


def _backup(f: Path) -> None:
    from . import backup as bk
    bk.save([f], f"summary-rule-{f.stem}")     # None when the file did not exist yet: nothing to keep


def install(files: list[Path], force: bool = False) -> list[Path]:
    """Write or update the block in each file; returns the files that changed.

    Every file is checked before any is written, so a refusal changes nothing. An edited block or a
    hand-written rule is refused unless `force`; a file that already holds the current block is left
    as it is (no write, no backup)."""
    blk = block()                                # raises when Settings lists no marker
    files = [Path(f) for f in files]
    for f in files:
        st = state(f)
        if st == "edited" and not force:
            raise FileExistsError(f"{f}: the ectype block was edited since ectype wrote it; --force replaces it "
                                  f"(the file is backed up first)")
        if st == "hand-written" and not force:
            raise FileExistsError(f"{f} already asks for a closing summary in its own words (it names a marker "
                                  f"from Settings -> Summary); ectype adds no second rule. --force adds it anyway")
    written: list[Path] = []
    for f in files:
        st = state(f)
        if st == "ours":
            continue
        content = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        if _BLOCK.search(content):
            new = _BLOCK.sub(lambda _m: blk + "\n", content, count=1)
        elif content.strip():
            new = content.rstrip("\n") + "\n\n" + blk + "\n"
        else:
            new = blk + "\n"
        _backup(f)
        _write(f, new)
        written.append(f)
    return written


def remove(files: list[Path], force: bool = False) -> tuple[list[Path], list[Path]]:
    """Take the block out of each file. Returns (removed, kept): an edited block is kept unless
    `force`; a file with no block is neither. A file that held nothing but the block is deleted."""
    removed: list[Path] = []
    kept: list[Path] = []
    for f in (Path(x) for x in files):
        st = state(f)
        if st in ("absent", "none", "hand-written"):
            continue
        if st == "edited" and not force:
            kept.append(f)
            continue
        content = f.read_text(encoding="utf-8", errors="replace")
        m = _BLOCK.search(content)
        rest = (content[:m.start()].rstrip("\n") + ("\n" if content[:m.start()].strip() else "")
                + content[m.end():].lstrip("\n")) if m else content
        _backup(f)
        if rest.strip():
            _write(f, rest.rstrip("\n") + "\n")
        else:
            f.unlink()
        removed.append(f)
    return removed, kept


def status(files: list[Path] | None = None) -> list[dict]:
    """One row per agent's instruction file, then one per extra file: label, path, state."""
    rows = [{"agent": a, "label": spec["label"], "path": str(path(a)), "state": state(path(a))} for a, spec in AGENTS.items()]
    rows += [{"agent": None, "label": str(f), "path": str(f), "state": state(Path(f))} for f in files or []]
    return rows


def targets(agents: list[str] | None, files: list[str] | None) -> list[Path]:
    """The files a command names: the agents' instruction files, then any given by path."""
    out = [path(a) for a in agents or []]
    out += [Path(os.path.expanduser(f)) for f in files or []]
    return out
