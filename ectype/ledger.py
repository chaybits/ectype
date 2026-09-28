"""The change ledger, and the summaries it keeps: an agent's own past, made greppable.

`ectype summarize` produces a summary of one session with no model and no network. This module
keeps one such summary per session, on ectype's side, and remembers which version of each file it
summarised, so a second run only touches what is new or changed. `search` then answers "have I
dealt with this before?" across every store at once, and says how much of the ground it covered.

Layout, beside the settings file so `ECTYPE_CONFIG` moves all of it together (the Windows bundle
keeps everything in its own folder for the same reason):

    <config dir>/ledger.json                       what was summarised, and the file it came from
    <config dir>/summaries/<agent>/<id>.txt        the summary itself, as `ectype summarize` prints it

Identity is `<agent>:<id>`; when the same id exists under two project folders (Claude Code writes a
session resumed elsewhere under the new folder with the same id) the newest file wins, which is
also what the command line resolves to.

Nothing here writes into an agent's store. Everything is read-only on that side.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import adapters, settings
from .model import Session, SessionRef
from .summary import FORMAT as SUMMARY_FORMAT, markers as summary_markers, summarize

TAIL_BYTES = 4096            # the fingerprint hashes the file's tail: an agent can rewrite a mutation log in place at the same size
LEDGER = "ledger.json"


def root() -> Path:
    """`summaries/` beside the settings file."""
    return settings.config_path().parent / "summaries"


def ledger_path() -> Path:
    return settings.config_path().parent / LEDGER


def summary_path(agent: str, sid: str) -> Path:
    """`summaries/<agent>/<readable prefix>.<hash of the id>.txt`.

    The prefix keeps the file findable by eye (SillyTavern ids carry a slash, Aider ids a '#', so
    those become '_'); the hash makes it unique and the length bounded. The sanitised id alone mapped
    `my proj#0` and `my_proj#0` to one file, and a long id past the 255-byte limit failed every run."""
    # bounded in BYTES: a file name is limited to 255 bytes, and a CJK character is three of them
    safe = re.sub(r"[^\w.\-]+", "_", sid).encode("utf-8")[:150].decode("utf-8", "ignore")
    return root() / agent / f"{safe}.{hashlib.sha1(sid.encode('utf-8')).hexdigest()[:10]}.txt"


def _legacy_summary_path(agent: str, sid: str) -> Path:
    """Where format-2 ledgers kept a summary; read as a fallback until the entry is redone."""
    return root() / agent / f"{re.sub(r'[^\w.\-]+', '_', sid)}.txt"


def _existing_summary(agent: str, sid: str) -> Path | None:
    for p in (summary_path(agent, sid), _legacy_summary_path(agent, sid)):
        try:
            if p.is_file():
                return p
        except OSError:                # a legacy name past the file-name limit
            continue
    return None


def fingerprint(path: Path) -> dict | None:
    """(mtime, size, sha256 of the last TAIL_BYTES) of a session file, or None when it is not there.

    Size and mtime alone miss an in-place rewrite of the same length; the tail catches it cheaply
    without reading the whole file.
    """
    try:
        st = path.stat()
        with open(path, "rb") as fh:
            fh.seek(max(0, st.st_size - TAIL_BYTES))
            tail = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None
    return {"mtime": st.st_mtime, "size": st.st_size, "tail": tail}


def load() -> dict:
    """The ledger, `{"<agent>:<id>": entry}`. A missing file is an empty ledger; an unreadable one
    is reported on stderr and treated as empty, never silently, because everything downstream
    trusts it."""
    p = ledger_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        import sys
        print(f"ectype: ledger ignored, starting empty ({p}: {e})", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    """Atomic write, like the settings file."""
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _key(ref: SessionRef) -> str:
    return f"{ref.agent}:{ref.id}"


def record(ref: SessionRef, s: Session, data: dict | None = None) -> Path:
    """Write one session's summary and its ledger entry. Returns the summary file.

    `data` is the loaded ledger when the caller holds it (a bulk run); otherwise it is loaded and
    saved here, so a single `--save` costs one read and one write.
    """
    own = data is None
    data = load() if own else data
    out = summary_path(ref.agent, ref.id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(summarize(s), encoding="utf-8")
    os.replace(tmp, out)
    legacy = _legacy_summary_path(ref.agent, ref.id)
    try:
        if legacy != out and legacy.is_file():
            legacy.unlink()            # moved to its unique name; the next id that shared it is redone too
    except OSError:
        pass                           # a legacy name the file system cannot even stat: nothing to remove
    data[_key(ref)] = {"agent": ref.agent, "id": ref.id, "path": str(ref.path),
                       "title": s.title or ref.title, "project": s.project or ref.project,
                       "fingerprint": fingerprint(ref.path),
                       # what shape the summary was written in, and with which closing-summary
                       # markers: update() redoes an entry when either no longer matches
                       "format": SUMMARY_FORMAT, "markers": summary_markers(),
                       "summarised": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if own:
        save(data)
    return out


def update(agents: list[str] | None = None, force: bool = False,
           progress: Callable[[SessionRef, str], None] | None = None) -> dict:
    """Summarise every session that is new or changed since the last run (all of them with `force`).

    Which stores: with `agents` None, the ones switched on in Settings → Agents, which means the
    coding agents and not the chat apps unless the user turned one on. Reading a chat app's
    conversations into summaries is a choice the user makes, not a default; naming an agent
    explicitly (`-a lmstudio`) takes it whether or not it is switched on.

    A stored summary is also redone when it was written in an older format (`summary.FORMAT`) or
    with other closing-summary markers than Settings → Summary holds now, so a change there
    reaches the store on the next run without `--force`.

    Args:
        agents: restrict to these adapter names; None = every store that is enabled in Settings.
        force: re-summarise even when the fingerprint has not moved.
        progress: called per session with "summarised" | "unchanged" | "failed"; None = quiet.
    Returns:
        {"seen", "summarised", "unchanged", "failed": [(agent, id, error)], "gone": n}.
        A session that fails to load is counted and named, never skipped in silence.
    """
    data = load()
    changed: set[str] = set()                          # the keys this run wrote, merged into a fresh read at the end
    marks = summary_markers()
    refs = adapters.all_refs(agents, enabled_only=agents is None)
    # the agents this run enumerates, whether or not their store is there: an entry can only be
    # declared "gone" by a run that looked for it, so a plain run never marks a chat app's summaries
    # gone because Settings keeps that app off
    covered = {n for n, a in adapters.ADAPTERS.items() if ((n in agents) if agents else a.enabled())}
    seen_keys: set[str] = set()
    stats: dict = {"seen": 0, "summarised": 0, "unchanged": 0, "failed": [], "gone": 0}
    for ref in refs:                                   # newest first: a duplicate id keeps the newest file
        k = _key(ref)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        stats["seen"] += 1
        entry = data.get(k)
        fp = fingerprint(ref.path)
        if (not force and entry and entry.get("fingerprint") == fp and entry.get("format") == SUMMARY_FORMAT
                and entry.get("markers") == marks and summary_path(ref.agent, ref.id).is_file()):
            stats["unchanged"] += 1
            if progress:
                progress(ref, "unchanged")
            continue
        try:
            s = adapters.load(ref)
            record(ref, s, data)
            changed.add(k)
        except Exception as e:                          # noqa: BLE001, one broken session must not stop the run, but it must be named
            stats["failed"].append((ref.agent, ref.id, f"{type(e).__name__}: {e}"))
            if progress:
                progress(ref, "failed")
            continue
        stats["summarised"] += 1
        if progress:
            progress(ref, "summarised")
    for k, entry in data.items():                      # a session whose file is gone keeps its summary, flagged
        if k in seen_keys:
            if entry.pop("gone", None):                # it is back (or never left)
                changed.add(k)
        elif entry.get("agent") in covered and not entry.get("gone") and not _still_there(entry):
            # only when the file itself is missing: a store that failed to enumerate (a locked
            # database, an unmounted drive) used to flag every one of its summaries gone
            entry["gone"] = True
            changed.add(k)
            stats["gone"] += 1
    # merge, do not overwrite: a `recall` or `summarize --save` during this run wrote its own entry
    # into the file, and saving the copy loaded at the start erased it
    fresh = load()
    fresh.update({k: data[k] for k in changed if k in data})
    save(fresh)
    return stats


def _still_there(entry: dict) -> bool:
    try:
        return bool(entry.get("path")) and Path(entry["path"]).exists()
    except OSError:
        return False


def search(pattern: str, agents: list[str] | None = None, max_lines: int = 6, refresh: bool = True) -> dict:
    """Match a regex (case-insensitive) against every stored summary.

    Args:
        pattern: a regular expression.
        agents: restrict to these adapter names; None = the stores enabled in Settings, as `update()`.
        max_lines: matching lines shown per session; the summary itself is a few hundred tokens
            and a match is a pointer to it, not a substitute for reading it.
        refresh: bring the store up to date first (`update()`): a session never recalled or
            summarised is summarised now, one whose file changed is summarised again, and every other
            one is only fingerprinted, so a repeat search reads no transcript. Without it a session
            nobody had pulled in was invisible to search (the user, 2026-09-27).
    Returns {"matches": [{agent, id, title, project, gone, lines: [...]}], "summarised": n,
    "known": n, "never": n, "refreshed": n, "failed": n}: `known` is what the stores hold right now,
    `never` how many of those have no summary, `refreshed` how many this call summarised first.
    Raises re.error for a bad pattern, with the pattern in the message.
    """
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        raise re.error(f"bad pattern {pattern!r}: {e}") from e
    stats = update(agents) if refresh else None
    data = load()
    matches = []
    unreadable: set[str] = set()
    for k, entry in data.items():
        if agents and entry.get("agent") not in agents:
            continue
        p = _existing_summary(entry["agent"], entry["id"])
        try:
            if p is None:
                raise FileNotFoundError(k)
            text = p.read_text(encoding="utf-8")
        except OSError:
            unreadable.add(k)            # counted, and not counted as covered: the search did not see it
            continue
        lines = text.splitlines()
        hit = [i for i, line in enumerate(lines) if rx.search(line)]
        if not hit:
            continue
        shown = [lines[i].strip() for i in hit[:max_lines]]
        matches.append({"agent": entry["agent"], "id": entry["id"], "title": entry.get("title"),
                        "project": entry.get("project"), "gone": bool(entry.get("gone")),
                        "summarised": entry.get("summarised"), "lines": shown})
    known = {_key(r) for r in adapters.all_refs(agents, enabled_only=agents is None)}   # the same set `update()` covers
    have = {k for k, e in data.items() if not e.get("gone") and (agents is None or e.get("agent") in agents)} - unreadable
    return {"matches": matches, "summarised": len(have & known), "known": len(known),
            "never": len(known - have), "unreadable": len(unreadable & known),
            "refreshed": stats["summarised"] if stats else 0, "failed": len(stats["failed"]) if stats else 0}


def render_search(res: dict, pattern: str) -> str:
    """The search result as text, for the CLI and the MCP tool alike."""
    lines = []
    for m in res["matches"]:
        head = f"{m['agent']:<13} {m['id'][:8]:<10} {(m['title'] or '')[:60]}"
        if m["gone"]:
            head += "   (file no longer on disk)"
        lines.append(head)
        lines += [f"    {x}" for x in m["lines"]]
    cover = (f"{res['summarised']} of {res['known']} sessions summarised"
             + (f", {res['refreshed']} of them new or changed and summarised just now" if res.get("refreshed") else "")
             + (f"; {res['failed']} could not be read" if res.get("failed") else "")
             + (f"; {res['never']} without a summary" if res["never"] else "")
             + (f"; {res['unreadable']} of them because the summary file could not be read" if res.get("unreadable") else ""))
    if not res["matches"]:
        return f"no summary matches {pattern!r} ({cover})\n"
    return "\n".join(lines) + f"\n\n{len(res['matches'])} session(s) match {pattern!r} ({cover})\n"
