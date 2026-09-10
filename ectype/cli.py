"""ectype: AI Agent Ectype, command line.

  ectype agents                       which agents are installed, where, how many sessions, shown in the GUI?
  ectype list [-a AGENT] [-n N]       recent sessions across all agents
  ectype show <id-prefix> [opts]      render one session (text)
  ectype json <id-prefix>             dump the canonical model as JSON
  ectype export <id-prefix> [opts]    filtered + redacted export: text, markdown, html, json, jsonl, csv (+ import notice)
                                    --mode brief|custom|full · --tool-headers · --hide user|assistant
  ectype fixture <id-prefix> -o DIR   copy the agent's RAW files, redacted, as a test fixture
  ectype gui [--port N] [--no-open]   local web UI: browse, toggle, live token budget, export, settings
  ectype settings [--path]            print the settings file (where store paths and GUI defaults live)
ectype backup <id> | --list | --restore NAME
                                    copies of a session's files, and of anything an install would disturb
ectype summarize <id-prefix>        what happened in it: asks, tools, files, commands, errors, keywords
ectype mcp [--allow-write]          MCP server on stdin/stdout: an agent reads its own past sessions
  ectype convert <id-prefix> [--to AGENT] [--template FILE] [--workspace DIR] [-o DIR | --install]
                                    rewrite the conversation in another agent's format (+ fidelity report).
                                    Without --to: a NATIVE copy in the session's own format; nothing is
                                    folded or dropped, because the file already is that format.
"""
from __future__ import annotations

import argparse
import os
import dataclasses
import json
import sys
from datetime import datetime
from pathlib import Path

from . import adapters, budget, notice, settings, convert as conv
from .render import formats
from .render.text import render
from .model import json_default
from .transform import Redactor
from .convert import NATIVE, convert


def _size(n: int) -> str:
    """Human file size. Never a bare 'K': the list column was read as tokens once."""
    return f"{n} B" if n < 1024 else f"{n / 1024:,.0f} KB" if n < 1048576 else f"{n / 1048576:,.1f} MB"


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _agent(name: str | None) -> str | None:
    """Validate `-a`. Without this a typo just filters everything out, and an empty listing looks
    exactly like a store with no sessions in it."""
    if name is not None and name not in adapters.ADAPTERS:
        sys.exit(f"ectype: unknown agent {name!r}; known: {', '.join(adapters.ADAPTERS)}")
    return name


def _resolve(prefix: str, agent: str | None, project: str | None = None):
    """The one SessionRef an id prefix names.

    Claude Code writes a session resumed from another directory under that directory's slug with
    the SAME id, so a full id can name two files. `--project` (a substring of the file's path)
    picks one; byte-identical copies resolve to the newest by themselves; anything else is listed
    with its path, because "use a longer prefix" cannot help when the ids are equal."""
    agent = _agent(agent)
    refs = [r for r in adapters.all_refs([agent] if agent else None) if r.id.startswith(prefix)]
    if project:
        refs = [r for r in refs if project in str(r.path)]
    if not refs:
        sys.exit(f"no session starts with {prefix!r}" + (f" for agent {agent}" if agent else "") + (f" under a path containing {project!r}" if project else ""))
    if len(refs) > 1 and len({r.id for r in refs}) == 1:
        import filecmp
        if all(filecmp.cmp(refs[0].path, r.path, shallow=False) for r in refs[1:]):
            newest = max(refs, key=lambda r: r.mtime)
            print(f"ectype: {len(refs)} identical copies of {refs[0].id} under different project folders; using {newest.path.parent.name}", file=sys.stderr)
            return newest
    if len(refs) > 1:
        same_id = len({r.id for r in refs}) == 1
        for r in refs:
            print(f"  {r.agent:<12} {r.id}  {_local(r.mtime)}  {r.path}")
        sys.exit(f"{len(refs)} sessions match {prefix!r}; " + ("they share one id with different content; pick one with --project <part of its path>"
                                                              if same_id else "use a longer prefix or -a AGENT"))
    return refs[0]


def cmd_agents(_a):
    print(f"{'agent':<13}{'label':<21}{'type':<8}{'found':<7}{'sessions':>8}  {'gui':<5} store (source)")
    for name, ad in adapters.ADAPTERS.items():
        ok = ad.available()
        n = len(ad.discover()) if ok else 0
        print(f"{name:<13}{ad.label:<21}{ad.category:<8}{'yes' if ok else 'no':<7}{n:>8}  {'on' if ad.enabled() else 'off':<5}"
              f"{ad.home()} ({ad.home_source()}; override with ${ad.env_home} or Settings)")


def cmd_list(a):
    agent = _agent(a.agent)
    refs = adapters.all_refs([agent] if agent else None)[: a.n]
    print(f"{'agent':<12} {'id':<10} {'modified':<16} {'size':>8}  {'project':<14} title")
    for r in refs:
        print(f"{r.agent:<12} {r.short:<10} {_local(r.mtime):<16} {_size(r.size):>8}  {(r.project or '-')[:14]:<14} {(r.title or '')[:60]}")


def cmd_show(a):
    """`export --format text` with the Settings defaults and no redaction or notice: one pipeline,
    so what `show` prints is exactly what an export would contain."""
    ref = _resolve(a.id, a.agent, a.project)
    s0 = adapters.load(ref)
    view = settings.load()["view"]
    opts = {"thinking": a.thinking, "tools": not a.no_tools, "env": a.env, "mode": "custom",
            "cap": view["cap"] if a.cap is None else a.cap, "collapse": view["collapse"],
            "stamps": view["stamps"], "markers": view["markers"], "tool_headers": view["tool_headers"]}
    s = budget.filtered(s0, opts)
    out = render(s, budget.render_options(opts, local_time=not a.utc), source=s0)
    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
        print(f"wrote {a.output} ({len(out):,} chars)")
    else:
        sys.stdout.write(out)


def cmd_json(a):
    ref = _resolve(a.id, a.agent, a.project)
    s = adapters.load(ref)
    out = json.dumps(dataclasses.asdict(s), ensure_ascii=False, indent=1, default=json_default)
    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
        print(f"wrote {a.output} ({len(out):,} chars)")
    else:
        sys.stdout.write(out + "\n")


def _redactor(a):
    """Redactor from Settings → Redaction, plus the command line's --redact-term / --redact-off."""
    if not getattr(a, "redact", False):
        return None
    rc = settings.load()["redact"]
    options = {k: rc[k] for k in settings.REDACT_RULES}
    for name in getattr(a, "redact_off", None) or []:
        if name not in options:
            sys.exit(f"unknown redaction rule {name!r}; rules: {', '.join(options)}")
        options[name] = False
    return Redactor.defaults(extra=[*rc["terms"], *(a.redact_term or [])], machine=not a.no_machine_rules, options=options)


def _notice_text(a, s) -> str | None:
    """The import notice, if the flags/settings ask for it."""
    cfg = settings.load()["notice"]
    want = getattr(a, "notice", None)
    if want is None:
        want = cfg["enabled"]
    return notice.build(s, cfg, adapters.get(s.agent).label) if want else None


def cmd_export(a):
    ref = _resolve(a.id, a.agent, a.project)
    s0 = adapters.load(ref)
    view = settings.load()["view"]
    opts = {"thinking": a.thinking, "tools": not a.no_tools, "env": a.env, "mode": a.mode,
            "cap": view["cap"] if a.cap is None else a.cap,                   # custom mode caps at the Settings value unless told otherwise
            "collapse": view["collapse"] if a.collapse is None else a.collapse,
            "tool_headers": a.tool_headers, "stamps": a.stamps, "markers": a.markers, "wrap": a.wrap, "hide_roles": a.hide or [], "start": a.start, "end": a.end}
    s = budget.filtered(s0, opts)
    red = _redactor(a)
    if red:
        s = red.session(s)
    text = _notice_text(a, s0)
    if text and red:
        text = red.text(text)
    rep = None
    if a.convert:
        rep = conv.FidelityReport(s0.agent, a.convert)
        # the notice becomes the last user turn, as in a conversion; the leading import BANNER
        # already tells the reader it was folded and how, so nothing says it in the header
        s = conv.folded(s, a.convert, rep, notice=text)
    elif text:
        s = notice.attach(s, {}, None)
        s.messages[-1].blocks[0].text = text
    # --utc converts the clock; the redaction rule only stops the offset being printed
    hide_tz = red is not None and settings.load()["redact"]["timezone"]
    out = formats.export(s, a.format, budget.render_options(opts, local_time=not a.utc, show_offset=not hide_tz),
                         source=s0)
    if a.output:
        path = Path(a.output)
        if path.is_dir():
            path = path / f"{s.agent}_{s.id[:8]}.{formats.extension(a.format)}"
        path.write_text(out, encoding="utf-8")
        print(f"wrote {path} ({len(out):,} chars)" + (f"  redacted: {red.report()}" if red else ""))
        if rep:
            print(rep.render())
    else:
        sys.stdout.write(out)
        if red:
            print(f"\n[redacted: {red.report()}]", file=sys.stderr)
        if rep:
            print(rep.render(), file=sys.stderr)


def cmd_fixture(a):
    ref = _resolve(a.id, a.agent, a.project)
    ad = adapters.get(ref.agent)
    red = Redactor.defaults(extra=a.redact_term or [], machine=not a.no_machine_rules)
    dest = Path(a.output)          # mirrors the agent's home layout → loadable via $<ENV_HOME>
    total = 0
    files = ad.artifacts(ref)
    for src, rel in files:
        # the RELATIVE path is redacted too: Claude Code's project folder is a slug of the cwd, and
        # a fixture named -run-media-<user>-… would leak the username in a directory name
        total += red.file(src, dest / red.text(rel))
    (dest / f"FIXTURE_{ref.short}.md").write_text(
        f"# Fixture: {ad.label} session {ref.id}\n\n"
        f"Raw files of the agent, copied with string-level redaction only (format untouched).\n"
        f"Source: `{red.text(str(ref.path))}`\nFiles: {len(files)}\nRedaction: {red.report()}\n"
        f"Layout mirrors `{red.text(str(ad.home()))}`; load with `{ad.env_home}=<this directory> ectype list -a {ad.name}`\n", encoding="utf-8")
    print(f"{ad.label}: {len(files)} files, {total / 1024:,.0f} KB -> {dest}")
    print(f"redacted: {red.report()}")


def cmd_convert(a):
    ref = _resolve(a.id, a.agent, a.project)
    ad = adapters.get(ref.agent)
    source = adapters.load(ref)
    red = _redactor(a)
    target = a.to or ref.agent                   # no --to: this agent's own format, i.e. a native copy
    native = target == ref.agent and target in NATIVE
    # a native copy reads the ORIGINAL file, so it must keep the real id, cwd and path; redaction
    # is applied to the records as they are copied instead of to the model.
    s = source if native else (red.session(source) if red else source)
    out_dir = Path(a.output or "ectype-converted")
    extra = [(src, rel) for src, rel in ad.artifacts(ref) if src != source.path] if native else None
    if a.install and a.backup:
        from . import backup as bk
        risky = bk.at_risk(target, adapters.get(target).home())
        folder = bk.save(risky, f"install-into-{target}", agent=target)
        if folder:
            print(f"backed up {len(risky)} store file(s) first: {folder}")
    template = Path(a.template) if a.template else None
    if a.mint_template:
        if target == ref.agent:
            sys.exit("--mint-template is for a cross-agent conversion: a copy of this agent's own session needs no envelope")
        from .agentcli import AgentUnavailable, mint_template
        try:
            template = mint_template(target, a.workspace)
        except (AgentUnavailable, OSError) as e:
            sys.exit(f"could not mint a template: {e}")
        print(f"minted a template from a fresh {target} session: {template}")
    path, rep = convert(s, target, out_dir, template=template,
                        banner=not a.no_banner, install=a.install, redact=red.text if red else None,
                        notice=_notice_text(a, source), source_path=source.path,
                        workspace=a.workspace, extra=extra)
    report = rep.render() + (f"redacted: {red.report()}\n" if red else "")
    # never leave side files inside a live agent store: with --install the report goes to out_dir
    rp = (out_dir / f"{path.stem}.fidelity.md") if a.install else path.with_suffix(path.suffix + ".fidelity.md")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report, encoding="utf-8")
    print(f"wrote {path}")
    print(f"fidelity report: {rp}")
    print(report, end="")
    if a.verify:
        if not a.install:
            sys.exit("--verify needs --install: an agent can only resume a session that is in its own store")
        from .agentcli import AgentUnavailable, verify_resume
        try:
            ok, detail = verify_resume(target, path.stem, a.workspace or s.cwd)
        except (AgentUnavailable, OSError) as e:
            sys.exit(f"could not verify: {e}")
        print(f"verify: {'RESUMED' if ok else 'DID NOT RESUME'} ({detail})")
        if not ok:
            sys.exit(1)


def cmd_gui(a):
    from .web import serve
    serve(port=a.port, open_browser=not a.no_open, export_dir_default=Path(a.export_dir) if a.export_dir else None)


def cmd_backup(a):
    from . import backup as bk
    if a.list:
        rows = bk.listing()
        if not rows:
            print(f"no backups under {bk.root()}")
            return
        print(f"{'name':<44}{'files':>6}  why")
        for r in rows:
            print(f"{r['name']:<44}{len(r['files']):>6}  {r['why']}")
        return
    if a.restore:
        done = bk.restore(a.restore, dry_run=a.dry_run)
        for dest, ok in done:
            print(("would restore " if a.dry_run else "restored ") + str(dest) if ok else f"MISSING in backup: {dest}")
        if a.dry_run:
            print("nothing was written (--dry-run); run it again without --dry-run to put these back")
        return
    if not a.id:
        sys.exit("ectype backup: give a session id, or --list, or --restore NAME")
    ref = _resolve(a.id, a.agent, a.project)
    ad = adapters.get(ref.agent)
    folder = bk.save([src for src, _ in ad.artifacts(ref)], f"{ref.agent}-{ref.id[:8]}", agent=ref.agent)
    if folder is None:
        sys.exit(f"nothing to back up: no file of {ref.id[:8]} is on disk")
    n = len(json.loads((folder / bk.MANIFEST).read_text(encoding="utf-8"))["files"])
    print(f"backed up {n} file(s) to {folder}")
    print(f"put them back with: ectype backup --restore {folder.name}")


def cmd_summarize(a):
    from .summary import summarize
    ref = _resolve(a.id, a.agent, a.project)
    s = adapters.load(ref)
    view = budget.filtered(s, {"mode": "full", "cap": 0, "thinking": False, "tools": True,
                               "env": False, "start": a.start, "end": a.end}) if (a.start or a.end) else s
    text = summarize(view if (a.start or a.end) else s)
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
        print(f"wrote {a.output}")
    else:
        print(text, end="")


def cmd_mcp(a):
    from .mcp import serve
    sys.exit(serve(allow_write=a.allow_write))


def cmd_settings(a):
    p = settings.config_path()
    if a.path:
        print(p)
        return
    print(f"# {p}{'' if p.exists() else '  (not created yet, all defaults)'}")
    print(json.dumps(settings.load(), ensure_ascii=False, indent=2))


def _add_redact_args(sp):
    sp.add_argument("--redact", action="store_true", help="apply the redaction rules from Settings (home/user/host/e-mails/keys …)")
    sp.add_argument("--redact-term", action="append", metavar="TEXT[=REPL]", help="extra literal to redact (repeatable)")
    sp.add_argument("--redact-off", action="append", metavar="RULE", help="switch one rule off for this run (home, media, username, hostname, email, keys, ip)")
    sp.add_argument("--no-machine-rules", action="store_true", help="skip home/user/hostname rules")


def _add_notice_args(sp):
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--notice", dest="notice", action="store_true", default=None, help="append the import notice (Settings → Import notice) as the last message")
    g.add_argument("--no-notice", dest="notice", action="store_false", help="never append it")


def main(argv=None):
    if os.name == "nt":                       # cp1252 consoles choke on ▸ ● ↳ ⚑
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    p = argparse.ArgumentParser(prog="ectype", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agents").set_defaults(fn=cmd_agents)
    ls = sub.add_parser("list"); ls.add_argument("-a", "--agent"); ls.add_argument("-n", type=int, default=20); ls.set_defaults(fn=cmd_list)
    for name, fn in (("show", cmd_show), ("json", cmd_json)):
        s = sub.add_parser(name); s.add_argument("id"); s.add_argument("-a", "--agent"); s.add_argument("-o", "--output")
        s.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
        if name == "show":
            s.add_argument("--thinking", action="store_true"); s.add_argument("--no-tools", action="store_true")
            s.add_argument("--env", action="store_true", help="include injected system/environment messages")
            s.add_argument("--cap", type=int, default=None, help="TOKENS kept per tool result (default: the custom cap in Settings → View; 0 = all); thinking is never capped")
            s.add_argument("--utc", action="store_true", help="print times in UTC instead of local time")
        s.set_defaults(fn=fn)
    e = sub.add_parser("export"); e.add_argument("id"); e.add_argument("-a", "--agent"); e.add_argument("-o", "--output")
    e.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    e.add_argument("--format", choices=tuple(formats.FORMATS), default="text")
    e.add_argument("--thinking", action="store_true"); e.add_argument("--no-tools", action="store_true")
    e.add_argument("--env", action="store_true"); e.add_argument("--cap", type=int, default=None, help="TOKENS kept per tool result in --mode custom (default: the custom cap in Settings → View; 0 = all); thinking is never capped")
    e.add_argument("--mode", choices=("brief", "custom", "full"), default="custom",
                   help="brief: the tool's name alone · custom: results capped at --cap and arguments at 200 characters · "
                        "full: every argument and every byte. Thinking is on or off (--thinking), never capped")
    e.add_argument("--tool-headers", action="store_true", dest="tool_headers",
                   help="give each tool turn its own '[TOOL] <timestamp>' line; off by default because it costs "
                        "about 21 tokens per turn to repeat a timestamp the turn above already carries")
    e.add_argument("--hide", action="append", choices=("user", "assistant", "tool", "system"),
                   help="drop these turns entirely (repeatable), e.g. --hide user to read only what the agent did")
    e.add_argument("--start", type=int); e.add_argument("--end", type=int)
    e.add_argument("--utc", action="store_true", help="convert every time to UTC (separate from redaction, which only hides the offset)")
    e.add_argument("--stamps", choices=("full", "time", "none"), default="full",
                   help="per-message timestamp: full date+time (18 tokens), time only (11), or none (5)")
    e.add_argument("--no-markers", action="store_false", dest="markers",
                   help="drop the [USER] / [ASSISTANT] lines entirely")
    e.add_argument("--wrap", choices=("user", "system", "assistant"), default="",
                   help="the whole transcript becomes ONE message of this role, in every format (text prints the session header once, outside it)")
    e.add_argument("--collapse", action=argparse.BooleanOptionalAction, default=None,
                   help="merge back-to-back turns of the same actor into one (default: Settings → View, on unless changed). "
                        "A tool result rides with the assistant turn that called it, so a run of calls costs one header, not one per call")
    e.add_argument("--convert", metavar="AGENT", choices=tuple(conv.WRITERS),
                   help="fold the conversation exactly as a conversion to AGENT folds it (tool activity as bracketed notes, "
                        "thinking dropped, import banner first) and write THAT in --format, e.g. --convert codex --format text "
                        "for a Codex-style transcript you feed in yourself")
    _add_redact_args(e); _add_notice_args(e); e.set_defaults(fn=cmd_export)
    f = sub.add_parser("fixture"); f.add_argument("id"); f.add_argument("-a", "--agent"); f.add_argument("-o", "--output", required=True)
    f.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    _add_redact_args(f); f.set_defaults(fn=cmd_fixture)
    c = sub.add_parser("convert"); c.add_argument("id"); c.add_argument("-a", "--agent")
    c.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    c.add_argument("--to", metavar="AGENT",
                   help=f"target agent ({', '.join(conv.WRITERS)}); omit for a NATIVE copy in the "
                        "session's own format (lossless). A source-only agent is refused with the reason.")
    c.add_argument("--template", help="a real session FILE of the target agent to copy the envelope from")
    c.add_argument("--mint-template", action="store_true",
                   help="run the target agent once (cheapest model, one word) and use the session it writes as the template, "
                        "so the envelope matches the release you actually have; spends a small API call")
    c.add_argument("--verify", action="store_true",
                   help="after --install, resume the installed session in the target agent and report whether it answered; "
                        "spends a small API call")
    c.add_argument("-o", "--output", help="output dir (default ./ectype-converted); mirrors the target's home layout")
    c.add_argument("--install", action="store_true", help="write straight into the target agent's live store")
    c.add_argument("--no-backup", dest="backup", action="store_false",
                   help="skip the copy of any store file the install would modify (Codex's session index); on by default")
    c.add_argument("--workspace", help="the working directory the copy belongs to (decides where the agent files it)")
    c.add_argument("--no-banner", action="store_true", help="omit the leading 'imported from …' user message")
    _add_redact_args(c); _add_notice_args(c); c.set_defaults(fn=cmd_convert)
    g = sub.add_parser("gui"); g.add_argument("--port", type=int, default=8765); g.add_argument("--no-open", action="store_true")
    g.add_argument("--export-dir", help="folder for 'save to folder' exports when Settings has none (default ~/ectype-exports)")
    g.set_defaults(fn=cmd_gui)
    st = sub.add_parser("settings"); st.add_argument("--path", action="store_true", help="print only the file path")
    st.set_defaults(fn=cmd_settings)
    bkp = sub.add_parser("backup", help="copy a session's files, list backups, or put one back")
    bkp.add_argument("id", nargs="?"); bkp.add_argument("-a", "--agent")
    bkp.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    bkp.add_argument("--list", action="store_true", help="every backup taken so far, newest first")
    bkp.add_argument("--restore", metavar="NAME", help="put one back where it came from")
    bkp.add_argument("--dry-run", action="store_true", help="with --restore: say what would be written, write nothing")
    bkp.set_defaults(fn=cmd_backup)
    sm = sub.add_parser("summarize", help="what happened in a session, extracted (no model, no API key)")
    sm.add_argument("id"); sm.add_argument("-a", "--agent"); sm.add_argument("-o", "--output")
    sm.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    sm.add_argument("--start", type=int); sm.add_argument("--end", type=int)
    sm.set_defaults(fn=cmd_summarize)
    m = sub.add_parser("mcp", help="serve the MCP tools on stdin/stdout (for an agent, not a terminal)")
    m.add_argument("--allow-write", action="store_true",
                   help="also offer install_session, which writes into an agent's live store")
    m.set_defaults(fn=cmd_mcp)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (ValueError, KeyError) as e:
        # The library raises these for "you asked for something that isn't possible": an
        # unwritable conversion target, an unknown agent or format, a session with nothing to
        # convert. They carry an explanatory message, so print it the way argparse prints its
        # own errors instead of a traceback. Anything else is a real fault and still raises.
        sys.exit(f"ectype: {e}")           # str(e), not args[0]: a UnicodeEncodeError's args[0] is the codec name


if __name__ == "__main__":
    main()
