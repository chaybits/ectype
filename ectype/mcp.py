"""`ectype mcp`: an MCP server over stdio, so an agent can read past sessions itself.

Why this exists. The CLI already answers "what did we do in that session?", but only to a human
who types the command. An agent that can call these tools can answer it mid-conversation: find the
session, see what it would cost, and pull in only the part that matters. That is the same job the
token budget does for the web UI, exposed to the thing that actually spends the tokens.

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, which is what MCP's stdio transport
is, so this needs no dependencies and works in any MCP client (Claude Code, Codex, Cursor, Cline).

READ-ONLY unless `--allow-write` is given. The write tool installs a session into an agent's live
store, which is the one thing here that changes somebody else's files, so it is not in the tool
list at all unless it was asked for: a tool an agent cannot see is a tool it cannot be talked into
calling.

⚠ stdout belongs to the protocol. Anything that prints (an adapter warning, a stray debug line)
would corrupt the stream, so `serve()` moves `sys.stdout` to stderr and keeps the real handle to
itself.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__, adapters, budget, convert as conv
from .render.text import render

PROTOCOL = "2025-06-18"          # echoed back to the client when it asks for something else

_MODE = {"type": "string", "enum": ["brief", "custom", "full"],
         "description": "how much of each tool result to include: brief = the tool's name alone, "
                        "custom = the first `cap` tokens, full = everything"}


def _tools(allow_write: bool) -> list[dict]:
    t = [
        {"name": "list_sessions",
         "description": "Recent sessions across every AI agent store on this machine, newest first: "
                        "agent, id, when, size, project and title.",
         "inputSchema": {"type": "object", "properties": {
             "agent": {"type": "string", "description": f"one of: {', '.join(adapters.ADAPTERS)}"},
             "query": {"type": "string", "description": "substring of the title, id, project or agent"},
             "limit": {"type": "integer", "description": "how many rows (default 20)"}}}},
        {"name": "show_session",
         "description": "Read one session as text. This is the tool that carries a past conversation "
                        "into the current one: start with mode=brief to see what happened cheaply, "
                        "then re-read with a cap or a message range for the part that matters.",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {
             "id": {"type": "string", "description": "session id, or any unambiguous prefix"},
             "agent": {"type": "string"},
             "project": {"type": "string", "description": "part of the file's path, when one id exists under two projects"},
             "mode": _MODE,
             "cap": {"type": "integer", "description": "tokens kept per tool result in custom mode (0 = all)"},
             "thinking": {"type": "boolean", "description": "include the agent's reasoning (off by default, it is large)"},
             "start": {"type": "integer", "description": "first message index to include"},
             "end": {"type": "integer", "description": "last message index to include"}}}},
        {"name": "session_budget",
         "description": "What a session costs in tokens, element by element (your messages, assistant "
                        "text, thinking, tool calls, tool results), and what the whole thing would cost "
                        "with nothing left out. Call this before show_session on a large session.",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {
             "id": {"type": "string"}, "agent": {"type": "string"}, "project": {"type": "string"},
             "mode": _MODE, "cap": {"type": "integer"}, "thinking": {"type": "boolean"}}}},
        {"name": "summarize_session",
         "description": "What happened in a session, extracted from the transcript with no model: "
                        "what was asked, which tools ran and how often, which files were touched, "
                        "which commands ran, what failed, plus a keyword line built for grep. Far "
                        "cheaper than reading the session, and enough to decide whether to.",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {
             "id": {"type": "string"}, "agent": {"type": "string"}, "project": {"type": "string"}}}},
        {"name": "list_agents",
         "description": "Which agent stores exist on this machine, where they are, how many sessions "
                        "each holds, and which of them ectype can write back to.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    if allow_write:
        t.append(
            {"name": "install_session",
             "description": "WRITES. Copy a session into an agent's own store so that agent can resume "
                            "it. Same agent in and out is a lossless copy; a different target is a "
                            "conversion that folds tool activity into text and drops thinking.",
             "inputSchema": {"type": "object", "required": ["id"], "properties": {
                 "id": {"type": "string"}, "agent": {"type": "string"}, "project": {"type": "string"},
                 "to": {"type": "string", "description": f"target agent; omit for a copy. Writable: {', '.join(conv.WRITERS)}"},
                 "workspace": {"type": "string", "description": "the working directory the copy belongs to"}}}})
    return t


def _ref(a: dict):
    """The one session an id names, as `ectype show` resolves it."""
    from .cli import _resolve
    return _resolve(a["id"], a.get("agent"), a.get("project"))


def _opts(a: dict) -> dict:
    return {"mode": a.get("mode") or "custom", "cap": a.get("cap"), "thinking": bool(a.get("thinking")),
            "tools": True, "env": False, "collapse": True, "stamps": "full", "markers": True,
            "start": a.get("start"), "end": a.get("end")}


def _call(name: str, a: dict, allow_write: bool) -> str:
    # Names are checked before anything is resolved. Without this an unknown tool reached the id
    # lookup first and came back as a bare KeyError('id'), which tells the agent nothing.
    known = {t["name"] for t in _tools(True)}
    if name not in known:
        raise ValueError(f"unknown tool {name!r}; this server has: {', '.join(sorted(known))}")

    if name == "list_agents":
        rows = ["agent         type    sessions  writable  store"]
        for n, ad in adapters.ADAPTERS.items():
            if not ad.available():
                continue
            rows.append(f"{n:<13} {ad.category:<7} {len(ad.discover()):>8}  {'yes' if ad.writable else 'no':<8}  {ad.home()}")
        return "\n".join(rows) if len(rows) > 1 else "no agent stores found on this machine"

    if name == "list_sessions":
        agent = a.get("agent")
        if agent and agent not in adapters.ADAPTERS:
            raise ValueError(f"unknown agent {agent!r}; known: {', '.join(adapters.ADAPTERS)}")
        refs = adapters.all_refs([agent] if agent else None)
        q = (a.get("query") or "").lower()
        if q:
            refs = [r for r in refs if q in (r.title or "").lower() or q in r.id or q in (r.project or "").lower() or q in r.agent]
        refs = refs[: int(a.get("limit") or 20)]
        rows = [f"{'agent':<13} {'id':<10} {'modified':<17} {'project':<12} title"]
        for r in refs:
            rows.append(f"{r.agent:<13} {r.short:<10} {r.mtime.astimezone():%Y-%m-%d %H:%M}  {(r.project or '-')[:12]:<12} {(r.title or '')[:70]}")
        return "\n".join(rows) if refs else "no sessions match"

    ref = _ref(a)
    if name == "show_session":
        o = _opts(a)
        s = adapters.load(ref)
        view = budget.filtered(s, o)
        return render(view, budget.render_options(o), source=s, with_footer=True)

    if name == "summarize_session":
        from .summary import summarize
        return summarize(adapters.load(ref))

    if name == "session_budget":
        o = _opts(a)
        s = adapters.load(ref)
        b = budget.breakdown(s, o)
        lines = [f"{s.agent} · {s.id}  {s.title or ''}".rstrip(),
                 f"selected {b['selected']:,} tokens of {b['everything']:,} for the whole session "
                 f"({b['messages']} of {b['messages_full']} messages, mode {b['mode']}"
                 + (f", cap {b['cap']}" if b["mode"] == "custom" else "") + ")",
                 "" if b["exact"] else "counts are estimated; install the tokens extra for exact ones",
                 f"{'element':<22}{'now':>12}{'if included in full':>22}"]
        for p in b["parts"]:
            lines.append(f"{p['label']:<22}{p['tokens']:>12,}{p['tokens'] + p['remaining']:>22,}")
        lines.append(f"{'structure':<22}{b['structure']:>12,}")
        return "\n".join(x for x in lines if x != "")

    if name == "install_session":
        if not allow_write:
            raise PermissionError("this server is read-only; start it with `ectype mcp --allow-write`")
        target = a.get("to") or ref.agent
        if target not in conv.WRITERS:
            raise ValueError(f"{target} cannot be written; writable: {', '.join(conv.WRITERS)}")
        import tempfile
        from pathlib import Path
        ad = adapters.get(ref.agent)
        s = adapters.load(ref)
        native = target == ref.agent and target in conv.NATIVE
        out_dir = Path(tempfile.mkdtemp(prefix="ectype-mcp-"))     # the fidelity report's home; the session goes to the store
        extra = [(src, rel) for src, rel in ad.artifacts(ref) if src != s.path] if native else None
        path, rep = conv.convert(s, target, out_dir, install=True, source_path=s.path,
                                 workspace=a.get("workspace"), extra=extra)
        return f"installed as {target}: {path}\n\n{rep.render()}"

    raise ValueError(f"unknown tool {name!r}")


def serve(allow_write: bool = False) -> int:
    """Read JSON-RPC lines from stdin until it closes. One message per line, both ways."""
    out, sys.stdout = sys.stdout, sys.stderr        # the protocol owns the real stdout
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _send(out, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "not JSON"}})
                continue
            reply = _handle(msg, allow_write)
            if reply is not None:                    # a notification gets no answer, by spec
                _send(out, reply)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


def _send(out, obj: dict) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def _handle(msg: dict, allow_write: bool) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if mid is None and method:                       # notification
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return _ok(mid, {"protocolVersion": want or PROTOCOL,
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "ectype", "version": __version__}})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": _tools(allow_write)})
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            text = _call(p.get("name") or "", p.get("arguments") or {}, allow_write)
        except SystemExit as e:                      # _resolve() exits on an unknown id
            return _ok(mid, _err_content(str(e)))
        except Exception as e:                       # a failed tool is a RESULT, not a protocol error
            return _ok(mid, _err_content(f"{type(e).__name__}: {e}"))
        return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def _err_content(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}
