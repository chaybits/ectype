"""Checks for the slash command's files: whose they are, and whether ectype may replace them.

    python3 tests/test_skill_state.py

A file ectype wrote carries a marker line, and since 2026-09-27 the marker also carries a hash of
what was written. Four states follow from it, and each one has to read right on every surface:

    ours    exactly what this ectype writes
    stale   untouched since ectype wrote it, but written from an older template: Install updates it
    edited  changed by someone after ectype wrote it: refused unless replaced on purpose (kept as .bak)
    foreign no marker at all: somebody's own command, never touched

Before, an untouched file from an older template read as "edited", so a plain install refused to
update the user's own untouched files, and the web page called them "a file not written by ectype".
Every folder here is a temp dir; the agents' command folders are redirected through their own
environment variables. Written before the code, so each check went red first.
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
TMP = Path(tempfile.mkdtemp(prefix="ectype-skill-state-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude-config")
os.environ["CODEX_HOME"] = str(TMP / "codex-home")
os.environ["GEMINI_CLI_HOME"] = str(TMP / "gemini-home")
NOTHING = TMP / "nothing-here"
for _v in ("ECTYPE_CLAUDE_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME", "ECTYPE_AIDER_HOME",
           "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME", "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(NOTHING)

from ectype import skill  # noqa: E402
from ectype.web import make_server  # noqa: E402

FAILS: list[str] = []
NAME = "probecmd"
AGENTS = ("claude-code", "gemini-cli", "codex")


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def _state(agent: str) -> str:
    return skill.state(agent, NAME)[0]


def _older(agent: str) -> str:
    """What an earlier ectype wrote: the template with one sentence changed, stamped the way
    ectype stamps what it writes. Nobody edited it afterwards."""
    text = skill.render(agent, NAME, stamp=False).replace("Bring a past session", "Bring an earlier session", 1)
    stamp = getattr(skill, "_stamped", None)
    return stamp(text) if stamp else text


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "ectype", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", env=os.environ.copy(), timeout=120)


def test_1_a_fresh_install_is_ours_and_carries_a_hash() -> None:
    written = skill.install(NAME)
    check(len(written) == 3 and all(str(p).startswith(str(TMP)) for p in written),
          f"install writes the three files under the redirected folders ({[str(p) for p in written]})")
    check(all(_state(a) == "ours" for a in AGENTS), f"all three read as ours ({[_state(a) for a in AGENTS]})")
    line = next(ln for ln in written[0].read_text(encoding="utf-8").splitlines() if skill.MARK in ln)
    check(re.search(r"\(ectype [^,)]+, sha256 [0-9a-f]{12}\)", line) is not None,
          f"the marker line names the version and a hash of what was written ({line.strip()[:110]!r})")


def test_2_an_untouched_file_from_an_older_template_is_stale_and_updates_without_force() -> None:
    p = skill.path("claude-code", NAME)
    p.write_text(_older("claude-code"), encoding="utf-8")
    check(_state("claude-code") == "stale", f"an older ectype's untouched file reads as stale ({_state('claude-code')})")
    st = next(s for s in skill.status(NAME) if s["agent"] == "claude-code")
    check(st.get("state") == "stale" and st.get("installed") and not st.get("edited"),
          f"status says so ({ {k: st.get(k) for k in ('state', 'installed', 'ours', 'edited', 'stale')} })")
    try:
        skill.install(NAME)
        ok = True
    except FileExistsError as e:
        ok = False
        check(False, f"a plain install updates a stale file ({e})")
    if ok:
        check(p.read_text(encoding="utf-8") == skill.render("claude-code", NAME), "a plain install brings it up to date")
    check(not list(p.parent.glob(p.name + ".bak-*")), "no .bak for a file that was ectype's own, untouched")


def test_3_an_edited_file_is_refused_and_replaced_only_on_purpose() -> None:
    p = skill.path("gemini-cli", NAME)
    p.write_text(p.read_text(encoding="utf-8") + "\n# my own note\n", encoding="utf-8")
    check(_state("gemini-cli") == "edited", f"a file changed after ectype wrote it reads as edited ({_state('gemini-cli')})")
    try:
        skill.install(NAME)
        check(False, "a plain install refuses an edited file")
    except FileExistsError as e:
        check("edited" in str(e), f"a plain install refuses an edited file, and says why ({e})")
    skill.install(NAME, force=True)
    check(p.read_text(encoding="utf-8") == skill.render("gemini-cli", NAME), "replace writes the current text")
    check(len(list(p.parent.glob(p.name + ".bak-*"))) == 1, "and keeps the edited file as .bak")


def test_4_files_without_a_hash_keep_the_old_rule() -> None:
    p = skill.path("codex", NAME)
    current = skill.render("codex", NAME)
    legacy = re.sub(r"\(ectype ([^,)]+), sha256 [0-9a-f]+\)", r"(ectype \1)", current)
    p.write_text(legacy, encoding="utf-8")
    check(legacy != current and _state("codex") == "ours", f"a hashless file equal to the current text is ours ({_state('codex')})")
    p.write_text(legacy.replace("Bring a past session", "Bring an earlier session", 1), encoding="utf-8")
    check(_state("codex") == "edited", "a hashless file that differs cannot be told from an edit, so it stays edited")
    skill.install(NAME, ["codex"], force=True)


def test_5_remove_takes_ours_and_stale_and_keeps_the_rest() -> None:
    skill.path("claude-code", NAME).write_text(_older("claude-code"), encoding="utf-8")
    g = skill.path("gemini-cli", NAME)
    g.write_text(g.read_text(encoding="utf-8") + "\n# mine\n", encoding="utf-8")
    removed, kept = skill.remove(NAME)
    check(skill.path("claude-code", NAME) in removed and skill.path("codex", NAME) in removed,
          f"a stale and a current file are ectype's and go ({[str(p) for p in removed]})")
    check(kept == [g] and g.exists(), "the edited one stays")
    skill.remove(NAME, force=True)


def test_6_the_command_line_names_each_state() -> None:
    skill.install(NAME)
    skill.path("claude-code", NAME).write_text(_older("claude-code"), encoding="utf-8")
    g = skill.path("gemini-cli", NAME)
    g.write_text(g.read_text(encoding="utf-8") + "\n# mine\n", encoding="utf-8")
    out = _cli("skill", "status", "--name", NAME).stdout
    row = {a: next((ln for ln in out.splitlines() if ln.strip().startswith(lbl)), "")
           for a, lbl in (("claude-code", "Claude Code"), ("gemini-cli", "Gemini CLI"), ("codex", "Codex"))}
    check("older" in row["claude-code"] and "install" in out.lower(), f"stale: says an older ectype wrote it ({row['claude-code'].strip()!r})")
    check("edited" in row["gemini-cli"], f"edited: says it was edited ({row['gemini-cli'].strip()!r})")
    check("installed" in row["codex"] and "edited" not in row["codex"] and "older" not in row["codex"],
          f"current: installed ({row['codex'].strip()!r})")
    r = _cli("skill", "install", "--name", NAME)
    check(r.returncode != 0 and "edited" in r.stderr and "--force" in r.stderr,
          f"install refuses the edited one and says how to replace it ({r.stderr.strip()[:140]})")
    check(skill.path("claude-code", NAME).read_text(encoding="utf-8") == _older("claude-code"),
          "and writes nothing at all when one target is refused (every target is checked first)")
    skill.remove(NAME, force=True)


def test_7_the_web_endpoint_reports_states_and_can_replace() -> None:
    skill.install(NAME)
    g = skill.path("gemini-cli", NAME)
    g.write_text(g.read_text(encoding="utf-8") + "\n# mine\n", encoding="utf-8")
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

    st, r = call("GET", f"/api/skill?name={NAME}")
    states = {a["agent"]: a.get("state") for a in r.get("agents", [])}
    check(st == 200 and states == {"claude-code": "ours", "gemini-cli": "edited", "codex": "ours"},
          f"GET /api/skill carries each file's state ({states})")
    st, r = call("POST", "/api/skill", {"action": "install", "name": NAME})
    check(st >= 400 and "edited" in r.get("error", ""), f"an install that meets an edited file is an error that says so ({st})")
    st, r = call("POST", "/api/skill", {"action": "install", "name": NAME, "force": True})
    check(st == 200 and all(a.get("state") == "ours" for a in r.get("agents", [])), "force: true replaces it")
    srv.shutdown()
    skill.remove(NAME, force=True)


def test_8_the_page_names_each_state_and_offers_replace() -> None:
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    fn = page.split("function renderSkillStatus(", 1)[1].split("\nfunction ", 1)[0] if "function renderSkillStatus(" in page else ""
    check("'stale'" in fn and "'edited'" in fn and "'foreign'" in fn, "the status line tells stale, edited and foreign apart")
    check("a file not written by ectype" not in fn.split("'foreign'", 1)[0] if "'foreign'" in fn else False,
          "and 'not written by ectype' is said only of a foreign file")
    check('id="s_kReplace"' in page and "force:true" in page.replace(" ", ""), "an edited file can be replaced from the page (force)")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
        except Exception as e:                  # noqa: BLE001, a crash is one failure, reported, and the rest still run
            check(False, f"{fn.__name__} raised {type(e).__name__}: {e}")
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all skill-state checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
