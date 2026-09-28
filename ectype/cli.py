"""ectype: AI Agent Ectype, command line.

  ectype agents                       which agents are installed, where, how many sessions, shown in the GUI?
  ectype list [-a AGENT] [-n N]       recent sessions across all agents
  ectype show <id-prefix> [opts]      render one session (text); --mode brief|custom|full
  ectype recall <id-prefix> [opts]    show it the way the /ectype skill wants it (Settings → Agent skill) and save its summary
  ectype search <regex> [-a AGENT]    grep every summary ectype keeps: have I dealt with this before?
  ectype json <id-prefix>             dump the canonical model as JSON
  ectype export <id-prefix> [opts]    filtered export (redacted with --redact): text, markdown, html, json, jsonl, csv (+ import notice)
                                    --mode brief|custom|full · --tool-headers · --hide user|assistant
  ectype fixture <id-prefix> -o DIR   copy the agent's RAW files, redacted, as a test fixture
  ectype gui [--port N] [--no-open]   local web UI: browse, toggle, live token budget, export, settings
  ectype settings [--path]            print the settings file (where store paths and GUI defaults live)
ectype backup <id> | --list | --restore NAME | --prune
                                    copies of a session's files, and of anything an install would disturb
ectype skill status|install|remove|print [--name N] [-a AGENT]
                                    the /ectype slash command for Claude Code, Gemini CLI and Codex, under any name
ectype summarize <id-prefix> [--save] | --all [--force]
                                    what happened in it: asks, tools, files, commands, errors, keywords;
                                    --save / --all keep the summaries in ectype's store for `search`
ectype summary-rule status|install|remove|print [-a AGENT] [--file PATH]
                                    the paragraph asking an agent for a closing summary, in the files you pick
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

from . import __version__, adapters, budget, notice, settings, convert as conv
from .render import formats
from .render.text import render
from .model import Session, json_default
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
        missing = ""
        if agent and not adapters.get(agent).available():
            ad = adapters.get(agent)
            missing = f"; {agent}'s store is not there: {ad.home()} ({ad.home_source()})"
        sys.exit(f"no session starts with {prefix!r}" + (f" for agent {agent}" if agent else "")
                 + (f" under a path containing {project!r}" if project else "") + missing)
    if len(refs) > 1 and len({r.id for r in refs}) == 1:
        import filecmp
        if all(filecmp.cmp(refs[0].path, r.path, shallow=False) for r in refs[1:]):
            newest = max(refs, key=lambda r: r.mtime)
            print(f"ectype: {len(refs)} identical copies of {refs[0].id} under different project folders; using {newest.path.parent.name}", file=sys.stderr)
            return newest
    if len(refs) > 1:
        same_id = len({r.id for r in refs}) == 1
        # the candidates travel IN the message: on stdout they landed in a redirected file, and the MCP
        # server (whose stdout is the protocol) never showed them to the agent (audit F74)
        rows = "\n".join(f"  {r.agent:<12} {r.id}  {_local(r.mtime)}  {r.path}" for r in refs)
        sys.exit(f"{len(refs)} sessions match {prefix!r}:\n{rows}\n"
                 + ("they share one id with different content; give part of the file's path (--project, or `project` over MCP)"
                    if same_id else "give a longer id, or the agent (-a, or `agent` over MCP)"))
    return refs[0]


def cmd_agents(_a):
    print(f"{'agent':<13}{'label':<21}{'type':<8}{'found':<7}{'sessions':>8}  {'gui':<5} store (source)")
    for name, ad in adapters.ADAPTERS.items():
        ok = ad.available()
        n, err = 0, None
        if ok:
            try:
                n = len(ad.discover())
            except Exception as e:                    # noqa: BLE001, a store that cannot be read is a row that says so, not a dead table
                err = f"{type(e).__name__}: {e}"
        print(f"{name:<13}{ad.label:<21}{ad.category:<8}{'yes' if ok else 'no':<7}{n:>8}  {'on' if ad.enabled() else 'off':<5}"
              f"{ad.home()} ({ad.home_source()}; override with ${ad.env_home} or Settings)"
              + (f"  [plug-in {adapters.PLUGINS[name]}]" if name in adapters.PLUGINS else "")
              + (f"  COULD NOT BE READ: {err}" if err else ""))


def cmd_list(a):
    agent = _agent(a.agent)
    if agent and not adapters.get(agent).available():
        # an empty table looks exactly like a store with no sessions; say which one it is (F76)
        ad = adapters.get(agent)
        print(f"ectype: {agent}'s store is not there: {ad.home()} ({ad.home_source()})", file=sys.stderr)
    refs = adapters.all_refs([agent] if agent else None)
    refs = refs[: a.n] if a.n else refs                      # 0 = all
    print(f"{'agent':<12} {'id':<10} {'modified':<16} {'size':>8}  {'project':<14} title")
    for r in refs:
        print(f"{r.agent:<12} {r.short:<10} {_local(r.mtime):<16} {_size(r.size):>8}  {(r.project or '-')[:14]:<14} {(r.title or '')[:60]}")


# (option key, command-line attribute): every option that shapes a view, in one list, so `show`,
# `export` and `recall` read the same set and a new option cannot reach one surface and miss another
_VIEW_OPTS = (("mode", "mode"), ("cap", "cap"), ("thinking", "thinking"), ("tools", "tools"), ("env", "env"),
              ("notices", "notices"), ("questions", "questions"), ("peers", "peers"), ("collapse", "collapse"),
              ("stamps", "stamps"), ("markers", "markers"), ("tool_headers", "tool_headers"), ("wrap", "wrap"))


def _view_opts(a, base: dict) -> dict:
    """A view's options: each one typed on the command line, else `base` (Settings → View for `show` and
    `export`, Settings → Agent skill for `recall`; D14). `hide_roles` comes from repeated --hide."""
    o = {}
    for key, attr in _VIEW_OPTS:
        v = getattr(a, attr, None)
        o[key] = base.get(key) if v is None else v
    o["mode"] = o.get("mode") or "custom"
    o["wrap"] = o.get("wrap") or ""
    o["hide_roles"] = list(getattr(a, "hide", None) or base.get("hide_roles") or [])
    o["start"], o["end"] = getattr(a, "start", None), getattr(a, "end", None)
    return o


def _show_text(ref, a, opts: dict, red=None, with_notice: bool = False) -> tuple[str, Session]:
    """The text render of one session: one pipeline, so what `show` and `recall` print is exactly what
    an export with the same options would contain. `red` redacts the whole session before it is
    filtered (third audit, F02); `with_notice` appends the import notice (Settings → Import notice) as
    the last message."""
    s0 = adapters.load(ref)
    s = budget.filtered(red.session(s0) if red else s0, opts)
    if with_notice:
        text = notice.build(s0, settings.load()["notice"], adapters.get(ref.agent).label)
        s = notice.attach(s, {}, None)
        s.messages[-1].blocks[0].text = red.text(text) if red else text
    return render(s, budget.render_options(opts, local_time=not getattr(a, "utc", False)), source=s0), s0


def cmd_show(a):
    ref = _resolve(a.id, a.agent, a.project)
    out, _ = _show_text(ref, a, _view_opts(a, settings.load()["view"]))
    if a.output:
        _write(a.output, out)
        print(f"wrote {a.output} ({len(out):,} chars)")
    else:
        sys.stdout.write(out)


def cmd_recall(a):
    """`show` shaped for an agent: every view option (tool output, thinking, the conversation's
    questions and other sessions' messages, timestamps, merging, role markers, hidden sides,
    redaction, the import notice) comes from Settings → Agent skill unless typed on the command line,
    and the session's summary is saved into ectype's own store when that setting asks for it, so a
    session an agent pulled in once is greppable afterwards with `ectype search`. With only an id it
    brings in the conversation alone (the user, 2026-09-27)."""
    from . import ledger
    ref = _resolve(a.id, a.agent, a.project)
    cfg = settings.load()
    sk = cfg["skill"]
    red = None
    if sk["redact"] if a.redact is None else a.redact:
        rc = cfg["redact"]
        red = Redactor.defaults(extra=rc["terms"], options={k: rc[k] for k in settings.REDACT_RULES})
    out, s0 = _show_text(ref, a, _view_opts(a, sk), red=red,
                         with_notice=sk["notice"] if a.notice is None else a.notice)
    sys.stdout.write(out)
    want_summary = sk["summarize"] if a.summarize is None else a.summarize
    if want_summary:
        try:
            p = ledger.record(ref, s0)
        except OSError as e:
            # an agent running in a sandbox may not be allowed to write under the config dir; the
            # recall itself (the transcript above) succeeded, so this is a warning, not a failure
            print(f"[summary NOT saved: {e}]", file=sys.stderr)
        else:
            print(f"[summary saved for `ectype search`: {p}]", file=sys.stderr)


def cmd_json(a):
    ref = _resolve(a.id, a.agent, a.project)
    s = adapters.load(ref)
    out = json.dumps(dataclasses.asdict(s), ensure_ascii=False, indent=1, default=json_default)
    if a.output:
        _write(a.output, out)
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
    opts = _view_opts(a, settings.load()["view"])      # a flag left out = Settings → View (D14)
    red = _redactor(a)
    if red:
        # redact the whole session FIRST, then filter: the cap cut a result at a token boundary and
        # an e-mail or home path split there matched no rule (third audit, F02). `red` itself counts
        # over the unredacted view, so "what was replaced" lists what this export shows, not what
        # a hidden part held.
        s = budget.filtered(_redactor(a).session(s0), opts)
        red.session(budget.filtered(s0, opts))
    else:
        s = budget.filtered(s0, opts)
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
            # sanitised like the web's: a SillyTavern id carries a `/` (F83)
            path = path / formats.export_name(None, s.agent, s.id, a.format)
        _write(path, out)
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
    # always redacted, and with the user's own Settings: their terms and rule toggles and --redact-off.
    # The built-in defaults alone kept a name the user had listed in Settings → Redaction (F87)
    red = _redactor(argparse.Namespace(**{**vars(a), "redact": True}))
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
    target = a.to or ref.agent                   # no --to: this agent's own format, i.e. a native copy
    # Flag combinations are rejected BEFORE anything is read, backed up or written: a --verify
    # without --install used to be refused only after the converted file was on disk.
    if a.verify and not a.install:
        sys.exit("--verify needs --install: an agent can only resume a session that is in its own store")
    if a.mint_template and target == ref.agent:
        sys.exit("--mint-template is for a cross-agent conversion: a copy of this agent's own session needs no envelope")
    source = adapters.load(ref)
    red = _redactor(a)
    native = target == ref.agent and target in NATIVE
    # a native copy reads the ORIGINAL file, so it must keep the real id, cwd and path; redaction
    # is applied to the records as they are copied instead of to the model.
    s = source if native else (red.session(source) if red else source)
    out_dir = Path(a.output or "ectype-converted")
    extra = [(src, rel) for src, rel in ad.artifacts(ref) if src != source.path] if native else None
    template = Path(a.template) if a.template else None
    if a.mint_template:
        from .agentcli import AgentUnavailable, mint_template
        try:
            template = mint_template(target, a.workspace)
        except (AgentUnavailable, OSError) as e:
            sys.exit(f"could not mint a template: {e}")
        print(f"minted a template from a fresh {target} session: {template}")
    path, rep = convert(s, target, out_dir, template=template,
                        banner=not a.no_banner, install=a.install, redact=red.text if red else None,
                        notice=_notice_text(a, source), source_path=source.path,
                        workspace=a.workspace, extra=extra, backup=a.backup)
    report = rep.render() + (f"redacted: {red.report()}\n" if red else "")
    # never leave side files inside a live agent store: with --install the report goes to out_dir
    rp = (out_dir / f"{path.stem}.fidelity.md") if a.install else path.with_suffix(path.suffix + ".fidelity.md")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report, encoding="utf-8")
    print(f"wrote {path}")
    print(f"fidelity report: {rp}")
    print(report, end="")
    if a.verify:
        from .agentcli import AgentUnavailable, verify_resume
        try:
            # the id the writer produced, never the file's stem: a Codex rollout is named
            # rollout-<time>-<uuid>.jsonl, and its stem is not a thread id
            ok, detail = verify_resume(target, rep.new_id or path.stem, a.workspace or s.cwd)
        except (AgentUnavailable, OSError) as e:
            sys.exit(f"could not verify: {e}")
        print(f"verify: {'RESUMED' if ok else 'DID NOT RESUME'} ({detail})")
        if not ok:
            sys.exit(1)


def cmd_gui(a):
    from .web import serve
    port = settings.load()["gui"]["port"] if a.port is None else a.port      # --port wins over Settings → Web UI
    try:
        serve(port=port, open_browser=not a.no_open, export_dir_default=Path(a.export_dir) if a.export_dir else None)
    except OSError as e:
        if e.errno in (98, 48, 10048):          # EADDRINUSE on Linux, macOS, Windows
            sys.exit(f"ectype: port {port} is already in use (another ectype gui, probably). "
                     f"Stop that one, or start this one with --port <other>, or change Settings → Web UI → port.")
        raise


def _session_backup_modes(ad, ref, arts) -> dict:
    """How each file of a one-session backup goes back (see `backup.save`). The session's own file and
    its sidecar folder are replaced; a store index other sessions share is merged; a scratch export
    (Open WebUI, Cursor: one chat copied out of a shared database) or a file several sessions share
    (an Aider history) is kept in the backup only. Restoring those in place used to write into /tmp
    while saying "restored", or roll back every other session (the 2026-09-26 ideas round)."""
    try:
        home = ad.home().resolve()
    except OSError:
        home = None
    try:
        shares_file = any(r.path == ref.path and r.id != ref.id for r in ad.discover())
    except Exception:                                   # noqa: BLE001, a store that cannot be listed: treat the file as shared, the safe side
        shares_file = True
    modes = {}
    for src, rel in arts:
        try:
            inside = home is not None and src.resolve().is_relative_to(home)
        except OSError:
            inside = False
        if not inside and src != ref.path:
            modes[src] = "export"
        elif src == ref.path:
            modes[src] = "keep" if shares_file else "replace"
        elif ref.id in rel:
            modes[src] = "replace"
        elif src.suffix in (".jsonl", ".json"):
            modes[src] = "merge"
        else:
            modes[src] = "keep"
    return modes


def cmd_backup(a):
    from . import backup as bk
    if a.id and (a.list or a.prune):
        sys.exit("ectype backup: --list and --prune act on every backup; drop the session id")
    if a.prune:
        removed = bk.prune()
        cfg = settings.load()["backup"]
        print(f"retention: keep_last {cfg['keep_last'] or 'all'}, keep_days {cfg['keep_days'] or 'forever'} (Settings → Backups)")
        for d in removed:
            print(f"removed {d}")
        print(f"{len(removed)} backup folder(s) removed, {len(bk.listing())} left under {bk.root()}")
        return
    if a.list:
        rows = bk.listing()
        if not rows:
            print(f"no backups under {bk.root()}")
            return
        print(f"{'name':<44}{'files':>6}{'size':>10}  why")
        for r in rows:
            print(f"{r['name']:<44}{len(r['files']):>6}{_size(r['bytes']):>10}  {r['why']}")
        print(f"{len(rows)} backup(s), {_size(sum(r['bytes'] for r in rows))} under {bk.root()}")
        return
    if a.restore:
        done = bk.restore(a.restore, dry_run=a.dry_run)
        for dest, ok in done:
            print(("would restore " if a.dry_run else "restored ") + str(dest) if ok else f"MISSING in backup: {dest}")
        for dest, why in done.skipped:
            print(f"not written back: {dest} ({why}); it stays in the backup folder")
        if a.dry_run:
            print("nothing was written (--dry-run); run it again without --dry-run to put these back")
        elif done.safety:
            print(f"what was there before is kept as {done.safety.name}; undo with: ectype backup --restore {done.safety.name}")
        return
    if not a.id:
        sys.exit("ectype backup: give a session id, or --list, or --restore NAME")
    ref = _resolve(a.id, a.agent, a.project)
    ad = adapters.get(ref.agent)
    arts = [(Path(src), rel) for src, rel in ad.artifacts(ref)]
    folder = bk.save([src for src, _ in arts], f"{ref.agent}-{ref.id[:8]}", agent=ref.agent,
                     modes=_session_backup_modes(ad, ref, arts))
    if folder is None:
        sys.exit(f"nothing to back up: no file of {ref.id[:8]} is on disk")
    n = len(json.loads((folder / bk.MANIFEST).read_text(encoding="utf-8"))["files"])
    print(f"backed up {n} file(s) to {folder}")
    print(f"put them back with: ectype backup --restore {folder.name}")


def cmd_skill(a):
    """The slash command's files: where they go, under what name, and writing or removing them."""
    from . import skill
    name = a.name or settings.load()["skill"]["command"]
    agents = a.agent or None
    for ag in agents or []:
        if ag not in skill.AGENTS:
            sys.exit(f"ectype skill: {ag!r} has no command folder; agents with one: {', '.join(skill.AGENTS)}")
    if a.action == "status":
        print(f"command name: /{name}   (Settings → Agent skill → command name; --name overrides)")
        words = {"ours": "installed",
                 "stale": "installed by an older ectype, untouched since: `ectype skill install` updates it",
                 "edited": "edited since ectype wrote it: `ectype skill install --force` replaces it (kept as .bak)",
                 "foreign": "a file NOT written by ectype (left alone)", "absent": "not installed"}
        for st in skill.status(name, agents):                   # -a filters the rows (F69)
            print(f"  {st['label']:<12} {words[st['state']]}")
            print(f"  {'':<12} file: {st['path']}")
            print(f"  {'':<12} use: {st['use']}")
        return
    if a.action == "print":
        if not agents or len(agents) != 1:
            sys.exit("ectype skill print: give exactly one -a AGENT")
        sys.stdout.write(skill.render(agents[0], name))
        return
    if a.action == "install":
        old = settings.load()["skill"]["command"]
        try:
            written = skill.install(name, agents, force=a.force)
        except FileExistsError as e:
            sys.exit(f"ectype skill: {e}")                      # nothing was written: every target is checked first (F68)
        for p in written:
            print(f"wrote {p}")
        if a.name and a.name != old:
            # a rename: the old name's files ectype wrote (and nobody edited) go, so the agents do not
            # answer to both, and `status` under the new name does not hide them (F68)
            removed, kept = skill.remove(old, agents)
            for p in removed:
                print(f"removed {p} (the previous name)")
            for p in kept:
                print(f"kept {p}: under the previous name, but not ectype's as written")
            cur = settings.load()
            cur["skill"]["command"] = a.name                    # the name chosen here is the name from now on
            settings.save(cur)
        print(f"use it as /{name} <id> [options]; `ectype skill status` shows what is installed")
        return
    if a.action == "remove":
        removed, kept = skill.remove(name, agents, force=a.force)
        for p in removed:
            print(f"removed {p}")
        for p in kept:
            print(f"kept {p}: not written by ectype, or edited since (--force moves an edited one aside)")
        if not removed and not kept:
            print(f"nothing is installed as /{name}")
        return


def cmd_summary_rule(a):
    """The paragraph that asks an agent for a closing summary, in the instruction files the user picks
    (`ectype.summary_rule`). Nothing is written without a target named."""
    from . import summary_rule as rule
    for ag in a.agent or []:
        if ag not in rule.AGENTS:
            sys.exit(f"ectype summary-rule: no instruction file known for {ag!r}; known: {', '.join(rule.AGENTS)} (or --file PATH)")
    files = rule.targets(a.agent, a.file)
    if a.action == "print":
        sys.stdout.write(rule.text() + "\n")
        return
    if a.action == "status":
        words = {"absent": "no such file", "none": "no rule in it",
                 "hand-written": "already asks in its own words (left alone)", "ours": "installed",
                 "stale": "installed for other markers: `ectype summary-rule install` updates it",
                 "edited": "the block was edited by hand (`--force` replaces it)"}
        own = {rule.path(x) for x in rule.AGENTS}
        for row in rule.status([f for f in files if f not in own]):
            print(f"  {row['label']:<12} {words[row['state']]}")
            print(f"  {'':<12} file: {row['path']}")
        return
    if not files:
        sys.exit("ectype summary-rule: name the files: -a claude-code, -a codex, -a gemini-cli (repeatable), or --file PATH")
    if a.action == "install":
        try:
            written = rule.install(files, force=a.force)
        except FileExistsError as e:
            sys.exit(f"ectype summary-rule: {e}")               # nothing was written: every target is checked first
        for p in files:
            print(("wrote " if p in written else "already current: ") + str(p))
        if written:
            print("each changed file was backed up first: `ectype backup --list`")
        return
    removed, kept = rule.remove(files, force=a.force)
    for p in removed:
        print(f"removed the rule from {p}")
    for p in kept:
        print(f"kept {p}: its block was edited by hand (--force takes it out anyway)")
    if not removed and not kept:
        print("none of those files holds the rule")


def cmd_summarize(a):
    from . import ledger
    from .summary import summarize
    if a.marker and (a.save or a.all):
        sys.exit("--marker is for one look at one session; the store uses the markers in Settings → Summary "
                 "(`ectype settings`), and `summarize --all` redoes stored summaries when those change")
    if a.all and (a.id or a.start is not None or a.end is not None or a.output or a.project or a.redact):
        sys.exit("ectype summarize --all covers every session into the store; it takes no id, range, -o or --project")
    if a.force and not a.all:
        sys.exit("ectype summarize: --force only means something with --all")
    if a.all:
        agents = [_agent(a.agent)] if a.agent else None
        stats = ledger.update(agents, force=a.force)
        print(f"{stats['seen']} session(s) seen: {stats['summarised']} summarised, {stats['unchanged']} unchanged"
              + (f", {stats['gone']} no longer on disk" if stats["gone"] else "")
              + f"  ({ledger.root()})")
        for agent, sid, err in stats["failed"]:      # every failure named; a silent skip would hide a broken adapter
            print(f"  FAILED {agent} {sid[:8]}: {err}", file=sys.stderr)
        if stats["failed"]:
            sys.exit(1)
        return
    if not a.id:
        sys.exit("ectype summarize: give a session id, or --all")
    ref = _resolve(a.id, a.agent, a.project)
    s = adapters.load(ref)
    ranged = bool(a.start or a.end)
    view = budget.filtered(s, {"mode": "full", "cap": 0, "thinking": False, "tools": True,
                               "env": False, "notices": True, "start": a.start, "end": a.end}) if ranged else s
    text = summarize(view, marks=a.marker)
    red = _redactor(a)
    if red:
        if a.save:
            sys.exit("ectype summarize: --redact is for what is printed or written with -o; the store keeps the summary as extracted")
        text = red.text(text)
        print(f"[redacted: {red.report()}]", file=sys.stderr)
    if a.save:
        if ranged:
            sys.exit("--save keeps the summary of the WHOLE session; drop --start/--end")
        p = ledger.record(ref, s)
        print(f"saved {p}")
    if a.output:
        _write(a.output, text)
        print(f"wrote {a.output}")
    elif not a.save:
        print(text, end="")


def cmd_search(a):
    """Regex over every stored summary; says how much of the stores it could see."""
    import re
    from . import ledger
    agents = [_agent(a.agent)] if a.agent else None
    try:
        res = ledger.search(a.pattern, agents)
    except re.error as e:
        sys.exit(f"ectype search: {e}")
    sys.stdout.write(ledger.render_search(res, a.pattern))
    if not res["matches"]:
        sys.exit(1)


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


def _add_view_flags(sp):
    """thinking / tools / env / notices, each defaulting to Settings → View when left out (D14)."""
    sp.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=None, help="the agent's reasoning (default: Settings → View)")
    sp.add_argument("--tools", action=argparse.BooleanOptionalAction, default=None,
                    help="tool calls and results at all; --no-tools is the conversation alone (default: Settings → View)")
    sp.add_argument("--env", action=argparse.BooleanOptionalAction, default=None, help="injected system/environment messages (default: Settings → View)")
    sp.add_argument("--notices", action=argparse.BooleanOptionalAction, default=None,
                    help="the agent's own notices where the store keeps them: API errors, refusals, away summaries (default: Settings → View)")
    sp.add_argument("--questions", action=argparse.BooleanOptionalAction, default=None,
                    help="a question the agent put to you, and your answer, as the turns they are rather than tool traffic (default: Settings → View, on)")
    sp.add_argument("--peers", action=argparse.BooleanOptionalAction, default=None,
                    help="messages other sessions sent in, each under its own [PEER] label (default: Settings → View, on)")


def _count(v: str) -> int:
    n = int(v)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 (all) or more")
    return n


def _write(path, text: str) -> None:
    """Write an output file, or exit with one line: an unwritable -o used to end in a traceback (F79)."""
    try:
        Path(path).write_text(text, encoding="utf-8", errors="replace")
    except OSError as e:
        sys.exit(f"ectype: cannot write {path}: {e.strerror or e}")


def _add_notice_args(sp):
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--notice", dest="notice", action="store_true", default=None,
                   help="append the import notice as the last message (default: Settings → Import notice, on unless changed)")
    g.add_argument("--no-notice", dest="notice", action="store_false", help="never append it")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            if os.name == "nt":               # cp1252 consoles choke on ▸ ● ↳ ⚑
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:                             # a lone surrogate from a transcript prints as ?, never a traceback
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="ectype", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Before the subparsers, so `ectype --version` answers without a subcommand: argparse
    # runs a version action the moment it consumes the flag, ahead of the required-cmd check.
    p.add_argument("-V", "--version", action="version", version=f"ectype {__version__}",
                   help="print the version and exit")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agents").set_defaults(fn=cmd_agents)
    ls = sub.add_parser("list"); ls.add_argument("-a", "--agent")
    ls.add_argument("-n", type=_count, default=20, help="how many rows (default 20; 0 = all)"); ls.set_defaults(fn=cmd_list)
    for name, fn in (("show", cmd_show), ("json", cmd_json)):
        s = sub.add_parser(name); s.add_argument("id"); s.add_argument("-a", "--agent"); s.add_argument("-o", "--output")
        s.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
        if name == "show":
            s.add_argument("--mode", choices=("brief", "custom", "full"), default=None,
                           help="brief: the tool's name alone · custom: results capped at --cap · full: everything "
                                "(default: Settings → View → default tool output)")
            _add_view_flags(s)
            s.add_argument("--cap", type=int, default=None, help="TOKENS kept per tool result in --mode custom (default: the custom cap in Settings → View; 0 = all); thinking is never capped")
            s.add_argument("--start", type=int); s.add_argument("--end", type=int)
            s.add_argument("--utc", action="store_true", help="print times in UTC instead of local time")
        s.set_defaults(fn=fn)
    rc = sub.add_parser("recall", help="show a session the way the /ectype skill wants it (every default from Settings → Agent skill) and save its summary")
    rc.add_argument("id"); rc.add_argument("-a", "--agent")
    rc.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    rc.add_argument("--mode", choices=("brief", "custom", "full"), default=None, help="tool-output mode, once tool calls are on (default: Settings → Agent skill)")
    rc.add_argument("--cap", type=int, default=None, help="TOKENS kept per tool result in custom mode (default: Settings → Agent skill)")
    rc.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=None, help="include the reasoning (default: Settings → Agent skill)")
    rc.add_argument("--tools", action=argparse.BooleanOptionalAction, default=None,
                    help="tool calls and results at all (default: Settings → Agent skill, off: the conversation alone)")
    rc.add_argument("--env", action=argparse.BooleanOptionalAction, default=None, help="injected system/environment messages (default: Settings → Agent skill)")
    rc.add_argument("--notices", action=argparse.BooleanOptionalAction, default=None, help="the agent's own notices: API errors, refusals, away summaries (default: Settings → Agent skill)")
    rc.add_argument("--questions", action=argparse.BooleanOptionalAction, default=None,
                    help="a question the agent put to the user, and the answer, as turns (default: Settings → Agent skill, on)")
    rc.add_argument("--peers", action=argparse.BooleanOptionalAction, default=None,
                    help="messages other sessions sent in, each under [PEER] (default: Settings → Agent skill, on)")
    rc.add_argument("--collapse", action=argparse.BooleanOptionalAction, default=None, help="merge back-to-back turns of the same actor (default: Settings → Agent skill)")
    rc.add_argument("--stamps", choices=("full", "time", "none"), default=None, help="per-message timestamp (default: Settings → Agent skill)")
    rc.add_argument("--markers", action=argparse.BooleanOptionalAction, default=None, help="the [USER] / [ASSISTANT] lines (default: Settings → Agent skill)")
    rc.add_argument("--tool-headers", action=argparse.BooleanOptionalAction, default=None, dest="tool_headers",
                    help="a tool turn's own [TOOL] line (default: Settings → Agent skill)")
    rc.add_argument("--wrap", choices=("user", "system", "assistant"), default=None, help="the whole transcript as ONE message of this role (default: Settings → Agent skill)")
    rc.add_argument("--hide", action="append", choices=("user", "assistant"), help="leave that side's turns out (repeatable; default: Settings → Agent skill)")
    rc.add_argument("--redact", action=argparse.BooleanOptionalAction, default=None, help="apply the rules in Settings → Redaction (default: Settings → Agent skill)")
    _add_notice_args(rc)
    rc.add_argument("--start", type=int); rc.add_argument("--end", type=int)
    rc.add_argument("--summarize", action=argparse.BooleanOptionalAction, default=None,
                    help="also save the session's summary into ectype's own store for `ectype search` (default: Settings → Agent skill)")
    rc.set_defaults(fn=cmd_recall)
    e = sub.add_parser("export"); e.add_argument("id"); e.add_argument("-a", "--agent"); e.add_argument("-o", "--output")
    e.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    e.add_argument("--format", choices=tuple(formats.FORMATS), default="text")
    _add_view_flags(e)
    e.add_argument("--cap", type=int, default=None, help="TOKENS kept per tool result in --mode custom (default: the custom cap in Settings → View; 0 = all); thinking is never capped")
    e.add_argument("--mode", choices=("brief", "custom", "full"), default=None,
                   help="brief: the tool's name alone · custom: results capped at --cap and arguments at 200 characters · "
                        "full: every argument and every byte. Thinking is on or off (--thinking), never capped "
                        "(default: Settings → View → default tool output)")
    e.add_argument("--tool-headers", action=argparse.BooleanOptionalAction, default=None, dest="tool_headers",
                   help="give each tool turn its own '[TOOL] <timestamp>' line (default: Settings → View, off: it costs "
                        "about 21 tokens per turn to repeat a timestamp the turn above already carries)")
    e.add_argument("--hide", action="append", choices=("user", "assistant", "tool", "system"),
                   help="drop these turns entirely (repeatable), e.g. --hide user to read only what the agent did")
    e.add_argument("--start", type=int); e.add_argument("--end", type=int)
    e.add_argument("--utc", action="store_true", help="convert every time to UTC (separate from redaction, which only hides the offset)")
    e.add_argument("--stamps", choices=("full", "time", "none"), default=None,
                   help="per-message timestamp: full date+time (18 tokens), time only (11), or none (5) (default: Settings → View)")
    e.add_argument("--markers", action=argparse.BooleanOptionalAction, default=None, dest="markers",
                   help="the [USER] / [ASSISTANT] lines; --no-markers drops them (default: Settings → View)")
    e.add_argument("--wrap", choices=("user", "system", "assistant"), default=None,
                   help="the whole transcript becomes ONE message of this role, in every format (text prints the session header once, outside it) (default: Settings → View)")
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
    g = sub.add_parser("gui"); g.add_argument("--port", type=int, default=None, help="listen here (default: Settings → Web UI → port, 8765 unless changed)"); g.add_argument("--no-open", action="store_true")
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
    bkp.add_argument("--prune", action="store_true", help="apply the retention from Settings → Backups now (keep_last / keep_days; 0 = no limit)")
    bkp.set_defaults(fn=cmd_backup)
    sk = sub.add_parser("skill", help="the /ectype slash command's files for Claude Code, Gemini CLI and Codex: status, install, remove, print")
    sk.add_argument("action", choices=("status", "install", "remove", "print"))
    sk.add_argument("--name", help="the command's name (default: Settings → Agent skill → command name, `ectype`); install remembers it")
    sk.add_argument("-a", "--agent", action="append", help="only this agent (repeatable); default: all three")
    sk.add_argument("--force", action="store_true", help="replace (install) or remove a file ectype wrote that was edited since; the edited file is kept as .bak-<stamp>")
    sk.set_defaults(fn=cmd_skill)
    sm = sub.add_parser("summarize", help="what happened in a session, extracted (no model, no API key)")
    sm.add_argument("id", nargs="?"); sm.add_argument("-a", "--agent"); sm.add_argument("-o", "--output")
    sm.add_argument("--project", help="part of the session file's path; picks one when the same id exists under two project folders")
    sm.add_argument("--start", type=int); sm.add_argument("--end", type=int)
    sm.add_argument("--save", action="store_true", help="keep this session's summary in ectype's own store, where `ectype search` looks")
    sm.add_argument("--all", action="store_true", help="summarise every session that is new or changed since the last run (with -a: one agent) into the store")
    sm.add_argument("--force", action="store_true", help="with --all: re-summarise everything, changed or not")
    sm.add_argument("--marker", action="append", metavar="TEXT",
                    help="closing-summary marker to look for instead of Settings → Summary (repeatable; one look only, not with "
                         "--save or --all). Write --marker=TEXT when TEXT starts with a dash, as the older -*-summary-*- does")
    srp = sub.add_parser("summary-rule", help="the paragraph that asks an agent for a closing summary, in the instruction files you pick: status, install, remove, print")
    srp.add_argument("action", choices=("status", "install", "remove", "print"))
    srp.add_argument("-a", "--agent", action="append", help="an agent's global instruction file: claude-code, codex, gemini-cli (repeatable)")
    srp.add_argument("--file", action="append", help="any other instruction file, by path (repeatable)")
    srp.add_argument("--force", action="store_true",
                     help="replace a block edited by hand, or add the rule to a file that already asks in its own words")
    srp.set_defaults(fn=cmd_summary_rule)
    _add_redact_args(sm)
    sm.set_defaults(fn=cmd_summarize)
    se = sub.add_parser("search", help="regex over every summary in ectype's store: have I dealt with this before?")
    se.add_argument("pattern"); se.add_argument("-a", "--agent")
    se.set_defaults(fn=cmd_search)
    m = sub.add_parser("mcp", help="serve the MCP tools on stdin/stdout (for an agent, not a terminal)")
    m.add_argument("--allow-write", action="store_true",
                   help="also offer install_session, which writes into an agent's live store")
    m.set_defaults(fn=cmd_mcp)
    a, extra = p.parse_known_args(argv)
    if extra:
        # reported by the SUBCOMMAND's parser, so the usage shown lists its own options: the top-level
        # usage never showed `--mode`, which is what someone typing `--brief` needed to see (F73)
        hint = next((f" (did you mean --mode {x[2:]}?)" for x in extra if x in ("--brief", "--custom", "--full")), "")
        sub.choices[a.cmd].error(f"unrecognized arguments: {' '.join(extra)}{hint}")
    try:
        a.fn(a)
    except BrokenPipeError:
        # the reader stopped early (`| head`): not an error, and no traceback under the lines it wanted
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(141)
    except (ValueError, KeyError) as e:
        # The library raises these for "you asked for something that isn't possible": an
        # unwritable conversion target, an unknown agent or format, a session with nothing to
        # convert. They carry an explanatory message, so print it the way argparse prints its
        # own errors instead of a traceback. Anything else is a real fault and still raises.
        sys.exit(f"ectype: {e}")           # str(e), not args[0]: a UnicodeEncodeError's args[0] is the codec name


if __name__ == "__main__":
    main()
