"""Regression checks for the 2026-09-18 evening audit: one function per finding (the report is kept in
the workbench under notes/audit/), written before the fixes so each went red, then green.

    python3 tests/test_audit2.py

Every store here is synthetic and lives in a temp dir; no real session is read, the settings file is a
throwaway, and every other agent's store points at nothing. A check names the defect it guards, so a
failure says what came back.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ectype-audit2-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
NOTHING = TMP / "nothing-here"
for _v in ("ECTYPE_CLAUDE_HOME", "CODEX_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME", "ECTYPE_AIDER_HOME",
           "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME", "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(NOTHING)
os.environ.pop("CLAUDE_CONFIG_DIR", None)

from ectype import adapters, agentcli, budget, cli, convert as conv, ledger, mcp, settings, skill, titles, web  # noqa: E402
from ectype.adapters import base  # noqa: E402
from ectype.model import ContentBlock, Message, Session  # noqa: E402
from ectype.transform import Redactor  # noqa: E402

FAILS: list[str] = []
TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


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


BASE = {"cwd": "/tmp/proj", "isSidechain": False, "version": "2.1.0"}


def rec(sid, t, uid, parent, sec, content, model=None):
    m = {"role": "user" if t == "user" else "assistant", "content": content}
    if model:
        m["model"] = model
    return {**BASE, "type": t, "uuid": uid, "parentUuid": parent, "sessionId": sid,
            "timestamp": f"2026-09-10T10:00:{sec:02d}.000Z", "message": m}


def two_turns(sid: str, ask: str = "ask", reply: str = "answer") -> list:
    return [rec(sid, "user", "u1", None, 0, [{"type": "text", "text": ask}]),
            rec(sid, "assistant", "a1", "u1", 1, [{"type": "text", "text": reply}], "m")]


def write_claude(home: Path, sid: str, recs: list, spills: dict | None = None) -> Path:
    proj = home / "-tmp-proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    for name, data in (spills or {}).items():
        d = proj / sid / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(data, encoding="utf-8")
    return proj


def spill_session(sid: str, spill_abs: Path) -> list:
    stub = (f"<persisted-output>Output too large (1KB). Full output saved to: {spill_abs}\n\n"
            f"Preview (first 2KB):\nthe</persisted-output>")
    return [rec(sid, "user", "u1", None, 0, [{"type": "text", "text": "run"}]),
            rec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}], "m"),
            rec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": stub}]),
            rec(sid, "assistant", "a2", "u2", 3, [{"type": "text", "text": "done"}], "m")]


def cline_store(home: Path, sid: str, reply: str = "THREE WIDGETS") -> Path:
    d = home / sid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"version": 1, "session_id": sid, "source": "cli", "pid": 1, "started_at": "2026-09-10T10:00:00.000Z",
            "ended_at": "2026-09-10T10:00:05.000Z", "exit_code": 0, "status": "completed", "interactive": False,
            "provider": "p", "model": "m", "cwd": "/tmp/proj", "workspace_root": "/tmp/proj", "prompt": "count",
            "metadata": {}, "messages_path": str(d / f"{sid}.messages.json")}
    msgs = {"version": 1, "agent": "lead", "sessionId": sid,
            "origin": {"source": "cli", "mode": "user", "sessionId": sid, "version": "3.0.62"}, "system_prompt": "SYS",
            "messages": [
                {"id": "m1", "role": "user", "ts": 1, "content": [{"type": "text", "text": '<user_input mode="act">count the widgets</user_input>', "thinking": ""}]},
                {"id": "m2", "role": "assistant", "ts": 2, "content": [{"type": "text", "text": reply, "thinking": ""}], "modelInfo": {"id": "m", "provider": "p"}}]}
    (d / f"{sid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / f"{sid}.messages.json").write_text(json.dumps(msgs), encoding="utf-8")
    return home


def gemini_store(home: Path, gsid: str, records: list) -> Path:
    proj = home / "p"
    (proj / "chats").mkdir(parents=True, exist_ok=True)
    (proj / ".project_root").write_text("/tmp/p", encoding="utf-8")
    head = {"sessionId": gsid, "projectHash": "h", "startTime": "2026-09-10T10:00:00.000Z", "lastUpdated": "2026-09-10T10:00:05.000Z", "kind": "main"}
    (proj / "chats" / f"session-2026-09-10T10-00-{gsid[:8]}.jsonl").write_text("\n".join(json.dumps(r) for r in [head, *records]) + "\n", encoding="utf-8")
    return proj


def run_cli(argv: list[str]) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code:
                err.write(str(e.code))
    return out.getvalue(), err.getvalue(), code


def post(port: int, path: str, body: dict):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.request("POST", path, body=json.dumps(body), headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    raw = r.read()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, raw


# --------------------------------------------------------------------------- F01
def test_f01_cline_downloads_carry_both_files() -> None:
    """The download branch zipped only when a spill-file note was present; a Cline copy or conversion
    (two files, no such note) downloaded the metadata JSON alone, without the conversation."""
    home = TMP / "f01-cline" / "sessions"
    sid = "1700000000000_abcde"
    cline_store(home, sid)
    chome = TMP / "f01-claude"
    csid = "aaaa0002-0000-0000-0000-000000000002"
    write_claude(chome, csid, two_turns(csid, "what is the code word", "ZEBRA-QUARTZ"))
    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with env(ECTYPE_CLINE_HOME=str(home)):
            st, h, raw = post(port, "/api/export", {"agent": "cline", "id": sid, "opts": {}, "format": "native", "mode": "download"})
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist() if raw.startswith(b"PK") else []
        check(st == 200 and h.get("x-bundle") == "zip" and any(n.endswith(".messages.json") for n in names) and len(names) == 2,
              f"a Cline native copy downloads as a zip of both files ({h.get('x-bundle')}, {names})")
        body = zipfile.ZipFile(io.BytesIO(raw)).read([n for n in names if n.endswith(".messages.json")][0]) if names else b""
        check(b"THREE WIDGETS" in body, "the conversation travels inside it")
        with env(ECTYPE_CLAUDE_HOME=str(chome)):
            st, h, raw = post(port, "/api/export", {"agent": "claude-code", "id": csid, "opts": {}, "format": "native", "convert": "cline", "mode": "download"})
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist() if raw.startswith(b"PK") else []
        check(st == 200 and h.get("x-bundle") == "zip" and len(names) == 2 and b"ZEBRA-QUARTZ" in raw or (names and b"ZEBRA-QUARTZ" in zipfile.ZipFile(io.BytesIO(raw)).read([n for n in names if n.endswith(".messages.json")][0])),
              f"a conversion to Cline downloads as a zip holding the reply ({h.get('x-bundle')}, {names})")
        check(h.get("x-target-relpath", "").endswith(".json"), "the relpath still names the metadata file the agent reads")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- F02
def test_f02_gone_means_the_file_is_gone() -> None:
    """A plain `summarize --all` flagged every entry of an agent it did not enumerate as gone."""
    lm = TMP / "f02-lms"
    lm.mkdir()
    f = lm / "1000.conversation.json"
    doc = {"name": "probe chat", "createdAt": 1700000000000, "messages": [
        {"versions": [{"type": "singleStep", "role": "user", "content": [{"type": "text", "text": "hello there"}]}], "currentlySelected": 0},
        {"versions": [{"type": "multiStep", "role": "assistant", "steps": [{"type": "contentBlock", "content": [{"type": "text", "text": "hi"}]}]}], "currentlySelected": 0}]}
    f.write_text(json.dumps(doc), encoding="utf-8")
    chome = TMP / "f02-claude"
    sid = "aaaa0003-0000-0000-0000-000000000003"
    write_claude(chome, sid, two_turns(sid))
    settings.save({})
    with env(ECTYPE_LMSTUDIO_HOME=str(lm), ECTYPE_CLAUDE_HOME=str(chome)):
        ledger.update(["lmstudio"])
        st = ledger.update()
        entry = ledger.load().get("lmstudio:1000") or {}
        check(not entry.get("gone") and st["gone"] == 0, f"a run that did not enumerate LM Studio leaves its summary alone (gone={entry.get('gone')})")
        res = ledger.search("hello there", ["lmstudio"])
        check(res["matches"] and res["matches"][0]["gone"] is False and res["never"] == 0,
              f"search still counts it as summarised ({res['summarised']} of {res['known']}, never {res['never']})")
        f.unlink()
        st = ledger.update(["lmstudio"])
        check(st["gone"] == 1 and ledger.load()["lmstudio:1000"].get("gone") is True, "a file that really vanished is flagged")
        f.write_text(json.dumps(doc), encoding="utf-8")
        ledger.update(["lmstudio"])
        check(not ledger.load()["lmstudio:1000"].get("gone"), "and the flag clears when the file is back")
    settings.save({})


# --------------------------------------------------------------------------- F03
def test_f03_spill_paths_with_spaces_are_restored() -> None:
    """Both pointer regexes captured `\\S+`, so a home like `C:\\Users\\John Smith` cut the path at
    the space and every spilled result stayed a stub marked 'truncated by agent'."""
    home = TMP / "with space" / "claude"
    sid = "aaaa0001-0000-0000-0000-000000000001"
    proj = write_claude(home, sid, [], spills={"spill.txt": "FULL TEXT " * 100})
    spill = proj / sid / "tool-results" / "spill.txt"
    write_claude(home, sid, spill_session(sid, spill), spills={"spill.txt": "FULL TEXT " * 100})
    with env(ECTYPE_CLAUDE_HOME=str(home)):
        ad = adapters.get("claude-code")
        s = ad.load(ad.discover()[0])
    b = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"][0]
    check(not b.truncated and b.meta.get("restored_from_spill") and b.text.startswith("FULL TEXT"),
          f"Claude Code: a spill under a path with a space is restored (truncated={b.truncated}, restored={b.meta.get('restored_from_spill')})")
    ghome = TMP / "with space" / "gemini"
    gsid = "g1g1g1g1-1111-2222-3333-444444444444"
    proj = gemini_store(ghome, gsid, [])
    sd = proj / "tool-outputs" / f"session-{gsid}"
    sd.mkdir(parents=True)
    (sd / "call-1.txt").write_text("FULL " * 100, encoding="utf-8")
    stub = "head... For full output see: " + str(sd / "call-1.txt")
    gemini_store(ghome, gsid, [
        {"id": "m1", "timestamp": "2026-09-10T10:00:01.000Z", "type": "user", "content": [{"text": "list"}]},
        {"id": "m2", "timestamp": "2026-09-10T10:00:02.000Z", "type": "gemini", "content": "done", "toolCalls": [
            {"id": "call-1", "name": "run_shell_command", "args": {}, "status": "success",
             "result": [{"functionResponse": {"response": {"output": stub}}}]}]}])
    with env(ECTYPE_GEMINI_HOME=str(ghome)):
        ad = adapters.get("gemini-cli")
        s = ad.load(ad.discover()[0])
    b = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"][0]
    check(not b.truncated and b.meta.get("restored_from_spill") and b.text.startswith("FULL "),
          f"Gemini CLI: the same (truncated={b.truncated}, restored={b.meta.get('restored_from_spill')})")


# --------------------------------------------------------------------------- F04
def test_f04_redaction_reaches_the_cline_template_prompt() -> None:
    """`write_cline` copied the template's system prompt verbatim after every other envelope field
    had been redacted; the Codex writer blanks and redacts its equivalent."""
    tmpl = TMP / "f04" / "t.json"
    tmpl.parent.mkdir(parents=True)
    tmpl.write_text(json.dumps({"version": 1, "provider": "p", "model": "m", "cwd": "/srv/realname/proj",
                                "metadata": {"sessionHistoryOrigin": {"mode": "user", "version": "3.0.62"}}}), encoding="utf-8")
    tmpl.with_name("t.messages.json").write_text(json.dumps({"version": 1, "agent": "lead",
                                                             "system_prompt": "Home Directory: /srv/realname SECRET-MARKER"}), encoding="utf-8")
    s = Session("claude-code", "cccc0004-0000-0000-0000-000000000004", TMP / "x.jsonl",
                [Message(0, "user", None, [ContentBlock("text", "hi")]), Message(1, "assistant", None, [ContentBlock("text", "yo")])],
                cwd="/srv/realname/proj")
    red = Redactor.defaults(extra=["/srv/realname=/srv/user", "SECRET-MARKER=[X]"], machine=False)
    path, rep = conv.convert(red.session(s), "cline", TMP / "f04-out", template=tmpl, redact=red.text, backup=False)
    msgs = json.loads(path.with_name(f"{rep.new_id}.messages.json").read_text(encoding="utf-8"))
    check("realname" not in msgs["system_prompt"] and "SECRET-MARKER" not in msgs["system_prompt"] and "/srv/user" in msgs["system_prompt"],
          f"the template's system prompt is redacted like every other envelope field ({msgs['system_prompt']!r})")
    path2, rep2 = conv.convert(s, "cline", TMP / "f04-out2", template=tmpl, backup=False)
    msgs2 = json.loads(path2.with_name(f"{rep2.new_id}.messages.json").read_text(encoding="utf-8"))
    check("SECRET-MARKER" in msgs2["system_prompt"], "without --redact the template's prompt is carried as before")


# --------------------------------------------------------------------------- F05
def test_f05_one_unreadable_store_does_not_abort_the_listing() -> None:
    """A `discover()` that raised (a permission-denied directory) took `ectype list`, `ectype agents`
    and the MCP `list_agents` down with a traceback; the twelve readable stores were not shown."""
    chome = TMP / "f05-claude"
    sid = "aaaa0005-0000-0000-0000-000000000005"
    write_claude(chome, sid, two_turns(sid))
    broken = TMP / "f05-cline"
    broken.mkdir()
    ad = adapters.get("cline")
    real = ad.discover
    ad.discover = lambda: (_ for _ in ()).throw(PermissionError(13, "Permission denied", str(broken)))
    try:
        with env(ECTYPE_CLAUDE_HOME=str(chome), ECTYPE_CLINE_HOME=str(broken)):
            out, err, code = run_cli(["list"])
            check(code == 0 and sid[:8] in out and "cline" in err and "Permission denied" in err,
                  f"list prints the readable stores and names the broken one on stderr (exit {code}, stderr {err.strip()[:70]!r})")
            out, err, code = run_cli(["agents"])
            row = next((l for l in out.splitlines() if l.startswith("cline")), "")
            check(code == 0 and "Permission denied" in row, f"agents prints the failure in the store's own row ({row[:90]!r})")
            text = mcp._call("list_agents", {}, False)
            check("claude-code" in text and "cline" in text and "Permission denied" in text, "the MCP list_agents does the same")
    finally:        ad.discover = real


# --------------------------------------------------------------------------- F07
def test_f07_a_non_object_line_is_counted_not_fatal() -> None:
    """`iter_jsonl` yielded any JSON value and every adapter called .get on it."""
    chome = TMP / "f07"
    sid = "aaaa0008-0000-0000-0000-000000000008"
    proj = write_claude(chome, sid, two_turns(sid))
    with open(proj / f"{sid}.jsonl", "a", encoding="utf-8") as fh:
        fh.write("42\n[1, 2]\n")
    with env(ECTYPE_CLAUDE_HOME=str(chome)):
        ad = adapters.get("claude-code")
        s = ad.load(ad.discover()[0])
    check(len(s.messages) == 2 and s.meta.get("skipped_lines") == 2,
          f"valid JSON that is not an object is counted with the unreadable lines ({s.meta.get('skipped_lines')})")


# --------------------------------------------------------------------------- F16
def test_f16_the_title_cap_is_one_named_constant() -> None:
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "ectype" / "adapters").glob("*.py") if p.name != "base.py")
    check(base.TITLE_CHARS == 80 and "[:80]" not in src, "adapters cut titles with TITLE_CHARS, never a bare [:80]")


# --------------------------------------------------------------------------- F18
def test_f18_redaction_survives_a_missing_login_name() -> None:
    """`getpass.getuser()` raises where no login name and no passwd entry exist; every redaction died."""
    import ectype.transform as tr
    real = tr.getpass.getuser
    tr.getpass.getuser = lambda: (_ for _ in ()).throw(KeyError("getpwuid(): uid not found"))
    try:
        red = Redactor.defaults()
    finally:
        tr.getpass.getuser = real
    names = {r.name for r in red.rules}
    check("username" not in names and "media" not in names and "email" in names,
          f"no login name: the username and media rules are skipped, the rest stand ({sorted(names)[:4]})")
    check(red.text("a@b.co") == "user@example.com", "and redaction still runs")


# --------------------------------------------------------------------------- F09
def test_f09_command_folders_follow_the_agents_own_variables() -> None:
    """The three command folders were fixed under ~ while Codex honours CODEX_HOME and Claude Code
    CLAUDE_CONFIG_DIR; the file went where the agent never looks and status said installed."""
    with env(CODEX_HOME=str(TMP / "codexhome"), CLAUDE_CONFIG_DIR=str(TMP / "claudecfg")):
        codex, claude, gemini = skill.path("codex", "x"), skill.path("claude-code", "x"), skill.path("gemini-cli", "x")
    check(codex == TMP / "codexhome" / "prompts" / "x.md", f"CODEX_HOME decides Codex's prompts folder ({codex})")
    check(claude == TMP / "claudecfg" / "commands" / "x.md", f"CLAUDE_CONFIG_DIR decides Claude Code's ({claude})")
    check(str(gemini).endswith("/.gemini/commands/x.toml"), "Gemini CLI keeps ~/.gemini/commands")
    with env(CODEX_HOME=None, CLAUDE_CONFIG_DIR=None):
        check(str(skill.path("codex", "x")).endswith("/.codex/prompts/x.md"), "unset, the default under ~ stands")


# --------------------------------------------------------------------------- F10 / F17
def test_f10_f17_fixture_redaction_quotes_names_and_leaves_no_half_copy() -> None:
    """Table names were interpolated bare and every table was assumed to have a rowid; a failure
    midway left an unredacted copy of the database on disk with its handle open."""
    src = TMP / "f17" / "store.db"
    src.parent.mkdir(parents=True)
    con = sqlite3.connect(src)
    con.execute('create table "group" (id integer primary key, note text)')
    con.execute("create table keyed (k text, v text, primary key (k)) without rowid")
    con.execute('insert into "group" values (1, "call SECRET-ONE")')
    con.execute("insert into keyed values ('a', 'call SECRET-TWO')")
    con.commit()
    con.close()
    red = Redactor.defaults(extra=["SECRET-ONE=[X]", "SECRET-TWO=[Y]"], machine=False)
    dst = TMP / "f17" / "copy.db"
    red.file(src, dst)
    con = sqlite3.connect(dst)
    got = (con.execute('select note from "group"').fetchone()[0], con.execute("select v from keyed").fetchone()[0])
    con.close()
    check(got == ("call [X]", "call [Y]"), f"a reserved table name and a WITHOUT ROWID table are both redacted ({got})")

    class Boom(Redactor):
        def text(self, s):
            raise RuntimeError("redactor broke")
    bad = TMP / "f17" / "bad.db"
    try:
        Boom().file(src, bad)
        check(False, "a failing redaction raises")
    except RuntimeError:
        check(not bad.exists(), "a failing redaction leaves no half-redacted copy behind")


# --------------------------------------------------------------------------- F11
def test_f11_a_workspaces_failure_is_said_not_swallowed() -> None:
    ad = adapters.get("codex")
    real = ad.workspaces
    ad.workspaces = lambda: (_ for _ in ()).throw(OSError("mount went away"))
    home = TMP / "f11-codex"
    (home / "sessions").mkdir(parents=True)
    err = io.StringIO()
    try:
        with env(CODEX_HOME=str(home)), contextlib.redirect_stderr(err):
            ws = web.workspaces()
    finally:
        ad.workspaces = real
    check(ws.get("codex") == [] and "codex" in err.getvalue() and "mount went away" in err.getvalue(),
          f"the picker stays usable and stderr names the store and the error ({err.getvalue().strip()[:80]!r})")


# --------------------------------------------------------------------------- F06
def test_f06_the_terminal_driver_stops_when_the_ui_is_gone() -> None:
    """A UI that died after the prompt cost the whole TIMEOUT; one that never started raised EIO."""
    binroot = TMP / "f06"
    binroot.mkdir()
    late = binroot / "dies-after-load"
    late.write_text("#!/bin/sh\nsleep 1\nexit 3\n", encoding="utf-8")
    late.chmod(0o755)
    never = binroot / "never-starts"
    never.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    never.chmod(0o755)
    old = (agentcli.TIMEOUT, agentcli.TUI_SETTLE, agentcli.TUI_QUIET)
    agentcli.TIMEOUT, agentcli.TUI_SETTLE, agentcli.TUI_QUIET = 30, 0.3, 3
    try:
        spec = {"bin": str(late), "tui_resume": lambda sid: [sid], "replied": lambda sid: (0, ""), "note": "fake"}
        t0 = time.time()
        ok, detail = agentcli._tui_resume(spec, "x", "hello", None)
        took = time.time() - t0
        check(ok is False and took < 10 and "code 3" in detail, f"a UI that dies after the prompt is reported at once with its exit code ({took:.1f}s: {detail})")
        spec["bin"] = str(never)
        t0 = time.time()
        ok, detail = agentcli._tui_resume(spec, "x", "hello", None)
        took = time.time() - t0
        check(ok is False and took < 10 and "code 2" in detail, f"a UI that never starts is reported, not raised ({took:.1f}s: {detail})")
    finally:
        agentcli.TIMEOUT, agentcli.TUI_SETTLE, agentcli.TUI_QUIET = old


# --------------------------------------------------------------------------- F08
def test_f08_settings_view_is_the_default_on_every_surface() -> None:
    """`show`/`export` defaulted --mode to custom and the MCP hard-coded mode, merge, stamps and
    markers, while the GUI's toolbar read Settings -> View (ARCHITECTURE D14)."""
    chome = TMP / "f08"
    sid = "aaaa0006-0000-0000-0000-000000000006"
    write_claude(chome, sid, [rec(sid, "user", "u1", None, 0, [{"type": "text", "text": "ask"}]),
                              rec(sid, "assistant", "a1", "u1", 1, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}], "m"),
                              rec(sid, "user", "u2", "a1", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]),
                              rec(sid, "assistant", "a2", "u2", 3, [{"type": "text", "text": "done"}], "m")])
    settings.save({"view": {"stamps": "none", "mode": "brief", "collapse": False}})
    stamped = lambda t: bool(re.search(r"^\[(USER|ASSISTANT)\] \d{4}-", t, re.M))  # noqa: E731
    try:
        with env(ECTYPE_CLAUDE_HOME=str(chome)):
            cli_text, _, _ = run_cli(["show", sid[:8]])
            exp_text, _, _ = run_cli(["export", sid[:8]])
            mcp_text = mcp._call("show_session", {"id": sid[:8]}, False)
            full, _, _ = run_cli(["show", sid[:8], "--mode", "full"])
        check("● Bash(" not in cli_text and "● Bash" in cli_text, "show follows Settings -> View -> default tool output")
        check("● Bash(" not in exp_text, "export does too")
        check("● Bash(" not in mcp_text and not stamped(mcp_text), "the MCP show_session follows the mode and the timestamps setting")
        check("● Bash(" in full, "an explicit --mode still wins")
    finally:
        settings.save({})


# --------------------------------------------------------------------------- F12
def test_f12_system_messages_and_agent_notices_are_two_rows() -> None:
    """One 'notices' row mixed the store's own system messages with the agent notices D19 made
    optional, and was hard-coded on, so with notices off it read as 'left out (over the cap)'."""
    sid = "aaaa0010-0000-0000-0000-000000000010"
    chome = TMP / "f12"
    write_claude(chome, sid, two_turns(sid) + [{"type": "system", "subtype": "api_error", "content": "API Error: 529 overloaded",
                                                "timestamp": "2026-09-10T10:00:04.000Z", "uuid": "s1", "sessionId": sid}])
    with env(ECTYPE_CLAUDE_HOME=str(chome)):
        s = adapters.load(adapters.get("claude-code").discover()[0])
    b = budget.breakdown(s, {"mode": "full", "cap": 0, "thinking": True, "tools": True, "env": True, "notices": False, "collapse": True})
    row = next(p for p in b["parts"] if p["key"] == "notices")
    check(row["on"] is False and row["remaining"] > 0 and row["tokens"] == 0, f"with agent notices off their row says off ({row['on']}, remaining {row['remaining']})")
    plain = Session("aider", "x#0", TMP / "x.md", [Message(0, "user", TS, [ContentBlock("text", "hi")]),
                                                    Message(1, "system", TS, [ContentBlock("system", "Model: gpt")])])
    b2 = budget.breakdown(plain, {"mode": "full", "cap": 0, "notices": False})
    sysrow = next(p for p in b2["parts"] if p["key"] == "system")
    check(sysrow["on"] and sysrow["tokens"] > 0, "a store's own system message counts under system messages and is always on")
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check("system messages, agent notices" in page, "the Help lists both rows")


# --------------------------------------------------------------------------- F13
def test_f13_gemini_and_antigravity_notices_follow_the_toggle() -> None:
    """Only Claude Code's system records carried the agent_notice flag; Gemini CLI's info/error
    records and Antigravity's SYSTEM_MESSAGE steps were always shown, whatever the toggle said."""
    ghome = TMP / "f13-gemini"
    gsid = "g2g2g2g2-1111-2222-3333-444444444444"
    gemini_store(ghome, gsid, [
        {"id": "m1", "timestamp": "2026-09-10T10:00:01.000Z", "type": "user", "content": [{"text": "hi"}]},
        {"id": "m2", "timestamp": "2026-09-10T10:00:02.000Z", "type": "error", "content": "API Error: 429 rate limited"},
        {"id": "m3", "timestamp": "2026-09-10T10:00:03.000Z", "type": "gemini", "content": "hello"}])
    with env(ECTYPE_GEMINI_HOME=str(ghome)):
        off, _, _ = run_cli(["show", gsid[:8], "-a", "gemini-cli", "--mode", "full"])
        on, _, _ = run_cli(["show", gsid[:8], "-a", "gemini-cli", "--mode", "full", "--notices"])
        summ, _, _ = run_cli(["summarize", gsid[:8], "-a", "gemini-cli"])
    check("429" not in off and "429" in on, "a Gemini CLI error record is an agent notice: off by default, on with --notices")
    check("429" in summ, "summarize still lists it under errors")
    ag = TMP / "f13-agy"
    d = ag / "brain" / "11111111-1111-1111-1111-111111111111" / ".system_generated" / "logs"
    d.mkdir(parents=True)
    steps = [{"step_index": "0", "type": "USER_INPUT", "content": "<USER_REQUEST>go</USER_REQUEST>", "created_at": "2026-09-10T10:00:00Z"},
             {"step_index": "1", "type": "SYSTEM_MESSAGE", "content": "changed setting `Model Selection` from A to B.", "created_at": "2026-09-10T10:00:01Z"},
             {"step_index": "2", "type": "PLANNER_RESPONSE", "content": "done", "created_at": "2026-09-10T10:00:02Z"}]
    (d / "transcript_full.jsonl").write_text("\n".join(json.dumps(x) for x in steps) + "\n", encoding="utf-8")
    with env(ECTYPE_ANTIGRAVITY_HOME=str(ag)):
        ad = adapters.get("antigravity")
        s = ad.load(ad.discover()[0])
    check(any(m.meta.get("agent_notice") for m in s.messages if m.role == "system"), "an Antigravity SYSTEM_MESSAGE step carries the agent_notice flag")


# --------------------------------------------------------------------------- F14
def test_f14_the_skill_test_redirects_the_windows_home_too() -> None:
    src = (ROOT / "tests" / "test_report.py").read_text(encoding="utf-8")
    fn = src.split("def test_skill_command_name_install_and_remove", 1)[1].split("\ndef ", 1)[0]
    check('"USERPROFILE"' in fn and '"CODEX_HOME"' in fn and '"CLAUDE_CONFIG_DIR"' in fn,
          "the skill test redirects HOME, USERPROFILE and the agents' own variables, so Windows never writes into the real command folders")


# --------------------------------------------------------------------------- F15
def test_f15_the_docs_agree_with_the_tree() -> None:
    index = (ROOT / "index.md").read_text(encoding="utf-8")
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    said = re.search(r"decisions \(D1 to D(\d+)\)", index)
    have = len(re.findall(r"^### D\d+", arch, re.M))
    check(said is not None and int(said.group(1)) == have, f"index.md names the D-range ARCHITECTURE.md actually has ({said and said.group(1)} vs {have})")
    check("Cline" in arch.split("## Decisions", 1)[0], "the Shape block names all four writers")
    check("notes/assets/annotate-overview.py" in (ROOT / "notes" / "assets" / "annotate-overview.py").read_text(encoding="utf-8"),
          "the screenshot script names its own folder")


# --------------------------------------------------------------------------- F19
def test_f19_the_repoint_note_says_what_was_repointed() -> None:
    """The note said 'paths repointed' whenever files were copied, even when no record named them."""
    orig = TMP / "f19-orig" / "claude"
    sid = "aaaa0009-0000-0000-0000-000000000009"
    proj = write_claude(orig, sid, [], spills={"spill.txt": "FULL"})
    spill_abs = proj / sid / "tool-results" / "spill.txt"
    write_claude(orig, sid, spill_session(sid, spill_abs), spills={"spill.txt": "FULL"})
    import shutil
    moved = TMP / "f19-moved" / "claude"
    shutil.copytree(orig, moved)
    notes = {}
    for label, home in (("same path", orig), ("other path", moved)):
        with env(ECTYPE_CLAUDE_HOME=str(home)):
            ad = adapters.get("claude-code")
            ref = ad.discover()[0]
            s = ad.load(ref)
            extra = [(a, b) for a, b in ad.artifacts(ref) if a != s.path]
            _, rep = conv.convert(s, "claude-code", TMP / f"f19-out-{label[:4]}", source_path=s.path, extra=extra, backup=False)
        notes[label] = next((n for n in rep.notes if "sidecar" in n), "")
    check("and their paths repointed" in notes["same path"], f"read at the path the agent wrote: repointed ({notes['same path'][:60]!r})")
    check("0 of their paths repointed" in notes["other path"], f"read through another path: the note says so ({notes['other path'][:90]!r})")


# --------------------------------------------------------------------------- F20
def test_f20_a_restore_keeps_what_it_overwrites() -> None:
    from ectype import backup as bk
    src = TMP / "f20-live.txt"
    src.write_text("version one", encoding="utf-8")
    settings.save({})
    folder = bk.save([src], "f20")
    src.write_text("version two", encoding="utf-8")
    done = bk.restore(folder.name)
    check(src.read_text(encoding="utf-8") == "version one" and done.safety is not None and "before-restore" in done.safety.name,
          f"the restore puts version one back and saves version two first ({done.safety and done.safety.name})")
    again = bk.restore(done.safety.name)
    check(src.read_text(encoding="utf-8") == "version two" and len(again) == 1, "and that safety copy restores version two: a restore is reversible")
    dry = bk.restore(folder.name, dry_run=True)
    check(dry.safety is None and src.read_text(encoding="utf-8") == "version two", "a dry run saves nothing and writes nothing")
    out, _, code = run_cli(["backup", "--restore", folder.name])
    check(code == 0 and "undo with: ectype backup --restore" in out, "the CLI says how to undo a restore")


# --------------------------------------------------------------------------- F21
def test_f21_titles_does_not_shadow_builtins() -> None:
    check(callable(getattr(titles, "names", None)) and callable(getattr(titles, "put", None))
          and not hasattr(titles, "all") and not hasattr(titles, "set"), "titles.names()/put() replaced all()/set()")


# --------------------------------------------------------------------------- F22
def test_f22_codex_rows_carry_their_project() -> None:
    home = TMP / "f22-codex"
    d = home / "sessions" / "2026" / "09" / "10"
    d.mkdir(parents=True)
    sid = "01a08ac2-8900-7000-8000-000000000001"
    recs = [{"timestamp": "2026-09-10T10:00:00.000Z", "type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/myproj", "cli_version": "0.1"}},
            {"timestamp": "2026-09-10T10:00:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}}]
    (d / f"rollout-2026-09-10T13-00-00-{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    with env(CODEX_HOME=str(home)):
        refs = adapters.get("codex").discover()
    check(len(refs) == 1 and refs[0].project == "myproj", f"a Codex row lists the project from its header line ({refs and refs[0].project})")


# --------------------------------------------------------------------------- F25 (found in the browser, 2026-09-25)
def test_f25_every_toolbar_control_rerenders() -> None:
    """The agent-notices checkbox had no change handler: ticking it changed nothing until another
    control re-rendered. Every toolbar control that `opts()` reads must be wired to a re-render."""
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    wired = set(re.findall(r"'([a-zA-Z]+)'", page.split(".forEach(id=>$('#'+id).onchange=rerender)", 1)[0].rsplit("[", 1)[1]))
    wired |= {m for m in re.findall(r"\$\('#([a-zA-Z]+)'\)\.onchange=rerender", page)}
    body = "\n".join(page.split("function opts()", 1)[1].split("\n")[:8])          # the whole function, not its first line
    read = set(re.findall(r"\$\('#([a-zA-Z]+)'\)\.(?:checked|value)", body))
    read -= {"terms", "start", "end", "redact"}          # terms/start/end debounce through sched(); redact has its own handler
    missing = sorted(read - wired)
    check(not missing, f"every control opts() reads is wired to a re-render (missing: {missing or 'none'})")


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
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all audit2 checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
