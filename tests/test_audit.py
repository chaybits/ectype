"""Regression checks for the 2026-09-10 audit: one function per finding (the report is kept in the workbench),
plus later defects found the same way (a `test_f2_*` name is a feedback round, not an audit finding).

    python3 tests/test_audit.py

Every store here is synthetic and lives in a temp dir; no real session is read, and the settings
file is a throwaway. A check names the defect it guards, so a failure says what came back.
"""
# publish-safe:path-placeholders: the paths asserted below are the redactor's own placeholders
# (/home/user, C:\Users\user), never a real one.
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = Path(tempfile.mkdtemp(prefix="ectype-audit-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "settings.json")

from ectype import adapters, budget, convert as conv, settings, tokens  # noqa: E402
from ectype.jsonl import iter_jsonl, read_jsonl                        # noqa: E402
from ectype.model import ContentBlock, Message, Session                # noqa: E402
from ectype.render import formats                                      # noqa: E402
from ectype.transform import Redactor                                  # noqa: E402

FAILS: list[str] = []
TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def claude_store(name: str, sessions: dict[str, list[dict]], spills: dict[str, bytes] | None = None) -> Path:
    """A Claude Code store under TMP: `<name>/-tmp-proj/<sid>.jsonl`, plus optional spill files
    `<sid>/tool-results/<file>` given as {"<sid>/<file>": bytes}."""
    home = TMP / name
    proj = home / "-tmp-proj"
    proj.mkdir(parents=True, exist_ok=True)
    for sid, recs in sessions.items():
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    for key, data in (spills or {}).items():
        sid, fname = key.split("/", 1)
        d = proj / sid / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_bytes(data)
    return home


def rec(sid: str, t: str, uid: str, parent: str | None, sec: int, content, model: str | None = None) -> dict:
    m = {"role": "user" if t == "user" else "assistant", "content": content}
    if model:
        m["model"] = model
    return {"type": t, "uuid": uid, "parentUuid": parent, "cwd": "/tmp/proj", "sessionId": sid, "version": "2.1.0",
            "timestamp": f"2026-09-10T10:00:{sec:02d}.000Z", "message": m}


def with_env(**env):
    """Context manager: set env vars for the block, restore after."""
    class _Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in env}
            for k, v in env.items():
                os.environ[k] = v
        def __exit__(self, *a):
            for k, v in self.old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _Ctx()


# --------------------------------------------------------------------------- F08 / F32
def test_f08_jsonl_reader_keeps_the_first_record_behind_a_bom() -> None:
    """`convert._read_jsonl` read strict utf-8 and passed over a JSONDecodeError, so a BOM-prefixed
    rollout lost its `session_meta` (the record Codex resumes from) without a word."""
    p = TMP / "bom.jsonl"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"type": "session_meta"}).encode() + b"\n"
                  + b"not json at all\n" + json.dumps({"type": "turn_context"}).encode() + b"\n")
    recs, bad = read_jsonl(p)
    check([r["type"] for r in recs] == ["session_meta", "turn_context"], f"a BOM does not cost the first record ({[r['type'] for r in recs]})")
    check(bad == [2], f"the unparseable line is counted by number ({bad})")
    rep = conv.FidelityReport("codex", "codex")
    conv._read_jsonl(p, rep)
    check(any("could not be parsed" in n and "2" in n for n in rep.notes), "the converter reports the skipped line in the fidelity notes")
    seen = list(iter_jsonl(p))
    check(seen[0][0] == 1 and seen[1][0] == 3, f"line numbers are 1-based and skip the bad line ({[n for n, _ in seen]})")


# --------------------------------------------------------------------------- F06
def test_f06_install_honours_the_store_saved_in_settings() -> None:
    """The writers resolved the target store as `env var or default` and never read Settings, so a
    store path saved in the GUI was honoured for reading and ignored for `--install`."""
    store = TMP / "settings-only-claude-store"
    settings.save({"agents": {"claude-code": {"enabled": True, "home": str(store)}}})
    s = Session("codex", "f06-src", TMP / "nowhere.jsonl", [Message(0, "user", TS, [ContentBlock("text", "hi")]),
                                                              Message(1, "assistant", TS, [ContentBlock("text", "yo")])], cwd="/w")
    assert "ECTYPE_CLAUDE_HOME" not in os.environ
    path, _rep = conv.convert(s, "claude-code", TMP / "unused-out", install=True)
    check(str(path).startswith(str(store)), f"install lands in the Settings store ({path})")
    settings.save({})


# --------------------------------------------------------------------------- F14
def test_f14_a_session_without_a_cwd_is_filed_under_home_not_the_process_cwd() -> None:
    """Fallback was `Path.cwd()` of the ectype process: from the desktop launcher, the tool's own
    source folder, so a Copilot Chat or LM Studio session (no cwd recorded) landed under a slug
    carrying that path."""
    s = Session("codex", "f14-src", TMP / "nowhere.jsonl", [Message(0, "user", TS, [ContentBlock("text", "hi")]),
                                                              Message(1, "assistant", TS, [ContentBlock("text", "yo")])], cwd=None)
    out = TMP / "f14-out"
    path, rep = conv.convert(s, "claude-code", out)
    check(path.parent.name == conv._slug(str(Path.home())), f"filed under the home slug ({path.parent.name})")
    check(str(Path.cwd()) not in str(path), "the process cwd is not used")
    check(any("no working directory recorded" in n for n in rep.notes), "the fidelity report says why")


# --------------------------------------------------------------------------- F30
def test_f30_truncate_reports_the_total_it_already_counted() -> None:
    """The cap path tokenized every capped result twice: `truncate()` encoded the whole text, then
    `count(b.text)` encoded it again for the "does the cap save anything" check."""
    text = "word " * 400
    kept, cut, total = tokens.truncate(text, 10)
    check(total == tokens.count(text), f"truncate's total equals count() ({total} vs {tokens.count(text)})")
    check(cut == total - tokens.count(kept), f"kept + cut == total ({tokens.count(kept)} + {cut} = {total})")
    kept2, cut2, total2 = tokens.truncate("short", 10)
    check(kept2 == "short" and cut2 == 0 and total2 == tokens.count("short"), "a text that fits is returned whole with its count")


# --------------------------------------------------------------------------- F27
def test_f27_a_missing_timestamp_is_as_wide_as_a_real_one() -> None:
    from ectype.render.text import _fmt
    real = _fmt(TS, False)
    check(len(_fmt(None, True)) == len(real), f"full placeholder {_fmt(None, True)!r} matches {real!r} in width")
    check(len(_fmt(None, True, style="time")) == len(_fmt(TS, False, style="time")), "time placeholder matches too")


# --------------------------------------------------------------------------- F05
def test_f05_full_mode_prints_every_argument() -> None:
    """Tool-call arguments were clipped at a hard 200 characters in every text-based format and
    every mode; the README promised `full` = every argument."""
    s = Session("claude-code", "f05", TMP / "x.jsonl", [
        Message(0, "user", TS, [ContentBlock("text", "go")]),
        Message(1, "assistant", TS, [ContentBlock("tool_call", name="Bash", args={"command": "echo " + "x" * 600})]),
        Message(2, "tool", TS, [ContentBlock("tool_result", "ok")])], cwd="/w", started=TS, ended=TS)
    for fmt in ("text", "markdown", "html", "csv"):
        full = formats.export(budget.filtered(s, {"mode": "full"}), fmt, budget.render_options({"mode": "full"}), source=s)
        custom = formats.export(budget.filtered(s, {"mode": "custom", "cap": 150}), fmt, budget.render_options({"mode": "custom", "cap": 150}), source=s)
        check("x" * 600 in full and "…[+" not in full.split("\n", 8)[-1], f"full mode {fmt}: all 600 argument characters, no cut marker")
        check("x" * 600 not in custom and "…[+" in custom, f"custom mode {fmt}: arguments cut and marked")
    b = budget.breakdown(s, {"mode": "full", "tools": True, "thinking": True, "env": True, "cap": 0})
    check(b["selected"] == b["everything"], "with everything on, selected still equals the ceiling")
    bc = budget.breakdown(s, {"mode": "custom", "tools": True, "thinking": True, "env": True, "cap": 150})
    check(sum(p["tokens"] for p in bc["parts"]) + bc["structure"] == bc["selected"], "with arguments cut, the table still adds up to selected")
    check(next(p["tokens"] for p in bc["parts"] if p["key"] == "tool_calls") < next(p["tokens"] for p in b["parts"] if p["key"] == "tool_calls"),
          "the tool-calls row is smaller when arguments are cut")


# --------------------------------------------------------------------------- F01
def test_f01_redaction_reaches_session_and_message_meta() -> None:
    """`Redactor.session()` redacted title, cwd, project, path and every block, never `meta`, so a
    `--redact --format json` export kept the real source path in `meta.source_file`."""
    s = Session("claude-code", "f01", Path("/secret-root/x.jsonl"), [Message(0, "user", TS, [ContentBlock("text", "hi")], meta={"note": "/secret-root/y"})],
                cwd="/secret-root", meta={"source_file": "/secret-root/x.jsonl", "subagents": ["/secret-root/agent-1.jsonl"]})
    red = Redactor.defaults(extra=["/secret-root=ROOT"], machine=False)
    out = formats.export(red.session(s), "json", budget.render_options({}), source=s)
    check("/secret-root" not in out, "a redacted json export carries no unredacted meta path")
    check("ROOT/x.jsonl" in out and "ROOT/agent-1.jsonl" in out, "session meta strings are replaced, lists included")
    check('"note": "ROOT/y"' in out, "message meta is replaced too")


# --------------------------------------------------------------------------- F02
def test_f02_an_invalid_byte_in_a_spill_file_does_not_kill_the_export() -> None:
    """The spill was read with `surrogateescape` and every sink writes strict UTF-8, so one 0xFF in a
    Claude `tool-results/*.txt` raised UnicodeEncodeError from `show`, every `export` and the GUI,
    reported by the CLI as the bare `ectype: utf-8`."""
    sid = "f02f02f0-0000-0000-0000-000000000001"
    spill_name = "deadbeef.txt"
    home = claude_store("f02", {}, {f"{sid}/{spill_name}": b"line one\n\xff\xfe junk\nline three\n"})
    spill = home / "-tmp-proj" / sid / "tool-results" / spill_name
    stub = f"<persisted-output>Output too large (1KB). Full output saved to: {spill}\n\nPreview (first 2KB):\nline one</persisted-output>"
    claude_store("f02", {sid: [rec(sid, "user", "u1", None, 0, [{"type": "text", "text": "run"}]),
                               rec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat f"}}], "m"),
                               rec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": stub}]),
                               rec(sid, "assistant", "a2", "u2", 3, [{"type": "text", "text": "done"}], "m")]})
    with with_env(ECTYPE_CLAUDE_HOME=str(home)):
        ad = adapters.get("claude-code")
        s = ad.load(next(r for r in ad.discover() if r.id == sid))
        res = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"][0]
        check(res.meta.get("restored_from_spill") is not None and "line three" in res.text, "the spill file is restored")
        out = formats.export(budget.filtered(s, {"mode": "full"}), "text", budget.render_options({"mode": "full"}), source=s)
        ok = True
        try:
            out.encode("utf-8")
        except UnicodeEncodeError:
            ok = False
        check(ok and "�" in out, "the export encodes as UTF-8 and marks the bad byte with U+FFFD")


# --------------------------------------------------------------------------- F03 / F16
def test_f03_roo_code_stamps_come_from_the_conversation_file() -> None:
    """Per-message timestamps were `ui_messages[i]` for API entry i, although the UI log holds
    several events per turn, so most messages got the time of an unrelated earlier event."""
    home = TMP / "roo"; d = home / "task-1"; d.mkdir(parents=True, exist_ok=True)
    t0 = 1_760_000_000_000
    (d / "api_conversation_history.json").write_text(json.dumps([
        {"role": "user", "ts": t0, "content": [{"type": "text", "text": "<task>go</task>"}]},
        {"role": "assistant", "ts": t0 + 60_000, "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "c1", "name": "ls", "input": {}}]},
        {"role": "user", "ts": t0 + 61_000, "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "errors.py\nmain.py\n"}]},
        {"role": "assistant", "ts": t0 + 120_000, "content": [{"type": "tool_use", "id": "c2", "name": "cat", "input": {}}]},
        {"role": "user", "ts": t0 + 121_000, "content": [{"type": "tool_result", "tool_use_id": "c2", "content": "Error: no such file"}]},
    ]), encoding="utf-8")
    (d / "ui_messages.json").write_text(json.dumps([{"type": "say", "say": k, "ts": t0 + i * 5_000} for i, k in
                                                    enumerate(["text", "api_req_started", "tool", "api_req_started", "text", "api_req_started", "error", "completion_result"])]), encoding="utf-8")
    (d / "history_item.json").write_text(json.dumps({"task": "go", "ts": t0, "workspace": "/w"}), encoding="utf-8")
    with with_env(ECTYPE_ROO_HOME=str(home)):
        ad = adapters.get("roo-code"); s = ad.load(ad.discover()[0])
    stamps = [int(m.timestamp.timestamp() * 1000) for m in s.messages]
    check(stamps == [t0, t0 + 60_000, t0 + 61_000, t0 + 120_000, t0 + 121_000], f"every message carries the conversation file's own ts ({[(x - t0) // 1000 for x in stamps]} s)")
    check(s.ended is not None and int(s.ended.timestamp() * 1000) == t0 + 35_000, "the session end still comes from the UI log")
    results = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"]
    check(results[0].is_error is False, "a listing that starts with 'errors.py' is not an error (F16)")
    check(results[1].is_error is True, "'Error: no such file' still is")


# --------------------------------------------------------------------------- F09
def test_f09_no_adapter_cuts_a_tool_result_at_load_time() -> None:
    """Continue (8,000), Copilot Chat (2,000 / 4,000), Open WebUI and LM Studio (4,000) sliced results
    with no `truncated` flag, so `full` was not full and the ceiling understated those agents."""
    home = TMP / "continue"; home.mkdir(exist_ok=True)
    (home / "s1.json").write_text(json.dumps({"sessionId": "s1", "title": "t", "workspaceDirectory": "/w", "history": [
        {"message": {"role": "user", "content": "go"}},
        {"message": {"role": "assistant", "content": "ok"}, "toolCallStates": [{"status": "done", "toolCall": {"id": "c1", "function": {"name": "run", "arguments": "{}"}}, "output": "y" * 20000}]}]}), encoding="utf-8")
    with with_env(ECTYPE_CONTINUE_HOME=str(home)):
        c = adapters.get("continue"); s = c.load(c.discover()[0])
    res = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"][0]
    check(len(res.text) == 20000, f"a 20,000-char Continue result loads whole ({len(res.text)})")
    import re as _re
    src = "".join((Path(__file__).resolve().parents[1] / "ectype" / "adapters" / f).read_text(encoding="utf-8")
                  for f in ("copilot_chat.py", "continue_dev.py", "open_webui.py", "lmstudio.py"))
    check(not _re.search(r"\[:\d{4,}\]", src), "no adapter slices a result with a four-digit literal")


# --------------------------------------------------------------------------- F10
def test_f10_antigravity_side_workspaces_follow_the_log() -> None:
    """`_side_workspaces()` memoised on the singleton adapter with no key, so a GUI left running never
    saw the workspace of a headless run started after launch."""
    ag = TMP / "agy"; (ag / "brain").mkdir(parents=True, exist_ok=True)
    (ag / "cli.log").write_text("Initializing CLI store manager for workspace /w1\nCreated conversation 11111111-1111-1111-1111-111111111111\n", encoding="utf-8")
    with with_env(ECTYPE_ANTIGRAVITY_HOME=str(ag)):
        a = adapters.get("antigravity"); a._side_ws = None
        first = dict(a._side_workspaces())
        with open(ag / "cli.log", "a", encoding="utf-8") as fh:
            fh.write("Initializing CLI store manager for workspace /w2\nCreated conversation 22222222-2222-2222-2222-222222222222\n")
        second = a._side_workspaces()
        check(len(first) == 1 and len(second) == 2, f"a conversation logged after the first read is seen ({len(first)} -> {len(second)})")
        check(a._side_workspaces() is second, "an unchanged log is served from the memo")


# --------------------------------------------------------------------------- F13 / F18
def test_f13_open_webui_node_missing_from_its_parents_children_does_not_crash() -> None:
    """`parent['childrenIds'].index(n['id'])` raised ValueError for an upserted node whose parent never
    listed it. Also F18: an unreadable SQLite store lists nothing instead of aborting every agent."""
    import sqlite3
    home = TMP / "owui"; home.mkdir(exist_ok=True)
    db = home / "webui.db"
    con = sqlite3.connect(db)
    con.execute("create table chat (id text primary key, user_id text, title text, created_at integer, updated_at integer, archived integer, pinned integer, meta text, current_message_id text, chat text)")
    tree = {"u1": {"role": "user", "content": "hi", "parentId": None, "childrenIds": ["a1"], "timestamp": 1700000000},
            "a1": {"role": "assistant", "content": "first", "parentId": "u1", "childrenIds": [], "timestamp": 1700000001},
            "a2": {"role": "assistant", "content": "regenerated", "parentId": "u1", "childrenIds": [], "timestamp": 1700000002}}
    chat = {"title": "t", "models": ["m"], "messages": [], "history": {"currentId": "a2", "messages": tree}}
    con.execute("insert into chat values (?,?,?,?,?,?,?,?,?,?)", ("c1", "user", "t", 1700000000, 1700000002, 0, 0, "{}", "a2", json.dumps(chat)))
    con.commit(); con.close()
    with with_env(ECTYPE_OPENWEBUI_HOME=str(home)):
        ad = adapters.get("open-webui"); refs = ad.discover()
        check(len(refs) == 1, "the synthetic Open WebUI store lists its chat")
        s = ad.load(refs[0])
        texts = [m.texts() for m in s.messages]
        check(texts == ["hi", "regenerated"], f"the current branch loads through the unlisted node ({texts})")
        check(s.messages[1].blocks[0].meta.get("swipe") == "1/2", f"the unlisted node reports position 1 of 2 ({s.messages[1].blocks[0].meta.get('swipe')})")
        art = ad.artifacts(refs[0])
        check(art and art[0][0].exists(), "artifacts() still builds the single-chat database")
    bad = TMP / "owui-bad"; bad.mkdir(exist_ok=True); (bad / "webui.db").write_text("not a database", encoding="utf-8")
    with with_env(ECTYPE_OPENWEBUI_HOME=str(bad)):
        check(adapters.get("open-webui").discover() == [], "a file that is not SQLite lists nothing and raises nothing")
    badc = TMP / "cursor-bad"; badc.mkdir(exist_ok=True); (badc / "state.vscdb").write_text("not a database", encoding="utf-8")
    with with_env(ECTYPE_CURSOR_HOME=str(badc)):
        check(adapters.get("cursor").discover() == [], "same for Cursor")


# --------------------------------------------------------------------------- F2 (feedback round)
def test_f2_open_webui_tool_trace_lives_in_the_output_parts() -> None:
    """A tool-enabled Open WebUI turn writes NO message-level `tool_calls`. The call and its result
    are parts of `output`, beside the reasoning. The adapter read only `type == "reasoning"` there,
    so every tool call in every Open WebUI chat was silently dropped: the shape below was captured
    from a real tool run on Open WebUI 0.11.3 on 2026-09-10, which is what found it."""
    import sqlite3
    home = TMP / "owui-tools"; home.mkdir(exist_ok=True)
    con = sqlite3.connect(home / "webui.db")
    con.execute("create table chat (id text primary key, user_id text, title text, created_at integer, updated_at integer, archived integer, pinned integer, meta text, current_message_id text, chat text)")
    out = [
        {"type": "reasoning", "status": "completed", "start_tag": "<think>", "end_tag": "</think>",
         "attributes": {"type": "reasoning_content"},
         "content": [{"type": "output_text", "text": "The user wants the probe tool.\n"}], "summary": None},
        {"type": "function_call", "id": "gKC2tbGpu0YN", "call_id": "gKC2tbGpu0YN",
         "name": "get_probe_value", "arguments": '{"city": "Ankara"}', "status": "completed"},
        {"type": "function_call_output", "id": "fco_dffe6fc3", "call_id": "gKC2tbGpu0YN",
         "output": [{"type": "input_text", "text": "PROBE_OK city=Ankara value=42"}], "status": "completed"},
        {"type": "function_call", "id": "c2", "call_id": "c2", "name": "broken_tool",
         "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "id": "fco_2", "call_id": "c2", "output": "boom", "status": "error"},
    ]
    tree = {"u1": {"id": "u1", "role": "user", "content": "call the probe tool", "parentId": None,
                   "childrenIds": ["a1"], "timestamp": 1700000000},
            "a1": {"id": "a1", "role": "assistant", "content": "PROBE_OK city=Ankara value=42",
                   "parentId": "u1", "childrenIds": [], "timestamp": 1700000001, "output": out}}
    chat = {"title": "probe", "models": ["m"], "messages": [], "history": {"currentId": "a1", "messages": tree}}
    con.execute("insert into chat values (?,?,?,?,?,?,?,?,?,?)",
                ("c1", "user", "probe", 1700000000, 1700000001, 0, 0, "{}", "a1", json.dumps(chat)))
    con.commit(); con.close()
    with with_env(ECTYPE_OPENWEBUI_HOME=str(home)):
        ad = adapters.get("open-webui")
        s = ad.load(ad.discover()[0])
    blocks = s.messages[1].blocks
    kinds = [b.kind for b in blocks]
    check(kinds == ["thinking", "tool_call", "tool_result", "tool_call", "tool_result", "text"],
          f"the output parts become thinking, call, result, call, result, text in order ({kinds})")
    call, res = blocks[1], blocks[2]
    check(call.name == "get_probe_value" and call.args == '{"city": "Ankara"}',
          f"the call carries its name and arguments ({call.name}, {call.args})")
    check(call.call_id == res.call_id == "gKC2tbGpu0YN", "the result is tied to its call by call_id")
    check(res.text == "PROBE_OK city=Ankara value=42" and not res.is_error,
          f"a list-of-parts output flattens to its text ({res.text!r})")
    check(blocks[4].is_error and blocks[4].text == "boom",
          "a status other than completed marks the result as an error")


def test_f2_backup_copies_a_session_and_puts_it_back() -> None:
    """`ectype backup` is the answer to "point it at data you can afford to lose", which was advice
    rather than a feature. A backup copies every file of a session (transcript AND sidecars),
    records where each came from, and restores to exactly those paths. `at_risk` stays honest: an
    install that only adds a file puts nothing at risk, and Codex is the one writer that appends to
    a store file that already exists."""
    from ectype import backup as bk
    spill = {"aaaa2222-0000-0000-0000-00000000000b/out.txt": b"the full tool output"}
    home = claude_store("bk", {"aaaa2222-0000-0000-0000-00000000000b":
                               [rec("aaaa2222-0000-0000-0000-00000000000b", "user", "u1", None, 0, "hello")]},
                        spills=spill)
    cfg = TMP / "bk-config"; cfg.mkdir(exist_ok=True)
    with with_env(ECTYPE_CLAUDE_HOME=str(home), ECTYPE_CONFIG=str(cfg / "settings.json")):
        ad = adapters.get("claude-code")
        ref = ad.discover()[0]
        folder = bk.save([src for src, _ in ad.artifacts(ref)], "test", agent="claude-code")
        check(folder is not None and folder.parent == cfg / "backups", f"the backup lands beside the settings file ({folder})")
        man = json.loads((folder / bk.MANIFEST).read_text(encoding="utf-8"))
        check(len(man["files"]) == 2, f"the transcript AND its spill file are copied ({len(man['files'])})")
        check(all(Path(f["from"]).exists() for f in man["files"]), "the manifest records real source paths")
        # break the original, then put it back
        ref.path.write_text("ruined\n", encoding="utf-8")
        dry = bk.restore(folder.name, dry_run=True)
        check(ref.path.read_text(encoding="utf-8") == "ruined\n", "a dry run writes nothing")
        check(len(dry) == 2 and all(ok for _, ok in dry), "a dry run still names every destination")
        done = bk.restore(folder.name)
        check(len(done) == 2 and ref.path.read_text(encoding="utf-8") != "ruined\n", "restoring puts the transcript back")
        check(adapters.get("claude-code").load(ref).messages, "the restored session parses again")
        check(bk.save([TMP / "does-not-exist"], "nothing") is None, "nothing to save is None, not an empty folder")
        names = [b["name"] for b in bk.listing()]
        check(folder.name in names, "the backup shows up in the listing")
    # the database names carry a schema number (0.153: thread_history_1, state_5); the fixed name this
    # once asserted did not exist, so nothing was backed up (third audit, F18)
    cx = TMP / "codex-store"
    cx.mkdir(exist_ok=True)
    for n in ("thread_history_1.sqlite", "state_5.sqlite", "logs_2.sqlite"):
        (cx / n).write_bytes(b"")
    check([p.name for p in bk.at_risk("codex", cx)] == ["session_index.jsonl", "thread_history_1.sqlite", "state_5.sqlite"],
          "Codex names the index and the versioned databases its install modifies, not its logs")
    check(bk.at_risk("claude-code", Path("/store")) == [] and bk.at_risk("gemini-cli", Path("/store")) == [],
          "a writer that only adds a file puts nothing at risk")


def test_f2_rename_native_where_the_store_has_a_name_local_everywhere_else() -> None:
    """Renaming writes into the agent's own store only where the store has a place for a name a
    human chose. Claude Code does (`custom-title.json`, the file it writes itself); the other
    twelve do not, so the name is kept in ectype's `titles.json` rather than guessed into someone
    else's database. Either way the new name reaches the list AND a loaded session."""
    from ectype import titles
    home = claude_store("rename", {"aaaa1111-0000-0000-0000-00000000000a":
                                   [rec("aaaa1111-0000-0000-0000-00000000000a", "user", "u1", None, 0, "hello")]})
    cfg = TMP / "rename-config"; cfg.mkdir(exist_ok=True)
    with with_env(ECTYPE_CLAUDE_HOME=str(home), ECTYPE_CONFIG=str(cfg / "settings.json")):
        titles.invalidate()
        ref = adapters.get("claude-code").discover()[0]
        where = adapters.rename(ref, "a name I chose")
        check(where == "native", f"Claude Code takes the name into its own store ({where})")
        side = ref.path.parent / ref.id / "custom-title.json"
        check(side.is_file() and json.loads(side.read_text(encoding="utf-8"))["customTitle"] == "a name I chose",
              "the name is in the custom-title.json the agent itself reads")
        check(not titles.path().exists() or not titles.get("claude-code", ref.id),
              "no stale local copy is kept once the store owns the name")
        check(adapters.all_refs(["claude-code"])[0].title == "a name I chose", "the list shows it")
        check(adapters.load(ref).title == "a name I chose", "a loaded session shows it")

        # the local tier: pretend the store refuses (a read-only mount is the real case)
        ad = adapters.get("claude-code")
        real = ad.rename
        ad.rename = lambda r, t: False
        try:
            where = adapters.rename(ref, "kept on our side")
        finally:
            ad.rename = real
        check(where == "local", f"an agent with nowhere to put it falls back to a local name ({where})")
        check(titles.get("claude-code", ref.id) == "kept on our side", "the local name is in titles.json")
        check(titles.path().parent == cfg, "titles.json sits beside the settings file, not inside it")
        check(adapters.load(ref).title == "kept on our side", "the local name wins for a loaded session")
        adapters.rename(ref, "")
        check(not titles.get("claude-code", ref.id), "an empty name clears the local one")
        titles.invalidate()


# --------------------------------------------------------------------------- F17
def test_f17_lmstudio_title_with_a_comma() -> None:
    lm = TMP / "lms"; lm.mkdir(exist_ok=True)
    (lm / "1000.conversation.json").write_text(json.dumps({"name": "Hello, world", "messages": []}), encoding="utf-8")
    (lm / "1001.conversation.json").write_text(json.dumps({"name": "plain", "messages": []}), encoding="utf-8")
    with with_env(ECTYPE_LMSTUDIO_HOME=str(lm)):
        titles = sorted((r.title or "") for r in adapters.get("lmstudio").discover())
    check(titles == ["Hello, world", "plain"], f"the comma survives the title peek ({titles})")


# --------------------------------------------------------------------------- F25
def test_f25_a_short_username_is_redacted_only_inside_paths() -> None:
    """The hostname rule skips names of 3 characters or fewer; the username rule had no guard, so a
    user called `me` had every standalone "me" in the transcript replaced with "user"."""
    import ectype.transform as tr
    real = tr.getpass.getuser
    try:
        tr.getpass.getuser = lambda: "me"
        red = Redactor.defaults(extra=[], options={"home": False, "media": False, "hostname": False, "email": False, "keys": False})
        out = red.text("tell me about /home/me/x and C:\\Users\\me\\y")
    finally:
        tr.getpass.getuser = real
    check(out == "tell me about /home/user/x and C:\\Users\\user\\y", f"paths are redacted, the word is not ({out!r})")


# --------------------------------------------------------------------------- F11 / F12
def test_f11_a_corrupt_settings_file_is_reported_and_kept() -> None:
    """A settings file that failed to parse was treated as absent, silently; the next Save then
    wrote the defaults over the user's text."""
    p = Path(os.environ["ECTYPE_CONFIG"])
    p.write_text('{"view": {"cap": 999}, "redact": {"ip": true},', encoding="utf-8")
    d = settings.load(fresh=True)
    check(d["view"]["cap"] == 150 and settings.load_error() and "999" not in (settings.load_error() or "") and str(p) in settings.load_error(),
          f"defaults apply and the reason is available ({settings.load_error()})")
    settings.save({"view": {"cap": 300}})
    kept = sorted(p.parent.glob(p.name + ".bad-*"))
    check(len(kept) == 1 and '"cap": 999' in kept[0].read_text(encoding="utf-8"), "the user's broken text is kept beside the new file")
    check(json.loads(p.read_text(encoding="utf-8"))["view"]["cap"] == 300 and settings.load_error() is None, "a fresh valid file is written and the error clears")
    check(not list(p.parent.glob(p.name + ".tmp")), "no temp file is left behind (atomic rename)")
    settings.save({})
    for k in kept:
        k.unlink()
    import threading
    errors: list[str] = []
    def hammer(i: int) -> None:
        try:
            for j in range(40):
                budget.everything(Session("x", f"id-{i}-{j}", Path(f"/x/{i}-{j}"), [Message(0, "user", TS, [ContentBlock("text", "x")])]))
                budget.invalidate("x", f"id-{i}-{j}")
        except Exception as e:  # noqa: BLE001, the point is to catch whatever a race raises
            errors.append(f"{type(e).__name__}: {e}")
    ts = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    check(not errors, f"concurrent ceiling reads, inserts and evictions raise nothing ({errors[:2]})")


# --------------------------------------------------------------------------- F04
def test_f04_one_id_under_two_project_folders() -> None:
    """Identity was (agent, id): the GUI served the first folder's file for both list rows, and the
    CLI refused the full id as ambiguous with advice that could not help."""
    sid = "f04f04f0-0000-0000-0000-000000000004"
    home = TMP / "dup"
    for slug, text in (("-home-user", "FIRST"), ("-home-user-proj", "SECOND")):
        proj = home / slug; proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in [
            rec(sid, "user", "u1", None, 0, [{"type": "text", "text": text}]),
            rec(sid, "assistant", "a1", "u1", 1, [{"type": "text", "text": "ok"}], "m")]) + "\n", encoding="utf-8")
    from ectype import cli, web
    with with_env(ECTYPE_CLAUDE_HOME=str(home)):
        refs = [r for r in adapters.get("claude-code").discover() if r.id == sid]
        check(len(refs) == 2, "both files are discovered")
        second = str(home / "-home-user-proj" / f"{sid}.jsonl")
        s, ref = web._load("claude-code", sid, second)
        check(s.messages[0].texts() == "SECOND" and str(ref.path) == second, "the GUI loader honours the row's path")
        s1, _ = web._load("claude-code", sid, str(home / "-home-user" / f"{sid}.jsonl"))
        check(s1.messages[0].texts() == "FIRST", "and the other row still gets the other file (separate cache entries)")
        e1 = budget.everything(s1)["tokens"]; e2 = budget.everything(s)["tokens"]
        check(e1 != e2 or s1.messages[0].texts() != s.messages[0].texts(), "the ceiling cache keys on the file, not the id alone")
        try:
            cli._resolve(sid, "claude-code"); refused = False
        except SystemExit as e:
            refused = "--project" in str(e)
        check(refused, "the CLI refuses two different files with one id and says how to pick")
        check(str(cli._resolve(sid, "claude-code", "-home-user-proj").path) == second, "--project picks the file")
        (home / "-home-user-proj" / f"{sid}.jsonl").write_bytes((home / "-home-user" / f"{sid}.jsonl").read_bytes())
        check(cli._resolve(sid, "claude-code").id == sid, "byte-identical copies resolve on their own")


# --------------------------------------------------------------------------- F07
def test_f07_a_native_download_carries_its_sidecar_files() -> None:
    """Download mode returned the transcript alone; the spill files `native_copy` had copied and
    re-pointed the transcript at were deleted with the temp dir, and the report said "copied"."""
    import http.client, io, threading, urllib.parse, zipfile
    from ectype import web
    sid = "f07f07f0-0000-0000-0000-000000000007"
    home = claude_store("f07", {}, {f"{sid}/spill.txt": b"the full output\n"})
    spill = home / "-tmp-proj" / sid / "tool-results" / "spill.txt"
    stub = f"<persisted-output>Output too large (1KB). Full output saved to: {spill}\n\nPreview (first 2KB):\nthe</persisted-output>"
    claude_store("f07", {sid: [rec(sid, "user", "u1", None, 0, [{"type": "text", "text": "run"}]),
                               rec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}], "m"),
                               rec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": stub}]),
                               rec(sid, "assistant", "a2", "u2", 3, [{"type": "text", "text": "done"}], "m")]})
    srv = web.make_server(0); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with with_env(ECTYPE_CLAUDE_HOME=str(home)):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request("POST", "/api/export", body=json.dumps({"agent": "claude-code", "id": sid, "opts": {}, "format": "native", "mode": "download"}),
                      headers={"Host": f"127.0.0.1:{port}"})
            r = c.getresponse(); raw = r.read(); h = {k.lower(): v for k, v in r.getheaders()}
        check(r.status == 200 and h.get("x-bundle") == "zip" and h.get("content-type") == "application/zip", f"a session with spill files downloads as a zip ({r.status}, {h.get('x-bundle')})")
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
        rel = urllib.parse.unquote(h.get("x-target-relpath", ""))
        check(rel in names and any(n.endswith("/tool-results/spill.txt") for n in names), f"the zip holds the transcript and its spill file ({names})")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- F21 / F23
def test_f21_a_store_that_raises_is_reported_not_hidden() -> None:
    """`agent_info()` swallowed a `discover()` failure into `sessions: null`, so Settings showed a
    tick and no count for a store that could not be read."""
    from ectype import web
    ad = adapters.get("cline"); real = ad.discover
    home = TMP / "cline-broken"; home.mkdir(exist_ok=True)
    try:
        ad.discover = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        with with_env(ECTYPE_CLINE_HOME=str(home)):
            row = next(a for a in web.agent_info() if a["name"] == "cline")
    finally:
        ad.discover = real
    check(row["error"] == "RuntimeError: boom" and row["sessions"] is None, f"the failure text reaches the Settings payload ({row['error']})")
    page = (Path(__file__).resolve().parents[1] / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check("/api/session?" not in page and "api/session\"" not in page, "no caller for the removed /api/session endpoint (F23)")
    check("a.error" in page and "load_error" in page, "the page shows a store error and a settings-file error")


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        fn()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all audit checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
