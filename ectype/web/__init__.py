"""Local web UI, stdlib only. `ectype gui` serves it on 127.0.0.1 and opens the browser.

Endpoints (JSON unless noted):
  GET  /                               the page (read from disk on every request: edit and reload)
  GET  /api/agents                     every adapter: label, category, detected, enabled, store path
                                       and which layer chose it (env / settings / default), session count
  GET  /api/sessions                   SessionRefs of the ENABLED agents, newest first
  GET  /api/settings                   settings + config-file path + the option catalogues the page needs
  POST /api/settings {settings}        validate, save, apply
  GET  /api/convert-matrix?source=&target=
                                       what that PAIR carries / folds / drops (same agent = a copy)
  POST /api/rename   {agent,id[,path],title} -> {where}: "native" when the agent's own store
                                       took the name (Claude Code writes custom-title.json),
                                       "local" when ectype kept it in titles.json; empty title
                                       restores the generated one
  POST /api/notice   {agent,id[,path][,notice]} -> {text}: the import notice with this session's real values
  POST /api/render   {agent,id[,path],opts}   -> {text, budget, redaction, notice, notice_text}
  POST /api/export   {agent,id[,path],opts,name,format[,convert][,mode][,workspace]}
                                       `path` (the list row's) picks the file when one id exists under two
                                       project folders
                                       format "native": this agent's own resumable format (a copy)
                                       mode "download" (default from settings): the file itself, with
                                       Content-Disposition: attachment, so the browser offers to save it
                                       mode "folder": written server-side -> {path, bytes}
                                       mode "install": written into the target agent's live store
                                       convert = target agent: the conversion instead of a format; the
                                       fidelity report rides in X-Fidelity (download) or "report" (else)
                                       workspace = the cwd the installed copy belongs to
                                       convert + a generic format: the conversation folded exactly as
                                       the conversion folds it, written as text/markdown/… (X-Folded-For)
opts: mode (brief|custom|full), thinking, tools, env, hide_roles[], start, end, collapse,
      stamps, wrap, tool_headers, markers, redact, redact_rules{}, redact_terms[], notice
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
import threading
import webbrowser
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import adapters, budget, convert as conv, notice, settings
from ..model import json_default as _default
from ..render import formats
from ..render.text import footer, render
from ..transform import RULES, Redactor


def _html_bytes() -> bytes:
    """The packaged page, read through importlib.resources rather than a path beside `__file__`:
    inside a zipapp there is no such path, and the single-file build has to serve the UI too."""
    from importlib.resources import files
    return (files(__package__) / "index.html").read_bytes()


# Bounded: the GUI is long-lived and browsing a store touches an unbounded number of sessions,
# each of which is a fully parsed transcript. Oldest-first eviction; a session in view is
# re-requested on every render, so it stays at the hot end.
_CACHE: "OrderedDict[tuple[str, str], object]" = OrderedDict()
_CACHE_MAX = 8
_LOCK = threading.Lock()
DEFAULT_EXPORT_DIR: Path | None = None       # from `gui --export-dir`; settings.export.folder wins when set
_UNSAFE = re.compile(r"[^\w.\- ()]+")        # file names: word chars (any script), dot, dash, space, parens


def export_dir() -> Path:
    """settings.export.folder > `gui --export-dir` > ~/ectype-exports."""
    folder = settings.load()["export"]["folder"]
    if folder:
        return Path(os.path.expanduser(os.path.expandvars(folder)))
    if DEFAULT_EXPORT_DIR:
        return DEFAULT_EXPORT_DIR
    return Path.home() / "ectype-exports"


def _load(agent: str, sid: str, path: str | None = None):
    """(Session, SessionRef); the ref is kept because exports need the session's sidecar files.

    Cached per file, keyed on the file's mtime and size, so a session that is still being
    written (the agent is running) is re-read when it grows instead of showing a stale copy.
    `path` picks the file when the same id exists under two project folders (Claude Code writes a
    session resumed from another directory under that directory's slug, same id): without it the
    first folder's file answered for both list rows."""
    key = (agent, sid, path)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit:
            _CACHE.move_to_end(key)
    if hit:
        s, ref, stamp = hit
        if _stamp(ref.path) in (stamp, None):     # unchanged, or gone (a store re-pointed elsewhere): serve the copy
            return s, ref
    ref = _ref(agent, sid, path)
    s = adapters.load(ref)
    # the file changed (or is being written): its cached ceiling describes the old content
    budget.invalidate(agent, ref.id, str(ref.path))
    with _LOCK:
        _CACHE[key] = (s, ref, _stamp(ref.path))
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return s, ref


def _ref(agent: str, sid: str, path: str | None = None):
    """The SessionRef that (agent, id[, path]) names, without parsing the transcript.

    One discover(): the exact id first, then the same list by prefix. Calling `find()` as a
    fallback re-enumerated the whole store; on Claude Code that is a partial read of every session
    file, twice, for one cache miss."""
    ad = adapters.get(agent)
    found = ad.discover()
    refs = [r for r in found if r.id == sid] or [r for r in found if r.id.startswith(sid)] or ad.find(sid)
    if path:
        refs = [r for r in refs if str(r.path) == path] or refs
    if not refs:
        raise KeyError(f"{agent}:{sid}")
    return refs[0]


def _forget(agent: str, sid: str) -> None:
    """Drop the cached session(s) for one id. A rename changes the title without touching the
    transcript, and the cache is keyed on the transcript's mtime and size, so nothing else would
    tell it to re-read."""
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == agent and k[1] == sid]:
            del _CACHE[k]


def _stamp(p: Path):
    """(mtime, size) of the session file, or None when it is not there."""
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


class Prepared:
    """Everything one request needs, computed once."""

    def __init__(self, agent: str, sid: str, o: dict, path: str | None = None):
        cfg = settings.load()
        # the cap lives in Settings now; it only bites in `custom` mode
        o = {**o, "cap": cfg["view"]["cap"] if o.get("cap") is None else o.get("cap"),
             # merging back-to-back turns is a filter (it changes the message list), so it must be
             # decided before the view is built, like the cap
             "collapse": cfg["view"]["collapse"] if o.get("collapse") is None else bool(o.get("collapse"))}
        self.opts = o
        self.source, self.ref = _load(agent, sid, path)
        self.view = budget.filtered(self.source, o)
        self.red = None
        if o.get("redact"):
            rc = cfg["redact"]
            rules = {k: rc[k] for k in settings.REDACT_RULES}
            for k, v in (o.get("redact_rules") or {}).items():       # per-render override of Settings
                if k in rules:
                    rules[k] = bool(v)
            self.red = Redactor.defaults(extra=[*rc["terms"], *(o.get("redact_terms") or [])], options=rules)
            self.view = self.red.session(self.view)
        self.notice_text = None
        if o.get("notice"):
            self.notice_text = notice.build(self.source, cfg["notice"], adapters.get(agent).label)
            if self.red:
                self.notice_text = self.red.text(self.notice_text)
        # "hide timezone" is a redaction RULE, so the toolbar's per-render override decides it,
        # exactly like the others. Reading only the saved setting made the toolbar toggle do
        # nothing at all. It cannot be a text substitution: timestamps are formatted from datetime
        # objects at render time, so no pattern ever sees them; the render must print UTC instead.
        # "hide timezone" now means exactly that: the offset is not printed. It does NOT shift the
        # clock to UTC: that only moved every time by three hours without hiding anything more,
        # because a bare timestamp already carries no location. `--utc` stays a separate, explicit
        # choice for people who actually want UTC.
        tz = (o.get("redact_rules") or {}).get("timezone")
        if tz is None:
            tz = cfg["redact"]["timezone"]
        hide_tz = bool(o.get("redact")) and bool(tz)
        o = {**o,   # the request decides; Settings only supplies what it did not send
             "tool_headers": o.get("tool_headers", cfg["view"]["tool_headers"]),
             "stamps": o.get("stamps") or cfg["view"]["stamps"],
             "markers": o.get("markers", cfg["view"]["markers"]),
             "wrap": cfg["view"]["wrap"] if o.get("wrap") is None else o.get("wrap")}
        self.opts = o
        self.ro = budget.render_options(o, local_time=not bool(o.get("utc")), show_offset=not hide_tz)

    def with_notice(self):
        """The view plus the import notice as its last message (for renders and format exports)."""
        if not self.notice_text:
            return self.view
        out = dataclasses.replace(self.view)
        m = notice.message(self.view, {}, None)
        m.blocks[0].text = self.notice_text
        out.messages = list(self.view.messages) + [m]
        return out

    def redaction(self):
        return {"report": self.red.report(), "details": self.red.details()} if self.red else None


def agent_info() -> list[dict]:
    out = []
    for name, a in adapters.ADAPTERS.items():
        ok = a.available()
        count = error = None
        if ok:
            try:
                count = len(a.discover())
            except Exception as e:                  # noqa: BLE001, a broken store must not hide the others, but it must say so
                error = f"{type(e).__name__}: {e}"
        out.append({"name": name, "label": a.label, "category": a.category, "available": ok,
                    "enabled": a.enabled(), "home": str(a.home()), "home_source": a.home_source(),
                    "default_home": a.default_home, "env_home": a.env_home, "sessions": count, "error": error,
                    "writable": a.writable, "native_format": a.native_format})
    return out


def workspaces() -> dict[str, list[str]]:
    """Per writable agent, the directories it has sessions for: where an installed export lands."""
    out: dict[str, list[str]] = {}
    for name, a in adapters.ADAPTERS.items():
        if not a.writable or not a.available():
            continue
        try:
            out[name] = a.workspaces()
        except Exception:                       # noqa: BLE001, a broken store must not break Settings
            out[name] = []
    return out


def catalogues() -> dict:
    return {"formats": list(formats.FORMATS),
            "targets": {k: v["label"] for k, v in conv.TARGETS.items()},
            "agents": [{"name": n, "label": a.label, "category": a.category, "writable": a.writable,
                        "native_format": a.native_format} for n, a in adapters.ADAPTERS.items()],
            "categories": [{"key": k, "label": v} for k, v in adapters.CATEGORIES.items()],
            "modes": [{"key": k, "label": v} for k, v in budget.MODES.items()],
            "workspaces": workspaces(),
            "rules": [{"name": k, "label": v[0]} for k, v in RULES.items()],
            "notice_items": [{"key": k, "label": v[0]} for k, v in notice.ITEMS.items()],
            "columns": list(settings.TABLE_COLUMNS),
            "list_columns": list(settings.LIST_COLUMNS),
            "platform": "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")}


def _export_name(raw: str | None, s, fmt: str) -> str:
    base = _UNSAFE.sub("_", (raw or "").strip()).strip(" .") or f"{s.agent}_{s.id[:8]}"
    ext = formats.extension(fmt)
    return base if base.lower().endswith("." + ext) else f"{base}.{ext}"


_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback(hostport: str | None) -> bool:
    """True for 127.0.0.1 / localhost / ::1, with or without a port."""
    if not hostport:
        return False
    h = hostport.strip()
    if "//" in h:                                  # an Origin: strip the scheme
        h = h.split("//", 1)[1]
    if h.startswith("["):                          # [::1]:8765
        h = h[:h.index("]") + 1] if "]" in h else h
    elif ":" in h:
        h = h.rsplit(":", 1)[0]
    return h.lower() in _LOOPBACK


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # quiet
        pass

    def _guard(self) -> bool:
        """Refuse requests that did not come from this page.

        The server binds 127.0.0.1, which stops other MACHINES but not other PAGES: any site open
        in the same browser can post to it, and the reply is unreadable cross-origin but the
        SIDE EFFECT still happens: settings rewritten, a file written into the export folder, a
        session installed into an agent's store. Two cheap checks close that:

          Host   must be loopback. A page that resolves its own domain to 127.0.0.1 (DNS
                 rebinding) sends `Host: evil.example`, and that is then same-origin for the
                 browser, so it could also READ every transcript. This is the check that stops it.
          Origin must be loopback WHEN PRESENT. Browsers always send it on POST, so a cross-site
                 post is rejected; curl and scripts send none and keep working.
        """
        if not _is_loopback(self.headers.get("Host")):
            return False
        origin = self.headers.get("Origin")
        return origin is None or _is_loopback(origin)

    def _refuse(self) -> None:
        self._json({"error": "request refused: ectype only answers same-origin requests from "
                             "http://127.0.0.1 (see the Host/Origin guard in ectype/web/__init__.py)"}, 403)

    def _send(self, data: bytes, ctype: str, code: int = 200, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False, default=_default).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _file(self, data: bytes, name: str, mime: str, extra: dict | None = None) -> None:
        ascii_name = name.encode("ascii", "ignore").decode() or "export"
        self._send(data, mime, extra={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
            "X-Export-Bytes": str(len(data)), **(extra or {})})

    def do_GET(self):
        if not self._guard():
            self._refuse()
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                self._send(_html_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/agents":
                self._json(agent_info())
            elif u.path == "/api/sessions":
                refs = adapters.all_refs(enabled_only=True)
                self._json([{**dataclasses.asdict(r), "short": r.short} for r in refs])
            elif u.path == "/api/settings":
                self._json({"settings": settings.load(), "path": str(settings.config_path()),
                            "load_error": settings.load_error(),
                            "export_dir": str(export_dir()), **catalogues()})
            elif u.path == "/api/convert-matrix":
                # the pair decides: same agent in and out is a copy, so nothing is folded or dropped
                self._json({**conv.matrix(q.get("source"), q.get("target")),
                            "agents": {n: a.label for n, a in adapters.ADAPTERS.items()},
                            "writable": {n: a.writable for n, a in adapters.ADAPTERS.items()}})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:      # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._guard():
            self._refuse()
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/settings":
                data = body.get("settings", body)
                fmt = (data.get("export") or {}).get("format")
                if fmt is not None and fmt not in formats.FORMATS and fmt != "native":
                    raise ValueError(f"export.format must be 'native' or one of {', '.join(formats.FORMATS)}")
                saved = settings.save(data)
                self._json({"settings": saved, "path": str(settings.config_path()), "export_dir": str(export_dir())})
                return
            if self.path == "/api/rename":
                # `where` says which tier took it, because the two behave differently: a native
                # rename shows up in the agent as well, a local one only here.
                ref = _ref(body["agent"], body["id"], body.get("path"))
                where = adapters.rename(ref, body.get("title") or "")
                _forget(body["agent"], body["id"])
                self._json({"where": where, "title": body.get("title") or "",
                            "generated": ref.title if not (body.get("title") or "").strip() else None})
                return
            if self.path == "/api/notice":
                # the notice exactly as it would be appended, with THIS session's values in it
                s, _ = _load(body["agent"], body["id"], body.get("path"))
                cfg = settings.load()["notice"]
                self._json({"text": notice.build(s, {**cfg, **(body.get("notice") or {})},
                                                 adapters.get(body["agent"]).label)})
                return
            o = body.get("opts") or {}
            p = Prepared(body["agent"], body["id"], o, body.get("path"))
            if self.path == "/api/render":
                shown = p.with_notice()
                text = render(shown, p.ro, source=p.source, with_footer=False)
                # p.opts, not the raw request: it carries the cap and the merge flag Settings supplied,
                # and the ceiling must be measured with the same layout as the view
                b = budget.breakdown(p.source, p.opts, shown=shown, rendered=text)
                text += footer(b["selected"])
                self._json({"text": text, "budget": b, "tokens": b["selected"], "exact": b["exact"],
                            "messages": b["messages"], "redaction": p.redaction(),
                            "redacted": p.red.report() if p.red else None, "notice": bool(p.notice_text),
                            # the real wording, with this session's values; the notice popup shows it
                            "notice_text": p.notice_text,
                            "agent_label": adapters.get(body["agent"]).label,
                            "cwd": p.source.cwd, "writable": adapters.get(body["agent"]).writable})
            elif self.path == "/api/export":
                mode = body.get("mode") or settings.load()["export"]["mode"]
                fmt = body.get("format") or settings.load()["export"]["format"]
                target = body.get("convert") or None
                if fmt == "native" and not target:
                    target = p.source.agent          # "export as this agent's own format" = a native copy
                if target and fmt != "native":
                    self._folded(p, target, fmt, mode, body.get("name"))
                    return
                if target:
                    self._convert(p, target, mode, body.get("workspace") or None)
                    return
                if fmt not in formats.FORMATS:
                    raise ValueError(f"unknown format {fmt!r}")
                name = _export_name(body.get("name"), p.view, fmt)
                data = formats.export(p.with_notice(), fmt, p.ro, source=p.source).encode("utf-8")
                if mode == "folder":
                    out = export_dir() / name
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(data)
                    self._json({"path": str(out.resolve()), "bytes": len(data), "mode": "folder",
                                "redacted": p.red.report() if p.red else None})
                else:
                    self._file(data, name, formats.mime(fmt), {"X-Redacted": p.red.report() if p.red else ""})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:      # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _folded(self, p: Prepared, target: str, fmt: str, mode: str, raw_name: str | None) -> None:
        """convert to `target` + a generic format: the conversation folded exactly as a conversion
        to that agent folds it, written as text/markdown/… for feeding in by hand."""
        if fmt not in formats.FORMATS:
            raise ValueError(f"unknown format {fmt!r}")
        ad = adapters.ADAPTERS.get(target)
        if not ad:
            raise ValueError(f"unknown agent {target!r}")
        if mode == "install":
            raise ValueError(f"a {fmt} export cannot be installed into {ad.label}'s store; choose its own format to install")
        rep = conv.FidelityReport(p.source.agent, target)
        s = conv.folded(p.view, target, rep, notice=p.notice_text)
        name = _export_name(raw_name or f"{p.source.agent}_{p.source.id[:8]}_as_{target}", p.view, fmt)
        data = formats.export(s, fmt, p.ro, source=p.source).encode("utf-8")
        report = rep.render() + (f"redacted: {p.red.report()}\n" if p.red else "")
        if mode == "folder":
            out = export_dir() / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            rp = out.with_suffix(out.suffix + ".fidelity.md")
            rp.write_text(report, encoding="utf-8")
            self._json({"path": str(out.resolve()), "bytes": len(data), "mode": "folder", "report": report,
                        "report_path": str(rp), "folded_for": target,
                        "redacted": p.red.report() if p.red else None})
            return
        self._file(data, name, formats.mime(fmt), {"X-Fidelity": quote(report), "X-Folded-For": target,
                                                     "X-Redacted": p.red.report() if p.red else ""})

    def _convert(self, p: Prepared, target: str, mode: str, workspace: str | None = None) -> None:
        """mode: download (a file) · folder (written under the export folder) · install (straight
        into the target agent's own store, ready to resume)."""
        if target not in conv.WRITERS:
            ad = adapters.ADAPTERS.get(target)
            why = (f"{ad.label} is source-only: ectype can read its sessions but cannot write a resumable one"
                   if ad else "unknown agent")
            raise ValueError(f"cannot convert to {target!r}: {why}. Writable targets: {', '.join(conv.WRITERS)}")
        red_fn = p.red.text if p.red else None
        native = target == p.source.agent and target in conv.NATIVE
        # native copies the ORIGINAL file, so it needs the unredacted session (real id, cwd and
        # path); redaction still applies, record by record, inside native_copy.
        s = p.source if native else p.view
        extra = [(src, rel) for src, rel in adapters.get(p.source.agent).artifacts(p.ref)
                 if src != p.source.path] if native else None
        kw = dict(redact=red_fn, notice=p.notice_text, workspace=workspace,
                  source_path=p.source.path, extra=extra)

        if mode == "install":
            with tempfile.TemporaryDirectory(prefix="ectype-install-") as td:
                path, rep = conv.convert(s, target, Path(td), install=True, **kw)
            report = rep.render() + (f"redacted: {p.red.report()}\n" if p.red else "")
            rp = export_dir() / f"{path.stem}.fidelity.md"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(report, encoding="utf-8")          # never leave side files in a live store
            self._json({"path": str(path.resolve()), "bytes": path.stat().st_size, "mode": "install",
                        "report": report, "report_path": str(rp), "native": native,
                        "resume": next((n for n in rep.notes if n.startswith("resume with:")), ""),
                        "target_home": str(adapters.get(target).home())})
            return
        if mode == "folder":
            out_dir = export_dir() / "converted" / target
            path, rep = conv.convert(s, target, out_dir, **kw)
            report = rep.render() + (f"redacted: {p.red.report()}\n" if p.red else "")
            rp = path.with_suffix(path.suffix + ".fidelity.md")
            rp.write_text(report, encoding="utf-8")
            self._json({"path": str(path.resolve()), "bytes": path.stat().st_size, "mode": "folder",
                        "report": report, "report_path": str(rp), "relpath": str(path.relative_to(out_dir)),
                        "native": native, "target_home": str(adapters.get(target).home())})
            return
        with tempfile.TemporaryDirectory(prefix="ectype-convert-") as td:
            path, rep = conv.convert(s, target, Path(td), **kw)
            rel = str(path.relative_to(td))
            sidecars = any("sidecar file(s) copied" in n for n in rep.notes)
            if sidecars:
                # the transcript points at spill files copied beside it; a download of the transcript
                # alone left every one of those a dead pointer while the report said "copied"
                import io, zipfile
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(Path(td).rglob("*")):
                        if f.is_file():
                            z.write(f, f.relative_to(td).as_posix())
                data, name, mime = buf.getvalue(), f"{path.stem}.zip", "application/zip"
            else:
                data, name, mime = path.read_bytes(), path.name, "application/x-ndjson; charset=utf-8"
        report = rep.render() + (f"redacted: {p.red.report()}\n" if p.red else "")
        self._file(data, name, mime, {
            "X-Fidelity": quote(report), "X-Target-Relpath": quote(rel), "X-Native": "1" if native else "0",
            "X-Bundle": "zip" if sidecars else "file",
            "X-Target-Home": quote(str(adapters.get(target).home()))})


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    """Bound but not yet serving; port 0 picks a free one (tests)."""
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int = 8765, open_browser: bool = True, export_dir_default: Path | None = None) -> None:
    global DEFAULT_EXPORT_DIR
    if export_dir_default:
        DEFAULT_EXPORT_DIR = Path(export_dir_default)
    srv = make_server(port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"ectype gui → {url}   settings: {settings.config_path()}   exports: {export_dir()}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
