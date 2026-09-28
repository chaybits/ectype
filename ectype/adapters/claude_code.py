"""Claude Code: ~/.claude/projects/<project-slug>/<session-uuid>.jsonl

Append-only JSONL. Records of type user/assistant carry the conversation; tool results
arrive as `tool_result` blocks inside the NEXT user record. A message that arrives while the agent
is busy (typed by the user, or sent by another session) is an `attachment` record of type
`queued_command`, read as the message it is. A user record whose `origin.kind` is "peer" is another
session's message. Other record types (ai-title, file-history-snapshot, queue-operation, the other
attachments, ...) are metadata.

SIDECAR FILES. A session may also own a directory of its own id next to the .jsonl:

    <slug>/<uuid>/tool-results/<hash>.txt   the FULL text of a tool result too large to inline.
                                            The transcript keeps only a stub:
                                            "<persisted-output>Output too large (46.3KB). Full
                                             output saved to: <path>  Preview (first 2KB): …"
    <slug>/<uuid>/subagents/agent-*.jsonl   Task-tool subagent transcripts, separate sessions,
                                            not part of this conversation
    <slug>/<uuid>/custom-title.json         {"customTitle": "..."}, the title you typed
    <slug>/sessions-index.json              index of the folder (first prompt, summary, projectPath, counts)

The spill files are read back in, so a tool result shows what actually came out instead of a
2 KB preview. Everything else is metadata about the session, not content of it.

TITLES, in order of precedence: the sidecar `custom-title.json` (what `rename` writes, and what
Claude Code 2.1.27x reads back), the `custom-title` record Claude Code also appends to the
transcript (older versions wrote the record alone), the `ai-title` record it generates, the
index's summary or first prompt, and finally the first human prompt in the file. The record types
in a store today run to twenty (attachment, progress, queue-operation, last-prompt, system with
subtypes api_error / away_summary / turn_duration ...); user, assistant, the queued attachments and
the title records carry content this model represents.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ..jsonl import iter_jsonl
from ..model import ContentBlock, Message, Session, SessionRef, TokenUsage, parse_ts
from ._anthropic import result_text
from .base import TITLE_CHARS, Adapter

# The path runs to the end of its line: a `\S+` capture cut it at the first space, and a home such as
# a home folder with a space in its name left every spilled result unrestored (the cut prefix exists as a directory,
# so even the name-based fallback never ran). `<` is excluded for a stub that closes its tag on the
# same line.
_SPILL = re.compile(r"Full output saved to:[ \t]*([^\r\n<]+?)[ \t]*(?:<|\r?\n|$)")


def _project_label(slug: str) -> str:
    # the slug's last dash-segment: "-home-user-projects-app" -> "app"; "C--Projects-app" -> "app".
    # A slug cannot tell a hyphen inside a folder name from a separator ("…-Source-cli-skills" ->
    # "skills"), which is why _project_of prefers the session's own recorded path when there is one
    return slug.rstrip("-").split("-")[-1] or slug


def _project_of(entry: dict, slug: str) -> str:
    """The session's project name: the last component of its own `projectPath` in
    `sessions-index.json` when the index has one for it (per session, because a folder's entries
    can record different paths, and with either separator, because the path was written by
    whichever OS ran the session), else the slug's last segment."""
    pp = entry.get("projectPath") if isinstance(entry, dict) else None
    if isinstance(pp, str):
        tail = re.split(r"[\\/]+", pp.strip().rstrip("\\/"))[-1]
        if tail:
            return tail
    return _project_label(slug)


def _peer(origin: dict) -> dict:
    """Who sent a message from another session: its name and its id, whichever the record carries.
    Claude Code's peer messages record `origin.kind` "peer" with `name`, `from` and more."""
    out = {k: str(origin[k]) for k in ("name", "from") if isinstance(origin.get(k), (str, int)) and str(origin[k]).strip()}
    return out or {"name": "another session"}


def _answers(result) -> list[list[str]]:
    """AskUserQuestion's structured reply, from a record's `toolUseResult`: [[question, answer], ...],
    with the note the user typed beside an option appended when there is one. [] for anything else."""
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        return []
    notes = result.get("annotations") if isinstance(result.get("annotations"), dict) else {}
    out = []
    for q, a in result["answers"].items():
        text = ", ".join(str(x) for x in a) if isinstance(a, list) else str(a)
        note = notes.get(q, {}).get("notes") if isinstance(notes.get(q), dict) else None
        out.append([str(q), text + (f" (note: {note})" if isinstance(note, str) and note.strip() else "")])
    return out


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    label = "Claude Code"
    env_home = "ECTYPE_CLAUDE_HOME"
    default_home = "~/.claude/projects"
    writable = True
    native_format = "Claude Code .jsonl"
    question_tools = frozenset({"AskUserQuestion"})

    def discover(self) -> list[SessionRef]:
        out = []
        for proj in sorted(self.home().iterdir()):
            if not proj.is_dir():
                continue
            index = self._index(proj)
            for f in proj.glob("*.jsonl"):
                if f.stem.startswith("agent-"):
                    continue          # subagent side files, not sessions
                st = f.stat()
                entry = index.get(f.stem) or {}
                custom, ai = self._tail_titles(f)
                # the name you typed (sidecar, then the record Claude Code also writes), then the
                # title the agent generated, then what its own resume picker shows, then a peek
                title = (self._custom_title(proj / f.stem) or custom or ai
                         or entry.get("summary") or entry.get("firstPrompt") or self._peek_title(f))
                out.append(SessionRef(self.name, f.stem, f,
                                      datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                      st.st_size, project=_project_of(entry, proj.name),
                                      title=(title or "").strip()[:TITLE_CHARS] or None))
        return out

    @staticmethod
    def _index(proj: Path) -> dict[str, dict]:
        """`<slug>/sessions-index.json`, the index behind `claude --resume`'s picker, keyed by
        session id: {firstPrompt, summary, projectPath, messageCount, created, modified, ...}.
        Claude Code writes it lazily (2 of 60 project folders here have one), so it is a source,
        never a requirement. Unreadable = empty."""
        p = proj / "sessions-index.json"
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else data
        return {e["sessionId"]: e for e in entries or [] if isinstance(e, dict) and e.get("sessionId")}

    @staticmethod
    def _tail_titles(f: Path, tail_bytes: int = 65536) -> tuple[str | None, str | None]:
        """(customTitle, aiTitle) from the LAST `custom-title` / `ai-title` record in the file's
        final `tail_bytes`, or None each.

        Claude Code keeps a session's title as a record inside the transcript, appended again on
        each resume, so the last one is current and it sits near the end: measured on this
        machine, 19 of 19 custom titles and 192 of 192 AI titles were inside the final 64 KB,
        while only 8 of the 19 were within the first 60 lines the head peek reads. The sidecar
        `custom-title.json` is also written by newer versions but not by older ones (14 of the 19
        renamed sessions here had the record alone), so both are read.
        """
        custom = ai = None
        try:
            size = f.stat().st_size
            with open(f, "rb") as fh:
                fh.seek(max(0, size - tail_bytes))
                chunk = fh.read()
        except OSError:
            return None, None
        if b'"custom-title"' not in chunk and b'"ai-title"' not in chunk:
            return None, None
        for raw in chunk.splitlines():
            if b'"custom-title"' not in raw and b'"ai-title"' not in raw:
                continue
            try:
                r = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue            # the first line of the chunk may be a cut record
            if not isinstance(r, dict):
                continue
            if r.get("type") == "custom-title" and r.get("customTitle"):
                custom = str(r["customTitle"])
            elif r.get("type") == "ai-title" and r.get("aiTitle"):
                ai = str(r["aiTitle"])
        return custom, ai

    @staticmethod
    def _custom_title(session_dir: Path) -> str | None:
        p = session_dir / "custom-title.json"
        if not p.is_file():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        v = d.get("customTitle") if isinstance(d, dict) else None
        return str(v) if v else None

    @staticmethod
    def _peek_title(f: Path, max_lines: int = 60) -> str | None:
        """First human prompt within the first few lines; never parses the whole file."""
        try:
            with open(f, encoding="utf-8-sig") as fh:
                for i, line in enumerate(fh):
                    if i >= max_lines:
                        break
                    if '"type":"user"' not in line and '"type": "user"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # discovery runs over every session of every project: a record of an unexpected
                    # shape here once dropped the whole Claude Code store from the listing
                    if not isinstance(r, dict) or r.get("isMeta"):
                        continue
                    msg = r.get("message")
                    c = msg.get("content") if isinstance(msg, dict) else None
                    text = c if isinstance(c, str) else "".join(
                        str(b.get("text", "")) for b in (c if isinstance(c, list) else []) if isinstance(b, dict) and b.get("type") == "text")
                    text = text.strip()
                    if text and not text.startswith(("<", "Caveat:")):
                        return text.splitlines()[0][:TITLE_CHARS]
        except OSError:
            pass
        return None

    def _sidecar(self, ref: SessionRef) -> Path:
        """<slug>/<session-id>/: the session's own folder of spill files and subagents."""
        return ref.path.parent / ref.id

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        slug = ref.path.parent.name
        out = [(ref.path, f"{slug}/{ref.path.name}")]
        side = self._sidecar(ref)
        # tool-results hold real content the transcript points at; subagents are separate
        # sessions and custom-title is metadata, so only the spill files travel with a fixture.
        for f in sorted((side / "tool-results").glob("*")) if (side / "tool-results").is_dir() else []:
            if f.is_file():
                out.append((f, f"{slug}/{ref.id}/tool-results/{f.name}"))
        return out

    def rename(self, ref: SessionRef, title: str) -> bool:
        """Write `<slug>/<id>/custom-title.json`, which is the file Claude Code itself writes when
        a session is named by hand, so the new name shows up in the agent and not only here. An
        empty title deletes the file, putting the generated title back. The transcript is never
        touched: the name is metadata beside it, which is why this is safe to do and the other
        twelve stores are not."""
        side = self._sidecar(ref)
        p = side / "custom-title.json"
        try:
            if title.strip():
                side.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(json.dumps({"customTitle": title.strip()}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, p)
            elif p.is_file():
                p.unlink()
            return True
        except OSError:
            return False       # read-only store, or a mount that went away: fall back to a local name

    def workspaces(self) -> list[str]:
        """Every directory Claude Code has a project folder for, read from the two newest sessions
        in each, the first that records a cwd (the slug itself is lossy: '-run-media-…' cannot be
        turned back into a path)."""
        out: list[str] = []
        try:
            projects = sorted(self.home().iterdir())
        except OSError:
            return out
        for proj in projects:
            if not proj.is_dir():
                continue
            # the agent's own index names the real path outright, when it exists
            found = next((e.get("projectPath") for e in self._index(proj).values() if e.get("projectPath")), None)
            if found:
                if found not in out:
                    out.append(found)
                continue
            files = sorted((f for f in proj.glob("*.jsonl") if not f.stem.startswith("agent-")),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[:2]:
                cwd = self._peek_cwd(f)
                if cwd and cwd not in out:
                    out.append(cwd)
                    break
        return sorted(out)

    @staticmethod
    def _peek_cwd(f: Path, max_lines: int = 40) -> str | None:
        try:
            with open(f, encoding="utf-8-sig") as fh:
                for i, line in enumerate(fh):
                    if i >= max_lines:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        continue
                    if cwd:
                        return cwd
        except OSError:
            pass
        return None

    @staticmethod
    def _restore_spill(b: ContentBlock, side: Path) -> None:
        """Swap a '<persisted-output> … Preview (first 2KB)' stub for the full sidecar text."""
        if b.kind != "tool_result" or "Full output saved to:" not in b.text:
            return
        m = _SPILL.search(b.text)
        if not m:
            return
        p = Path(m.group(1).rstrip(">").rstrip())
        if not p.exists():
            p = side / "tool-results" / p.name
        b.spill_path = p
        if not p.is_file():
            b.truncated = True                      # pointed somewhere we cannot read
            return
        try:
            # `replace`, not `surrogateescape`: a lone surrogate cannot be written back as UTF-8, and
            # every sink is strict, so one invalid byte in a spill file killed every export of the
            # session. U+FFFD is visible and encodable, the same policy as the Antigravity and Aider readers.
            full = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            b.truncated = True
            return
        b.meta["restored_from_spill"] = len(full) - len(b.text)
        b.text = full

    @staticmethod
    def _queued(rec: dict, index: int) -> Message | None:
        """A `queued_command` attachment as the message it is, or None for any other attachment.

        Claude Code files a message that arrives while the agent is busy as an attachment record: what
        the user typed mid-turn (`origin.kind` "human", `humanTurn`), what another session sent
        (`origin.kind` "peer"), and the harness's own task notifications (no origin). The first is the
        user's, the second a peer message, the third injected context."""
        a = rec.get("attachment")
        if not isinstance(a, dict) or a.get("type") != "queued_command":
            return None
        p = a.get("prompt")
        if isinstance(p, list):
            p = "".join(str(x.get("text", "")) for x in p if isinstance(x, dict))
        if not isinstance(p, str) or not p.strip():
            return None
        origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
        if origin.get("kind") == "peer":
            meta: dict = {"peer": _peer(origin), "queued": True}
        elif origin.get("kind") == "human" or a.get("humanTurn") or (not origin and a.get("commandMode") in ("prompt", "bash")):
            meta = {"queued": True}                  # an older record with no origin: a prompt or ! command is still the user's
        else:
            meta = {"env": True, "queued": True}     # a task notification, or anything else the harness queued
        return Message(index, "user", parse_ts(rec.get("timestamp") or a.get("timestamp")), [ContentBlock("text", p)],
                       id=rec.get("uuid"), meta=meta)

    def load(self, ref: SessionRef) -> Session:
        msgs: list[Message] = []
        title = cwd = version = model = branch = None
        started = ended = None
        side = self._sidecar(ref)
        custom_title = None                     # what YOU named it; wins over the generated title
        ct = side / "custom-title.json"
        if ct.is_file():
            try:
                custom_title = json.loads(ct.read_text(encoding="utf-8-sig")).get("customTitle") or None
            except (OSError, ValueError):
                pass
        subagents = sorted((side / "subagents").glob("agent-*.jsonl")) if (side / "subagents").is_dir() else []
        bad: list[int] = []                     # lines that did not parse: counted, never hidden
        record_title = None                     # the `custom-title` record: the same name, kept in the transcript
        for _, rec in iter_jsonl(ref.path, bad):
            t = rec.get("type")
            if t == "ai-title":
                title = rec.get("aiTitle") or title
                continue
            if t == "custom-title":
                record_title = rec.get("customTitle") or record_title
                continue
            if t == "summary":
                title = title or rec.get("summary")
                continue
            if t == "attachment":
                # a message delivered while the agent was working is filed as an attachment, not as a
                # user record: skipping every attachment dropped what the user typed mid-turn (and what
                # other sessions sent) from every view, and a model reading the import was told
                # nothing it typed was missing (the 2026-09-27 feedback)
                q = self._queued(rec, len(msgs))
                if q is not None:
                    msgs.append(q)
                    ended = q.timestamp or ended
                continue
            if t == "system":
                # The agent's own notices, kept as system messages the view can switch on: an API
                # error, a refusal answered by a fallback model, the summary it wrote while you
                # were away. Off by default in every view (`Filter.notices`), because they are not
                # part of the conversation; `summarize` sees them regardless, so an API error
                # shows up under *errors*. `turn_duration` is a number, not a notice.
                st = rec.get("subtype") or "system"
                text = (rec.get("content") or "").strip() if isinstance(rec.get("content"), str) else ""
                if st == "turn_duration" or not text:
                    continue
                kind = "error" if st in ("api_error", "model_refusal_fallback") else "info"
                msgs.append(Message(len(msgs), "system", parse_ts(rec.get("timestamp")),
                                    [ContentBlock(kind, text, meta={"subtype": st})],
                                    id=rec.get("uuid"), meta={"agent_notice": st}))
                continue
            if t not in ("user", "assistant"):
                continue
            ts = parse_ts(rec.get("timestamp"))
            started = started or ts
            ended = ts or ended
            cwd = cwd or rec.get("cwd")
            version = version or rec.get("version")
            branch = branch or rec.get("gitBranch")
            m = rec.get("message")
            if not isinstance(m, dict):           # a record of an unexpected shape is skipped, not fatal to the session
                m = {}
            blocks = self._blocks(m.get("content"), t)
            if not blocks:
                continue
            for b in blocks:
                self._restore_spill(b, side)
            usage = m.get("usage")
            tok = None
            if isinstance(usage, dict) and usage:
                tok = TokenUsage(
                    input=usage.get("input_tokens"),
                    output=usage.get("output_tokens"),
                    cached=usage.get("cache_read_input_tokens"),
                    thinking=(usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}).get("thinking_tokens"),
                )
                tok.total = sum(v for v in (tok.input, tok.output, tok.cached,
                                             usage.get("cache_creation_input_tokens")) if v)
            model = m.get("model") or model
            role = "user" if t == "user" else "assistant"
            # a user record that is ONLY tool results is the tool channel, not the human
            if role == "user" and all(b.kind == "tool_result" for b in blocks):
                role = "tool"
            meta = {}
            if rec.get("isSidechain"):
                meta["sidechain"] = True
            if rec.get("isMeta"):
                meta["env"] = True
            if role == "user" and blocks and blocks[0].text.lstrip().startswith(("<system-reminder>", "<local-command", "<command-name>")):
                meta["env"] = True
            origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
            if role == "user" and origin.get("kind") == "peer":
                # another session's message: Claude Code flags it isMeta, but it is a message the
                # conversation answered, not context the harness injected (shown per `Filter.peers`)
                meta.pop("env", None)
                meta["peer"] = _peer(origin)
            answers = _answers(rec.get("toolUseResult"))
            if answers:
                for b in blocks:
                    if b.kind == "tool_result":
                        b.meta["answers"] = answers     # AskUserQuestion's structured reply: exact, where the text is a paraphrase
            msgs.append(Message(len(msgs), role, ts, blocks, id=rec.get("uuid"),
                                model=m.get("model"), tokens=tok, meta=meta))
        return Session(self.name, ref.id, ref.path, msgs, title=custom_title or record_title or title, project=ref.project,
                       cwd=cwd, model=model, cli_version=version, started=started, ended=ended,
                       meta={"git_branch": branch, "source_file": str(ref.path),
                             "subagents": [str(p) for p in subagents],
                             **({"skipped_lines": len(bad)} if bad else {})})

    @staticmethod
    def _blocks(content, rec_type: str) -> list[ContentBlock]:
        if isinstance(content, str):
            return [ContentBlock("text", content)] if content.strip() else []
        out: list[ContentBlock] = []
        for b in content or []:
            if isinstance(b, str):
                out.append(ContentBlock("text", b))
                continue
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                out.append(ContentBlock("text", b.get("text", "")))
            elif bt == "thinking":
                out.append(ContentBlock("thinking", b.get("thinking", "")))
            elif bt == "tool_use":
                out.append(ContentBlock("tool_call", name=b.get("name"), call_id=b.get("id"), args=b.get("input")))
            elif bt == "tool_result":
                out.append(ContentBlock("tool_result", result_text(b.get("content")),
                                        call_id=b.get("tool_use_id"), is_error=bool(b.get("is_error"))))
            elif bt == "image":
                out.append(ContentBlock("image", "[image]"))
        return out
