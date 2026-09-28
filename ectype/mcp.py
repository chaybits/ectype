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

from . import __version__, adapters, budget, convert as conv, settings
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
             "limit": {"type": "integer", "description": "how many rows (default 20; 0 = all)"}}}},
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
             "thinking": {"type": "boolean", "description": "include the agent's reasoning (default: Settings, off unless changed; it is large)"},
             "tools": {"type": "boolean", "description": "tool calls and results at all (default: Settings)"},
             "env": {"type": "boolean", "description": "injected system/environment messages (default: Settings)"},
             "notices": {"type": "boolean", "description": "include the agent's own notices where the store keeps them: API errors, refusals, away summaries (default: Settings)"},
             "questions": {"type": "boolean", "description": "a question the agent put to the user, and the answer, as turns rather than tool traffic (default: Settings, on)"},
             "peers": {"type": "boolean", "description": "messages other sessions sent in, each under [PEER] (default: Settings, on)"},
             "start": {"type": "integer", "description": "first message index to include"},
             "end": {"type": "integer", "description": "last message index to include"}}}},
        {"name": "session_budget",
         "description": "What a session costs in tokens, element by element (your messages, assistant "
                        "text, thinking, tool calls, tool results), and what the whole thing would cost "
                        "with nothing left out. Call this before show_session on a large session.",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {
             "id": {"type": "string"}, "agent": {"type": "string"}, "project": {"type": "string"},
             "mode": _MODE, "cap": {"type": "integer"}, "thinking": {"type": "boolean"}, "tools": {"type": "boolean"},
             "env": {"type": "boolean"}, "notices": {"type": "boolean"}, "questions": {"type": "boolean"}, "peers": {"type": "boolean"},
             "start": {"type": "integer", "description": "first message index to price"},
             "end": {"type": "integer", "description": "last message index to price"}}}},
        {"name": "summarize_session",
         "description": "What happened in a session, extracted from the transcript with no model: "
                        "every closing summary the agent wrote between markers (Settings → Summary), "
                        "first, then what was asked, what the user answered to the agent's questions, "
                        "which tools ran and how often, which files were touched, which commands ran, "
                        "what failed, plus a keyword line built for grep. Far cheaper than reading the "
                        "session, and enough to decide whether to.",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {
             "id": {"type": "string"}, "agent": {"type": "string"}, "project": {"type": "string"}}}},
        {"name": "search_sessions",
         "description": "Regex search over the summaries ectype keeps of past sessions (every agent), "
                        "for 'have I dealt with this before?'. It first summarises every session that is "
                        "new or changed since the last look (the rest are only fingerprinted), then "
                        "returns the matching sessions with the matching lines and says how much of the "
                        "stores it covered.",
         "inputSchema": {"type": "object", "required": ["pattern"], "properties": {
             "pattern": {"type": "string", "description": "a regular expression, case-insensitive"},
             "agent": {"type": "string", "description": "restrict to one agent"}}}},
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
                 "workspace": {"type": "string", "description": "the working directory the copy belongs to"},
                 "notice": {"type": "boolean", "description": "append the import notice (default: Settings → Import notice)"}}}})
    return t


def _ref(a: dict):
    """The one session an id names, as `ectype show` resolves it."""
    from .cli import _resolve
    return _resolve(a["id"], a.get("agent"), a.get("project"))


def _opts(a: dict) -> dict:
    # A missing cap means the cap in Settings → View, as on the command line and in the web UI.
    # It used to mean 0, "no cap", so show_session with no arguments (mode defaults to custom)
    # returned the whole session: 307,897 tokens on the benchmark against 68,849 with the setting.
    # The presentation defaults (mode, merge, timestamps, markers, tool headers) come from Settings ->
    # View too, as on the command line and in the web UI: one default per setting (ARCHITECTURE D14).
    view = settings.load()["view"]
    cap = view["cap"] if a.get("cap") is None else a.get("cap")
    return {"mode": a.get("mode") or view["mode"], "cap": cap,
            "thinking": view["thinking"] if a.get("thinking") is None else bool(a.get("thinking")),
            "tools": view["tools"] if a.get("tools") is None else bool(a.get("tools")),
            "env": view["env"] if a.get("env") is None else bool(a.get("env")),
            "notices": view["notices"] if a.get("notices") is None else bool(a.get("notices")),
            "questions": view["questions"] if a.get("questions") is None else bool(a.get("questions")),
            "peers": view["peers"] if a.get("peers") is None else bool(a.get("peers")),
            "collapse": view["collapse"], "stamps": view["stamps"], "markers": view["markers"], "tool_headers": view["tool_headers"],
            "start": a.get("start"), "end": a.get("end")}


def _call(name: str, a: dict, allow_write: bool) -> str:
    # Names are checked before anything is resolved. Without this an unknown tool reached the id
    # lookup first and came back as a bare KeyError('id'), which tells the agent nothing.
    # only what THIS server serves: naming install_session to a read-only server told the agent it
    # exists and how to enable it, which is what hiding it was for
    known = {t["name"] for t in _tools(allow_write)}
    if name not in known:
        raise ValueError(f"unknown tool {name!r}; this server has: {', '.join(sorted(known))}")

    if name == "list_agents":
        rows = ["agent         type    sessions  writable  store"]
        for n, ad in adapters.ADAPTERS.items():
            if not ad.available():
                continue
            try:
                count = f"{len(ad.discover()):>8}"
            except Exception as e:                       # noqa: BLE001, one unreadable store is a row that says so
                count = f"{'?':>8}  (could not be read: {type(e).__name__}: {e})"
            rows.append(f"{n:<13} {ad.category:<7} {count}  {'yes' if ad.writable else 'no':<8}  {ad.home()}")
        return "\n".join(rows) if len(rows) > 1 else "no agent stores found on this machine"

    if name == "list_sessions":
        agent = a.get("agent")
        if agent and agent not in adapters.ADAPTERS:
            raise ValueError(f"unknown agent {agent!r}; known: {', '.join(adapters.ADAPTERS)}")
        skipped: list = []
        refs = adapters.all_refs([agent] if agent else None, skipped=skipped)
        q = (a.get("query") or "").lower()
        if q:
            refs = [r for r in refs if q in (r.title or "").lower() or q in r.id or q in (r.project or "").lower() or q in r.agent]
        limit = 20 if a.get("limit") is None else int(a.get("limit"))
        if limit < 0:
            raise ValueError("limit must be 0 (all) or more")
        refs = refs[:limit] if limit else refs
        from .cli import _size
        rows = [f"{'agent':<13} {'id':<10} {'modified':<17} {'size':>8}  {'project':<12} title"]
        for r in refs:
            rows.append(f"{r.agent:<13} {r.short:<10} {r.mtime.astimezone():%Y-%m-%d %H:%M}  {_size(r.size):>8}  "
                        f"{(r.project or '-')[:12]:<12} {(r.title or '')[:70]}")
        # the model reads this answer, not the server's stderr: an unreadable store is said here
        tail = [f"(not listed: {n}: {why})" for n, why in skipped]
        return "\n".join((rows if refs else ["no sessions match"]) + tail)

    if name == "search_sessions":
        from . import ledger
        agent = a.get("agent")
        if agent and agent not in adapters.ADAPTERS:
            raise ValueError(f"unknown agent {agent!r}; known: {', '.join(adapters.ADAPTERS)}")
        pattern = a.get("pattern") or ""
        if not pattern:
            raise ValueError("search_sessions needs a pattern")
        return ledger.render_search(ledger.search(pattern, [agent] if agent else None), pattern)

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
        extra = [(src, rel) for src, rel in ad.artifacts(ref) if src != s.path] if native else None
        # the session goes into the store; the scratch directory is only what the writer wants as
        # an out_dir, and it is removed here rather than left behind once per call
        from . import notice as _notice
        cfg = settings.load()["notice"]
        want = cfg["enabled"] if a.get("notice") is None else bool(a.get("notice"))
        # the same import notice the CLI and the GUI append by default (D14; it was never passed here)
        text = _notice.build(s, cfg, ad.label) if want else None
        with tempfile.TemporaryDirectory(prefix="ectype-mcp-") as td:
            path, rep = conv.convert(s, target, Path(td), install=True, source_path=s.path,
                                     workspace=a.get("workspace"), extra=extra, notice=text)
        return f"installed as {target}: {path}\n\n{rep.render()}"

    raise ValueError(f"unknown tool {name!r}")


def serve(allow_write: bool = False) -> int:
    """Read JSON-RPC lines from stdin until it closes. One message per line, both ways."""
    out, sys.stdout = sys.stdout, sys.stderr        # the protocol owns the real stdout
    # bytes, decoded line by line as UTF-8 (what MCP's stdio transport is): the text stream used the
    # locale's encoding (cp1252 on Windows: Turkish arguments arrived garbled, `Á` killed the server)
    # and one invalid byte anywhere ended the session, with the replies to earlier lines
    src = getattr(sys.stdin, "buffer", None)
    try:
        for raw in (src if src is not None else sys.stdin):
            try:
                line = raw.decode("utf-8").strip() if isinstance(raw, bytes) else raw.strip()
            except UnicodeDecodeError:
                _send(out, _error(None, -32700, "not UTF-8"))
                continue
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _send(out, _error(None, -32700, "not JSON"))
                continue
            if isinstance(msg, list):
                _send(out, _error(None, -32600, "batches are not supported: send one request per line"))
                continue
            if not isinstance(msg, dict):
                _send(out, _error(None, -32600, "a request must be a JSON object"))
                continue
            try:
                reply = _handle(msg, allow_write)
            except Exception as e:                   # noqa: BLE001, no single line may end the session
                reply = _error(msg.get("id"), -32603, f"{type(e).__name__}: {e}")
            if reply is not None:                    # a notification gets no answer, by spec
                _send(out, reply)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        sys.stdout = out
    return 0


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _send(out, obj: dict) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def _handle(msg: dict, allow_write: bool) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if mid is None and method:                       # notification
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return _ok(mid, {"protocolVersion": want or PROTOCOL,
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "ectype", "version": __version__}})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": _tools(allow_write)})
    if method == "tools/call":
        p = params
        args = p.get("arguments") if p.get("arguments") is not None else {}
        if not isinstance(args, dict):
            return _ok(mid, _err_content("arguments must be an object"))
        try:
            text = _call(p.get("name") or "", args, allow_write)
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
