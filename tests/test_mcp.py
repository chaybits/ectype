"""MCP server test: drives `ectype mcp` as a real subprocess over stdin/stdout.

    python3 tests/test_mcp.py

A synthetic Claude Code store in a temp dir, so no real session is read, and the settings file is
a throwaway. The point of running it as a subprocess rather than calling `_handle()` is the part
that is easy to get wrong: stdout carries the protocol, so anything that prints would corrupt it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ectype-mcp-test-")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def store() -> Path:
    """One Claude Code session: a prompt, a reply, and a tool call with a result."""
    home = Path(_TMP) / "claude"
    proj = home / "-tmp-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "abcdef12-0000-0000-0000-00000000000a"
    recs = [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid, "cwd": "/tmp/proj",
         "timestamp": "2026-09-10T10:00:00.000Z", "message": {"role": "user", "content": "count the files"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid, "cwd": "/tmp/proj",
         "timestamp": "2026-09-10T10:00:01.000Z", "message": {"role": "assistant", "model": "probe-model", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls | wc -l"}}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": sid, "cwd": "/tmp/proj",
         "timestamp": "2026-09-10T10:00:02.000Z", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "12\n" + "x" * 4000}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": sid, "cwd": "/tmp/proj",
         "timestamp": "2026-09-10T10:00:03.000Z", "message": {"role": "assistant", "model": "probe-model",
                                                              "content": [{"type": "text", "text": "twelve files"}]}},
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return home


def talk(msgs: list[dict], allow_write: bool = False) -> list[dict]:
    """Send every message, return every reply. One JSON object per line, both ways."""
    env = {**os.environ, "ECTYPE_CLAUDE_HOME": str(store()),
           "ECTYPE_CONFIG": str(Path(_TMP) / "settings.json")}
    for a in ("CODEX_HOME", "ECTYPE_GEMINI_HOME", "ECTYPE_ANTIGRAVITY_HOME", "ECTYPE_VSCODE_HOME",
              "ECTYPE_CURSOR_HOME", "ECTYPE_CLINE_HOME", "ECTYPE_ROO_HOME", "ECTYPE_CONTINUE_HOME",
              "ECTYPE_AIDER_HOME", "ECTYPE_AIDER_DIRS", "ECTYPE_LMSTUDIO_HOME",
              "ECTYPE_OPENWEBUI_HOME", "ECTYPE_SILLYTAVERN_HOME"):
        env[a] = str(Path(_TMP) / "nothing-here")        # every other store points at nothing
    cmd = [sys.executable, "-m", "ectype", "mcp"] + (["--allow-write"] if allow_write else [])
    p = subprocess.run(cmd, input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    out = []
    for line in p.stdout.splitlines():
        if line.strip():
            out.append(json.loads(line))                  # a non-JSON line here IS the bug this catches
    return out


def call(name: str, args: dict, mid: int = 9) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": args}}


def main() -> int:
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    r = talk([init,
              {"jsonrpc": "2.0", "method": "notifications/initialized"},      # a notification: no reply
              {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
              {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
    check(len(r) == 3, f"a notification gets no answer, three requests get three ({len(r)})")
    check(r[0]["result"]["serverInfo"]["name"] == "ectype" and r[0]["result"]["protocolVersion"] == "2025-06-18",
          "initialize names the server and echoes the protocol version the client asked for")
    names = [t["name"] for t in r[1]["result"]["tools"]]
    check(names == ["list_sessions", "show_session", "session_budget", "summarize_session", "search_sessions", "list_agents"],
          f"read-only by default: no write tool in the list ({', '.join(names)})")
    check(all(t.get("inputSchema", {}).get("type") == "object" for t in r[1]["result"]["tools"]),
          "every tool declares an object input schema")
    check(r[2]["result"] == {}, "ping answers")

    r = talk([init, call("list_sessions", {}, 2), call("session_budget", {"id": "abcdef12"}, 3),
              call("show_session", {"id": "abcdef12", "mode": "brief"}, 4),
              call("show_session", {"id": "abcdef12", "mode": "full"}, 5)])
    by = {x["id"]: x for x in r}
    listing = by[2]["result"]["content"][0]["text"]
    check("abcdef12" in listing and "claude-code" in listing, "list_sessions finds the synthetic session")
    budget_text = by[3]["result"]["content"][0]["text"]
    check("tool results" in budget_text and "if included in full" in budget_text,
          "session_budget names each element and what it would cost in full")
    brief, full = by[4]["result"]["content"][0]["text"], by[5]["result"]["content"][0]["text"]
    r2 = talk([init, call("summarize_session", {"id": "abcdef12"}, 6)])
    summary = r2[1]["result"]["content"][0]["text"]
    check("count the files" in summary and "Bash×1" in summary,
          "the summary names what was asked and which tools ran")
    check("ls | wc -l" in summary and "keywords:" in summary,
          "it lists the commands and ends with a keyword line for grep")
    check(len(summary) < len(full) / 3, f"a summary is a fraction of the session ({len(summary)} vs {len(full)})")
    check("Bash" in brief and "xxxx" not in brief, "brief keeps the tool's name and drops its output")
    check("xxxx" in full, "full keeps the output")
    check(len(brief) < len(full), f"brief is smaller than full ({len(brief)} < {len(full)})")

    r = talk([init, call("install_session", {"id": "abcdef12"}, 2),
              call("show_session", {"id": "nosuchid"}, 3),
              call("no_such_tool", {}, 4),
              {"jsonrpc": "2.0", "id": 5, "method": "nope/x"}])
    by = {x["id"]: x for x in r}
    # third audit, F81: a read-only server does not serve install_session, so it answers as for any
    # unknown tool, and the list it gives names only the tools it serves
    txt = by[2]["result"]["content"][0]["text"]
    check(by[2]["result"]["isError"] and "unknown tool" in txt and "install_session;" not in txt and ", install_session" not in txt,
          "the write tool does not exist on a read-only server, and is not advertised there")
    check(by[3]["result"]["isError"], "an unknown id is an error RESULT, not a dead connection")
    check(by[4]["result"]["isError"] and "unknown tool" in by[4]["result"]["content"][0]["text"],
          "an unknown tool says so")
    check(by[5].get("error", {}).get("code") == -32601, "an unknown method is a protocol error")

    r = talk([init, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], allow_write=True)
    names = [t["name"] for t in r[1]["result"]["tools"]]
    check("install_session" in names, "--allow-write adds the write tool")

    # agentcli: the part that can be checked without spending an API call is that an agent it
    # cannot drive, or one whose CLI is missing, is REPORTED rather than silently skipped.
    from ectype import agentcli
    try:
        agentcli.mint_template("cursor")
        check(False, "minting refuses an agent it cannot drive")
    except agentcli.AgentUnavailable as e:
        check("cannot drive" in str(e), f"minting refuses an agent it cannot drive ({e})")
    real = agentcli.AGENT_CLI["claude-code"]["bin"]
    agentcli.AGENT_CLI["claude-code"]["bin"] = "definitely-not-installed-xyz"
    try:
        agentcli.verify_resume("claude-code", "whatever")
        check(False, "verifying refuses when the CLI is not on PATH")
    except agentcli.AgentUnavailable as e:
        check("not on PATH" in str(e), f"verifying refuses when the CLI is not on PATH ({e})")
    finally:
        agentcli.AGENT_CLI["claude-code"]["bin"] = real

    shutil.rmtree(_TMP, ignore_errors=True)
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else "all mcp checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
