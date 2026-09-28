"""Checks for the 2026-09-25 round: the closing summary caught from markers on every surface, the
Claude Code project label taken per session from the recorded path, and build-provenance
attestation in the release workflow.

    python3 tests/test_closing_summary.py

Every store here is synthetic and lives in a temp dir; no real session is read, the settings file
is a throwaway, and every other agent's store points at nothing. Each check names the behaviour it
guards, so a failure says what came back. Written before the code, so each went red first.

Amended 2026-09-27: every closing summary in a session is quoted, in order (the user: "if there are
multiple summaries, consider them both"), and the default marker is %%SUMMARY%%; the store below
still uses the older marker, set explicitly in Settings, so the checks keep testing what they did.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ectype-closing-summary-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
NOTHING = TMP / "nothing-here"
for _v in ("ECTYPE_CLAUDE_HOME", "CODEX_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME", "ECTYPE_AIDER_HOME",
           "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME", "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(NOTHING)
os.environ.pop("CLAUDE_CONFIG_DIR", None)

from ectype import adapters, ledger, settings, summary  # noqa: E402
from ectype.model import ContentBlock, Message, Session  # noqa: E402
from ectype.web import make_server  # noqa: E402

FAILS: list[str] = []
M = "-*-summary-*-"
SID = "cccc1111-0000-4000-8000-000000000001"
CLOSING = "PROBE CLOSING SUMMARY: forty widgets; next, label them."
STORE = TMP / "claude"


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def _session(*turns: tuple[str, str]) -> Session:
    msgs = [Message(i, role, None, [ContentBlock("text", text=text)]) for i, (role, text) in enumerate(turns)]
    return Session("probe", "probe-1", TMP / "probe.jsonl", msgs)


def _claude_store(home: Path, slug: str, sessions: dict[str, str], index: list[dict] | None = None) -> None:
    """A Claude Code project folder: one .jsonl per (session id -> the assistant's text), whose user
    turn carries a pasted summary between the same markers, and optionally the folder's index."""
    proj = home / slug
    proj.mkdir(parents=True, exist_ok=True)
    for sid, text in sessions.items():
        base = {"cwd": "/srv/work", "isSidechain": False, "version": "2.1.0", "sessionId": sid}
        recs = [
            {**base, "type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-09-25T08:00:00.000Z",
             "message": {"role": "user", "content": f"probe ask\n\n{M}\nPASTED FROM BEFORE, not this session's\n{M}"}},
            {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-09-25T08:00:01.000Z",
             "message": {"role": "assistant", "model": "probe-model", "content": [{"type": "text", "text": text}]}},
        ]
        (proj / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    if index is not None:
        (proj / "sessions-index.json").write_text(json.dumps({"entries": index}), encoding="utf-8")


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "ectype", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", env=os.environ.copy(), timeout=120)


# --------------------------------------------------------------------------- the closing summary
def test_1_closing_summary_quoted_first_every_block_user_turns_ignored() -> None:
    s = _session(("user", f"ask\n{M}\nPASTED must not count\n{M}"),
                 ("assistant", f"first\n{M}\nEARLY ONE\n{M}"),
                 ("assistant", f"done\n{M}\nTHE CLOSING ONE\nline two\n{M}\nunpaired {M} tail"))
    found = summary.closing_summaries(s, [M])
    check([t for _, t in found] == ["EARLY ONE", "THE CLOSING ONE\nline two"],
          f"the assistant's blocks in order; the pasted one and the unpaired tail ignored ({found})")
    text = summary.summarize(s, marks=[M])
    check("closing summary 1 of 2 (the agent's own, between -*-summary-*- markers):\n  EARLY ONE" in text
          and "closing summary 2 of 2 (the agent's own, between -*-summary-*- markers):\n  THE CLOSING ONE\n  line two" in text,
          "every block is quoted, indented, each saying which of how many it is")
    check(0 <= text.find("EARLY ONE") < text.find("THE CLOSING ONE"), "in the order they were written")
    check(text.index("closing summary") < text.index("asked:"), "they come before the extracted sections")
    check("PASTED" not in text, "the pasted block is not quoted")
    check("closing summary" not in summary.summarize(_session(("assistant", "nothing marked here")), marks=[M]),
          "no section when nothing is marked")


def test_2_markers_are_a_setting_several_allowed_empty_switches_off() -> None:
    saved = settings.save({"summary": {"markers": [" <<S>> ", "=== END ===", "<<S>>", "  "]}})
    check(saved["summary"]["markers"] == ["<<S>>", "=== END ==="],
          f"trimmed, deduplicated, blanks dropped, order kept ({saved['summary']['markers']})")
    s = _session(("assistant", "a <<S>> ONE <<S>> b === END === TWO === END ==="))
    found = summary.closing_summaries(s)                      # no override: Settings decide
    check([t for _, t in found] == ["ONE", "TWO"], f"every listed marker counts ({found})")
    text = summary.summarize(s)
    check("closing summary 2 of 2 (the agent's own, between === END === markers)" in text,
          "each section names the marker of the block it quotes")
    settings.save({"summary": {"markers": []}})
    check("closing summary" not in summary.summarize(s), "an empty list switches the section off")
    for bad in ({"summary": {"markers": "x"}}, {"summary": {"markers": [1]}}, {"summary": ["x"]}):
        try:
            settings.validate(bad)
            refused = False
        except ValueError:
            refused = True
        check(refused, f"{bad} is refused")
    check(settings.validate({})["summary"]["markers"] == ["%%SUMMARY%%"], "the default is %%SUMMARY%% (since 2026-09-27)")
    settings.save({})


def test_3_cli_marker_is_a_one_off_look() -> None:
    settings.save({"summary": {"markers": [M]}})              # tests 3 to 5 work with the older marker, set explicitly
    _claude_store(STORE, "-srv-work-probe", {SID: f"There are forty.\n\n{M}\n{CLOSING}\n{M}"})
    os.environ["ECTYPE_CLAUDE_HOME"] = str(STORE)             # the later tests use this store too
    r = _cli("summarize", SID[:8])
    check(r.returncode == 0 and CLOSING in r.stdout and "PASTED" not in r.stdout,
          f"summarize prints the closing summary, with the markers from Settings (rc {r.returncode} {r.stderr.strip()[:160]})")
    r = _cli("summarize", SID[:8], "--marker", "@@")
    check(r.returncode == 0 and "closing summary" not in r.stdout, "--marker replaces the markers for this one look")
    # the default marker starts with a dash, so argparse takes it only in the --marker=TEXT form
    r = _cli("summarize", SID[:8], f"--marker={M}", "--marker", "@@")
    check(r.returncode == 0 and CLOSING in r.stdout, f"--marker=TEXT carries a marker that starts with a dash (rc {r.returncode} {r.stderr.strip()[:120]})")
    check("--marker=TEXT" in _cli("summarize", "--help").stdout, "the help says so")
    r = _cli("summarize", SID[:8], f"--marker={M}", "--save")
    check(r.returncode != 0 and "Settings" in r.stderr, f"--marker with --save is refused, pointing at Settings ({r.stderr.strip()[:120]})")
    r = _cli("summarize", "--all", f"--marker={M}")
    check(r.returncode != 0 and "Settings" in r.stderr, "--marker with --all is refused")


def test_4_ledger_redoes_a_summary_when_format_or_markers_change() -> None:
    st = ledger.update(["claude-code"])
    check(st["summarised"] == 1 and st["failed"] == [], f"the store's one session is summarised ({st})")
    kept = ledger.summary_path("claude-code", SID).read_text(encoding="utf-8")
    check(CLOSING in kept and "PASTED" not in kept, "the kept summary carries the closing summary, so search finds it")
    st = ledger.update(["claude-code"])
    check(st["unchanged"] == 1 and st["summarised"] == 0, "a second run touches nothing")
    settings.save({"summary": {"markers": ["@@"]}})
    st = ledger.update(["claude-code"])
    check(st["summarised"] == 1, "a change of markers in Settings redoes it on the next run, without --force")
    check(CLOSING not in ledger.summary_path("claude-code", SID).read_text(encoding="utf-8"),
          "and the redone summary reflects the new markers")
    settings.save({"summary": {"markers": [M]}})
    ledger.update(["claude-code"])
    data = ledger.load()
    key = next(k for k in data if k.endswith(SID))
    data[key]["format"] = 1
    ledger.save(data)
    st = ledger.update(["claude-code"])
    check(st["summarised"] == 1, "an entry written in an older summary format is redone too")
    check(ledger.load()[key]["format"] == summary.FORMAT and ledger.load()[key]["markers"] == [M],
          "the entry records the format and the markers it was written with")


def test_5_web_summarize_endpoint_and_settings_round_trip() -> None:
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

    st, r = call("GET", f"/api/summarize?agent=claude-code&id={SID}")
    check(st == 200 and CLOSING in r.get("text", "") and r.get("closing") == 1 and r.get("markers") == [M],
          f"GET /api/summarize returns the summary, the block count and the markers ({st} {str(r)[:120] if st != 200 else ''})")
    st, r = call("POST", "/api/settings", {"settings": {"summary": {"markers": ["<<S>>", "@@"]}}})
    check(st == 200 and r["settings"]["summary"]["markers"] == ["<<S>>", "@@"], "POST /api/settings saves the markers")
    st, r = call("GET", f"/api/summarize?agent=claude-code&id={SID}")
    check(st == 200 and r.get("closing") == 0 and r.get("markers") == ["<<S>>", "@@"], "the endpoint reads the saved markers at once")
    st, r = call("GET", "/api/summarize?agent=claude-code&id=nope")
    check(st in (404, 500) and "error" in r, f"an unknown id is an error reply, not a crash ({st})")
    call("POST", "/api/settings", {"settings": {}})
    srv.shutdown()


def test_6_page_wires_the_summary_button_and_the_markers_box() -> None:
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check('id="sumBtn"' in page and "$('#sumBtn').onclick=" in page, "the Summary button exists and has a click handler")
    first_line = page[page.index("async function rerender("):].split("\n", 1)[0]
    check("summaryOff()" in first_line, "every re-render puts the button back to 'Summary'")
    check("fetch('/api/summarize?" in page, "the button calls the endpoint")
    check('id="s_sumMarkers"' in page, "Settings has the markers box")
    check("$('#s_sumMarkers').value=" in page, "the dialog loads the saved markers into it")
    check("summary:{markers:$('#s_sumMarkers').value" in page,
          "Save sends them back (POST /api/settings replaces everything, so a box left out of the body resets the setting)")
    check("<dt>Summary</dt>" in page, "Help explains it")


# --------------------------------------------------------------------------- the project label
def test_7_project_label_is_per_session_from_the_recorded_path() -> None:
    home = TMP / "claude-label"
    a, b, c, d = ("dddd1111-0000-4000-8000-00000000000" + x for x in "1234")
    _claude_store(home, "-srv-work-my-app", {a: "x", b: "x", c: "x", d: "x"},
                  index=[{"sessionId": a, "projectPath": "/srv/work/my-app"},
                         {"sessionId": c, "projectPath": "C:\\srv\\ai-tools"},
                         {"sessionId": d, "projectPath": ""}])
    old = os.environ["ECTYPE_CLAUDE_HOME"]
    os.environ["ECTYPE_CLAUDE_HOME"] = str(home)
    try:
        labels = {r.id: r.project for r in adapters.get("claude-code").discover()}
    finally:
        os.environ["ECTYPE_CLAUDE_HOME"] = old
    check(labels.get(a) == "my-app", f"the session's own recorded path wins over the slug ({labels.get(a)!r})")
    check(labels.get(b) == "app", f"without an index entry, the slug's last segment as before ({labels.get(b)!r})")
    check(labels.get(c) == "ai-tools", f"a path recorded on Windows is split on its own separator ({labels.get(c)!r})")
    check(labels.get(d) == "app", f"an empty recorded path falls back to the slug ({labels.get(d)!r})")


# --------------------------------------------------------------------------- the release workflow
def test_8_release_workflow_attests_both_attached_files() -> None:
    wf = (ROOT / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
    uses = re.findall(r"uses: actions/attest-build-provenance@([0-9a-f]+)\s*#\s*(v[\d.]+)", wf)
    check(len(uses) == 2 and all(len(sha) == 40 for sha, _ in uses),
          f"the attestation action is used twice, pinned by full commit SHA with its tag beside it ({uses})")

    def job(name: str) -> str:
        i = wf.index(f"\n  {name}:\n")
        m = re.search(r"\n  [a-z-]+:\n", wf[i + 1:])
        return wf[i:i + 1 + m.start()] if m else wf[i:]

    for name in ("zipapp", "portable-windows"):
        j = job(name)
        check("attest-build-provenance" in j and "id-token: write" in j and "attestations: write" in j,
              f"{name}: attests, with the two permissions the action needs")
        check("attest-build-provenance" in j and "gh release upload" in j
              and j.index("attest-build-provenance") < j.index("gh release upload"),
              f"{name}: the attestation comes before the upload, so nothing unattested is attached")
    check("attestations: write" not in job("pypi") and "attestations: write" not in job("portable"),
          "the jobs that attach nothing get no attestation permission")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        fn()
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all closing-summary checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
