"""Checks for the closing summary, 2026-09-27: its marker, how many blocks are quoted, how search finds
it without anyone summarising first, and the rule that asks an agent to write one.

    python3 tests/test_summary_rule.py

The default marker renders as written in Markdown (the old one lost its asterisks to italics), every
closing summary in a session is quoted in order, `search` brings the store up to date before it
looks (a session whose file did not change is not read again), and `ectype summary-rule` writes the
one paragraph that asks an agent for a closing summary into the instruction files the user picks,
inside a block it owns and nowhere else. Every store and every instruction file is synthetic, under
a temp dir. Written before the code, so each check went red first.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ectype-summary-rule-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude-config")
os.environ["CODEX_HOME"] = str(TMP / "codex-home")
os.environ["GEMINI_CLI_HOME"] = str(TMP / "gemini-home")
NOTHING = TMP / "nothing-here"
for _v in ("ECTYPE_CLAUDE_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME", "ECTYPE_AIDER_HOME",
           "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME", "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(NOTHING)

from ectype import ledger, settings, summary  # noqa: E402
from ectype.model import ContentBlock, Message, Session  # noqa: E402
from ectype.web import make_server  # noqa: E402

FAILS: list[str] = []
NEW = "%%SUMMARY%%"
OLD = "-*-summary-*-"
STORE = TMP / "claude"
SID = "ffff1111-0000-4000-8000-000000000027"


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "ectype", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", env=os.environ.copy(), timeout=120)


def _write_session(text: str, extra: str | None = None) -> Path:
    """A Claude Code session whose assistant turn is `text`; `extra` appends a second exchange."""
    proj = STORE / "-srv-work-notes"
    proj.mkdir(parents=True, exist_ok=True)
    base = {"cwd": "/srv/work/notes", "isSidechain": False, "version": "2.1.280", "sessionId": SID}
    recs = [{**base, "type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-09-27T08:00:00.000Z",
             "message": {"role": "user", "content": "probe: write the notes"}},
            {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-09-27T08:00:01.000Z",
             "message": {"role": "assistant", "model": "probe-model", "content": [{"type": "text", "text": text}]}}]
    if extra:
        recs += [{**base, "type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": "2026-09-27T08:05:00.000Z",
                  "message": {"role": "user", "content": "probe: one more thing"}},
                 {**base, "type": "assistant", "uuid": "a2", "parentUuid": "u2", "timestamp": "2026-09-27T08:05:01.000Z",
                  "message": {"role": "assistant", "model": "probe-model", "content": [{"type": "text", "text": extra}]}}]
    p = proj / f"{SID}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    os.environ["ECTYPE_CLAUDE_HOME"] = str(STORE)
    return p


# --------------------------------------------------------------------------- the marker and the blocks
def test_1_the_default_marker_renders_as_written() -> None:
    marks = settings.DEFAULTS["summary"]["markers"]
    check(marks == [NEW], f"the default marker is {NEW} ({marks})")
    special = set("*_`~=#<>[]|\\")
    check(not (set(marks[0]) & special) and not marks[0].startswith(("-", "+", " ")),
          "it holds no character Markdown gives a meaning to, so a rendered reply shows exactly what was written")
    s = Session("probe", "p", TMP / "p.jsonl", [Message(0, "assistant", None, [ContentBlock("text", f"done\n\n{NEW}\nALL OF IT\n{NEW}")])])
    check(summary.closing_summaries(s) == [(NEW, "ALL OF IT")], "and it is what a closing summary is found by, with no setting")
    _write_session(f"done\n\n{NEW}\nPLAIN FORM\n{NEW}")
    r = _cli("summarize", SID[:8], "--marker", NEW)
    check(r.returncode == 0 and "PLAIN FORM" in r.stdout, f"--marker takes it without the = form, since it starts with no dash (rc {r.returncode} {r.stderr.strip()[:120]})")


def test_2_every_closing_summary_is_quoted_in_order() -> None:
    s = Session("probe", "p", TMP / "p.jsonl", [
        Message(0, "user", None, [ContentBlock("text", "ask")]),
        Message(1, "assistant", None, [ContentBlock("text", f"part one done\n\n{NEW}\nFIRST HALF\n{NEW}")]),
        Message(2, "user", None, [ContentBlock("text", "go on")]),
        Message(3, "assistant", None, [ContentBlock("text", f"all done\n\n{NEW}\nSECOND HALF\nline two\n{NEW}")])])
    text = summary.summarize(s)
    check("FIRST HALF" in text and "SECOND HALF" in text, "both blocks are quoted, not only the last")
    check(text.find("FIRST HALF") < text.find("SECOND HALF"), "in the order they were written")
    check("closing summary 1 of 2" in text and "closing summary 2 of 2" in text, "each says which of how many it is")
    one = summary.summarize(Session("probe", "p", TMP / "p.jsonl", [Message(0, "assistant", None, [ContentBlock("text", f"{NEW}\nONLY\n{NEW}")])]))
    check("closing summary (the agent's own" in one and " of 1" not in one, "a single block keeps the plain heading")
    check(summary.FORMAT >= 4, f"the summary format moved on, so kept summaries are redone once ({summary.FORMAT})")


# --------------------------------------------------------------------------- search keeps the store current
def test_3_search_finds_a_session_nobody_summarised_and_redoes_only_changed_files() -> None:
    p = _write_session(f"notes written\n\n{NEW}\nZEPHYRWORD decided: keep the notes short\n{NEW}")
    for f in (ledger.ledger_path(),):
        f.unlink(missing_ok=True)
    r = _cli("search", "zephyrword")
    check(r.returncode == 0 and SID[:8] in r.stdout, f"search finds a session that was never summarised or recalled (rc {r.returncode} {r.stdout.strip()[-160:]!r})")
    key = next((k for k in ledger.load() if k.endswith(SID)), None)
    check(key is not None, "and the store now holds its summary")
    first = ledger.load()[key]["summarised"] if key else None
    time.sleep(1.1)
    _cli("search", "zephyrword")
    check(key is not None and ledger.load()[key]["summarised"] == first, "a second search does not summarise an unchanged file again")
    time.sleep(1.1)
    _write_session(f"notes written\n\n{NEW}\nZEPHYRWORD decided\n{NEW}", extra=f"added\n\n{NEW}\nQUASARWORD appended later\n{NEW}")
    r = _cli("search", "quasarword")
    check(r.returncode == 0 and SID[:8] in r.stdout, "a file that changed is summarised again before the search looks")
    from ectype import mcp
    p.touch()
    out = mcp._call("search_sessions", {"pattern": "quasarword"}, False)
    check(SID[:8] in out, "the MCP search does the same")


# --------------------------------------------------------------------------- the rule in instruction files
def _rule():
    try:
        from ectype import summary_rule
        return summary_rule
    except ImportError:
        check(False, "ectype.summary_rule exists")
        return None


def test_4_install_writes_one_block_and_touches_nothing_else() -> None:
    sr = _rule()
    if not sr:
        return
    f = TMP / "notes" / "AGENTS.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    mine = "# My rules\n\n- keep answers short\n- Turkish is fine: şğıöüç\n"
    f.write_text(mine, encoding="utf-8")
    check(sr.state(f) == "none", f"a file with no rule reads as none ({sr.state(f)})")
    from ectype import backup as bk
    before = len(bk.listing())
    sr.install([f])
    text = f.read_text(encoding="utf-8")
    check(text.startswith(mine), "the user's own text is untouched, byte for byte, at the top")
    check(NEW in text and text.count(NEW) >= 1 and "ectype" in text, "the block asks for the closing summary between the marker lines")
    check("\u2014" not in text, "and holds no em dash")
    check(sr.state(f) == "ours", f"then reads as ours ({sr.state(f)})")
    check(len(bk.listing()) == before + 1, "the file was backed up before it was changed")
    sr.install([f])
    check(f.read_text(encoding="utf-8") == text and len(bk.listing()) == before + 1, "installing again changes nothing and takes no second backup")
    settings.save({"summary": {"markers": ["@@END@@"]}})
    check(sr.state(f) == "stale", f"a change of markers makes the block stale ({sr.state(f)})")
    sr.install([f])
    check("@@END@@" in f.read_text(encoding="utf-8") and sr.state(f) == "ours", "and a plain install brings it up to date")
    settings.save({})
    sr.install([f])
    edited = f.read_text(encoding="utf-8").replace("closing summary", "closing summary (edited by hand)", 1)
    f.write_text(edited, encoding="utf-8")
    check(sr.state(f) == "edited", f"an edit inside the block reads as edited ({sr.state(f)})")
    try:
        sr.install([f])
        check(False, "an edited block is refused")
    except FileExistsError as e:
        check("edited" in str(e), f"an edited block is refused, and it says why ({e})")
    removed, kept = sr.remove([f])
    check(kept == [f] and removed == [], "remove keeps an edited block too")
    removed, kept = sr.remove([f], force=True)
    check(removed == [f] and f.read_text(encoding="utf-8") == mine, "remove with force takes the block out and leaves the file as it was")


def test_5_a_file_that_already_asks_for_it_is_left_alone() -> None:
    sr = _rule()
    if not sr:
        return
    f = TMP / "notes" / "CLAUDE.md"
    f.write_text(f"## Context\nWrite the summary between two lines of {NEW}.\n", encoding="utf-8")
    check(sr.state(f) == "hand-written", f"a file that names the marker in its own words reads as hand-written ({sr.state(f)})")
    try:
        sr.install([f])
        check(False, "install refuses to add a second rule to it")
    except FileExistsError as e:
        check("already" in str(e), f"install refuses to add a second rule to it ({e})")
    missing = TMP / "notes" / "new" / "GEMINI.md"
    sr.install([missing])
    check(missing.is_file() and sr.state(missing) == "ours", "a file that does not exist yet is created with the block alone")


def test_6_the_agents_own_files_and_the_command_line() -> None:
    sr = _rule()
    if not sr:
        return
    paths = {a: sr.path(a) for a in ("claude-code", "codex", "gemini-cli")}
    check(paths["claude-code"] == TMP / "claude-config" / "CLAUDE.md" and paths["codex"] == TMP / "codex-home" / "AGENTS.md"
          and paths["gemini-cli"] == TMP / "gemini-home" / ".gemini" / "GEMINI.md",
          f"each agent's global instruction file, honouring the agent's own config variable ({paths})")
    r = _cli("summary-rule", "install")
    check(r.returncode != 0 and "-a" in r.stderr, f"install with no target is refused: the user picks the files ({r.stderr.strip()[:120]})")
    r = _cli("summary-rule", "install", "-a", "codex")
    check(r.returncode == 0 and sr.state(paths["codex"]) == "ours", f"-a codex writes Codex's AGENTS.md ({r.stderr.strip()[:120]})")
    r = _cli("summary-rule", "status")
    check("Codex" in r.stdout and "installed" in r.stdout.lower(), f"status lists the three agents' files ({r.stdout.strip()[:200]!r})")
    r = _cli("summary-rule", "print")
    check(r.returncode == 0 and NEW in r.stdout, "print shows the exact paragraph, for pasting by hand")
    r = _cli("summary-rule", "remove", "-a", "codex")
    check(r.returncode == 0 and (not paths["codex"].exists() or sr.state(paths["codex"]) == "none"),
          f"remove takes it out again; a file that held only the block goes with it (rc {r.returncode} {r.stderr.strip()[:100]})")


def test_7_web_endpoint_and_page() -> None:
    sr = _rule()
    if not sr:
        return
    srv = make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw)

    st, r = call("GET", "/api/summary-rule")
    check(st == 200 and NEW in r.get("text", "") and {a["agent"] for a in r.get("agents", [])} == {"claude-code", "codex", "gemini-cli"},
          f"GET /api/summary-rule returns the paragraph and the three agents' files ({st})")
    st, r = call("POST", "/api/summary-rule", {"action": "install", "agents": ["gemini-cli"]})
    check(st == 200 and next(a for a in r["agents"] if a["agent"] == "gemini-cli")["state"] == "ours", "POST installs it for the agents picked")
    st, r = call("POST", "/api/summary-rule", {"action": "remove", "agents": ["gemini-cli"]})
    check(st == 200, "and removes it")
    st, r = call("POST", "/api/summary-rule", {"action": "install"})
    check(st >= 400, "a POST that picks nothing is refused")
    srv.shutdown()
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check('id="s_sumRule"' in page and 'id="s_sumRuleRows"' in page, "Settings -> Summary shows the paragraph and one row per agent")
    check("fetch('/api/summary-rule'" in page, "the page talks to the endpoint")
    check("summary-rule" in page.split('<dialog id="help">', 1)[1], "Help explains it")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
        except Exception as e:                  # noqa: BLE001, a crash is one failure, reported, and the rest still run
            check(False, f"{fn.__name__} raised {type(e).__name__}: {e}")
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all summary-rule checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
