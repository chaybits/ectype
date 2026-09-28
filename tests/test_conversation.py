"""Checks for the 2026-09-27 round: what a view of a session shows, and what it says about itself.

    python3 tests/test_conversation.py

A message typed while the agent was busy is the user's, and shows as such. A question the agent put
to the user, and the user's answer, read as a question and an answer (an option, on by default), for
every agent that has such a tool. Messages other sessions sent in read as messages, under their own
label (an option, on by default). The header counts what the view shows and nothing else, so a model
reading an import never learns that something was left out. `ectype recall` takes every view option
from Settings -> Agent skill and starts with the conversation alone.

Every store is synthetic and lives in a temp dir; the settings file is a throwaway, and every other
agent's store points at nothing. Written before the code, so each check went red first.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ectype-conversation-tests-"))
os.environ["ECTYPE_CONFIG"] = str(TMP / "cfg" / "settings.json")
NOTHING = TMP / "nothing-here"
for _v in ("ECTYPE_CLAUDE_HOME", "CODEX_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
           "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME", "ECTYPE_AIDER_HOME",
           "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME", "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
    os.environ[_v] = str(NOTHING)
os.environ.pop("CLAUDE_CONFIG_DIR", None)

from ectype import adapters, budget, settings  # noqa: E402
from ectype.model import ContentBlock, Message, Session  # noqa: E402
from ectype.render.text import render  # noqa: E402

FAILS: list[str] = []
SID = "eeee1111-0000-4000-8000-000000000027"
STORE = TMP / "claude"
T = "2026-09-27T08:00:{:02d}.000Z"

# every string below is unique, so a check can say exactly which part of the session leaked or went missing
TYPED = "please count the widgets"
QUESTION = "Which box should I count?"
ANSWER = "Blue box"
QUEUED = "also count the green ones"
BASH_OUT = "FORTY-RESULT"
PEER_A = "FYI the green box is empty"
PEER_B = "PEER NOTE: labels are in the drawer"
TASK = "background job finished (probe)"
REMINDER = "probe reminder text"
THOUGHT = "probe private reasoning"
FINAL = "Blue box: forty widgets. Green box: none."


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def _records() -> list[dict]:
    """One Claude Code session holding every case this round is about, in the shapes Claude Code
    writes (2.1.28x): a typed prompt, an AskUserQuestion call and its answer, a Bash call, a message
    typed while the agent was busy (a `queued_command` attachment), two messages from other sessions
    (a user record and a queued attachment, both `origin.kind` "peer"), a task notification, a system
    reminder, thinking, and a final answer."""
    base = {"cwd": "/srv/work/widgets", "isSidechain": False, "version": "2.1.280", "sessionId": SID, "userType": "external"}
    q = {"question": QUESTION, "header": "Box", "multiSelect": False,
         "options": [{"label": "Red box", "description": "the one on the left"},
                     {"label": ANSWER, "description": "the one on the right"}]}
    return [
        {**base, "type": "user", "uuid": "u1", "parentUuid": None, "timestamp": T.format(0), "origin": {"kind": "human"},
         "message": {"role": "user", "content": TYPED}},
        {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": T.format(1),
         "message": {"role": "assistant", "model": "probe-model", "content": [
             {"type": "text", "text": "Before I count, one question."},
             {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": [q]}}]}},
        {**base, "type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": T.format(2),
         "toolUseResult": {"questions": [q], "answers": {QUESTION: ANSWER}},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q1",
                     "content": f'The user answered: "{QUESTION}"="{ANSWER}". Read the answers carefully, '
                                "they may request clarification, changes, or that you not proceed, and follow what they actually say."}]}},
        {**base, "type": "assistant", "uuid": "a2", "parentUuid": "u2", "timestamp": T.format(3),
         "message": {"role": "assistant", "model": "probe-model", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls blue | wc -l"}}]}},
        {**base, "type": "attachment", "uuid": "att1", "parentUuid": "a2", "timestamp": T.format(4),
         "attachment": {"type": "queued_command", "prompt": QUEUED, "commandMode": "prompt",
                        "origin": {"kind": "human"}, "humanTurn": True, "timestamp": T.format(4)}},
        {**base, "type": "user", "uuid": "u3", "parentUuid": "att1", "timestamp": T.format(5),
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": BASH_OUT}]}},
        {**base, "type": "user", "uuid": "u4", "parentUuid": "u3", "timestamp": T.format(6), "isMeta": True,
         "origin": {"kind": "peer", "from": "peer-1111", "name": "widget-helper", "body": PEER_A},
         "message": {"role": "user", "content": PEER_A}},
        {**base, "type": "attachment", "uuid": "att2", "parentUuid": "u4", "timestamp": T.format(7),
         "attachment": {"type": "queued_command", "prompt": PEER_B, "commandMode": "prompt",
                        "origin": {"kind": "peer", "from": "peer-2222", "name": "label-bot", "body": PEER_B}}},
        {**base, "type": "attachment", "uuid": "att3", "parentUuid": "att2", "timestamp": T.format(8),
         "attachment": {"type": "queued_command", "prompt": f"<task-notification>{TASK}</task-notification>",
                        "commandMode": "task-notification"}},
        {**base, "type": "user", "uuid": "u5", "parentUuid": "att3", "timestamp": T.format(9), "isMeta": True,
         "message": {"role": "user", "content": f"<system-reminder>{REMINDER}</system-reminder>"}},
        {**base, "type": "assistant", "uuid": "a3", "parentUuid": "u5", "timestamp": T.format(10),
         "message": {"role": "assistant", "model": "probe-model", "content": [
             {"type": "thinking", "thinking": THOUGHT}, {"type": "text", "text": FINAL}]}},
    ]


def _write_store() -> None:
    proj = STORE / "-srv-work-widgets"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{SID}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _records()), encoding="utf-8")
    os.environ["ECTYPE_CLAUDE_HOME"] = str(STORE)


def _session() -> Session:
    ref = next(r for r in adapters.get("claude-code").discover() if r.id == SID)
    return adapters.load(ref)


def _view(**opts) -> str:
    s = _session()
    o = {"mode": "full", "cap": 0, "thinking": False, "tools": True, "env": False, "notices": False, "collapse": True, **opts}
    return render(budget.filtered(s, o), budget.render_options(o), source=s, with_footer=False)


def _turns(text: str) -> list[tuple[str, str]]:
    """(label, body) per headed turn of a text render."""
    out: list[tuple[str, str]] = []
    for part in re.split(r"\n(?=\[(?:USER|ASSISTANT|PEER|TOOL|SYSTEM)\])", text):
        m = re.match(r"\[(USER|ASSISTANT|PEER|TOOL|SYSTEM)\]", part)
        if m:
            out.append((m.group(1), part))
    return out


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "ectype", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", env=os.environ.copy(), timeout=120)


# --------------------------------------------------------------------------- messages typed mid-turn
def test_1_a_message_typed_while_the_agent_worked_is_the_users() -> None:
    _write_store()
    s = _session()
    queued = [m for m in s.messages if any(QUEUED in b.text for b in m.blocks)]
    check(len(queued) == 1 and queued[0].role == "user" and not queued[0].is_env,
          f"the queued_command attachment loads as one user message, not injected context ({[(m.role, m.meta) for m in queued]})")
    out = _view(tools=False)
    user_turns = [body for label, body in _turns(out) if label == "USER"]
    check(any(QUEUED in b for b in user_turns), "it shows as a [USER] turn in the conversation alone")
    check(TASK not in out, "a task notification (a queued command the user did not type) stays injected context, hidden by default")
    check(TASK in _view(tools=False, env=True), "and shows with injected context on")


# --------------------------------------------------------------------------- messages from other sessions
def test_2_messages_from_other_sessions_are_messages_with_an_option() -> None:
    out = _view(tools=False)
    peers = [body for label, body in _turns(out) if label == "PEER"]
    check(len(peers) == 2, f"both messages from other sessions show, each as its own [PEER] turn ({len(peers)} found)")
    check(any(PEER_A in b and "widget-helper" in b for b in peers) and any(PEER_B in b and "label-bot" in b for b in peers),
          "each names the session it came from")
    check(not any(PEER_A in body or PEER_B in body for label, body in _turns(out) if label == "USER"),
          "neither is filed under the user's turn, merge on or off (the user did not write them)")
    hidden = _view(tools=False, peers=False)
    check(PEER_A not in hidden and PEER_B not in hidden and "[PEER]" not in hidden, "peers off: both are gone")
    check(QUEUED in hidden and TYPED in hidden, "and the user's own words stay")
    from ectype.summary import summarize
    text = summarize(_session())
    asked = text.split("asked:", 1)[1].split("\n\n", 1)[0] if "asked:" in text else ""
    check(TYPED in asked and QUEUED in asked, "summarize lists the typed and the queued prompt as asks")
    check(PEER_A not in asked and PEER_B not in asked and TASK not in asked and REMINDER not in asked,
          "and never another session's message, a task notification or a reminder")


# --------------------------------------------------------------------------- questions and answers
def test_3_a_question_and_its_answer_read_as_turns() -> None:
    out = _view(tools=False)
    turns = _turns(out)
    qi = next((i for i, (label, body) in enumerate(turns) if label == "ASSISTANT" and QUESTION in body), None)
    check(qi is not None, "the question shows in the assistant's turn with tool calls off")
    body = turns[qi][1] if qi is not None else ""
    check("Red box" in body and ANSWER in body, "with its options")
    nxt = turns[qi + 1] if qi is not None and qi + 1 < len(turns) else ("", "")
    check(nxt[0] == "USER" and ANSWER in nxt[1], f"the answer is the user's next turn ({nxt[0]!r})")
    check("Read the answers carefully" not in out, "the tool's instructions to the model are not the user's words")
    check("AskUserQuestion" not in out, "and nothing says a tool was involved")
    full = _view(tools=True)
    check("AskUserQuestion" not in full and QUESTION in full, "tool calls on: still the question as text, not a call")
    off = _view(tools=False, questions=False)
    check(QUESTION not in off and ANSWER not in off.replace(FINAL, ""), "questions off and tool calls off: both gone")
    raw = _view(tools=True, questions=False)
    check("AskUserQuestion" in raw and "Read the answers carefully" in raw, "questions off and tool calls on: the call and its result as recorded")
    from ectype.summary import summarize
    text = summarize(_session())
    check("answered:" in text and QUESTION in text.split("answered:", 1)[1] and ANSWER in text.split("answered:", 1)[1],
          "summarize lists the user's answers, so search finds a decision")


def test_4_every_agent_with_a_question_tool_is_marked() -> None:
    names = {n: getattr(adapters.get(n), "question_tools", None) for n in ("claude-code", "gemini-cli", "codex", "cline", "roo-code")}
    check(names == {"claude-code": {"AskUserQuestion"}, "gemini-cli": {"ask_user"}, "codex": {"request_user_input"},
                    "cline": {"ask_followup_question"}, "roo-code": {"ask_followup_question"}},
          f"each adapter names its agent's question tool ({names})")
    try:
        from ectype.adapters.base import mark_questions
    except ImportError:
        check(False, "adapters.base.mark_questions exists")
        return
    cases = {
        "cline": ("ask_followup_question", {"question": QUESTION, "options": json.dumps(["Red box", ANSWER])},
                  f"<answer>\n{ANSWER}\n</answer>"),
        "codex": ("request_user_input", {"questions": [{"id": "box", "header": "Box", "question": QUESTION,
                                                         "options": [{"label": "Red box"}, {"label": ANSWER}]}]},
                  json.dumps({"answers": {"box": {"answers": [ANSWER]}}})),
        "gemini-cli": ("ask_user", {"questions": [{"question": QUESTION, "header": "Box",
                                                    "options": [{"label": "Red box", "description": "left"}]}]},
                       f"User answered: {ANSWER}"),
    }
    for agent, (tool, args, result) in cases.items():
        s = Session(agent, "probe", TMP / "probe.json", [
            Message(0, "user", None, [ContentBlock("text", "count them")]),
            Message(1, "assistant", None, [ContentBlock("tool_call", name=tool, call_id="c1", args=args)]),
            Message(2, "tool", None, [ContentBlock("tool_result", result, call_id="c1")]),
            Message(3, "assistant", None, [ContentBlock("text", "done")])])
        mark_questions(s, {tool})
        o = {"mode": "brief", "tools": False, "collapse": True}
        out = render(budget.filtered(s, o), budget.render_options(o), with_footer=False)
        turns = _turns(out)
        check(any(label == "ASSISTANT" and QUESTION in body for label, body in turns),
              f"{agent}: {tool}'s question shows as the assistant's words")
        check(any(label == "USER" and ANSWER in body for label, body in turns),
              f"{agent}: the answer shows as the user's words ({[t[0] for t in turns]})")
        check("<answer>" not in out and '"answers"' not in out, f"{agent}: without the tool's wrapping")


# --------------------------------------------------------------------------- the header
def _header_counts(text: str) -> dict[str, int]:
    m = re.search(r"^messages: (\d+) \(([^)]*)\)", text, re.M)
    if not m:
        return {}
    return {k: int(v) for k, v in (p.rsplit(" ", 1) for p in m.group(2).split(", ") if p)}


def test_5_the_header_counts_what_the_view_shows_and_nothing_else() -> None:
    for opts, label in (({"tools": False}, "the conversation alone"), ({"tools": True, "mode": "brief"}, "names only"),
                        ({"tools": True, "thinking": True, "env": True}, "everything"), ({"tools": False, "collapse": False}, "no merge")):
        out = _view(**opts)
        head = out.split("\n\n", 1)[0]
        counts = _header_counts(head)
        shown: dict[str, int] = {}
        for lab, _ in _turns(out):
            shown[lab.lower()] = shown.get(lab.lower(), 0) + 1
        check(counts == shown, f"{label}: the header's turns are the turns printed below it ({counts} vs {shown})")
    head = _view(tools=False).split("\n\n", 1)[0]
    check("tool calls" not in head and "thinking" not in head,
          f"the conversation alone: the header says nothing about tool calls or thinking ({head.splitlines()[-1]!r})")
    head = _view(tools=True, mode="full", questions=False).split("\n\n", 1)[0]
    check("tool calls: 2" in head, f"tool calls on: it counts the calls shown ({head.splitlines()[-1]!r})")
    head = _view(tools=True, thinking=True).split("\n\n", 1)[0]
    check("thinking blocks: 1" in head, "thinking on: it counts the thinking shown")


def test_6_selected_never_exceeds_the_ceiling_and_equals_it_with_everything_on() -> None:
    s = _session()
    worst = []
    for mode in ("brief", "custom", "full"):
        for tools in (True, False):
            for thinking in (True, False):
                for env in (True, False):
                    for peers in (True, False):
                        for questions in (True, False):
                            for collapse in (True, False):
                                o = {"mode": mode, "cap": 5, "tools": tools, "thinking": thinking, "env": env, "notices": env,
                                     "peers": peers, "questions": questions, "collapse": collapse}
                                b = budget.breakdown(s, o)
                                if b["selected"] > b["everything"]:
                                    worst.append((o, b["selected"], b["everything"]))
    check(not worst, f"selected <= max context for every combination of options ({worst[:2]})")
    for questions in (True, False):
        o = {"mode": "full", "cap": 0, "tools": True, "thinking": True, "env": True, "notices": True, "peers": True,
             "questions": questions, "collapse": True}
        b = budget.breakdown(s, o)
        check(b["selected"] == b["everything"], f"everything on, questions {questions}: selected equals max context ({b['selected']} vs {b['everything']})")
    keys = [p["key"] for p in budget.breakdown(s, {"tools": False}).get("parts", [])]
    check("peers" in keys, f"the budget table has a row for other sessions' messages ({keys})")


# --------------------------------------------------------------------------- recall and Settings -> Agent skill
def test_7_recall_starts_with_the_conversation_and_takes_every_option_from_the_skill_section() -> None:
    sk = settings.DEFAULTS["skill"]
    want = {"command", "mode", "cap", "thinking", "tools", "env", "notices", "questions", "peers", "collapse", "wrap",
            "stamps", "markers", "tool_headers", "hide_roles", "redact", "notice", "summarize"}
    check(want <= set(sk), f"Settings -> Agent skill holds every option a view has (missing: {sorted(want - set(sk))})")
    check(sk.get("tools") is False and sk.get("questions") is True and sk.get("peers") is True,
          "by default it brings in the conversation alone, with questions and other sessions' messages as turns")
    v = settings.DEFAULTS["view"]
    check(v.get("questions") is True and v.get("peers") is True, "Settings -> View has both new options, on by default")
    for bad in ({"skill": {"hide_roles": ["robot"]}}, {"skill": {"stamps": "sometimes"}}, {"skill": {"wrap": "narrator"}},
                {"view": {"peers": "yes"}}, {"skill": {"questions": 1}}):
        try:
            settings.validate(bad)
            refused = False
        except ValueError:
            refused = True
        check(refused, f"{bad} is refused")
    r = _cli("recall", SID[:8], "--no-summarize")
    check(r.returncode == 0 and TYPED in r.stdout and QUESTION in r.stdout and QUEUED in r.stdout,
          f"recall with only an id: the conversation, the question and the queued message (rc {r.returncode} {r.stderr.strip()[:160]})")
    check("Bash" not in r.stdout and BASH_OUT not in r.stdout and "tool calls" not in r.stdout,
          "and no tool name, no tool output, no word about tools")
    r = _cli("recall", SID[:8], "--no-summarize", "--tools", "--mode", "brief")
    check(r.returncode == 0 and "Bash" in r.stdout, "--tools brings the calls back")
    settings.save({"skill": {"stamps": "none", "markers": False, "peers": False}})
    r = _cli("recall", SID[:8], "--no-summarize")
    check(r.returncode == 0 and "[USER]" not in r.stdout and PEER_A not in r.stdout and not re.search(r"\d\d:\d\d:\d\d", r.stdout.split("\n\n", 1)[1]),
          "the skill section's timestamps, role markers and peers settings are what recall uses")
    r = _cli("recall", SID[:8], "--no-summarize", "--peers", "--markers")
    check(r.returncode == 0 and PEER_A in r.stdout and "[USER]" in r.stdout, "a flag typed after the id wins over the section")
    settings.save({})


def test_8_page_wires_the_new_controls() -> None:
    page = (ROOT / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    for cid in ("questions", "peers"):
        check(f'id="{cid}"' in page, f"the toolbar has #{cid}")
        check(f"{cid}:$('#{cid}').checked" in page, f"opts() sends {cid}")
        check(f"id=\"s_{cid}\"" in page, f"Settings -> View has its default (#s_{cid})")
    skill_ids = ("s_kNotices", "s_kQuestions", "s_kPeers", "s_kStamps", "s_kMerge", "s_kMarkers", "s_kToolhdr",
                 "s_kHideUser", "s_kHideAsst", "s_kRedact", "s_kNotice")
    missing = [i for i in skill_ids if f'id="{i}"' not in page]
    check(not missing, f"Settings -> Agent skill has every view option (missing: {missing})")
    paint = page.split("function paint(", 1)[1].split("\nfunction ", 1)[0] if "function paint(" in page else ""
    role_line = next((ln for ln in paint.splitlines() if "USER|ASSISTANT" in ln), "")
    check("PEER" in role_line, f"the preview colours [PEER] headers like the other roles ({role_line.strip()[:80]!r})")


def main() -> int:
    tests = [v for k, v in sorted(globals().items(), key=lambda kv: int(re.match(r"test_(\d+)", kv[0]).group(1)) if re.match(r"test_\d+", kv[0]) else 0)
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
        except Exception as e:                  # noqa: BLE001, a crash is one failure, reported, and the rest still run
            check(False, f"{fn.__name__} raised {type(e).__name__}: {e}")
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all conversation checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
