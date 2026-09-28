"""Same agent in, same agent out: a lossless copy, never the fold."""
from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..model import parse_ts
from .common import (_iso, _mkdir, _no_cwd, _read_jsonl, _slug, _store, _uuid7, _write_bytes, _write_jsonl, _write_text,
                     cline_id, cline_index_problem, cline_register, gemini_project_dir)
from .fold import FidelityReport


# --------------------------------------------------------------------------- native (same agent)
# A session of agent X exported "to" agent X needs no conversion at all: the source file already
# IS the target format. Re-flattening it would fold tool calls and drop thinking for no reason, so
# `native_copy` copies the records instead and touches only what MUST change: the session id (so
# the copy does not collide with the original), the working directory when you re-home it, and the
# appended import notice. Nothing is folded, nothing is dropped.
NATIVE = {"claude-code", "codex", "gemini-cli", "cline"}


def _native_copy_cline(source: Path, old_id: str, out_dir: Path, rep: FidelityReport, *, install: bool,
                       redact, notice: str | None, new_cwd: str, counts: dict | None) -> Path:
    """The Cline CLI's two JSON files copied under a fresh id, plus the index row on install.

    Not JSONL, so not the record loop below: the metadata file and the messages file are each
    rewritten as one document. Redaction runs on the serialised text of both, so every occurrence
    (paths inside the system prompt included) is covered, and the id and the messages path are
    swapped after it, on the new values."""
    mp = source.with_name(f"{old_id}.messages.json")
    try:
        meta = json.loads(source.read_text(encoding="utf-8-sig"))
        msgs_doc = json.loads(mp.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"nothing to copy: {source.name} or its messages file could not be read ({e})") from e
    now = datetime.now(timezone.utc)
    new_id = cline_id(int(now.timestamp() * 1000))
    home = _store("cline")
    if install and (why := cline_index_problem(home)):
        raise ValueError(f"not installed: {why}")
    d = (home if install else out_dir) / new_id            # out_dir mirrors the store's home (…/sessions)
    new_mp = d / f"{new_id}.messages.json"
    # text-level pass, exactly like the JSONL copy: redact, then repoint id and path
    def rewrite(doc: dict) -> dict:
        text = json.dumps(doc, ensure_ascii=False)
        if redact:
            text = redact(text)
        text = text.replace(str(mp), str(new_mp)).replace(str(mp).replace("\\", "\\\\"), str(new_mp).replace("\\", "\\\\"))
        return json.loads(text.replace(old_id, new_id))
    meta, msgs_doc = rewrite(meta), rewrite(msgs_doc)
    meta["session_id"] = new_id
    # the real path only where Cline must follow it (an install); an export's copy is text someone may
    # share, and the path under a home folder names the user (it was the one field left unredacted)
    meta["messages_path"] = redact(str(new_mp)) if redact and not install else str(new_mp)
    meta["cwd"] = meta["workspace_root"] = new_cwd
    meta["source"] = "cli"
    msgs_doc["sessionId"] = new_id
    if isinstance(msgs_doc.get("origin"), dict):
        msgs_doc["origin"]["sessionId"] = new_id
    if notice:
        msgs_doc.setdefault("messages", []).append(
            {"id": "msg_ectype_notice", "role": "user", "ts": int(now.timestamp() * 1000),
             "content": [{"type": "text", "text": f'<user_input mode="act">{notice}</user_input>', "thinking": ""}]})
        rep.notes.append("import notice appended as the final user message")
    _write_text(new_mp, json.dumps(msgs_doc, ensure_ascii=False, indent=2))
    p = d / f"{new_id}.json"
    _write_text(p, json.dumps(meta, ensure_ascii=False, indent=2))
    for k, v in (counts or {}).items():
        rep.kept[k] += v
    if install:
        cline_register(home, meta, rep)
        rep.notes.append(f"resume with: cd {new_cwd} && cline --id {new_id}   (interactive; headless needs a terminal, see agentcli)")
    else:
        rep.notes.append(f"to try it: copy {new_id}/ under ~/.cline/data/sessions/ and let `ectype convert --install` add the index row")
    rep.new_id = new_id
    rep.notes.insert(0, f"native copy: {source.name} and its messages file are already cline format, so both were copied "
                        f"as-is (session id {old_id} → {new_id}, cwd {new_cwd})")
    return p


def _native_notice_record(agent: str, text: str, prev: dict | None, sid: str, cwd: str,
                          ts: datetime | None, parent: str | None = None, ordinal: int = 0) -> dict:
    """The import notice in the target's own record shape, appended as a final user turn.

    Claude Code: `parent` is the uuid of the chain's LAST record, whatever its type. Claude Code
    resumes from the leaf with the newest timestamp and walks `parentUuid` back (2.1.282: `vM` picks
    the leaf, the walk follows the parents), so a notice parented on the last *user* record made a
    branch: the resume skipped the session's final answer, or skipped the notice on a timestamp tie.
    `prev` (the last user record) still lends the envelope fields. Codex: `ordinal` continues the
    rollout's own sequence."""
    stamp = _iso(ts)
    if agent == "claude-code":
        return {"parentUuid": parent if parent is not None else (prev or {}).get("uuid"), "isSidechain": False,
                "userType": (prev or {}).get("userType", "external"), "cwd": cwd, "sessionId": sid,
                "version": (prev or {}).get("version", "unknown"), "gitBranch": (prev or {}).get("gitBranch", ""),
                "type": "user", "uuid": str(uuid.uuid4()), "timestamp": stamp,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    if agent == "codex":
        return {"timestamp": stamp, "type": "response_item", "payload": {
            "type": "message", "id": _uuid7(ts), "role": "user",
            "content": [{"type": "input_text", "text": text}]}, "ordinal": ordinal}
    return {"id": secrets.token_hex(16), "timestamp": stamp, "type": "user", "content": [{"text": text}]}


def _redact_record(obj, redact):
    """Redact every string of a decoded record (keys included), for the rare replacement that is not
    valid inside JSON text."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [_redact_record(v, redact) for v in obj]
    if isinstance(obj, dict):
        return {(redact(k) if isinstance(k, str) else k): _redact_record(v, redact) for k, v in obj.items()}
    return obj


def native_copy(agent: str, source: Path, old_id: str, out_dir: Path, rep: FidelityReport, *,
                install: bool = False, redact=None, notice: str | None = None,
                workspace: str | None = None, cwd: str | None = None,
                extra: list[tuple[Path, str]] | None = None, title: str | None = None,
                counts: dict | None = None) -> Path:
    """Copy a session into its OWN agent's store, losslessly. Returns the written path."""
    if agent not in NATIVE:
        raise ValueError(f"no native copy for {agent!r}; native agents: {', '.join(sorted(NATIVE))}")
    new_id = _uuid7() if agent == "codex" else str(uuid.uuid4())
    new_cwd = workspace or cwd or _no_cwd(rep)
    if redact:
        # written into every record AFTER the line-level redaction below, so it must be redacted
        # itself: a redacted copy once kept the real path in `cwd`, `workspace_roots` and
        # Gemini's .project_root while every other occurrence was a placeholder
        new_cwd = redact(new_cwd)
        title = redact(title) if title else title
    if agent == "cline":                                   # two JSON documents, not a record log
        return _native_copy_cline(source, old_id, out_dir, rep, install=install, redact=redact,
                                  notice=notice, new_cwd=new_cwd, counts=counts)
    raw = _read_jsonl(source, rep)
    if not raw:
        raise ValueError(f"nothing to copy: {source.name} holds no records")
    last_ts: datetime | None = None
    newest: datetime | None = None
    for r in raw:                              # the file name of a Codex/Gemini session needs this
        ts_r = parse_ts(r.get("timestamp") or r.get("lastUpdated"))
        last_ts = ts_r or last_ts
        if ts_r and (newest is None or ts_r > newest):
            newest = ts_r
    # the notice is the newest record by a millisecond, so it is the leaf an agent resumes from
    notice_ts = newest + timedelta(milliseconds=1) if newest else None

    if agent == "claude-code":
        home = _store("claude-code")
        dest = (home if install else out_dir) / _slug(new_cwd)
        path = dest / f"{new_id}.jsonl"
        rep.notes.append(f"resume with: cd {new_cwd} && claude --resume {new_id}" if install else
                         f"to try it: copy {path.parent.name}/{path.name} under ~/.claude/projects/ and run `claude --resume {new_id}`")
    elif agent == "codex":
        home = _store("codex")
        base = home if install else out_dir
        local = (last_ts or datetime.now(timezone.utc)).astimezone()
        path = base / "sessions" / local.strftime("%Y/%m/%d") / f"rollout-{local.strftime('%Y-%m-%dT%H-%M-%S')}-{new_id}.jsonl"
        idx = json.dumps({"id": new_id, "thread_name": (title or "imported session")[:60], "updated_at": _iso(last_ts)})
        rep.notes.append(f"resume with: cd {new_cwd} && codex resume {new_id}")
    else:
        home = _store("gemini-cli")
        base = gemini_project_dir(home if install else out_dir, new_cwd)
        local = (last_ts or datetime.now(timezone.utc)).astimezone()
        path = base / "chats" / f"session-{local.strftime('%Y-%m-%dT%H-%M')}-{new_id[:8]}.jsonl"
        rep.notes.append(f"resume with: cd {new_cwd} && gemini --resume {new_id}")

    # Sidecar files the transcript points at (Claude's tool-results spill, Gemini's tool-outputs):
    # planned first, read into memory, and written only after every record converted cleanly, so a
    # record that fails cannot leave orphan copies behind. Destinations follow the target's layout:
    # Claude Code keeps them under `<slug>/<id>/…` (the sub-path after the old id), Gemini under
    # `<project>/tool-outputs/session-<id>/…`, which is also where its reader looks by name.
    planned: list[tuple[str, Path, bytes]] = []
    skipped: list[str] = []
    for src_file, rel in extra or []:
        # only files of THIS session: a store-level index (Codex's session_index.jsonl) is not a
        # sidecar and is written separately below.
        if old_id not in rel:
            continue
        if not src_file.is_file():
            skipped.append(f"{Path(rel).name} (not a file)")
            continue
        parts = Path(rel).parts
        i = next((n for n, p in enumerate(parts) if old_id in p), None)
        tail = Path(*parts[i + 1:]) if i is not None and i + 1 < len(parts) else Path(parts[-1])
        dst = (base / "tool-outputs" / f"session-{new_id}" / tail) if agent == "gemini-cli" else (path.parent / new_id / tail)
        try:
            data = src_file.read_bytes()
        except OSError as e:
            skipped.append(f"{src_file.name} ({type(e).__name__}: {e.strerror or e})")
            continue
        if redact:
            # a spill file is text the transcript points at; it must not keep what the records
            # no longer carry (a redacted copy used to ship the raw tool output beside it)
            data = redact(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        planned.append((str(src_file), dst, data))
    copied = [(src, str(dst)) for src, dst, _ in planned]

    recs: list[dict] = []
    last_user: dict | None = None
    tip: str | None = None                      # the uuid of the chain's last record (Claude Code)
    last_ordinal: int | None = None             # the rollout's own sequence (Codex)
    repointed: set[str] = set()                 # the copied files whose path the records actually named
    for rec in raw:
        line = json.dumps(rec, ensure_ascii=False)
        for old_abs, new_abs in copied:         # exact per-file repoint, before the id swap
            esc_old = old_abs.replace("\\", "\\\\")
            if old_abs in line or esc_old in line:
                repointed.add(old_abs)
            line = line.replace(old_abs, new_abs)
            line = line.replace(esc_old, new_abs.replace("\\", "\\\\"))
        line = line.replace(old_id, new_id)
        if redact:
            red_line = redact(line)
            try:
                r = json.loads(red_line)
            except ValueError:
                # a replacement holding a quote or a backslash breaks the JSON text; redact the
                # decoded record string by string instead, which cannot
                r = _redact_record(json.loads(line), redact)
        else:
            r = json.loads(line)
        if agent == "claude-code":
            # D2: the working directory changes only when the copy is re-homed; a transcript whose
            # records carry several cwds keeps them (a plain copy used to flatten 1,424 of them)
            if workspace and r.get("cwd"):
                r["cwd"] = new_cwd
            if r.get("type") == "user":
                last_user = r
            if r.get("uuid") and not r.get("isSidechain"):
                tip = r["uuid"]
        elif agent == "codex":
            if isinstance(r.get("ordinal"), int):
                last_ordinal = r["ordinal"]
            if workspace:                        # D2: only when re-homed (line-level redaction covers the rest)
                if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict):
                    r["payload"]["cwd"] = new_cwd
                elif r.get("type") == "turn_context" and isinstance(r.get("payload"), dict):
                    if r["payload"].get("cwd"):
                        r["payload"]["cwd"] = new_cwd
                    if r["payload"].get("workspace_roots"):
                        r["payload"]["workspace_roots"] = [new_cwd]
        recs.append(r)
    for k, v in (counts or {}).items():                    # everything survives a native copy
        rep.kept[k] += v
    if notice:
        recs.append(_native_notice_record(agent, notice, last_user, new_id, new_cwd, notice_ts or last_ts,
                                          parent=tip, ordinal=(last_ordinal + 1) if last_ordinal is not None else 0))
        rep.notes.append("import notice appended as the final user message")
    if copied:
        # Say what happened, not what was meant to: a store read through another path than the one
        # the agent wrote into its stubs (a fixture copy, a symlinked home) matches no record, and
        # the reader then finds the files by name under the new session's folder.
        n_re = len(repointed)
        note = f"{len(copied)} sidecar file(s) copied with the session"
        note += (" and their paths repointed" if n_re == len(copied) else
                 f"; {n_re} of their paths repointed, {len(copied) - n_re} not named in the records by the path they were "
                 f"read from (the reader finds them by name under the new session's folder)")
        if redact:
            note += ("; redaction rewrites those paths too, so they no longer resolve on disk"
                     " (expected: every path in a redacted export is a placeholder)")
        rep.notes.append(note)
    if skipped:
        rep.notes.append(f"{len(skipped)} sidecar file(s) could not be copied and are missing from the copy: " + ", ".join(skipped))
    # Write order: sidecars, then the transcript, then (Gemini) the project marker, then (Codex) the
    # store index, last, so a failure leaves no index line pointing at a rollout that is not there.
    for _src, dst, data in planned:
        _write_bytes(dst, data)
    _write_jsonl(path, recs)
    if agent == "gemini-cli" and not (base / ".project_root").exists():
        _write_text(base / ".project_root", new_cwd)
    if agent == "codex":
        if install:
            with open(home / "session_index.jsonl", "a", encoding="utf-8") as fh:
                fh.write(idx + "\n")
        else:
            _write_text(base / "session_index.jsonl", idx + "\n")
    rep.new_id = new_id
    rep.notes.insert(0, f"native copy: {source.name} is already {agent} format, so every record was kept "
                        f"as-is (session id {old_id[:8]} → {new_id[:8]}, cwd {new_cwd})")
    return path
