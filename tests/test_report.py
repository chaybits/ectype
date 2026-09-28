"""Regression checks for the 2026-09-18 report: one function per finding, plus the features that
round added (the ledger, `recall`, the Settings sections).

    python3 tests/test_report.py

Every store here is synthetic and lives in a temp dir; no real session is read, the settings file
is a throwaway, and every other agent's store points at nothing. A check names the defect it
guards, so a failure says what came back.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ectype-report-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
for _v in ("CODEX_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME",
           "ECTYPE_AIDER_HOME", "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME",
           "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(TMP / "nothing-here")

from ectype import adapters, agentcli, budget, cli, convert as conv, ledger, mcp, notice, settings  # noqa: E402
from ectype.model import ContentBlock, Message, Session                                            # noqa: E402
from ectype.summary import summarize                                                               # noqa: E402

FAILS: list[str] = []
TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SID = "0e0e0e0e-1111-2222-3333-444444444444"
SID2 = "0f0f0f0f-1111-2222-3333-444444444444"


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def claude_records(sid: str, ask: str = "count the files", extra: list[dict] | None = None) -> list[dict]:
    base = {"sessionId": sid, "cwd": "/tmp/proj", "isSidechain": False, "version": "2.1.0"}
    recs = [
        {**base, "type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-09-10T10:00:00.000Z",
         "message": {"role": "user", "content": ask}},
        {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-09-10T10:00:01.000Z",
         "message": {"role": "assistant", "model": "probe-model", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls | wc -l"}}]}},
        {**base, "type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": "2026-09-10T10:00:02.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "12\n" + "x" * 3000}]}},
        {**base, "type": "assistant", "uuid": "a2", "parentUuid": "u2", "timestamp": "2026-09-10T10:00:03.000Z",
         "message": {"role": "assistant", "model": "probe-model", "content": [{"type": "text", "text": "twelve files"}]}},
    ]
    return recs + (extra or [])


def claude_store(name: str, files: dict[str, list[dict]]) -> Path:
    """`<TMP>/<name>/-tmp-proj/<sid>.jsonl` per entry; returns the store's home."""
    home = TMP / name
    proj = home / "-tmp-proj"
    proj.mkdir(parents=True, exist_ok=True)
    for sid, recs in files.items():
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    return home


def session(msgs: list[Message], sid: str = SID) -> Session:
    return Session("claude-code", sid, Path("/nowhere/x.jsonl"), msgs, cwd="/nowhere", started=TS, ended=TS)


def run_cli(argv: list[str]) -> tuple[str, str, int]:
    """cli.main in-process; (stdout, stderr, exit code)."""
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


# ---------------------------------------------------------------------------- the six defects
def test_mcp_cap_defaults_to_settings() -> None:
    """`show_session` / `session_budget` with no cap used cap 0 (everything) while mode defaulted
    to custom: the whole session for a call that gave no arguments. Now the Settings cap."""
    settings.save({"view": {"cap": 77}})
    check(mcp._opts({})["cap"] == 77, f"a missing cap means the Settings cap ({mcp._opts({})['cap']})")
    check(mcp._opts({"cap": 0})["cap"] == 0, "an explicit cap of 0 still means no cap")
    check(mcp._opts({"cap": 5})["cap"] == 5, "an explicit cap is kept")


def test_show_has_a_mode_flag_and_the_slash_command_uses_real_flags() -> None:
    """The shipped slash command ran `ectype show <id> --mode brief`; `show` had no --mode."""
    os.environ["ECTYPE_CLAUDE_HOME"] = str(claude_store("s-show", {SID: claude_records(SID)}))
    brief, _, code = run_cli(["show", SID[:8], "--mode", "brief"])
    full, _, _ = run_cli(["show", SID[:8], "--mode", "full"])
    check(code == 0 and "● Bash" in brief and "xxxx" not in brief, "show --mode brief keeps the name and drops the output")
    check("xxxx" in full, "show --mode full keeps the output")
    text = (ROOT / "integrations" / "claude-code" / "ectype.md").read_text(encoding="utf-8")
    bad = []
    for cmd, rest in re.findall(r"`ectype (\w+)([^`]*)`", text):
        helptext = subprocess.run([sys.executable, "-m", "ectype", cmd, "--help"], capture_output=True, text=True, cwd=ROOT).stdout
        for flag in re.findall(r"(--[a-z-]+)", rest):
            if flag not in helptext:
                bad.append(f"{cmd} {flag}")
    check(not bad, f"every flag the slash command uses exists on that subcommand ({', '.join(bad) or 'all present'})")


def test_gemini_spill_file_is_read_back() -> None:
    """Gemini's adapter pointed at the spill file and never read it; `full` was not full."""
    home = TMP / "gemini"
    proj = home / "myproj"
    (proj / "chats").mkdir(parents=True, exist_ok=True)
    (proj / ".project_root").write_text("/tmp/myproj", encoding="utf-8")
    gsid = "g1g1g1g1-1111-2222-3333-444444444444"
    spill_dir = proj / "tool-outputs" / f"session-{gsid}"
    spill_dir.mkdir(parents=True)
    body = "FULL OUTPUT " + "y" * 5000
    (spill_dir / "call-1.txt").write_text(body, encoding="utf-8")
    stub = "head of it... For full output see: " + str(spill_dir / "call-1.txt")
    recs = [{"sessionId": gsid, "projectHash": "h", "startTime": "2026-09-10T10:00:00.000Z", "lastUpdated": "2026-09-10T10:00:05.000Z", "kind": "main"},
            {"id": "m1", "timestamp": "2026-09-10T10:00:01.000Z", "type": "user", "content": [{"text": "list it"}]},
            {"id": "m2", "timestamp": "2026-09-10T10:00:02.000Z", "type": "gemini", "content": "done", "toolCalls": [
                {"id": "call-1", "name": "run_shell_command", "args": {"command": "ls"}, "status": "success",
                 "result": [{"functionResponse": {"response": {"output": stub}}}]},
                {"id": "call-2", "name": "run_shell_command", "args": {"command": "ls"}, "status": "success",
                 "result": [{"functionResponse": {"response": {"output": "For full output see: /nowhere/gone.txt"}}}]}]}]
    (proj / "chats" / "session-2026-09-10T10-00-g1g1g1g1.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    os.environ["ECTYPE_GEMINI_HOME"] = str(home)
    ref = adapters.get("gemini-cli").discover()[0]
    s = adapters.load(ref)
    res = [b for m in s.messages for b in m.blocks if b.kind == "tool_result"]
    check(len(res) == 2 and res[0].text == body and res[0].meta.get("restored_from_spill") == len(body) - len(stub),
          f"the spill file's text replaces the stub and says how much came back ({len(res[0].text)} chars)")
    check(res[1].truncated and res[1].spill_path and not res[1].meta.get("restored_from_spill"),
          "a spill file that is not on disk leaves the result marked truncated, with the pointer")
    os.environ["ECTYPE_GEMINI_HOME"] = str(TMP / "nothing-here")


def test_convert_reports_the_id_it_wrote() -> None:
    """--verify took the file's stem for the session id; a Codex rollout's stem is not its id."""
    s = session([Message(0, "user", TS, [ContentBlock("text", "hello")]),
                 Message(1, "assistant", TS, [ContentBlock("text", "hi")])])
    for target in ("claude-code", "codex", "gemini-cli"):
        path, rep = conv.convert(s, target, TMP / f"out-{target}", backup=False)
        ok = bool(rep.new_id) and rep.new_id[:8] in path.name       # Gemini's file name carries the first 8 characters only
        if target == "codex":
            ok = ok and path.stem != rep.new_id and path.stem.startswith("rollout-")
        if target == "claude-code":
            ok = ok and path.stem == rep.new_id
        check(ok, f"{target}: the report carries the id the writer used ({rep.new_id and rep.new_id[:8]}), stem {path.stem[:20]}…")


def test_install_backup_is_taken_by_convert_itself() -> None:
    """Only the CLI took the install-time backup; the GUI and MCP installs into Codex did not."""
    home = TMP / "codex-live"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "session_index.jsonl").write_text('{"id": "old", "thread_name": "kept"}\n', encoding="utf-8")
    os.environ["CODEX_HOME"] = str(home)
    s = session([Message(0, "user", TS, [ContentBlock("text", "hello")]),
                 Message(1, "assistant", TS, [ContentBlock("text", "hi")])])
    real_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(TMP / "empty-bin")       # no `codex` on PATH: the migration step must not run here
    try:
        path, rep = conv.convert(s, "codex", TMP / "scratch", install=True)
        note = next((n for n in rep.notes if n.startswith("backed up")), "")
        check("backed up 1 store file(s)" in note and "--restore" in note,
              f"an install through convert() backs up the index first, and says how to restore ({note[:60]}…)")
        backups = sorted((TMP / "cfg" / "backups").glob("*install-into-codex*"))
        manifest = json.loads((backups[-1] / "manifest.json").read_text(encoding="utf-8")) if backups else {}
        check(any(f["from"].endswith("session_index.jsonl") for f in manifest.get("files", [])),
              "the backup folder holds the store's own index")
        n_before = len(backups)
        conv.convert(s, "codex", TMP / "scratch2", install=True, backup=False)
        check(len(sorted((TMP / "cfg" / "backups").glob("*install-into-codex*"))) == n_before,
              "backup=False takes none")
    finally:
        os.environ["PATH"] = real_path
        os.environ["CODEX_HOME"] = str(TMP / "nothing-here")


def test_claude_title_records_are_read() -> None:
    """Claude Code keeps the title as a `custom-title` record in the transcript (older versions
    wrote only that) and an `ai-title` record; the adapter read only the sidecar file."""
    sid_custom, sid_ai, sid_side = SID, SID2, "0a0a0a0a-1111-2222-3333-444444444444"
    ai = {"type": "ai-title", "sessionId": sid_custom, "aiTitle": "AI: files counted"}
    custom = {"type": "custom-title", "sessionId": sid_custom, "customTitle": "My renamed session"}
    older = {"type": "custom-title", "sessionId": sid_custom, "customTitle": "An earlier name"}
    home = claude_store("s-titles", {
        sid_custom: [ai] + claude_records(sid_custom, extra=[older, custom]),        # last record wins
        sid_ai: claude_records(sid_ai, extra=[{"type": "ai-title", "sessionId": sid_ai, "aiTitle": "Only the AI title"}]),
        sid_side: claude_records(sid_side, extra=[{"type": "custom-title", "sessionId": sid_side, "customTitle": "record name"}]),
    })
    side = home / "-tmp-proj" / sid_side
    side.mkdir(parents=True, exist_ok=True)
    (side / "custom-title.json").write_text(json.dumps({"customTitle": "sidecar name"}), encoding="utf-8")
    (home / "-tmp-proj" / "sessions-index.json").write_text(json.dumps({"version": 1, "entries": [
        {"sessionId": sid_ai, "firstPrompt": "count the files", "summary": "idx summary", "projectPath": "/tmp/from-index"}]}), encoding="utf-8")
    os.environ["ECTYPE_CLAUDE_HOME"] = str(home)
    ad = adapters.get("claude-code")
    titles = {r.id: r.title for r in ad.discover()}
    check(titles.get(sid_custom) == "My renamed session", f"list: the LAST custom-title record wins over the AI title ({titles.get(sid_custom)!r})")
    check(titles.get(sid_ai) == "Only the AI title", f"list: the ai-title record wins over the index and the first prompt ({titles.get(sid_ai)!r})")
    check(titles.get(sid_side) == "sidecar name", f"list: the sidecar file wins over the record ({titles.get(sid_side)!r})")
    loaded = {r.id: adapters.load(r).title for r in ad.discover()}
    check(loaded == titles, f"load() agrees with the list on every title ({loaded == titles})")
    check("/tmp/from-index" in ad.workspaces(), f"workspaces() takes projectPath from the agent's own index ({ad.workspaces()})")


# ---------------------------------------------------------------------------- the smaller ones
def test_keywords_accept_unicode_words() -> None:
    s = session([Message(0, "user", TS, [ContentBlock("text", "SillyTavern kartlarını düzenle ve için etiketleri temizle")]),
                 Message(1, "assistant", TS, [ContentBlock("text", "tamam")])])
    kw = summarize(s).splitlines()[-1]
    check("kartlarını" in kw and "düzenle" in kw and "etiketleri" in kw, f"Turkish words reach the keyword line ({kw})")
    check(" için " not in f" {kw} ", "a Turkish function word is a stop word")


def test_notice_is_part_of_the_ceiling() -> None:
    """F33: on a session smaller than the import notice, selected exceeded max context."""
    s = session([Message(0, "user", TS, [ContentBlock("text", "hi")]),
                 Message(1, "assistant", TS, [ContentBlock("text", "hello")])])
    opts = {**budget.ALL_OPTS, "collapse": True}
    plain = budget.breakdown(s, opts)
    shown = notice.attach(budget.filtered(s, opts), {}, "Claude Code")
    with_notice = budget.breakdown(s, opts, shown=shown)
    check(with_notice["selected"] == with_notice["everything"],
          f"with everything on and the notice appended, selected equals max context exactly ({with_notice['selected']} == {with_notice['everything']})")
    notice_row = next(p["tokens"] for p in with_notice["parts"] if p["key"] == "notice")
    check(with_notice["everything"] - plain["everything"] >= notice_row > 0,
          f"the ceiling grows by the notice as rendered, at least its {notice_row} block tokens")
    check(plain["everything"] == budget.everything(s, collapse=True)["tokens"], "without a notice the cached ceiling is unchanged")
    capped = budget.breakdown(s, {**opts, "mode": "custom", "cap": 5}, shown=notice.attach(budget.filtered(s, {**opts, "mode": "custom", "cap": 5}), {}, "Claude Code"))
    check(capped["selected"] <= capped["everything"], f"a capped view with the notice still sits under the ceiling ({capped['selected']} <= {capped['everything']})")


def test_mint_timeout_is_reported_not_raised() -> None:
    real_run = agentcli._run
    agentcli._run = lambda args, cwd: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, agentcli.TIMEOUT))
    real_cli = agentcli._cli
    agentcli._cli = lambda target: {"bin": "fake", "mint": ["ok"]}
    try:
        try:
            agentcli.mint_template("claude-code")
            check(False, "a hung agent is reported")
        except agentcli.AgentUnavailable as e:
            check(isinstance(e, agentcli.AgentTimedOut) and "did not finish" in str(e), f"a hung agent is reported as a timeout ({e})")
    finally:
        agentcli._run, agentcli._cli = real_run, real_cli


def test_convert_flags_are_validated_before_anything_is_written() -> None:
    os.environ["ECTYPE_CLAUDE_HOME"] = str(claude_store("s-flags", {SID: claude_records(SID)}))
    cwd = os.getcwd()
    work = TMP / "flagwork"
    work.mkdir(exist_ok=True)
    os.chdir(work)
    try:
        _, err, code = run_cli(["convert", SID[:8], "--verify"])
        check(code != 0 and "--verify needs --install" in err and not (work / "ectype-converted").exists(),
              "--verify without --install is refused before a file is written")
        _, err, code = run_cli(["convert", SID[:8], "--mint-template"])
        check(code != 0 and "cross-agent" in err and not (work / "ectype-converted").exists(),
              "--mint-template on a same-agent copy is refused before a file is written")
    finally:
        os.chdir(cwd)


def test_gui_says_when_the_port_is_busy() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        _, err, code = run_cli(["gui", "--port", str(port), "--no-open"])
        check(code != 0 and "already in use" in err and str(port) in err, f"a busy port is one sentence, not a traceback ({err.strip()[:70]})")
    finally:
        sock.close()


# ---------------------------------------------------------------------------- the round's features
def test_settings_gui_and_skill_sections() -> None:
    d = settings.load()
    # the skill section holds every view option since 2026-09-27, and starts with the conversation alone
    check(d["gui"]["port"] == 8765 and d["skill"] == {"command": "ectype", "mode": "brief", "cap": 150, "thinking": False, "tools": False,
                                                       "env": False, "notices": False, "questions": True, "peers": True, "collapse": True,
                                                       "wrap": "", "stamps": "full", "markers": True, "tool_headers": False, "hide_roles": [],
                                                       "redact": False, "notice": False, "summarize": True}
          and d["backup"] == {"keep_last": 0, "keep_days": 0},
          f"gui, skill and backup defaults are present ({d['skill']})")
    for bad, why in (({"gui": {"port": 70000}}, "port above 65535"), ({"gui": {"port": 0}}, "port 0"),
                     ({"skill": {"mode": "loud"}}, "unknown skill mode"), ({"skill": {"summarize": "yes"}}, "non-boolean summarize")):
        try:
            settings.validate(bad)
            check(False, f"{why} is rejected")
        except ValueError as e:
            check(True, f"{why} is rejected ({e})")
    saved = settings.save({"gui": {"port": 9000}, "skill": {"mode": "full", "cap": 0, "thinking": True, "summarize": False}})
    check(saved["gui"]["port"] == 9000 and saved["skill"]["mode"] == "full" and saved["skill"]["summarize"] is False,
          "both sections round-trip through save")
    settings.save({})


def test_ledger_update_and_search() -> None:
    home = claude_store("s-ledger", {SID: claude_records(SID, "count the files"),
                                     SID2: claude_records(SID2, "rename the tavern cards")})
    os.environ["ECTYPE_CLAUDE_HOME"] = str(home)
    st = ledger.update()
    check(st["seen"] == 2 and st["summarised"] == 2 and not st["failed"], f"first run summarises every session ({st})")
    st = ledger.update()
    check(st["summarised"] == 0 and st["unchanged"] == 2, f"a second run touches nothing ({st})")
    p = home / "-tmp-proj" / f"{SID}.jsonl"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u9", "parentUuid": "a2", "sessionId": SID, "cwd": "/tmp/proj",
                             "timestamp": "2026-09-10T10:00:09.000Z", "message": {"role": "user", "content": "also grep for widgets"}}) + "\n")
    st = ledger.update()
    check(st["summarised"] == 1 and st["unchanged"] == 1, f"a changed file is re-summarised, the other is not ({st})")
    res = ledger.search("tavern cards")
    check(len(res["matches"]) == 1 and res["matches"][0]["id"] == SID2 and res["never"] == 0,
          f"search finds the session by its ask and reports full coverage ({len(res['matches'])} match, {res['never']} never)")
    res = ledger.search("widgets")
    check(len(res["matches"]) == 1 and res["matches"][0]["id"] == SID, "the re-summarised session carries its new ask")
    (home / "-tmp-proj" / "0b0b0b0b-1111-2222-3333-444444444444.jsonl").write_text(
        "\n".join(json.dumps(r) for r in claude_records("0b0b0b0b-1111-2222-3333-444444444444", "never summarised")) + "\n", encoding="utf-8")
    res = ledger.search("zzz-nothing", refresh=False)
    text = ledger.render_search(res, "zzz-nothing")
    check(not res["matches"] and res["never"] == 1 and "1 without a summary" in text,
          f"without the refresh, an empty result says how many sessions have no summary yet ({text.strip()[:80]})")
    res = ledger.search("zzz-nothing")
    text = ledger.render_search(res, "zzz-nothing")
    check(not res["matches"] and res["never"] == 0 and res["refreshed"] == 1 and "summarised just now" in text,
          f"a plain search summarises the new session first, and says so (2026-09-27; {text.strip()[:110]})")
    try:
        ledger.search("(unclosed")
        check(False, "a bad pattern is an error with the pattern in it")
    except re.error as e:
        check("(unclosed" in str(e), f"a bad pattern is an error with the pattern in it ({e})")
    out, _, code = run_cli(["search", "tavern"])
    check(code == 0 and SID2[:8] in out and "summarised" in out, "ectype search prints the match and the coverage line")
    _, _, code = run_cli(["search", "zzz-nothing"])
    check(code == 1, "ectype search exits 1 when nothing matches, like grep")
    st = ledger.update(force=True)
    check(st["summarised"] == 3, f"--force re-summarises everything ({st['summarised']})")
    settings.save({"agents": {"claude-code": {"enabled": False}}})
    st = ledger.update()
    check(st["seen"] == 0, f"an agent switched off in Settings is not summarised ({st['seen']} seen)")
    st = ledger.update(["claude-code"])
    check(st["seen"] == 3, f"naming the agent explicitly takes it anyway ({st['seen']} seen)")
    settings.save({})


def test_recall_uses_skill_defaults_and_saves_a_summary() -> None:
    home = claude_store("s-recall", {SID: claude_records(SID, "recall me please")})
    os.environ["ECTYPE_CLAUDE_HOME"] = str(home)
    settings.save({})
    out, err, code = run_cli(["recall", SID[:8]])
    # since 2026-09-27 (the user: "only import assistant and user chats, don't even include basic tool names")
    check(code == 0 and "● Bash" not in out and "xxxx" not in out and "recall me please" in out,
          "recall starts with the conversation alone, no tool name")
    check("summary saved" in err and ledger.summary_path("claude-code", SID).is_file(), "recall saves the summary by default and says where")
    res = ledger.search("recall me")
    check(len(res["matches"]) == 1, "the saved summary is searchable at once")
    out, _, _ = run_cli(["recall", SID[:8], "--tools", "--no-summarize"])
    check("● Bash" in out and "xxxx" not in out, "with --tools, the skill's default mode: names only")
    out, _, _ = run_cli(["recall", SID[:8], "--tools", "--mode", "full", "--no-summarize"])
    check("xxxx" in out, "options after the id win over the skill defaults")
    out, _, _ = run_cli(["recall", SID[:8], "--no-tools", "--no-summarize"])
    check("● Bash" not in out and "twelve files" in out, "--no-tools is the conversation alone")
    settings.save({"skill": {"tools": False}})
    out, _, _ = run_cli(["recall", SID[:8], "--no-summarize"])
    check("● Bash" not in out and "twelve files" in out, "Settings → Agent skill → tools off is honoured")
    settings.save({})
    settings.save({"skill": {"tools": True, "mode": "full", "summarize": False}})
    ledger.summary_path("claude-code", SID).unlink()
    out, err, _ = run_cli(["recall", SID[:8]])
    check("xxxx" in out and "summary saved" not in err and not ledger.summary_path("claude-code", SID).is_file(),
          "Settings → Agent skill changes the default mode and switches the summary off")
    settings.save({})
    out, _, _ = run_cli(["summarize", SID[:8], "--save"])
    check("saved" in out and ledger.summary_path("claude-code", SID).is_file(), "summarize --save keeps one session's summary")


def test_mcp_search_tool_is_read_only_and_listed() -> None:
    names = [t["name"] for t in mcp._tools(False)]
    check("search_sessions" in names and "install_session" not in names, f"search_sessions is offered, the write tool is not ({names})")
    os.environ["ECTYPE_CLAUDE_HOME"] = str(claude_store("s-mcpsearch", {SID: claude_records(SID, "find the purple widget")}))
    ref = adapters.get("claude-code").discover()[0]
    ledger.record(ref, adapters.load(ref))
    text = mcp._call("search_sessions", {"pattern": "purple widget"}, False)
    check(SID[:8] in text and "summarised" in text, "the MCP search finds a saved summary and reports coverage")
    try:
        mcp._call("search_sessions", {}, False)
        check(False, "a search without a pattern is refused")
    except ValueError as e:
        check("pattern" in str(e), f"a search without a pattern is refused ({e})")


def test_adapter_plugins_load_from_entry_points() -> None:
    """FUTURE 2.2: a separate package registers an adapter under the `ectype.adapters` entry-point
    group and ectype lists it; a broken, a non-adapter and a duplicate one are reported and skipped."""
    import importlib
    import shutil
    site = TMP / "plugin-site"
    dist = site / "ectype_adapter_example-0.0.1.dist-info"          # what pip would install: metadata + the module
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: ectype-adapter-example\nVersion: 0.0.1\n", encoding="utf-8")
    (dist / "entry_points.txt").write_text("[ectype.adapters]\nexample = ectype_adapter_example:ExampleAdapter\n", encoding="utf-8")
    shutil.copy(ROOT / "integrations" / "adapter-plugin" / "ectype_adapter_example.py", site / "ectype_adapter_example.py")
    store = TMP / "example-store"
    store.mkdir(exist_ok=True)
    (store / "first.chat.jsonl").write_text('{"role":"user","text":"hello plug-in","ts":"2026-01-02T03:04:05Z"}\n'
                                            '{"role":"assistant","text":"hi there","ts":"2026-01-02T03:04:06Z"}\n', encoding="utf-8")
    os.environ["ECTYPE_EXAMPLE_HOME"] = str(store)
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    try:
        from importlib.metadata import entry_points
        found = [ep.name for ep in entry_points(group=adapters.ENTRY_POINT_GROUP)]
        check("example" in found, f"a dist-info on sys.path is found under the group ({found})")
        skipped = adapters.load_plugins()
        check("example" in adapters.ADAPTERS and adapters.PLUGINS.get("example") == "ectype_adapter_example:ExampleAdapter" and not skipped,
              f"the example plug-in is registered from its entry point ({adapters.PLUGINS.get('example')})")
        refs = adapters.get("example").discover()
        check(len(refs) == 1 and refs[0].title == "hello plug-in", "its discover() lists the session with a title")
        out, _, code = run_cli(["show", "first", "-a", "example", "--mode", "full"])
        check(code == 0 and "hello plug-in" in out and "hi there" in out, "ectype show renders a plug-in's session")
        out, _, _ = run_cli(["agents"])
        check("[plug-in ectype_adapter_example:ExampleAdapter]" in out, "ectype agents says which adapter is a plug-in")

        class EP:                                                    # the shape importlib.metadata gives: name, value, load()
            def __init__(self, name, value, obj=None, exc=None):
                self.name, self.value, self._obj, self._exc = name, value, obj, exc

            def load(self):
                if self._exc:
                    raise self._exc
                return self._obj
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            skipped = adapters.load_plugins([EP("broken", "nowhere:Nothing", exc=ImportError("no module named nowhere")),
                                             EP("notan", "x:y", obj=object),
                                             EP("dup", "x:z", obj=type(adapters.get("claude-code")))])
        reasons = dict(skipped)
        check(set(reasons) == {"broken", "notan", "dup"} and "could not be loaded" in reasons["broken"]
              and "not an ectype Adapter" in reasons["notan"] and "already taken" in reasons["dup"],
              f"a broken, a non-adapter and a duplicate plug-in are each skipped with the reason ({reasons})")
        check(err.getvalue().count("skipped") == 3 and isinstance(adapters.get("claude-code"), type(adapters.ADAPTERS["claude-code"])),
              "each is reported on stderr and the built-ins are untouched")
    finally:
        sys.path.remove(str(site))
        adapters.ADAPTERS.pop("example", None)
        adapters.PLUGINS.pop("example", None)
        os.environ["ECTYPE_EXAMPLE_HOME"] = str(TMP / "nothing-here")


def test_agent_notices_are_an_option() -> None:
    """Idea 3: Claude Code's own `system` records (an API error, a refusal, an away summary) load
    as agent notices; off by default in every view, on with --notices; summarize lists the errors
    regardless; a turn duration is not a notice."""
    recs = claude_records(SID, "why did it stall") + [
        {"type": "system", "subtype": "api_error", "content": "API Error: 529 overloaded", "timestamp": "2026-09-10T10:00:04.000Z", "uuid": "s1", "sessionId": SID, "isMeta": True},
        {"type": "system", "subtype": "away_summary", "content": "While you were away: the files were counted", "timestamp": "2026-09-10T10:00:05.000Z", "uuid": "s2", "sessionId": SID},
        {"type": "system", "subtype": "turn_duration", "content": "12", "timestamp": "2026-09-10T10:00:06.000Z", "uuid": "s3", "sessionId": SID},
    ]
    os.environ["ECTYPE_CLAUDE_HOME"] = str(claude_store("s-notices", {SID: recs}))
    s = adapters.load(adapters.get("claude-code").discover()[0])
    notes = [m for m in s.messages if m.meta.get("agent_notice")]
    check([m.meta["agent_notice"] for m in notes] == ["api_error", "away_summary"]
          and notes[0].blocks[0].kind == "error" and notes[1].blocks[0].kind == "info" and notes[0].role == "system",
          "an API error loads as an error notice, an away summary as info, a turn duration not at all")
    off, _, _ = run_cli(["show", SID[:8], "--mode", "full"])
    on, _, _ = run_cli(["show", SID[:8], "--mode", "full", "--notices"])
    check("529 overloaded" not in off and "529 overloaded" in on and "⚑ error" in on and "While you were away" in on,
          "notices are off by default and --notices shows them")
    summ, _, _ = run_cli(["summarize", SID[:8]])
    check("[api_error] API Error: 529 overloaded" in summ, "summarize lists the API error under errors without any flag")
    everything = {"mode": "full", "cap": 0, "thinking": True, "tools": True, "env": True, "notices": True, "collapse": True}
    b = budget.breakdown(s, everything)
    check(b["selected"] == b["everything"], f"with everything on, notices included, selected equals max context ({b['selected']} == {b['everything']})")
    b2 = budget.breakdown(s, {**everything, "notices": False})
    check(b2["selected"] < b2["everything"], "with notices off the view is under the ceiling, which still holds them")
    check(settings.load()["view"]["notices"] is False, "the Settings default is off")
    text = mcp._call("show_session", {"id": SID[:8], "mode": "full", "notices": True}, False)
    check("529 overloaded" in text, "the MCP show_session takes a notices flag")


def test_cline_writer_writes_the_store_shape_and_registers_the_index_row() -> None:
    """FUTURE 3.1: a Cline CLI session is two files plus a row in `db/sessions.db`; the writer
    produces the two files alone without install and all three with, backing the index up first."""
    import sqlite3
    s = session([Message(0, "user", TS, [ContentBlock("text", "count the widgets")]),
                 Message(1, "assistant", TS, [ContentBlock("text", "three widgets")])])
    path, rep = conv.convert(s, "cline", TMP / "out-cline", backup=False)
    d = path.parent
    meta = json.loads(path.read_text(encoding="utf-8"))
    msgs = json.loads((d / f"{rep.new_id}.messages.json").read_text(encoding="utf-8"))
    check(path.name == f"{rep.new_id}.json" and d.name == rep.new_id and d.parent == TMP / "out-cline",
          f"without install the files land under <out>/<id>/, the store's own layout ({d.parent.name}/{d.name})")
    check(meta["session_id"] == rep.new_id and meta["status"] == "completed" and meta["messages_path"].endswith(f"{rep.new_id}.messages.json"),
          "the metadata file carries the id, a completed status and the messages path")
    roles = [m["role"] for m in msgs["messages"]]
    user_text = msgs["messages"][1]["content"][0]["text"]
    asst = msgs["messages"][-1]
    check(msgs["sessionId"] == rep.new_id and msgs["origin"]["sessionId"] == rep.new_id and roles == ["user", "user", "assistant"],
          f"the messages file carries the id twice and the turns in order (banner, ask, reply: {roles})")
    check(user_text == '<user_input mode="act">count the widgets</user_input>' and asst["content"][0].get("thinking") == "" and asst["modelInfo"],
          "user text is wrapped the way the CLI writes it; an assistant block carries the empty thinking key and modelInfo")
    # install into a fabricated store: sessions/ beside db/sessions.db with the CLI's schema
    home = TMP / "cline-live" / "sessions"
    home.mkdir(parents=True, exist_ok=True)
    db = TMP / "cline-live" / "db" / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table sessions (session_id TEXT PRIMARY KEY, source TEXT NOT NULL, pid INTEGER NOT NULL, started_at TEXT NOT NULL, "
                "ended_at TEXT, exit_code INTEGER, status TEXT NOT NULL, status_lock INTEGER NOT NULL DEFAULT 0, interactive INTEGER NOT NULL, "
                "provider TEXT NOT NULL, model TEXT NOT NULL, cwd TEXT, workspace_root TEXT, team_name TEXT, enable_tools INTEGER, "
                "enable_spawn INTEGER, enable_teams INTEGER, parent_session_id TEXT, parent_agent_id TEXT, agent_id TEXT, conversation_id TEXT, "
                "is_subagent INTEGER, prompt TEXT, metadata_json TEXT, transcript_path TEXT, hook_path TEXT, messages_path TEXT, updated_at TEXT)")
    con.commit()
    con.close()
    tmpl = TMP / "cline-live" / "tmpl.json"
    tmpl.write_text(json.dumps({"version": 1, "provider": "probe-provider", "model": "probe/model", "team_name": "team-x",
                                "metadata": {"sessionHistoryOrigin": {"mode": "user", "version": "3.0.62"}}}), encoding="utf-8")
    tmpl.with_name("tmpl.messages.json").write_text(json.dumps({"version": 1, "agent": "lead", "system_prompt": "SYS PROMPT"}), encoding="utf-8")
    os.environ["ECTYPE_CLINE_HOME"] = str(home)
    try:
        path, rep = conv.convert(s, "cline", TMP / "scratch-cline", install=True, template=tmpl)
        check(path.parent.parent == home, f"install writes under the store ({path.parent.parent})")
        con = sqlite3.connect(db)
        row = con.execute("select status, prompt, provider, model, messages_path, source from sessions where session_id=?", (rep.new_id,)).fetchone()
        con.close()
        check(row is not None and row[0] == "completed" and row[1] == "count the widgets" and row[2] == "probe-provider" and row[3] == "probe/model"
              and row[4] == str(path.with_name(f"{rep.new_id}.messages.json")) and row[5] == "cli",
              f"the index row is inserted with the template's provider and model and the file's path ({row})")
        msgs = json.loads(path.with_name(f"{rep.new_id}.messages.json").read_text(encoding="utf-8"))
        check(msgs["system_prompt"] == "SYS PROMPT" and msgs["origin"]["version"] == "3.0.62", "the messages envelope comes from the template")
        note = next((n for n in rep.notes if n.startswith("backed up")), "")
        check("backed up 1 store file(s)" in note and "sessions.db" in json.dumps(rep.notes), f"the index was backed up before the insert ({note[:50]}…)")
        check(any("cline --id " + rep.new_id in n for n in rep.notes), "the report says how to resume it")
    finally:
        os.environ["ECTYPE_CLINE_HOME"] = str(TMP / "nothing-here")


def test_skill_command_name_install_and_remove() -> None:
    """The slash command's name is the file name in each agent's command folder; `ectype skill
    install` writes it under any name, marks its files, never overwrites a user's own, and the
    repository's integrations/ copies equal the templates rendered for /ectype."""
    from ectype import skill
    fake_home = TMP / "home"
    fake_home.mkdir(exist_ok=True)
    # Path.home() follows HOME on POSIX and USERPROFILE on Windows, and the agents' own folder
    # variables would send the files elsewhere: all four redirected, so no platform writes into a
    # real command folder (on Windows this test used to leave `mine.md` in the real one).
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(fake_home)
    os.environ.pop("CODEX_HOME", None)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    try:
        for bad in ("", "1abc", "no space", "a/b", "x" * 65):
            try:
                skill.valid_name(bad)
                check(False, f"{bad!r} is rejected as a command name")
            except ValueError:
                check(True, f"{bad!r} is rejected as a command name")
        for agent, rel in (("claude-code", "claude-code/ectype.md"), ("gemini-cli", "gemini-cli/ectype.toml"), ("codex", "codex/ectype.md")):
            shipped = (ROOT / "integrations" / rel).read_text(encoding="utf-8")
            check(shipped == skill.render(agent, "ectype", stamp=False),
                  f"integrations/{rel} equals the template rendered for /ectype, without the version stamp "
                  f"(regenerate it with skill.render(agent, 'ectype', stamp=False) after a template change)")
        written = skill.install("bringmedasummary")
        names = [p.name for p in written]
        check(names == ["bringmedasummary.md", "bringmedasummary.toml", "bringmedasummary.md"] and all(str(p).startswith(str(fake_home)) for p in written),
              f"install writes the three files under the chosen name in the agents' command folders ({[str(p.relative_to(fake_home)) for p in written]})")
        st = skill.status("bringmedasummary")
        check(all(s["installed"] and s["ours"] for s in st) and st[0]["use"].startswith("/bringmedasummary"), "status sees them as installed and ours")
        text = written[1].read_text(encoding="utf-8")
        check("/bringmedasummary" in text and "__NAME__" not in text and skill.MARK in text, "a rendered file names the command and carries the marker")
        own = skill.path("claude-code", "mine")
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("my own command\n", encoding="utf-8")
        try:
            skill.install("mine", ["claude-code"])
            check(False, "a user's own command file is never overwritten")
        except FileExistsError:
            check(True, "a user's own command file is never overwritten")
        removed, kept = skill.remove("mine")
        check(removed == [] and kept == [own], "remove keeps a file ectype did not write")
        removed, _ = skill.remove("bringmedasummary")
        check(len(removed) == 3 and not skill.status("bringmedasummary")[0]["installed"], "remove deletes the three it wrote")
        out, err, code = run_cli(["skill", "install", "--name", "recallit"])
        check(code == 0 and settings.load()["skill"]["command"] == "recallit" and skill.status("recallit")[0]["installed"],
              f"ectype skill install --name writes the files and remembers the name in Settings ({err.strip()[:60]})")
        out, _, _ = run_cli(["skill", "status"])
        check("/recallit" in out and "installed" in out, "ectype skill status reports the configured name")
        out, _, _ = run_cli(["skill", "print", "-a", "gemini-cli"])
        check(out.startswith("# generated by ectype skill install") and "/recallit" in out, "ectype skill print shows the text for one agent")
        run_cli(["skill", "remove"])
        try:
            settings.validate({"skill": {"command": "bad name"}})
            check(False, "Settings rejects a bad command name")
        except ValueError:
            check(True, "Settings rejects a bad command name")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        settings.save({})


def test_backup_retention_is_a_setting() -> None:
    """Backups are kept forever by default; Settings → Backups sets a count or an age, applied on
    every new backup and by `ectype backup --prune`; the newest folder never goes by age alone."""
    import shutil
    from datetime import datetime, timedelta
    from ectype import backup as bk
    shutil.rmtree(bk.root(), ignore_errors=True)                   # earlier tests' install backups
    src = TMP / "bk-src.txt"
    src.write_text("x", encoding="utf-8")
    settings.save({})
    made = [bk.save([src], f"why-{i}") for i in range(4)]
    rows = bk.listing()
    check(len(rows) == 4 and all(r.get("bytes", 0) > 0 for r in rows), f"backups are listed with their size ({len(rows)})")
    check(bk.prune() == [] and len(bk.listing()) == 4, "with both limits at 0 nothing is ever deleted")
    settings.save({"backup": {"keep_last": 2}})
    removed = bk.prune()
    left = sorted(d.name for d in bk.root().iterdir() if d.is_dir())
    check(len(removed) == 2 and len(left) == 2 and left == sorted(f.name for f in made[2:]), f"keep_last 2 removes the two oldest ({[d.name[:15] for d in removed]})")
    settings.save({"backup": {"keep_days": 5}})
    old_folder = bk.root() / left[0]
    m = old_folder / bk.MANIFEST
    data = json.loads(m.read_text(encoding="utf-8"))
    data["created"] = (datetime.now().astimezone() - timedelta(days=10)).isoformat(timespec="seconds")
    m.write_text(json.dumps(data), encoding="utf-8")
    removed = bk.prune()
    check([d.name for d in removed] == [left[0]] and len(bk.listing()) == 1, "keep_days removes a folder older than the limit")
    newest = bk.root() / left[1]
    m = newest / bk.MANIFEST
    data = json.loads(m.read_text(encoding="utf-8"))
    data["created"] = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
    m.write_text(json.dumps(data), encoding="utf-8")
    check(bk.prune() == [] and newest.is_dir(), "the newest folder is never removed by the age rule alone")
    settings.save({"backup": {"keep_last": 1}})
    bk.save([src], "why-new")
    check(len(bk.listing()) == 1 and not newest.is_dir(), "a new backup applies the retention by itself")
    out, _, code = run_cli(["backup", "--prune"])
    check(code == 0 and "retention: keep_last 1" in out, "ectype backup --prune reports the rule it applied")
    out, _, _ = run_cli(["backup", "--list"])
    check("size" in out and "1 backup(s)" in out, "ectype backup --list shows sizes and a total")
    settings.save({})


def test_version_lives_in_one_place() -> None:
    """Idea 7: pyproject reads the version from ectype/__init__.py; the CLI prints that one."""
    from ectype import __version__
    t = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    check('dynamic = ["version"]' in t and 'version = {attr = "ectype.__version__"}' in t and '\nversion = "' not in t,
          "pyproject.toml carries no version literal and points at ectype.__version__")
    out, _, code = run_cli(["--version"])
    check(code == 0 and out.strip() == f"ectype {__version__}", f"ectype --version prints the package's one version ({out.strip()})")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else "all report checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
