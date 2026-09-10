"""Same agent in, same agent out: a lossless copy, never the fold."""
from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..model import parse_ts
from .common import _iso, _no_cwd, _read_jsonl, _slug, _store, _uuid7, _write_jsonl
from .fold import FidelityReport


# --------------------------------------------------------------------------- native (same agent)
# A session of agent X exported "to" agent X needs no conversion at all: the source file already
# IS the target format. Re-flattening it would fold tool calls and drop thinking for no reason, so
# `native_copy` copies the records instead and touches only what MUST change: the session id (so
# the copy does not collide with the original), the working directory when you re-home it, and the
# appended import notice. Nothing is folded, nothing is dropped.
NATIVE = {"claude-code", "codex", "gemini-cli"}


def _native_notice_record(agent: str, text: str, prev: dict | None, sid: str, cwd: str,
                          ts: datetime | None) -> dict:
    """The import notice in the target's own record shape, appended as a final user turn."""
    stamp = _iso(ts)
    if agent == "claude-code":
        return {"parentUuid": (prev or {}).get("uuid"), "isSidechain": False,
                "userType": (prev or {}).get("userType", "external"), "cwd": cwd, "sessionId": sid,
                "version": (prev or {}).get("version", "unknown"), "gitBranch": (prev or {}).get("gitBranch", ""),
                "type": "user", "uuid": str(uuid.uuid4()), "timestamp": stamp,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    if agent == "codex":
        return {"timestamp": stamp, "type": "response_item", "payload": {
            "type": "message", "id": _uuid7(ts), "role": "user",
            "content": [{"type": "input_text", "text": text}]}, "ordinal": 0}
    return {"id": secrets.token_hex(16), "timestamp": stamp, "type": "user", "content": [{"text": text}]}


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
    raw = _read_jsonl(source, rep)
    if not raw:
        raise ValueError(f"nothing to copy: {source.name} holds no records")
    last_ts: datetime | None = None
    for r in raw:                              # the file name of a Codex/Gemini session needs this
        last_ts = parse_ts(r.get("timestamp") or r.get("lastUpdated")) or last_ts

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
        (home if install else base).mkdir(parents=True, exist_ok=True)
        with open((home if install else base) / "session_index.jsonl", "a" if install else "w", encoding="utf-8") as fh:
            fh.write(idx + "\n")
        rep.notes.append(f"resume with: cd {new_cwd} && codex resume {new_id}")
    else:
        home = _store("gemini-cli")
        proj = Path(new_cwd).name.lower() or "project"
        base = (home if install else out_dir) / proj
        local = (last_ts or datetime.now(timezone.utc)).astimezone()
        path = base / "chats" / f"session-{local.strftime('%Y-%m-%dT%H-%M')}-{new_id[:8]}.jsonl"
        root = base / ".project_root"
        root.parent.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            root.write_text(new_cwd, encoding="utf-8")
        rep.notes.append(f"resume with: cd {new_cwd} && gemini --resume latest  (Gemini resumes by index, not id)")

    # Sidecar files the transcript points at (Claude's tool-results spill, Gemini's tool-outputs):
    # copy them under the NEW session's folder, keeping the sub-path after the old session id, so
    # the absolute paths written inside the records still resolve after the rewrite below.
    copied: list[tuple[str, str]] = []
    for src_file, rel in extra or []:
        # only files of THIS session: a store-level index (Codex's session_index.jsonl) is not a
        # sidecar and is written separately above.
        if not src_file.is_file() or old_id not in rel:
            continue
        parts = Path(rel).parts
        i = next((n for n, p in enumerate(parts) if old_id in p), None)
        tail = Path(*parts[i + 1:]) if i is not None and i + 1 < len(parts) else Path(parts[-1])
        dst = path.parent / new_id / tail
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = src_file.read_bytes()
            if redact:
                # a spill file is text the transcript points at; it must not keep what the records
                # no longer carry (a redacted copy used to ship the raw tool output beside it)
                data = redact(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
            dst.write_bytes(data)
            copied.append((str(src_file), str(dst)))
        except OSError:
            continue

    recs: list[dict] = []
    last_user: dict | None = None
    for rec in raw:
        line = json.dumps(rec, ensure_ascii=False)
        for old_abs, new_abs in copied:         # exact per-file repoint, before the id swap
            line = line.replace(old_abs, new_abs)
            line = line.replace(old_abs.replace("\\", "\\\\"), new_abs.replace("\\", "\\\\"))
        line = line.replace(old_id, new_id)
        if redact:
            line = redact(line)
        r = json.loads(line)
        if agent == "claude-code":
            if r.get("cwd"):
                r["cwd"] = new_cwd
            if r.get("type") == "user":
                last_user = r
        elif agent == "codex":
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
        recs.append(_native_notice_record(agent, notice, last_user, new_id, new_cwd, last_ts))
        rep.notes.append("import notice appended as the final user message")
    if copied:
        rep.notes.append(f"{len(copied)} sidecar file(s) copied with the session and their paths repointed"
                         + ("; redaction rewrites those paths too, so they no longer resolve on disk"
                            " (expected: every path in a redacted export is a placeholder)" if redact else ""))
    _write_jsonl(path, recs)
    rep.notes.insert(0, f"native copy: {source.name} is already {agent} format, so every record was kept "
                        f"as-is (session id {old_id[:8]} → {new_id[:8]}, cwd {new_cwd})")
    return path
