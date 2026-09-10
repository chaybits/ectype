"""One writer per agent that can be RESUMED from a file this tool wrote: Claude Code, Codex CLI,
Gemini CLI. Each was proven by installing a converted session and resuming it live (see
`tables.TARGETS` for the versions). The target agent writes its own envelope where a template is
given; without one the writer uses minimal defaults that were valid for the versions observed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..model import Session
from .common import _iso, _no_cwd, _read_jsonl, _slug, _store, _uuid7, _write_jsonl
from .fold import FidelityReport, Turn


# --------------------------------------------------------------------------- writers
def write_claude(s: Session, turns: list[Turn], out_dir: Path, template: Path | None, rep: FidelityReport,
                 install: bool, redact=None) -> Path:
    env = {"cwd": None, "version": "unknown", "gitBranch": "", "userType": "external",
           "entrypoint": "cli", "permissionMode": "default"}
    model = "claude"                       # the TARGET's model, never the source agent's
    if template:
        for r in _read_jsonl(template, rep):
            if r.get("type") in ("user", "assistant"):
                for k in env:
                    if r.get(k) is not None:
                        env[k] = r[k]
                if r.get("type") == "assistant" and (r.get("message") or {}).get("model"):
                    model = r["message"]["model"]
                    break
        rep.notes.append(f"envelope from template {template.name}: version {env['version']}, model {model}")
    else:
        rep.notes.append("no template: minimal envelope; pass --template <a real Claude .jsonl> for a version-exact one")
    env["cwd"] = s.cwd or env["cwd"] or _no_cwd(rep)               # source session's cwd wins
    if redact:
        env = {k: (redact(v) if isinstance(v, str) else v) for k, v in env.items()}
    sid = str(uuid.uuid4())
    recs, parent = [], None
    for i, t in enumerate(turns):
        uid = str(uuid.uuid4())
        base = {"parentUuid": parent, "isSidechain": False, "userType": env["userType"], "cwd": env["cwd"],
                "sessionId": sid, "version": env["version"], "gitBranch": env["gitBranch"], "type": t.role,
                "uuid": uid, "timestamp": _iso(t.timestamp)}
        if t.role == "user":
            base["message"] = {"role": "user", "content": [{"type": "text", "text": t.text}]}
            base["entrypoint"] = env["entrypoint"]; base["permissionMode"] = env["permissionMode"]
        else:
            base["message"] = {"id": f"msg_ectype_{i:04d}", "type": "message", "role": "assistant", "model": model,
                               "content": [{"type": "text", "text": t.text}], "stop_reason": "end_turn",
                               "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
            base["requestId"] = f"req_ectype_{i:04d}"
        recs.append(base); parent = uid
    slug = _slug(env["cwd"])
    dest = (_store("claude-code") / slug if install else out_dir / slug)
    p = dest / f"{sid}.jsonl"
    _write_jsonl(p, recs)
    rep.notes.append(f"resume with: cd {env['cwd']} && claude --resume {sid}" if install else
                     f"to try it: copy {p.parent.name}/{p.name} under ~/.claude/projects/ and run `claude --resume {sid}`")
    return p


def write_codex(s: Session, turns: list[Turn], out_dir: Path, template: Path | None, rep: FidelityReport,
                install: bool, redact=None) -> Path:
    meta = {"cwd": None, "originator": "codex-tui", "cli_version": "unknown", "source": "cli",
            "thread_source": "user", "model_provider": "openai", "base_instructions": "", "history_mode": "paginated"}
    tctx: dict = {"approval_policy": "untrusted", "sandbox_policy": "workspace-write", "collaboration_mode": "default",
                  "personality": "default", "realtime_active": False, "model": "unknown", "timezone": "UTC"}
    if template:
        for r in _read_jsonl(template, rep):
            if r.get("type") == "session_meta":
                meta = {**meta, **r["payload"]}          # take the WHOLE envelope, then override below
            elif r.get("type") == "turn_context":
                tctx = dict(r["payload"]); break
        rep.notes.append(f"envelope from template {template.name}: cli {meta['cli_version']}, model {tctx.get('model')}")
    else:
        rep.notes.append("no template: minimal envelope; pass --template <a real rollout-*.jsonl> for a version-exact one")
    meta["cwd"] = s.cwd or meta["cwd"] or _no_cwd(rep)
    meta["base_instructions"] = ""                                 # never carry a template's instructions
    if redact:
        meta = {k: (redact(v) if isinstance(v, str) else v) for k, v in meta.items()}
        tctx = {k: (redact(v) if isinstance(v, str) else v) for k, v in tctx.items()}
    start = turns[0].timestamp if turns and turns[0].timestamp else datetime.now(timezone.utc)
    sid = _uuid7(start)
    recs: list[dict] = []
    o = 0

    def rec(t: str, payload: dict, ts: datetime | None):
        nonlocal o
        recs.append({"timestamp": _iso(ts), "type": t, "payload": payload, "ordinal": o}); o += 1
    # Header in the LEGACY shape, the only one `codex migrate-rollouts` accepts. Codex (0.153) keeps
    # a thread's history in thread_history.sqlite, projected from the rollout; a foreign rollout is
    # never projected on its own, so `--install` runs the official migration right after writing.
    # (A paginated header {history_mode, context_window, ...} loads but resumes with EMPTY history.)
    mp = {"id": sid, "timestamp": _iso(start), "cwd": meta["cwd"], "originator": meta.get("originator", "codex-tui"),
          "cli_version": meta.get("cli_version", "unknown"), "instructions": None}
    rec("session_meta", mp, start)
    rec("world_state", {"full": False, "state": {}}, start)
    # Codex rebuilds a thread's history from `event_msg:item_completed` items (projected into
    # thread_history.sqlite), NOT from response_item records, so every turn emits both.
    tid = None
    turn_start = None
    def ms(ts): return int((ts or start).timestamp() * 1000)
    for t in turns:
        ts_ = t.timestamp or start
        if t.role == "user":
            if tid:   # close the previous turn
                rec("event_msg", {"type": "task_complete", "turn_id": tid, "last_agent_message": last_agent,
                                  "started_at": ms(turn_start) // 1000, "completed_at": ms(ts_) // 1000,
                                  "duration_ms": max(0, ms(ts_) - ms(turn_start))}, ts_)
            tid, turn_start, last_agent = _uuid7(ts_), ts_, ""
            tc = {**tctx, "turn_id": tid, "root_turn_id": tid, "cwd": meta["cwd"],
                  "current_date": ts_.strftime("%Y-%m-%d"), "workspace_roots": [meta["cwd"]]}
            rec("turn_context", tc, ts_)
            rec("event_msg", {"type": "task_started", "turn_id": tid, "started_at": ms(ts_) // 1000,
                              "collaboration_mode_kind": tctx.get("collaboration_mode", "default")}, ts_)
            iid = _uuid7(ts_)
            rec("response_item", {"type": "message", "id": iid, "role": "user",
                                  "content": [{"type": "input_text", "text": t.text}]}, ts_)
            rec("event_msg", {"type": "item_completed", "thread_id": sid, "turn_id": tid,
                              "item": {"type": "UserMessage", "id": iid,
                                       "content": [{"type": "text", "text": t.text, "text_elements": []}]},
                              "started_at_ms": ms(ts_), "completed_at_ms": ms(ts_)}, ts_)
        else:
            if tid is None:          # assistant before any user turn: open a synthetic turn
                tid, turn_start = _uuid7(ts_), ts_
                rec("turn_context", {**tctx, "turn_id": tid, "root_turn_id": tid, "cwd": meta["cwd"],
                                     "current_date": ts_.strftime("%Y-%m-%d"), "workspace_roots": [meta["cwd"]]}, ts_)
            iid = f"msg_ectype_{o:04d}"
            rec("response_item", {"type": "message", "id": iid, "role": "assistant",
                                  "content": [{"type": "output_text", "text": t.text}]}, ts_)
            rec("event_msg", {"type": "item_completed", "thread_id": sid, "turn_id": tid,
                              "item": {"type": "AgentMessage", "id": iid, "content": [{"type": "Text", "text": t.text}],
                                       "phase": "final_answer"},
                              "started_at_ms": ms(ts_), "completed_at_ms": ms(ts_)}, ts_)
            last_agent = t.text
    if tid:
        end_ts = turns[-1].timestamp or start
        rec("event_msg", {"type": "task_complete", "turn_id": tid, "last_agent_message": last_agent,
                          "started_at": ms(turn_start) // 1000, "completed_at": ms(end_ts) // 1000,
                          "duration_ms": max(0, ms(end_ts) - ms(turn_start))}, end_ts)
    local = start.astimezone()
    home = _store("codex")
    base = home if install else out_dir
    p = base / "sessions" / local.strftime("%Y/%m/%d") / f"rollout-{local.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
    _write_jsonl(p, recs)
    title = (s.title or "imported session")[:60]
    idx_line = json.dumps({"id": sid, "thread_name": title, "updated_at": _iso(turns[-1].timestamp if turns else None)})
    if install:
        with open(home / "session_index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(idx_line + "\n")
        # Codex resumes a LEGACY-header rollout directly; migrating it into paginated storage is
        # optional housekeeping. Try it, but never let its failure look like a broken conversion.
        import shutil, subprocess
        exe = shutil.which("codex")
        rep.notes.append(f"resume with: cd {meta['cwd']} && codex resume {sid}")
        if exe:
            try:
                r = subprocess.run([exe, "migrate-rollouts", "--thread", sid, "--apply"], capture_output=True, text=True, timeout=120)
                out = r.stdout + r.stderr
                m = re.search(r"Scanned \d+ rollout\(s\): (\d+) migrated, (\d+) already paginated, \d+ skipped.*?(\d+) failed", out)
                ok = bool(m) and int(m.group(1)) + int(m.group(2)) >= 1 and int(m.group(3)) == 0
                tail = [l for l in out.strip().splitlines() if l.strip()][-1:] or ["(no output)"]
                rep.notes.append("migrated into Codex's paginated thread history" if ok else
                                 f"not migrated to paginated storage (Codex still resumes the legacy rollout); migrator said: {tail[0][:120]!r}"
                                 f"; optional later: codex migrate-rollouts --thread {sid} --apply")
            except (subprocess.SubprocessError, OSError) as e:
                rep.notes.append(f"migration skipped ({type(e).__name__}); optional later: codex migrate-rollouts --thread {sid} --apply")
        else:
            rep.notes.append(f"optional later (codex not on PATH): codex migrate-rollouts --thread {sid} --apply")
    else:
        (base / "session_index.jsonl").write_text(idx_line + "\n", encoding="utf-8")
        rep.notes.append(f"to try it: copy sessions/… under $CODEX_HOME, append session_index.jsonl, run "
                         f"`codex migrate-rollouts --thread {sid} --apply`, then `codex resume {sid}`")
    return p


def write_gemini(s: Session, turns: list[Turn], out_dir: Path, template: Path | None, rep: FidelityReport,
                 install: bool, redact=None) -> Path:
    cwd = s.cwd or _no_cwd(rep)
    if redact:
        cwd = redact(cwd)
    project_hash = hashlib.sha256(cwd.encode()).hexdigest()
    kind = "main"
    if template:
        recs_t = _read_jsonl(template, rep)
        if recs_t and "sessionId" in recs_t[0]:
            project_hash = recs_t[0].get("projectHash") or project_hash
            kind = recs_t[0].get("kind") or kind
        rep.notes.append(f"header from template {template.name} (projectHash reused, only valid for the same project)")
    else:
        rep.notes.append("no template: projectHash = sha256(cwd), which may not match Gemini CLI's own hashing")
    start = turns[0].timestamp if turns and turns[0].timestamp else datetime.now(timezone.utc)
    end = turns[-1].timestamp if turns and turns[-1].timestamp else start
    sid = str(uuid.uuid4())
    recs: list[dict] = [{"sessionId": sid, "projectHash": project_hash, "startTime": _iso(start),
                         "lastUpdated": _iso(end), "kind": kind}]
    for t in turns:
        mid = secrets.token_hex(16)
        if t.role == "user":
            recs.append({"id": mid, "timestamp": _iso(t.timestamp), "type": "user", "content": [{"text": t.text}]})
        else:
            recs.append({"id": mid, "timestamp": _iso(t.timestamp), "type": "gemini", "content": t.text,
                         "model": "imported"})
    recs.append({"$set": {"lastUpdated": _iso(end)}})
    proj = Path(cwd).name.lower() or "project"
    home = _store("gemini-cli")
    base = (home if install else out_dir) / proj
    local = start.astimezone()
    p = base / "chats" / f"session-{local.strftime('%Y-%m-%dT%H-%M')}-{sid[:8]}.jsonl"
    _write_jsonl(p, recs)
    root = base / ".project_root"
    if not root.exists():
        root.write_text(cwd, encoding="utf-8")
    rep.notes.append(f"resume with: cd {cwd} && gemini --resume latest   (Gemini resumes by index/'latest', not by id)" if install else
                     f"to try it: copy {proj}/ under ~/.gemini/tmp/ and run `gemini --resume latest` from {cwd}")
    return p


WRITERS = {"claude-code": write_claude, "codex": write_codex, "gemini-cli": write_gemini}
