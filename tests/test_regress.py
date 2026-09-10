"""Regression tests for the bugs found in the 2026-09-09 review pass.

    python3 tests/test_regress.py

Each check names the defect it guards, so a failure says what came back rather than only which
assertion tripped. No fixtures and no real store: the sessions here are built in memory, and the
web checks run a server on a free port with a throwaway settings file.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ECTYPE_CONFIG"] = str(Path(tempfile.mkdtemp(prefix="ectype-regress-")) / "settings.json")

from ectype import budget, convert as conv                      # noqa: E402
from ectype.model import ContentBlock, Message, Session         # noqa: E402
from ectype.render import formats                               # noqa: E402

FAILS: list[str] = []
TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ARGS_MARK = "ARGUMENTS_MUST_NOT_APPEAR"
RESULT_MARK = "OUTPUT_MUST_NOT_APPEAR"


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def session(msgs: list[Message], sid: str = "0000abcd-1111-2222-3333-444455556666") -> Session:
    return Session("claude-code", sid, Path("/nowhere/x.jsonl"), msgs, cwd="/nowhere",
                   started=TS, ended=TS, title="probe")


# --------------------------------------------------------------------------- brief mode
def test_brief_is_a_filter_not_a_formatter_choice() -> None:
    """`names only` promised "the tool's name alone, no arguments". Only the text renderer
    honoured it: markdown, html, csv, json and jsonl all printed the full argument list, while
    the header said the opposite and the token budget counted the text version."""
    s = session([
        Message(0, "user", TS, [ContentBlock("text", "go")]),
        Message(1, "assistant", TS, [ContentBlock("tool_call", name="Bash",
                                                  args={"command": f"echo {ARGS_MARK}"})]),
        Message(2, "tool", TS, [ContentBlock("tool_result", RESULT_MARK * 20)]),
    ])
    opts = {"mode": "brief", "tools": True, "collapse": False}
    view = budget.filtered(s, opts)
    ro = budget.render_options(opts)
    for fmt in formats.FORMATS:
        out = formats.export(view, fmt, ro, source=s)
        check(ARGS_MARK not in out, f"brief mode: {fmt} carries no tool arguments")
        check(RESULT_MARK not in out, f"brief mode: {fmt} carries no tool output")
        check("Bash" in out, f"brief mode: {fmt} still names the call")


# --------------------------------------------------------------------------- fidelity report
def test_claude_template_note_follows_the_template() -> None:
    """The "no template: minimal envelope" note hung off `if redact:`, so a conversion WITH a
    template also claimed there was none, and one without a template but with --redact said
    nothing at all."""
    tpl = Path(tempfile.mkdtemp()) / "tpl.jsonl"
    tpl.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "cwd": "/t", "version": "9.9.9", "gitBranch": "main", "userType": "external",
         "message": {"role": "user", "content": "x"}},
        {"type": "assistant", "cwd": "/t", "version": "9.9.9",
         "message": {"model": "claude-tpl-model", "content": []}},
    ]) + "\n", encoding="utf-8")

    def notes(has_tpl: bool, redact) -> str:
        s = session([Message(0, "user", TS, [ContentBlock("text", "hi")]),
                     Message(1, "assistant", TS, [ContentBlock("text", "yo")])])
        s.agent = "codex"                       # cross-agent, so it goes through a writer
        _p, rep = conv.convert(s, "claude-code", Path(tempfile.mkdtemp()),
                               template=tpl if has_tpl else None, redact=redact)
        return " | ".join(n for n in rep.notes if "template" in n)

    both = notes(True, None)
    check("envelope from template" in both and "no template" not in both,
          "a conversion with a template does not also report 'no template'")
    check("no template" in notes(False, lambda x: x),
          "a conversion without a template says so even when redaction is on")


# --------------------------------------------------------------------------- budget cache
def test_ceiling_follows_the_content() -> None:
    """`everything` is cached on (agent, id) and the key says nothing about content, so a session
    an agent was still writing kept the ceiling measured on its first read while `selected` grew
    past it; the two headline numbers of the whole UI then disagreed."""
    small = session([Message(0, "user", TS, [ContentBlock("text", "short")])])
    first = budget.everything(small)["tokens"]
    grown = session([Message(0, "user", TS, [ContentBlock("text", "short")]),
                     Message(1, "assistant", TS, [ContentBlock("text", "a much longer reply " * 200)])])
    check(budget.everything(grown)["tokens"] == first,
          "the ceiling is cached (unchanged without an explicit invalidate)")
    budget.invalidate(grown.agent, grown.id)
    check(budget.everything(grown)["tokens"] > first,
          "after invalidate() the ceiling reflects the grown session")

    budget.invalidate()
    for i in range(budget._FULL_MAX + 20):      # the GUI is long-lived; the cache must not grow forever
        budget.everything(session([Message(0, "user", TS, [ContentBlock("text", "x")])], sid=f"id-{i}"))
    check(len(budget._FULL) <= budget._FULL_MAX,
          f"the ceiling cache stays bounded ({len(budget._FULL)} <= {budget._FULL_MAX})")


# --------------------------------------------------------------------------- header parity
def test_a_filtered_render_never_costs_more_than_the_ceiling() -> None:
    """The header used to carry a `shown:` line that got LONGER the more you
    filtered, so on a small session a filtered export could count more tokens than the unfiltered
    ceiling it is measured against. The line is gone (2026-09-09), so both renders share an
    identical header and filtering can only ever reduce."""
    s = session([
        Message(0, "user", TS, [ContentBlock("text", "hi")]),
        Message(1, "assistant", TS, [ContentBlock("thinking", "ok"), ContentBlock("text", "hello"),
                                     ContentBlock("tool_call", name="ls", args=None)]),
    ], sid="tiny-session-0000")
    for opts in ({"mode": "full", "thinking": True, "tools": True, "env": True, "collapse": True},
                 {"mode": "custom", "cap": 150, "thinking": False, "tools": False, "env": False, "collapse": True},
                 {"mode": "brief", "thinking": False, "tools": True, "env": False, "collapse": True}):
        b = budget.breakdown(s, opts)
        check(b["selected"] <= b["everything"],
              f"tiny session, mode={opts['mode']}: selected {b['selected']} <= ceiling {b['everything']}")


# --------------------------------------------------------------------------- web guard
def test_only_this_page_may_call_the_server() -> None:
    """Binding 127.0.0.1 stops other machines, not other PAGES: any site open in the same browser
    could POST an export or rewrite the settings, and a DNS-rebound host could read every
    transcript."""
    from ectype import web
    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method: str, path: str, body=None, headers=None) -> int:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        st = c.getresponse().status
        c.close()
        return st

    host = {"Host": f"127.0.0.1:{port}"}
    try:
        check(call("GET", "/api/settings", headers=host) == 200, "the page's own GET is served")
        check(call("POST", "/api/settings", {"settings": {}},
                   {**host, "Origin": f"http://127.0.0.1:{port}"}) == 200, "the page's own POST is served")
        check(call("POST", "/api/settings", {"settings": {}}, host) == 200,
              "a POST with no Origin (curl, scripts) still works")
        check(call("POST", "/api/settings", {"settings": {}},
                   {**host, "Origin": "https://evil.example"}) == 403, "a cross-site POST is refused")
        check(call("GET", "/api/sessions", headers={"Host": "evil.example"}) == 403,
              "a DNS-rebound Host is refused")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- settings round trip
def test_settings_dialog_carries_the_list_columns() -> None:
    """POST /api/settings is a full replace (see test_web.py). The Settings dialog builds its body
    by hand and left `view.list` out, so opening Settings and saving reset the session list's
    columns and sort order."""
    page = (Path(__file__).resolve().parents[1] / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check("list:SET.view.list" in page.replace(" ", ""),
          "the Settings dialog sends view.list, so saving does not reset the session list")


def main() -> int:
    for fn in (test_brief_is_a_filter_not_a_formatter_choice,
               test_claude_template_note_follows_the_template,
               test_ceiling_follows_the_content,
               test_a_filtered_render_never_costs_more_than_the_ceiling,
               test_only_this_page_may_call_the_server,
               test_settings_dialog_carries_the_list_columns):
        print(fn.__name__)
        fn()
    import shutil
    shutil.rmtree(os.path.dirname(os.environ["ECTYPE_CONFIG"]), ignore_errors=True)   # the throwaway settings dir
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else "all regression checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
