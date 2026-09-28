"""One writer per agent that can be RESUMED from a file this tool wrote: Claude Code, Codex CLI,
Gemini CLI and the Cline CLI. Each was proven by installing a converted session and resuming it live
(see `tables.TARGETS` for the versions). The target agent writes its own envelope where a template is
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
from .common import (_iso, _no_cwd, _read_jsonl, _slug, _store, _uuid7, _write_jsonl, _write_text, cline_id,
                     cline_index_problem, cline_register, gemini_project_dir)
from .fold import FidelityReport, Turn, banner_is


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
    rep.new_id = sid
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
    rep.new_id = sid
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
        _write_text(base / "session_index.jsonl", idx_line + "\n")
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
    home = _store("gemini-cli")
    base = gemini_project_dir(home if install else out_dir, cwd)      # the folder Gemini itself maps cwd to
    local = start.astimezone()
    p = base / "chats" / f"session-{local.strftime('%Y-%m-%dT%H-%M')}-{sid[:8]}.jsonl"
    _write_jsonl(p, recs)
    rep.new_id = sid
    root = base / ".project_root"
    if not root.exists():
        _write_text(root, cwd)
    # by id, not `latest`: Gemini 0.58 picks "latest" by the header's startTime, which is the source's
    # original start, so an older session installed into an active project was not the one resumed
    rep.notes.append(f"resume with: cd {cwd} && gemini --resume {sid}" if install else
                     f"to try it: copy {base.name}/ under ~/.gemini/tmp/ and run `gemini --resume {sid}` from {cwd}")
    return p


def write_cline(s: Session, turns: list[Turn], out_dir: Path, template: Path | None, rep: FidelityReport,
                install: bool, redact=None) -> Path:
    """Cline CLI (3.0.x): `<store>/<id>/<id>.json` (metadata) + `<id>.messages.json`, plus one row in
    the store's SQLite index `<data>/db/sessions.db` when installing, because `cline history` and
    `cline --id` read the index, not the folders (learned by minting a session, 2026-09-18).

    The store is `~/.cline/data/sessions`; the index sits beside it at `../db/sessions.db`. A
    template is a real `<id>.json` of the CLI (the `.messages.json` next to it supplies the
    envelope of the messages file, system prompt included). User text is wrapped in
    `<user_input mode="act">`, as the CLI writes it; assistant blocks carry both `text` and
    `thinking` keys with the unused one empty, as observed."""
    home = _store("cline")
    if install and (why := cline_index_problem(home)):
        raise ValueError(f"not installed: {why}")
    meta_t: dict = {}
    msgs_t: dict = {}
    if template:
        tp = Path(template)
        try:
            meta_t = json.loads(tp.read_text(encoding="utf-8-sig"))
            mp = tp.with_name(tp.stem + ".messages.json")
            if mp.is_file():
                msgs_t = json.loads(mp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            rep.notes.append(f"template {tp.name} could not be read ({e}); minimal envelope")
        else:
            rep.notes.append(f"envelope from template {tp.name}: cline "
                             f"{((meta_t.get('metadata') or {}).get('sessionHistoryOrigin') or {}).get('version', '?')}, "
                             f"provider {meta_t.get('provider')}, model {meta_t.get('model')}")
    else:
        rep.notes.append("no template: minimal envelope; pass --template <a real <id>.json of the Cline CLI> or --mint-template")
    provider = meta_t.get("provider") or "cline"
    model = meta_t.get("model") or "unknown"
    cli_version = ((meta_t.get("metadata") or {}).get("sessionHistoryOrigin") or {}).get("version") or "unknown"
    cwd = s.cwd or meta_t.get("cwd") or _no_cwd(rep)
    system_prompt = str(msgs_t.get("system_prompt") or "")
    team_name = str(meta_t.get("team_name") or "")
    if redact:
        # the template's prompt and team name are text from another session on this machine (the
        # CLI's prompt names its working directory); redacted like cwd, so a --redact conversion
        # carries no more of the template than of the source
        cwd, system_prompt, team_name = redact(cwd), redact(system_prompt), redact(team_name)
    now = datetime.now(timezone.utc)
    start = turns[0].timestamp if turns and turns[0].timestamp else now
    end = turns[-1].timestamp if turns and turns[-1].timestamp else start

    def ms(ts):
        return int((ts or start).timestamp() * 1000)
    sid = cline_id(ms(start))
    msgs: list[dict] = []
    for i, t in enumerate(turns):
        text = f'<user_input mode="act">{t.text}</user_input>' if t.role == "user" else t.text
        m: dict = {"id": f"msg_ectype_{i:04d}", "role": t.role,
                   "content": [{"type": "text", "text": text, "thinking": ""}], "ts": ms(t.timestamp)}
        if t.role == "assistant":
            m["modelInfo"] = {"id": model, "provider": provider}
        msgs.append(m)
    first_user = next((t.text for t in turns if t.role == "user" and not banner_is(t)), None)
    title = (s.title or first_user or "imported session").splitlines()[0][:100]
    prompt = (first_user or title).splitlines()[0][:500]
    base = home if install else out_dir                       # mirror the store's home (…/sessions): <id>/<id>.json
    d = base / sid
    mpath = d / f"{sid}.messages.json"
    zero = {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalCost": 0}
    metadata = {"sessionHistoryOrigin": {"mode": "user", "version": cli_version}, "title": title,
                "totalCost": 0, "aggregatedAgentsCost": 0, "usage": dict(zero), "aggregateUsage": dict(zero)}
    meta = {"version": meta_t.get("version", 1), "session_id": sid, "source": "cli", "pid": os.getpid(),
            "started_at": _iso(start), "ended_at": _iso(end), "exit_code": 0, "status": "completed",
            "interactive": False, "provider": provider, "model": model, "cwd": cwd, "workspace_root": cwd,
            "team_name": team_name, "enable_tools": bool(meta_t.get("enable_tools", True)),
            "enable_spawn": bool(meta_t.get("enable_spawn", True)), "enable_teams": bool(meta_t.get("enable_teams", True)),
            "prompt": prompt, "metadata": metadata,
            # the real path only where Cline must follow it (an install); an export may be shared
            "messages_path": redact(str(mpath)) if redact and not install else str(mpath)}
    messages_doc = {"version": msgs_t.get("version", 1), "updated_at": _iso(end), "agent": msgs_t.get("agent", "lead"),
                    "sessionId": sid, "origin": {"source": "cli", "mode": "user", "sessionId": sid, "version": cli_version},
                    "system_prompt": system_prompt, "messages": msgs}
    _write_text(mpath, json.dumps(messages_doc, ensure_ascii=False, indent=2))
    p = d / f"{sid}.json"
    _write_text(p, json.dumps(meta, ensure_ascii=False, indent=2))
    if install:
        cline_register(home, meta, rep)
        rep.notes.append(f"resume with: cd {cwd} && cline --id {sid}   (interactive; headless needs a terminal, see agentcli)")
    else:
        rep.notes.append(f"to try it: copy {sid}/ under ~/.cline/data/sessions/ and let `ectype convert --install` add the index row, or add it by hand")
    rep.new_id = sid
    return p


WRITERS = {"claude-code": write_claude, "codex": write_codex, "gemini-cli": write_gemini, "cline": write_cline}
