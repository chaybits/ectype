"""Regression checks for the third from-scratch audit (2026-09-25, the report is kept in the workbench
under notes/audit/): one function per finding, named by the report's F-number.

    python3 tests/test_audit3.py

Every store here is synthetic and lives in a temp dir; no real session is read, the settings file is a
throwaway, and every other agent's store points at nothing. A check names the defect it guards, so a
failure says what came back.
"""
# publish-safe:path-placeholders: the paths asserted below are the redactor's own placeholders
# (/home/user), never a real one.
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ectype-audit3-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
NOTHING = TMP / "nothing-here"
for _v in ("ECTYPE_CLAUDE_HOME", "CODEX_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME", "ECTYPE_AIDER_HOME",
           "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME", "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(NOTHING)
os.environ.pop("CLAUDE_CONFIG_DIR", None)

from ectype import adapters, agentcli, backup as bk, settings  # noqa: E402
from ectype import convert as conv  # noqa: E402
from ectype.convert import common  # noqa: E402
from ectype.model import ContentBlock, Message, Session  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


@contextlib.contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def files_under(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()) if d.exists() else []


# ----------------------------------------------------------------- synthetic stores
def crec(sid, t, uid, parent, sec, content, cwd="/srv/proj", **extra):
    r = {"cwd": cwd, "isSidechain": False, "version": "2.1.0", "type": t, "uuid": uid, "parentUuid": parent,
         "sessionId": sid, "timestamp": f"2026-09-20T10:00:{sec:02d}.000Z", **extra}
    if content is not None:
        r["message"] = {"role": "user" if t == "user" else "assistant", "content": content}
    return r


def claude_store(home: Path, sid: str, recs: list, spills: dict | None = None, slug="-srv-proj") -> Path:
    proj = home / slug
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    for name, data in (spills or {}).items():
        d = proj / sid / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(data, encoding="utf-8")
    return proj


def load_one(agent: str, **homes):
    with env(**homes):
        ad = adapters.get(agent)
        ref = ad.discover()[0]
        return ad, ref, ad.load(ref)


def native(agent: str, home_var: str, home: Path, out: Path, **kw):
    """A native copy of the only session in `home`, the way the CLI calls it."""
    with env(**{home_var: str(home)}):
        ad = adapters.get(agent)
        ref = ad.discover()[0]
        s = ad.load(ref)
        extra = ad.artifacts(ref) if hasattr(ad, "artifacts") else None
        extra = [(Path(p), rel) for p, rel in (extra or [])]
        return conv.convert(s, agent, out, source_path=ref.path, extra=extra, **kw)


# ================================================================= the store writers
def test_f01_claude_native_notice_hangs_on_the_chain_tip() -> None:
    """The notice was parented on the last `user` record; Claude Code resumes from the newest leaf and
    walks the parents, so the final answer (and anything after it) fell off the resumed chain."""
    home, sid = TMP / "f01" / "claude", "f01f01f0-0000-4000-8000-000000000001"
    recs = [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "ask"}]),
            crec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
            crec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]),
            crec(sid, "assistant", "a2", "u2", 3, [{"type": "text", "text": "FINAL ANSWER"}]),
            crec(sid, "system", "s1", "a2", 4, None, subtype="turn_duration")]
    claude_store(home, sid, recs)
    path, rep = native("claude-code", "ECTYPE_CLAUDE_HOME", home, TMP / "f01" / "out", notice="IMPORT NOTICE")
    out = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    notice = out[-1]
    check(notice["parentUuid"] == "s1", f"the notice's parent is the chain's last record (got {notice['parentUuid']!r})")
    check(notice["timestamp"] > max(r["timestamp"] for r in out[:-1]), "the notice is the newest record, so it is the leaf resumed from")
    by = {r["uuid"]: r for r in out if r.get("uuid")}
    chain, q = [], notice
    while q:
        chain.append(q["uuid"])
        q = by.get(q.get("parentUuid"))
    check("a2" in chain, "walking up from the notice reaches the final answer")


def test_f08_codex_native_notice_continues_the_ordinals() -> None:
    home = TMP / "f08" / "codex"
    day = home / "sessions" / "2026" / "09" / "20"
    day.mkdir(parents=True)
    sid = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
    recs = [{"timestamp": "2026-09-20T10:00:00.000Z", "type": "session_meta", "ordinal": 0,
             "payload": {"id": sid, "timestamp": "2026-09-20T10:00:00.000Z", "cwd": "/srv/proj", "history_mode": "paginated"}},
            {"timestamp": "2026-09-20T10:00:01.000Z", "type": "response_item", "ordinal": 1,
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
            {"timestamp": "2026-09-20T10:00:02.000Z", "type": "response_item", "ordinal": 2,
             "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}}]
    (day / f"rollout-2026-09-20T13-00-00-{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    path, _ = native("codex", "CODEX_HOME", home, TMP / "f08" / "out", notice="IMPORT NOTICE")
    out = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    check([r.get("ordinal") for r in out] == [0, 1, 2, 3], f"the notice's ordinal follows the last one ({[r.get('ordinal') for r in out]})")


def test_f05_gemini_files_where_gemini_looks() -> None:
    """Sessions went under `basename(cwd).lower()`; Gemini 0.58 maps a cwd through projects.json, then
    a `.project_root` scan, then its slug."""
    home = TMP / "f05" / "gemini" / "tmp"
    known = home / "my-project"
    known.mkdir(parents=True)
    (known / ".project_root").write_text("/srv/my_project", encoding="utf-8")
    (home.parent / "projects.json").write_text(json.dumps({"projects": {"/srv/my_project": "my-project"}}), encoding="utf-8")
    other = home / "proj-app"
    other.mkdir()
    (other / ".project_root").write_text("/elsewhere/proj app", encoding="utf-8")
    check(common.gemini_project_dir(home, "/srv/my_project") == known, "a registered cwd resolves to its registered folder")
    check(common.gemini_project_dir(home, "/srv/proj app") == home / "proj-app-1", "a slug that another project owns gets -1")
    check(common.gemini_project_dir(home, "/srv/Çalışma Dosyası") == home / "al-ma-dosyas", "a new cwd gets Gemini's slug")
    s = Session(agent="claude-code", id="x", path=TMP / "x.jsonl", cwd="/srv/my_project", messages=[
        Message(0, "user", datetime(2026, 9, 20, 10, tzinfo=timezone.utc), [ContentBlock(kind="text", text="hi")]),
        Message(1, "assistant", datetime(2026, 9, 20, 10, 1, tzinfo=timezone.utc), [ContentBlock(kind="text", text="yo")])])
    with env(ECTYPE_GEMINI_HOME=str(home)):
        path, rep = conv.convert(s, "gemini-cli", TMP / "f05" / "out", install=True, backup=False)
    check(path.parent.parent == known, f"an install lands in the folder Gemini reads for that cwd ({path.parent.parent.name})")
    check(any(f"gemini --resume {rep.new_id}" in n for n in rep.notes), "the resume line names the session id, not `latest`")
    check(agentcli.AGENT_CLI["gemini-cli"]["resume"]("SID")[:2] == ["--resume", "SID"], "--verify resumes Gemini by id (F06)")


def test_f19_a_failed_write_leaves_the_store_as_it_was() -> None:
    home, sid = TMP / "f19" / "claude", "f19f19f1-0000-4000-8000-000000000001"
    claude_store(home, sid, [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "ask \ud83d cut"}]),
                             crec(sid, "assistant", "a1", "u1", 1, [{"type": "text", "text": "reply"}])])
    target = TMP / "f19" / "target"
    with env(ECTYPE_CLAUDE_HOME=str(target)):
        target.mkdir(parents=True)
        path, _ = native("claude-code", "ECTYPE_CLAUDE_HOME", home, target, install=False)
    check(path.is_file() and "\\ud83d" in path.read_text(encoding="utf-8"), "a lone surrogate is written back as its escape, not a crash")
    real = common._write_jsonl
    def boom(p, recs):
        raise OSError(28, "No space left on device")
    common._write_jsonl = boom
    import ectype.convert.native as nat
    nat_real = nat._write_jsonl
    nat._write_jsonl = boom
    out = TMP / "f19" / "out2"
    try:
        spill_home, ssid = TMP / "f19" / "spill", "f19f19f1-0000-4000-8000-000000000002"
        proj = claude_store(spill_home, ssid, [], spills={"s.txt": "FULL " * 50})
        sp = proj / ssid / "tool-results" / "s.txt"
        stub = f"<persisted-output>Output too large. Full output saved to: {sp}\n\nPreview:\nFULL</persisted-output>"
        claude_store(spill_home, ssid, [crec(ssid, "user", "u1", None, 0, [{"type": "text", "text": "run"}]),
                                        crec(ssid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]),
                                        crec(ssid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t", "content": stub}])],
                     spills={"s.txt": "FULL " * 50})
        try:
            native("claude-code", "ECTYPE_CLAUDE_HOME", spill_home, out)
            check(False, "a failing transcript write raises")
        except ValueError as e:
            check("failed" in str(e) and "removed" in str(e), f"the error says what was undone ({e})")
    finally:
        common._write_jsonl = real
        nat._write_jsonl = nat_real
    check(files_under(out) == [], f"nothing is left where the copy was going ({files_under(out)})")


def test_f20_cline_refuses_an_index_it_cannot_fill_before_writing() -> None:
    store = TMP / "f20" / "cline" / "data" / "sessions"
    store.mkdir(parents=True)
    db = store.parent / "db" / "sessions.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("create table sessions (session_id text primary key, workspace_id text not null, cwd text)")
    con.commit(); con.close()
    s = Session(agent="claude-code", id="x", path=TMP / "x.jsonl", cwd="/srv/p", messages=[
        Message(0, "user", None, [ContentBlock(kind="text", text="hi")]),
        Message(1, "assistant", None, [ContentBlock(kind="text", text="yo")])])
    with env(ECTYPE_CLINE_HOME=str(store)):
        try:
            conv.convert(s, "cline", TMP / "f20" / "out", install=True, backup=False)
            check(False, "an index with an unknown required column is refused")
        except ValueError as e:
            check("workspace_id" in str(e), f"the refusal names the column ({e})")
    check(files_under(store) == [], f"no session folder is left in the store ({files_under(store)})")


def test_f21_a_redacted_cline_export_keeps_no_real_messages_path() -> None:
    s = Session(agent="claude-code", id="x", path=TMP / "x.jsonl", cwd="/srv/p", messages=[
        Message(0, "user", None, [ContentBlock(kind="text", text="hi")]),
        Message(1, "assistant", None, [ContentBlock(kind="text", text="yo")])])
    out = TMP / "f21" / "SECRETNAME" / "out"
    path, _ = conv.convert(s, "cline", out, redact=lambda t: t.replace("SECRETNAME", "user"))
    meta = json.loads(path.read_text(encoding="utf-8"))
    check("SECRETNAME" not in meta["messages_path"], f"messages_path is redacted in an export ({meta['messages_path']})")


def test_f22_gemini_native_spills_go_where_gemini_keeps_them() -> None:
    home = TMP / "f22" / "gemini"
    gsid = "a2a2a2a2-1111-4222-8333-444444444444"
    proj = home / "p"
    (proj / "chats").mkdir(parents=True)
    (proj / ".project_root").write_text("/srv/p", encoding="utf-8")
    sd = proj / "tool-outputs" / f"session-{gsid}"
    sd.mkdir(parents=True)
    (sd / "call-1.txt").write_text("FULL " * 100, encoding="utf-8")
    head = {"sessionId": gsid, "projectHash": "h", "startTime": "2026-09-20T10:00:00.000Z", "lastUpdated": "2026-09-20T10:00:05.000Z", "kind": "main"}
    recs = [head, {"id": "m1", "timestamp": "2026-09-20T10:00:01.000Z", "type": "user", "content": [{"text": "list"}]},
            {"id": "m2", "timestamp": "2026-09-20T10:00:02.000Z", "type": "gemini", "content": "done", "toolCalls": [
                {"id": "call-1", "name": "run_shell_command", "args": {}, "status": "success",
                 "result": [{"functionResponse": {"response": {"output": "head For full output see: " + str(sd / "call-1.txt")}}}]}]}]
    (proj / "chats" / f"session-2026-09-20T10-00-{gsid[:8]}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    path, rep = native("gemini-cli", "ECTYPE_GEMINI_HOME", home, TMP / "f22" / "out")
    got = files_under(path.parent.parent)
    check(any(f.startswith(f"tool-outputs/session-{rep.new_id}/") for f in got), f"the spill is under tool-outputs/session-<new id>/ ({got})")


def test_f36_a_turn_without_a_timestamp_takes_the_previous_one() -> None:
    t0 = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
    s = Session(agent="codex", id="x", path=TMP / "x.jsonl", cwd="/srv/p", started=t0, messages=[
        Message(0, "user", t0, [ContentBlock(kind="text", text="one")]),
        Message(1, "assistant", None, [ContentBlock(kind="text", text="two")]),
        Message(2, "user", t0.replace(second=9), [ContentBlock(kind="text", text="three")])])
    path, _ = conv.convert(s, "claude-code", TMP / "f36" / "out", banner=False)
    stamps = [json.loads(l)["timestamp"] for l in path.read_text(encoding="utf-8").splitlines()]
    check(stamps == sorted(stamps) and stamps[1].startswith("2026-09-20"), f"stamps stay in order, none is today ({stamps})")


def test_f37_a_plain_native_copy_keeps_every_record_cwd() -> None:
    home, sid = TMP / "f37" / "claude", "f37f37f3-0000-4000-8000-000000000001"
    claude_store(home, sid, [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "a"}], cwd="/srv/proj"),
                             crec(sid, "assistant", "a1", "u1", 1, [{"type": "text", "text": "b"}], cwd="/srv/proj/sub")])
    path, _ = native("claude-code", "ECTYPE_CLAUDE_HOME", home, TMP / "f37" / "out", notice=None)
    cwds = [json.loads(l).get("cwd") for l in path.read_text(encoding="utf-8").splitlines()]
    check(cwds == ["/srv/proj", "/srv/proj/sub"], f"the copy keeps both working directories ({cwds})")
    path, _ = native("claude-code", "ECTYPE_CLAUDE_HOME", home, TMP / "f37" / "out2", notice=None, workspace="/srv/new")
    cwds = [json.loads(l).get("cwd") for l in path.read_text(encoding="utf-8").splitlines()]
    check(cwds == ["/srv/new", "/srv/new"], f"re-homing still rewrites them ({cwds})")


def test_f38_a_sidecar_that_cannot_be_read_is_reported() -> None:
    if os.geteuid() == 0:
        check(True, "skipped as root (chmod 0 does not stop root)")
        return
    home, sid = TMP / "f38" / "claude", "f38f38f3-0000-4000-8000-000000000001"
    proj = claude_store(home, sid, [], spills={"s.txt": "FULL"})
    sp = proj / sid / "tool-results" / "s.txt"
    stub = f"<persisted-output>Output too large. Full output saved to: {sp}\n\nPreview:\nF</persisted-output>"
    claude_store(home, sid, [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "run"}]),
                             crec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]),
                             crec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t", "content": stub}])],
                 spills={"s.txt": "FULL"})
    sp.chmod(0)
    try:
        _, rep = native("claude-code", "ECTYPE_CLAUDE_HOME", home, TMP / "f38" / "out")
    finally:
        sp.chmod(0o644)
    check(any("could not be copied" in n and "s.txt" in n for n in rep.notes), f"the report names the missing sidecar ({rep.notes})")


def test_f39_the_banner_alone_is_not_a_conversation() -> None:
    s = Session(agent="claude-code", id="x", path=TMP / "x.jsonl", cwd="/srv/p", messages=[
        Message(0, "assistant", None, [ContentBlock(kind="thinking", text="hmm")])])
    try:
        conv.convert(s, "codex", TMP / "f39" / "out", banner=True)
        check(False, "a session with no text is refused with the banner on")
    except ValueError as e:
        check("nothing to convert" in str(e), f"refused: {e}")


def test_f40_a_redacted_install_says_where_it_went() -> None:
    s = Session(agent="codex", id="x", path=TMP / "x.jsonl", cwd="/srv/p", messages=[
        Message(0, "user", None, [ContentBlock(kind="text", text="hi")]),
        Message(1, "assistant", None, [ContentBlock(kind="text", text="yo")])])
    home = TMP / "f40" / "claude"
    with env(ECTYPE_CLAUDE_HOME=str(home)):
        _, rep = conv.convert(s, "claude-code", TMP / "f40" / "out", install=True, backup=False, redact=lambda t: t)
    check(any("placeholder path" in n for n in rep.notes), "an install with redaction on warns that it lists under the placeholder path")


def test_f41_f42_the_terminal_driver_kills_its_group_and_names_its_numbers() -> None:
    src = (ROOT / "ectype" / "agentcli.py").read_text(encoding="utf-8")
    check("_os.killpg(pid, sig)" in src, "the forced exit signals the UI's whole process group")
    check(agentcli.TUI_MIN_OUTPUT == 2000 and agentcli.TUI_MIN_RUN == 15 and "seen > 2000" not in src,
          "the quiet test's thresholds are named constants")


# ================================================================= backups
def test_f03_a_restore_never_prunes_what_it_restores() -> None:
    root = TMP / "f03"
    live = root / "session_index.jsonl"
    root.mkdir()
    for keep in (1, 2):
        settings.save({"backup": {"keep_last": keep, "keep_days": 0}})
        for d in bk.root().glob("*"):
            import shutil
            shutil.rmtree(d)
        live.write_text("GOOD STATE\n", encoding="utf-8")
        folder = bk.save([live], "install-into-codex", agent="codex")
        live.write_text("BROKEN\n", encoding="utf-8")
        done = bk.restore(folder.name)
        check(live.read_text(encoding="utf-8") == "GOOD STATE\n" and all(ok for _, ok in done),
              f"keep_last={keep}: the restore puts the file back")
        check(done.safety is not None and done.safety.is_dir(), f"keep_last={keep}: the safety copy it names exists")
    settings.save({"backup": {"keep_last": 0, "keep_days": 0}})


def test_f04_a_sqlite_backup_carries_the_wal() -> None:
    root = TMP / "f04"
    root.mkdir()
    db = root / "sessions.db"
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma wal_autocheckpoint=0")
    con.execute("create table sessions (id text)")
    con.executemany("insert into sessions values (?)", [("old",), ("recent",)])
    con.commit()                                   # committed, still in the -wal (no checkpoint)
    check((root / "sessions.db-wal").is_file(), "the fixture keeps rows in the WAL")
    folder = bk.save([db], "install-into-cline", agent="cline")
    con.close()
    saved = next(p for p in folder.iterdir() if p.name.endswith("sessions.db"))
    rows = [r[0] for r in sqlite3.connect(saved).execute("select id from sessions")]
    check(rows == ["old", "recent"], f"the backup holds the rows that lived in the WAL ({rows})")
    m = json.loads((folder / bk.MANIFEST).read_text(encoding="utf-8"))
    check(m["files"][0].get("kind") == "sqlite", "the manifest marks it as a database")


def test_f44_a_naive_manifest_date_does_not_stop_pruning() -> None:
    settings.save({"backup": {"keep_last": 0, "keep_days": 30}})
    f = TMP / "f44.txt"
    f.write_text("x", encoding="utf-8")
    folder = bk.save([f], "one")
    m = json.loads((folder / bk.MANIFEST).read_text(encoding="utf-8"))
    m["created"] = "2020-01-01T00:00:00"
    (folder / bk.MANIFEST).write_text(json.dumps(m), encoding="utf-8")
    try:
        bk.save([f], "two")
        check(not folder.exists(), "an old naive-dated backup is pruned, not a TypeError")
    except TypeError as e:
        check(False, f"prune raised {e}")
    settings.save({"backup": {"keep_last": 0, "keep_days": 0}})


# ================================================================= adapters and titles
def test_f09_a_record_of_the_wrong_shape_skips_itself_not_the_session() -> None:
    home, sid = TMP / "f09" / "claude", "f09f09f0-0000-4000-8000-000000000001"
    claude_store(home, sid, [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "ask"}]),
                             {**crec(sid, "assistant", "a0", "u1", 1, None), "message": "oops-a-string"},
                             crec(sid, "assistant", "a1", "u1", 2, [{"type": "text", "text": "fine"}])])
    _, _, s = load_one("claude-code", ECTYPE_CLAUDE_HOME=str(home))
    check([m.role for m in s.messages] == ["user", "assistant"], "Claude Code: a string `message` is skipped, the rest loads")
    sid2 = "f09f09f0-0000-4000-8000-000000000002"            # the bad record first, where discovery peeks for a title
    claude_store(home, sid2, [{**crec(sid2, "user", "u0", None, 0, None), "message": "a string"},
                              crec(sid2, "user", "u1", "u0", 1, [{"type": "text", "text": "real ask"}])])
    (home / "-srv-proj" / sid2).mkdir(exist_ok=True)
    (home / "-srv-proj" / sid2 / "custom-title.json").write_text("[1, 2]", encoding="utf-8")
    with env(ECTYPE_CLAUDE_HOME=str(home)):
        refs = adapters.get("claude-code").discover()
    check(len(refs) == 2, f"Claude Code: discovery lists both sessions despite the bad first record and sidecar ({len(refs)})")
    ghome = TMP / "f09" / "gemini"
    proj = ghome / "p"
    (proj / "chats").mkdir(parents=True)
    (proj / ".project_root").write_text("/srv/p", encoding="utf-8")
    gsid = "a9a9a9a9-1111-4222-8333-444444444444"
    recs = [{"sessionId": gsid, "projectHash": "h", "startTime": "2026-09-20T10:00:00.000Z", "lastUpdated": "2026-09-20T10:00:05.000Z", "kind": "main"},
            {"$set": {"messages": [{"id": "m1", "timestamp": "2026-09-20T10:00:01.000Z", "type": "user", "content": [{"text": "hi"}]},
                                   "not a dict", {"id": "m2", "timestamp": "2026-09-20T10:00:02.000Z", "type": "gemini", "content": "yo"}]}}]
    (proj / "chats" / f"session-2026-09-20T10-00-{gsid[:8]}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    _, _, s = load_one("gemini-cli", ECTYPE_GEMINI_HOME=str(ghome))
    check(len(s.messages) == 2, f"Gemini CLI: a non-object in a snapshot is skipped ({len(s.messages)} messages)")
    chome = TMP / "f09" / "cline"
    d = chome / "1790000000000_abcde"
    d.mkdir(parents=True)
    (d / "1790000000000_abcde.json").write_text(json.dumps({"session_id": "1790000000000_abcde", "cwd": "/srv/p", "started_at": "2026-09-20T10:00:00.000Z"}), encoding="utf-8")
    (d / "1790000000000_abcde.messages.json").write_text(json.dumps({"messages": [
        {"id": "m1", "role": "user", "ts": 1, "content": [{"type": "text", "text": "hi"}], "metrics": "n/a"},
        {"id": "m2", "role": "assistant", "ts": 2, "content": [{"type": "text", "text": "yo"}], "metrics": 5, "modelInfo": "x"}]}), encoding="utf-8")
    _, _, s = load_one("cline", ECTYPE_CLINE_HOME=str(chome))
    check(len(s.messages) == 2, "Cline: scalar `metrics` / `modelInfo` do not sink the session")
    rhome = TMP / "f09" / "roo"
    for name, hist in (("t-good", {"task": "good task", "ts": 1758000000000}), ("t-bad", ["a", "list"])):
        rd = rhome / name
        rd.mkdir(parents=True)
        (rd / "api_conversation_history.json").write_text(json.dumps([{"role": "user", "content": "x"}]), encoding="utf-8")
        (rd / "history_item.json").write_text(json.dumps(hist), encoding="utf-8")
    with env(ECTYPE_ROO_HOME=str(rhome)):
        refs = adapters.get("roo-code").discover()
    check(len(refs) == 2, f"Roo Code: a history_item.json that is a list does not drop the store ({len(refs)} listed)")


def test_f07_codex_full_outputs_are_the_result_text_so_filters_apply() -> None:
    home = TMP / "f07" / "codex"
    day = home / "sessions" / "2026" / "09" / "20"
    day.mkdir(parents=True)
    sid = "0199bbbb-bbbb-7ccc-8ddd-eeeeeeeeeeee"
    full = "BUILD LOG LINE\n" * 50
    recs = [{"timestamp": "2026-09-20T10:00:00.000Z", "type": "session_meta", "payload": {"id": sid, "cwd": "/srv/p"}},
            {"timestamp": "2026-09-20T10:00:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "build"}]}},
            {"timestamp": "2026-09-20T10:00:02.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": "{}"}},
            {"timestamp": "2026-09-20T10:00:03.000Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "BUILD LOG LINE [truncated]"}},
            {"timestamp": "2026-09-20T10:00:03.000Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": "make", "exit_code": 0, "aggregated_output": full}}},
            {"timestamp": "2026-09-20T10:00:04.000Z", "type": "response_item", "payload": "a scalar payload"}]
    (day / f"rollout-2026-09-20T13-00-00-{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    _, _, s = load_one("codex", CODEX_HOME=str(home))
    b = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"][0]
    check(b.text == full and "full_output" not in b.meta and b.meta.get("restored_from_output"),
          "the recovered output is the result's text, flagged, with no second copy in meta")
    from ectype import budget
    from ectype.render import formats
    from ectype.render.text import RenderOptions
    view = budget.filtered(s, {"mode": "brief", "tools": True})
    js = formats.export(view, "json", RenderOptions())
    check("BUILD LOG LINE" not in js, "a names-only json export carries none of the output")


def test_f23_a_rename_from_another_process_survives() -> None:
    from ectype import titles
    titles.invalidate()
    titles.put("aider", "one", "first name")
    data = json.loads(titles.path().read_text(encoding="utf-8"))
    data["aider:two"] = "named elsewhere"                   # what a second ectype process writes meanwhile
    titles.path().write_text(json.dumps(data), encoding="utf-8")
    titles.put("aider", "three", "third name")
    now = json.loads(titles.path().read_text(encoding="utf-8"))
    check(now.get("aider:two") == "named elsewhere", f"the other process's name is kept ({sorted(now)})")
    titles.invalidate()


# ================================================================= the pipeline: redaction, filters, render, budget, summary, ledger
def _sess(*msgs, agent="claude-code", sid="s-1", **kw) -> Session:
    return Session(agent=agent, id=sid, path=TMP / f"{sid}.jsonl", messages=list(msgs), **kw)


def _m(i, role, *blocks, ts=None, model=None, **meta) -> Message:
    return Message(i, role, ts, list(blocks), model=model, meta=meta)


def _t(text) -> ContentBlock:
    return ContentBlock(kind="text", text=text)


def _redactor(**opts):
    from ectype.transform import Redactor
    with env(HOME="/srv/zorbax", USER="zorbax", LOGNAME="zorbax"):
        return Redactor.defaults(options=opts or None)


def test_f02_redaction_runs_before_the_cap_can_split_a_name() -> None:
    """The cap cut a result first and redaction ran on what was left, so `/srv/zorbax` cut to
    `/srv/zorb` matched no rule. Exercised through the functions both surfaces now call in order."""
    from ectype import budget
    line = "src/a.py: owner jane.doe@corp-mail.com home /srv/zorbax/project/src/main.py\n"
    s = _sess(_m(0, "user", _t("find owners")),
              _m(1, "assistant", ContentBlock(kind="tool_call", name="Bash", args={"command": "grep"}, call_id="c")),
              _m(2, "tool", ContentBlock(kind="tool_result", text=line * 40, call_id="c")))
    from ectype import web
    home, sid = TMP / "f02" / "claude", "f02f02f0-0000-4000-8000-000000000001"
    claude_store(home, sid, [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "find owners"}]),
                             crec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "grep"}}]),
                             crec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t", "content": line * 40}])])
    web_leaks = 0
    with env(ECTYPE_CLAUDE_HOME=str(home), HOME="/srv/zorbax", USER="zorbax", LOGNAME="zorbax"):
        for cap in range(5, 60, 3):
            web._forget("claude-code", sid) if hasattr(web, "_forget") else None
            p = web.Prepared("claude-code", sid, {"mode": "custom", "cap": cap, "tools": True, "redact": True})
            res = [b.text for m in p.view.messages for b in m.blocks if b.kind == "tool_result"][0]
            web_leaks += any(f in res.splitlines()[-1] for f in ("zorb", "jane", "corp-mail"))
    check(web_leaks == 0, f"the web view leaks no fragment at any cap ({web_leaks} leaked)")
    src = (ROOT / "ectype" / "cli.py").read_text(encoding="utf-8") + (ROOT / "ectype" / "web" / "__init__.py").read_text(encoding="utf-8")
    check("budget.filtered(_redactor(a).session(s0), opts)" in src and "self.full = Redactor.defaults(extra=terms, options=rules).session(self.source)" in src and "self.view = budget.filtered(self.full, o)" in src,
          "the CLI export and the web view redact the source before they filter it")


def test_f10_a_quoted_marker_does_not_steal_the_closing_summary() -> None:
    from ectype.summary import closing_summaries
    M = "-*-summary-*-"
    def one(*texts):
        return closing_summaries(_sess(_m(0, "user", _t("q")), _m(1, "assistant", *[_t(x) for x in texts])), [M])
    check(one(f"I close with the {M} block.\n{M}\nREAL SUMMARY\n{M}") == [(M, "REAL SUMMARY")],
          "an inline mention before the marker lines does not shift the pair")
    check(one(f"{M}\nREAL\n{M}", f"the rule:\n```\n{M}\n[full session summary]\n{M}\n```") == [(M, "REAL")],
          "the rule quoted in a code fence after the real block is not taken for it")
    check(one(f"text {M} INLINE {M} end") == [(M, "INLINE")], "inline markers still pair when no marker line exists")
    two = closing_summaries(_sess(_m(0, "user", _t("q")), _m(1, "assistant", _t("<<B>>\nFIRST-B\n<<B>>"), _t(f"{M}\nLATER-A\n{M}"))), [M, "<<B>>"])
    check(two[-1] == (M, "LATER-A"), f"with two markers, the last written is last ({two})")


def test_f11_hidden_assistant_calls_are_not_filed_under_the_user() -> None:
    from ectype import budget
    from ectype.render.text import render
    s = _sess(_m(0, "user", _t("please list the files")),
              _m(1, "assistant", _t("sure"), ContentBlock(kind="tool_call", name="Bash", args={"command": "ls"}, call_id="c")),
              _m(2, "tool", ContentBlock(kind="tool_result", text="a.py", call_id="c")))
    opts = {"mode": "full", "tools": True, "hide_roles": ["assistant"]}
    out = render(budget.filtered(s, opts), budget.render_options(opts), with_footer=False)
    user_turn = out.split("[USER]", 1)[1].split("\n\n", 1)[0]
    check("Bash" not in user_turn and "[TOOL]" in out, "the calls get their own [TOOL] turn, not the user's")


def test_f13_f14_f33_redaction_catches_the_other_spellings_and_spares_words() -> None:
    red = _redactor()
    for raw in ("folder=%2Fsrv%2Fzorbax%2Fproj", "folder=%2fsrv%2fzorbax%2fproj", "Zorbax's laptop, ZORBAX-PC", "zorbax_backup.tar"):
        check("zorbax" not in red.text(raw).lower(), f"redacted: {raw!r} -> {red.text(raw)!r}")
    keys = {"github_pat_": "github_pat_" + "A1b2" * 14, "gsk_": "gsk_" + "a1B2" * 12, "hf_": "hf_" + "Ab12" * 9,
            "sk_live_": "sk_live_" + "x9Y8" * 7, "AKIA": "AKIA" + "ABCDEFGHIJKLMNOP", "JWT": "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12,
            "assignment": "GROQ_API_KEY=" + "q" * 20}
    for label, k in keys.items():
        check(k not in red.text(f"x {k} y"), f"the key rule catches {label}")
    check(red.text("GROQ_API_KEY=" + "q" * 20).startswith("GROQ_API_KEY="), "an assignment keeps its name, loses its value")
    for word in ("risk-assessment-framework-document", "task-refactor-the-ledger-now", "flask-sqlalchemy-extension"):
        check(red.text(word) == word, f"an ordinary word is not a key: {word}")


def test_f15_ids_dict_keys_tuples_and_paths_are_redacted() -> None:
    red = _redactor()
    s = _sess(_m(0, "user", _t("hi")), agent="aider", sid="zorbax#0",
              meta={"/srv/zorbax/key.txt": "dict key", "t": ("/srv/zorbax/tuple",), "p": Path("/srv/zorbax/pathobj")})
    r = red.session(s)
    blob = json.dumps({"id": r.id, "meta": {str(k): str(v) for k, v in r.meta.items()}})
    check("zorbax" not in blob, f"id, keys, tuples and Paths carry no username ({blob})")
    # F88 (the Chrome pass): a custom term reaches the model names and tool names too
    from ectype.transform import Redactor
    rt = Redactor.defaults(extra=["probe"], machine=False)
    s2 = _sess(_m(0, "assistant", ContentBlock(kind="tool_call", name="mcp__probe__run", args={}, call_id="c"), model="probe-model"), model="probe-model")
    r2 = rt.session(s2)
    check("probe" not in (r2.model or "") + (r2.messages[0].model or "") + (r2.messages[0].blocks[0].name or ""),
          "a custom term is replaced in model and tool names (F88)")


def test_f16_f17_keywords_are_complete_and_when_is_local() -> None:
    from ectype.summary import summarize
    asks = [_m(i, "user", _t(f"ask number{i} alpha{i} beta{i} gamma{i} delta{i} epsilon{i}")) for i in range(12)]
    asks.append(_m(12, "user", _t("finally zanzibarquux")))
    out = summarize(_sess(*asks))
    check("zanzibarquux" in out, "a word from the 13th ask is in the keyword block")
    t = datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)
    with env(TZ="Europe/Istanbul"):
        import time as _time
        _time.tzset()
        line = next(l for l in summarize(_sess(_m(0, "user", _t("x")), started=t, ended=t)).splitlines() if l.startswith("when:"))
        _time.tzset()
    check(line.startswith("when: 2026-09-25 01:30") and "+0300" in line, f"`when:` is local, with its offset ({line!r})")


def test_f24_f26_f27_cuts_that_do_not_save_are_not_made() -> None:
    from ectype import tokens
    from ectype.render.text import _clip_chars
    from ectype.transform import Filter, apply
    arg = "x " * 101                                      # 202 characters
    check(tokens.count(_clip_chars(arg, 200)) <= tokens.count(arg), "the argument clip never costs more than the argument")
    padded = " ".join(f"w{i}" for i in range(60)) + " \n\t" * 40
    s = _sess(_m(0, "tool", ContentBlock(kind="tool_result", text=padded, call_id="c")))
    n = tokens.count(padded.rstrip())
    out = apply(s, Filter(cap=n + 1)).messages[0].blocks[0]
    check("tokens cut" not in out.text, "trailing padding alone does not earn a cut marker")
    tr = "İstanbul Ğ çalışma Şeker " * 20
    check(all(not tokens.truncate(tr, k)[0].endswith("�") for k in range(5, 120)), "a cut never ends in U+FFFD")


def test_f28_markdown_and_html_brief_mention_no_results() -> None:
    from ectype import budget
    from ectype.render import formats
    s = _sess(_m(0, "user", _t("go")),
              _m(1, "assistant", ContentBlock(kind="tool_call", name="Bash", args={"command": "ls"}, call_id="c")),
              _m(2, "tool", ContentBlock(kind="tool_result", text="x" * 500, call_id="c")))
    opts = {"mode": "brief", "tools": True}
    v, ro = budget.filtered(s, opts), budget.render_options(opts)
    for fmt in ("markdown", "html"):
        out = formats.export(v, fmt, ro)
        check("500 chars" not in out and "mentioned only" not in out, f"{fmt} brief prints no result size")


def test_f29_to_f32_the_ledger_keeps_every_summary_apart_and_honest() -> None:
    from ectype import ledger
    check(ledger.summary_path("aider", "my proj#0") != ledger.summary_path("aider", "my_proj#0"), "two ids never share a summary file")
    long_id = "キャラクター" * 40
    check(len(ledger.summary_path("aider", long_id).name.encode("utf-8")) < 255, "a long id gives a legal file name")
    # gone: a store that fails to enumerate does not flag summaries whose files are still there
    f = TMP / "ledger-src.txt"
    f.write_text("x", encoding="utf-8")
    data = {"aider:proj#0": {"agent": "aider", "id": "proj#0", "path": str(f)}}
    ledger.save(data)
    real = adapters.all_refs
    adapters.all_refs = lambda *a, **k: []
    try:
        with env(ECTYPE_CONFIG=os.environ["ECTYPE_CONFIG"]):
            st = ledger.update(agents=["aider"])
    finally:
        adapters.all_refs = real
    check(st["gone"] == 0 and not ledger.load()["aider:proj#0"].get("gone"), "a summary whose file exists is never flagged gone")
    # search coverage: an entry whose summary file is missing is counted as unreadable, not as covered
    ledger.save({"aider:proj#0": {"agent": "aider", "id": "proj#0", "path": str(f)}})
    adapters.all_refs = lambda *a, **k: [type("R", (), {"agent": "aider", "id": "proj#0"})()]
    try:
        # the accounting alone: since 2026-09-27 a search first brings the store up to date, and this
        # stub is no store (its refs have no file to summarise)
        res = ledger.search("anything", agents=["aider"], refresh=False)
    finally:
        adapters.all_refs = real
    check(res["summarised"] == 0 and res.get("unreadable") == 1, f"a missing summary file is reported ({res})")
    src = (ROOT / "ectype" / "ledger.py").read_text(encoding="utf-8")
    check("fresh = load()" in src and "fresh.update(" in src, "update() merges its changes into a fresh read (F31)")


def test_f34_a_model_switch_is_not_merged_away() -> None:
    from ectype.transform import collapse
    out = collapse([_m(0, "assistant", _t("a"), model="model-B"), _m(1, "assistant", _t("b"), model="model-A")])
    check(len(out) == 2, "turns of two models stay two turns")


def test_f35_csv_cells_are_never_formulas() -> None:
    from ectype.render import formats
    from ectype.render.text import RenderOptions
    out = formats.export(_sess(_m(0, "user", _t('=HYPERLINK("https://example.invalid","x")'))), "csv", RenderOptions())
    check(",'=HYPERLINK" in out or ",\"'=HYPERLINK" in out, "a cell starting with = is escaped with an apostrophe")


def test_f45_the_home_rule_stands_on_its_own() -> None:
    with env(HOME="/srv/work"):
        from ectype.transform import Redactor
        with env(USER="zorbax", LOGNAME="zorbax"):
            red = Redactor.defaults(options={"username": False})
    check(red.text("open /srv/work/notes.txt") == "open /home/user/notes.txt",
          "with the username rule off, the home rule alone replaces a home outside /home/<user>")


# ================================================================= the web server, the MCP server, the CLI, the skill
@contextlib.contextmanager
def server(**homes):
    import http.client
    import threading
    from ectype import web
    with env(**homes):
        srv = web.make_server(0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        port = srv.server_address[1]
        def call(method, path, body=None, headers=None, raw=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
            data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            out = r.status, dict(r.getheaders()), r.read()
            c.close()
            return out
        try:
            yield call, port
        finally:
            srv.shutdown()
            srv.server_close()


def _web_store(tag: str):
    home, sid = TMP / tag / "claude", f"{tag[:4]}0000-0000-4000-8000-000000000001"
    claude_store(home, sid, [crec(sid, "user", "u1", None, 0, [{"type": "text", "text": "hello İşler"}]),
                             crec(sid, "assistant", "a1", "u1", 1, [{"type": "text", "text": "reply İşler cut \ud83d"}])])
    return home, sid


def test_f46_f47_f56_f58_the_web_guard_headers_and_request_edges() -> None:
    home, sid = _web_store("webA")
    with server(ECTYPE_CLAUDE_HOME=str(home)) as (call, port):
        st, _, _ = call("POST", "/api/render", {"agent": "claude-code", "id": sid, "opts": {}},
                        {"Origin": "http://127.0.0.1:8000", "Content-Type": "text/plain;charset=UTF-8"})
        check(st == 403, f"a page on another local port is refused (F46, got {st})")
        st, _, _ = call("GET", "/api/sessions", headers={"Sec-Fetch-Site": "cross-site"})
        check(st == 403, "a cross-site no-cors GET of the API is refused")
        st, _, _ = call("POST", "/api/render", {"agent": "claude-code", "id": sid, "opts": {}}, {"Origin": f"http://127.0.0.1:{port}"})
        check(st == 200, "the page's own origin is served")
        st, h, raw = call("POST", "/api/export", {"agent": "claude-code", "id": sid, "format": "text", "mode": "download",
                                                  "opts": {"redact": True, "redact_terms": ["İşler"], "mode": "full"}})
        check(st == 200 and raw.count(b"HTTP/1.") == 0 and "%" in h.get("X-Redacted", ""),
              f"a non-Latin-1 redaction report travels quoted, one clean 200 (F47, got {st})")
        check(b"\xef\xbf\xbd" in raw or b"?" in raw, "the lone surrogate is exported as a replacement, not a 500 (F58)")
        st, _, raw = call("POST", "/api/render", {"agent": "claude-code", "id": sid, "opts": {}})
        check(st == 200, f"a session with a lone surrogate renders (F58, got {st})")
        st, _, _ = call("POST", "/api/nope", {"agent": "x"})
        check(st == 404, f"an unknown POST path is a 404 before any work (F56, got {st})")
        st, _, _ = call("POST", "/api/render", raw=b"{}", headers={"Content-Length": "abc"})
        check(st == 400, f"a bad Content-Length is a 400, not a dead connection (got {st})")
        st, _, raw = call("POST", "/api/render", {"id": sid})
        check(st == 400 and b"missing field" in raw, "a missing field is a 400 that names it")
        st, _, _ = call("GET", "/api/summarize?agent=claude-code&id=nope")
        check(st == 404, f"an unknown session is a 404 (got {st})")
        st, _, _ = call("GET", "/api/skill?name=../../x")
        check(st == 400, f"GET /api/skill validates the name like POST (got {st})")
        st, _, raw = call("POST", "/api/render", {"agent": "claude-code", "id": sid, "opts": {"start": -1}})
        check(st == 400 and b"start" in raw, "a negative start is refused, not counted from the end (F61)")


def test_f48_clearing_a_title_returns_the_fallback_not_the_old_name() -> None:
    home, sid = _web_store("webB")
    with server(ECTYPE_CLAUDE_HOME=str(home)) as (call, _port):
        call("POST", "/api/rename", {"agent": "claude-code", "id": sid, "title": "TEMP NAME"})
        st, _, raw = call("POST", "/api/rename", {"agent": "claude-code", "id": sid, "title": ""})
        gen = json.loads(raw).get("generated")
    check(st == 200 and gen != "TEMP NAME", f"the restored title is the store's fallback ({gen!r})")


def test_f53_f83_export_names_are_sanitised_on_both_surfaces() -> None:
    from ectype.render import formats
    n = formats.export_name(None, "sillytavern", "Ann/Ann - 2026", "text")
    check("/" not in n and n.endswith(".txt"), f"a SillyTavern id makes a flat file name ({n!r})")
    check(len(formats.export_name("x" * 300, "a", "b", "text")) < 160, "a long typed name is bounded")
    src = (ROOT / "ectype" / "cli.py").read_text(encoding="utf-8")
    check("formats.export_name(None, s.agent, s.id, a.format)" in src, "the CLI's -o <folder> uses the same helper")


def test_f55_settings_follow_the_file_and_small_saves_merge() -> None:
    settings.save({"view": {"cap": 150}})
    p = settings.config_path()
    d = json.loads(p.read_text(encoding="utf-8"))
    d["view"]["cap"] = 999
    import time as _time
    _time.sleep(0.01)
    p.write_text(json.dumps(d), encoding="utf-8")
    check(settings.load()["view"]["cap"] == 999, "a change made on disk is seen without a restart")
    settings.patch({"notice": {"enabled": False}})
    now = settings.load(fresh=True)
    check(now["view"]["cap"] == 999 and now["notice"]["enabled"] is False, "a patch keeps what changed on disk meanwhile")
    p.write_text('{"view": {"cap": 7,}}', encoding="utf-8")
    check(settings.load_error() is not None, "a file broken after start-up is noticed")
    settings.save({"view": {"cap": 150}})
    check(any(x.name.startswith("settings.json.bad-") for x in p.parent.iterdir()), "and kept as .bad-<stamp> by the next save")
    settings.save({})


def test_f62_an_unreadable_titles_file_is_kept_not_overwritten() -> None:
    from ectype import titles
    titles.invalidate()
    titles.path().parent.mkdir(parents=True, exist_ok=True)
    titles.path().write_text('{"aider:a#0": "one", "aider:b#0": "two",}', encoding="utf-8")
    titles.invalidate()
    titles.put("aider", "c#0", "new")
    kept = [x for x in titles.path().parent.iterdir() if x.name.startswith("titles.json.bad-")]
    check(kept and "one" in kept[0].read_text(encoding="utf-8"), "the unreadable file is kept as .bad-<stamp>")
    titles.invalidate()


def _mcp(lines: bytes, allow_write: bool = False) -> list:
    import subprocess
    r = subprocess.run([sys.executable, "-m", "ectype", "mcp"] + (["--allow-write"] if allow_write else []),
                       input=lines, capture_output=True, cwd=ROOT, timeout=60, env=dict(os.environ))
    return [json.loads(x) for x in r.stdout.decode("utf-8").splitlines() if x.strip()]


def test_f63_f64_one_bad_line_never_ends_the_mcp_session() -> None:
    lines = (b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
             b'[{"jsonrpc":"2.0","id":2,"method":"ping"}]\n'
             b'"just a string"\n'
             b'{"jsonrpc":"2.0","id":4,"method":"initialize","params":[1]}\n'
             b'\xff\xfe not utf-8\n'
             b'{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"search_sessions","arguments":{"pattern":"\xc5\x9fablon"}}}\n'
             b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
    out = _mcp(lines)
    ids = [x.get("id") for x in out]
    check(ids == [1, None, None, 4, None, 6, 7], f"every line is answered and the session survives ({ids})")
    six = next(x for x in out if x.get("id") == 6)
    check("şablon" in six["result"]["content"][0]["text"], "a UTF-8 argument arrives intact")


def test_f81_f75_f77_f86_mcp_tools_say_what_they_serve() -> None:
    out = _mcp(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"nope","arguments":{}}}\n'
               b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_sessions","arguments":{"limit":-1}}}\n'
               b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n')
    t1 = out[0]["result"]["content"][0]["text"]
    check("install_session" not in t1, "a read-only server does not name install_session")
    check(out[1]["result"]["isError"], "a negative limit is refused")
    budget_schema = next(t for t in out[2]["result"]["tools"] if t["name"] == "session_budget")["inputSchema"]["properties"]
    check("start" in budget_schema and "end" in budget_schema, "session_budget lists the range it honours")
    from ectype import mcp
    src = (ROOT / "ectype" / "mcp.py").read_text(encoding="utf-8")
    check("skipped=skipped" in src and "(not listed:" in src, "list_sessions names a store it could not read (F75)")
    check('"notice": {"type": "boolean"' in src and "_notice.build(" in src, "install_session appends the import notice from Settings (F66)")


def _cli(argv: list[str]) -> tuple[int, str, str]:
    import subprocess
    r = subprocess.run([sys.executable, "-m", "ectype", *argv], capture_output=True, cwd=ROOT, timeout=120, env=dict(os.environ))
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def test_f65_f73_f74_f76_f77_f78_f79_f80_f82_f87_the_command_line() -> None:
    home, sid = _web_store("cliA")
    sid2 = sid[:4] + "1111-0000-4000-8000-000000000002"
    claude_store(home, sid2, [crec(sid2, "user", "u1", None, 0, [{"type": "text", "text": "other"}])])
    with env(ECTYPE_CLAUDE_HOME=str(home)):
        settings.save({"view": {"stamps": "none", "markers": False, "tool_headers": False}})
        rc1, show, _ = _cli(["show", sid, "-a", "claude-code"])
        rc2, exp, _ = _cli(["export", sid, "-a", "claude-code", "--no-notice"])
        check(rc1 == 0 and show == exp, "with Settings changed, show equals export --format text minus the notice (F65)")
        settings.save({})
        rc, _, err = _cli(["recall", sid, "--brief"])
        check(rc == 2 and "--mode brief" in err, "an unknown option is reported by the subcommand, with the --mode hint (F73)")
        rc, out, err = _cli(["show", sid[:4], "-a", "claude-code"])
        check(rc == 1 and sid in err and sid2 in err and sid not in out, "ambiguity candidates are in the error, not on stdout (F74)")
        rc, _, err = _cli(["list", "-n", "-1"])
        check(rc == 2, "a negative -n is refused (F77)")
        rc, _, err = _cli(["show", sid, "-o", str(TMP / "no" / "such" / "dir" / "x.txt")])
        check(rc == 1 and "cannot write" in err and "Traceback" not in err, "an unwritable -o is one line (F79)")
        rc, _, err = _cli(["summarize", sid, "--all"])
        check(rc == 1 and "no id" in err, "summarize X --all is refused (F82)")
    with env(ECTYPE_CLAUDE_HOME=str(TMP / "does-not-exist")):
        rc, _, err = _cli(["list", "-a", "claude-code"])
        check("store is not there" in err, "list -a over a missing store says so (F76)")
    rc, out, _ = _cli(["--help"])
    check("filtered export (redacted with --redact)" in out, "--help no longer promises redaction by default (F78)")
    src = (ROOT / "ectype" / "cli.py").read_text(encoding="utf-8")
    check("except BrokenPipeError:" in src and "sys.exit(141)" in src, "a closed pipe is not a traceback (F80)")
    check('_redactor(argparse.Namespace(**{**vars(a), "redact": True}))' in src, "fixture redacts with the user's Settings (F87)")


def test_f67_f68_f69_f70_f85_the_skill_installer() -> None:
    from ectype import skill
    fake = TMP / "skhome"
    with env(HOME=str(fake), CODEX_HOME=str(fake / "codex"), CLAUDE_CONFIG_DIR=str(fake / "claude"), GEMINI_CLI_HOME=str(fake / "gem")):
        check(str(skill.path("gemini-cli", "x")).startswith(str(fake / "gem" / ".gemini" / "commands")), "GEMINI_CLI_HOME is honoured (F85)")
        check(skill.NAME.fullmatch("ectype\n") is None, "a trailing newline is not a valid name (F70)")
        skill.install("ectype")
        p = skill.path("claude-code", "ectype")
        p.write_text(p.read_text(encoding="utf-8") + "\nMY OWN EXTRA LINE\n", encoding="utf-8")
        removed, kept = skill.remove("ectype", ["claude-code"])
        check(p.is_file() and kept == [p], "an edited command file is kept by remove (F67)")
        try:
            skill.install("ectype", ["claude-code"])
            check(False, "an edited command file is refused by install")
        except FileExistsError:
            check("MY OWN EXTRA LINE" in p.read_text(encoding="utf-8"), "and never overwritten (F67)")
        skill.install("ectype", ["claude-code"], force=True)
        check(any(x.name.startswith("ectype.md.bak-") for x in p.parent.iterdir()), "--force keeps the edited file aside")
        own = skill.path("gemini-cli", "other")
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("my own", encoding="utf-8")
        try:
            skill.install("other")
            check(False, "a user's own file under the name is refused")
        except FileExistsError:
            check(not skill.path("claude-code", "other").exists(), "and nothing was written for the other agents (F68)")
        f = skill.path("codex", "ectype")
        f.write_text(f.read_text(encoding="utf-8").replace(skill.__version__, "0.0.1"), encoding="utf-8")
        st = next(x for x in skill.status("ectype", ["codex"]))
        check(st["stale"] and st["version"] == "0.0.1" and len(skill.status("ectype", ["codex"])) == 1,
              "status names an older version, and -a filters the rows (F69)")


def test_f49_f50_f52_f54_f57_the_page() -> None:
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check("$('#terms').value=typed;" in page, "a Settings save keeps the typed redaction words (F49)")
    check("JSON.stringify({patch})" in page and "throw new Error(j.error||'settings not saved')" in page, "small saves patch and check the reply (F50)")
    check("home:home.value.trim()||null" in page, "a disabled store field sends the saved path back (F52)")
    check("(?: |$)/.exec(l)" in page, "a header with no timestamp is still a header (F54)")
    check('data-id="${esc(r.id)}"' in page and 'data-a="${esc(r.agent)}"' in page, "list attributes are escaped (F57)")
    check("addEventListener('unhandledrejection'" in page, "a failed request is said, whichever call it was (F51)")


# ================================================================= found in the ideas round (F89 to F100)
def test_f89_f90_one_session_restores_never_write_over_a_shared_store() -> None:
    import argparse
    from ectype import cli
    home = TMP / "f90" / "codex"
    day = home / "sessions" / "2026" / "09" / "20"
    day.mkdir(parents=True)
    sid = "0199cccc-bbbb-7ccc-8ddd-eeeeeeeeeeee"
    (day / f"rollout-2026-09-20T13-00-00-{sid}.jsonl").write_text(json.dumps(
        {"timestamp": "2026-09-20T10:00:00.000Z", "type": "session_meta", "payload": {"id": sid, "cwd": "/srv/p"}}) + "\n", encoding="utf-8")
    idx = home / "session_index.jsonl"
    idx.write_text(json.dumps({"id": sid, "thread_name": "one"}) + "\n", encoding="utf-8")
    with env(CODEX_HOME=str(home)):
        ad = adapters.get("codex")
        ref = ad.discover()[0]
        arts = [(Path(p), rel) for p, rel in ad.artifacts(ref)]
        modes = cli._session_backup_modes(ad, ref, arts)
        folder = bk.save([p for p, _ in arts], "codex-one", agent="codex", modes=modes)
        with open(idx, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "later-session", "thread_name": "two"}) + "\n")
        bk.restore(folder.name)
    ids = [json.loads(l)["id"] for l in idx.read_text(encoding="utf-8").splitlines() if l.strip()]
    check(ids == [sid, "later-session"], f"a one-session restore merges the shared index, keeping later entries (F90: {ids})")
    # an export copy (a file outside the store) is kept in the backup, never written over anything
    exp = TMP / "f89-scratch" / "webui.db"
    exp.parent.mkdir(parents=True)
    exp.write_text("one-chat export", encoding="utf-8")
    folder = bk.save([exp], "owui-one", agent="open-webui", modes={exp: "export"})
    exp.write_text("changed", encoding="utf-8")
    done = bk.restore(folder.name)
    check(exp.read_text(encoding="utf-8") == "changed" and done.skipped and not done,
          "an export copy is reported as not written back, and nothing is written (F89)")


def test_f91_a_names_only_view_is_never_folded_as_empty_results() -> None:
    from ectype import budget
    s = _sess(_m(0, "user", _t("go")),
              _m(1, "assistant", ContentBlock(kind="tool_call", name="Bash", args={"command": "ls"}, call_id="c")),
              _m(2, "tool", ContentBlock(kind="tool_result", text="x" * 6800, call_id="c")), agent="claude-code")
    v = budget.filtered(s, {"mode": "brief", "tools": True})
    rep = conv.FidelityReport("claude-code", "codex")
    folded = conv.flatten(v, rep, banner=False)
    txt = " ".join(t.text for t in folded)
    check("(empty)" not in txt and "not included in this view" in txt, "a hidden result is named as hidden, not empty")
    src = (ROOT / "ectype" / "web" / "__init__.py").read_text(encoding="utf-8")
    check("s = p.source if native else p.full" in src, "the GUI folds the whole (redacted) session, as the CLI does")


def test_f92_cline_reports_the_writers_version() -> None:
    chome = TMP / "f92" / "cline"
    d = chome / "1790000000001_abcde"
    d.mkdir(parents=True)
    (d / "1790000000001_abcde.json").write_text(json.dumps({"version": 1, "session_id": "1790000000001_abcde", "cwd": "/srv/p",
        "metadata": {"sessionHistoryOrigin": {"version": "3.0.62"}}}), encoding="utf-8")
    (d / "1790000000001_abcde.messages.json").write_text(json.dumps({"messages": [
        {"id": "m1", "role": "user", "ts": 1, "content": [{"type": "text", "text": "hi"}]}]}), encoding="utf-8")
    _, _, s = load_one("cline", ECTYPE_CLINE_HOME=str(chome))
    check(s.cli_version == "3.0.62", f"cli_version is the writer's release, not the schema's 1 ({s.cli_version!r})")


def test_f93_f94_f95_the_summary_names_what_really_happened() -> None:
    from ectype.summary import summarize
    patch = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** Add File: tests/test_app.py\n+x\n*** End Patch"
    s = _sess(_m(0, "user", _t("fix it")),
              _m(1, "assistant", ContentBlock(kind="tool_call", name="apply_patch", args=patch, call_id="p")),
              _m(2, "assistant", ContentBlock(kind="tool_call", name="shell", args={"command": ["bash", "-lc", "pytest -q"]}, call_id="s")),
              _m(3, "tool", ContentBlock(kind="tool_result", text="Traceback (most recent call last):\n  File x\nValueError: bad widget", call_id="s", is_error=True)),
              _m(4, "assistant", ContentBlock(kind="tool_call", name="Grep", args={"pattern": "widget", "path": "src/"}, call_id="g")),
              agent="codex")
    out = summarize(s)
    check("src/app.py" in out and "tests/test_app.py" in out, "apply_patch's files are listed (F93)")
    check("pytest -q" in out, "a shell command given as argv is listed (F93)")
    check("ValueError: bad widget" in out and "Traceback (most" not in out.split("errors:")[-1], "the error line is the one that says what failed (F94)")
    files = out.split("files:")[1].split("\n\n")[0] if "files:" in out else ""
    check("src/\n" not in files + "\n" and "- src/" not in files.replace("src/app.py", ""), "Grep's search folder is not a file (F95)")


def test_f96_f97_f98_custom_terms_in_any_case_and_as_escapes_and_every_id() -> None:
    from ectype.transform import Redactor
    r = Redactor.defaults(extra=["Işıkçı"], machine=False)
    check("IŞIKÇI" not in r.text("by IŞIKÇI today"), "a custom term matches in any case (F97)")
    esc = json.dumps("Işıkçı", ensure_ascii=True)[1:-1]
    check(esc not in r.text('{"owner": "' + esc + '"}'), "a custom term written as \\u escapes is redacted too (F96)")
    s = _sess(_m(0, "assistant", ContentBlock(kind="tool_call", name="Bash", args={}, call_id="call-Işıkçı-1")), cli_version="Işıkçı-build")
    red = r.session(s)
    check("Işıkçı" not in (red.cli_version or "") + (red.messages[0].blocks[0].call_id or ""), "call_id and cli_version are redacted (F98)")
    d = next(x for x in Redactor.defaults(extra=[f"t{i}" for i in range(3)], machine=False).details() if x["name"] == "term:t0")
    check("distinct" in d, "the redaction table says how many distinct values there were (F100)")


def test_f99_the_summary_follows_the_redaction_switch() -> None:
    home, sid = _web_store("f99x")
    with server(ECTYPE_CLAUDE_HOME=str(home)) as (call, _port):
        st, _, raw = call("GET", f"/api/summarize?agent=claude-code&id={sid}&redact=1&terms=hello")
        txt = json.loads(raw)["text"]
    check(st == 200 and "hello" not in txt.lower(), "the Summary is redacted when the switch is on (F99)")
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check("q.redact='1'" in page and "x.distinct>x.matches.length" in page, "the page asks for it, and shows unlisted matches (F99, F100)")
    with env(ECTYPE_CLAUDE_HOME=str(home)):
        rc, out, err = _cli(["summarize", sid, "-a", "claude-code", "--redact", "--redact-term", "hello"])
    check(rc == 0 and "hello" not in out.lower() and "[redacted:" in err, "summarize --redact redacts what it prints (F99)")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001, a check that crashes is a failure that must not hide the next one
            check(False, f"{fn.__name__} crashed: {type(e).__name__}: {e}")
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all audit3 checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
