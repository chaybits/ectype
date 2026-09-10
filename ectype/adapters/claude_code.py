"""Claude Code: ~/.claude/projects/<project-slug>/<session-uuid>.jsonl

Append-only JSONL. Records of type user/assistant carry the conversation; tool results
arrive as `tool_result` blocks inside the NEXT user record. Other record types
(attachment, ai-title, file-history-snapshot, queue-operation, ...) are metadata.

SIDECAR FILES. A session may also own a directory of its own id next to the .jsonl:

    <slug>/<uuid>/tool-results/<hash>.txt   the FULL text of a tool result too large to inline.
                                            The transcript keeps only a stub:
                                            "<persisted-output>Output too large (46.3KB). Full
                                             output saved to: <path>  Preview (first 2KB): …"
    <slug>/<uuid>/subagents/agent-*.jsonl   Task-tool subagent transcripts, separate sessions,
                                            not part of this conversation
    <slug>/<uuid>/custom-title.json         {"customTitle": "..."}, the title you typed
    <slug>/sessions-index.json              index of the folder (first prompt, summary, counts)

The spill files are read back in, so a tool result shows what actually came out instead of a
2 KB preview. Everything else is metadata about the session, not content of it.
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
from .base import Adapter

_SPILL = re.compile(r"Full output saved to:\s*(\S+)")


def _project_label(slug: str) -> str:
    # "-home-user-projects-app" -> "app"; "C--Projects-app" -> "app"
    return slug.rstrip("-").split("-")[-1] or slug


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    label = "Claude Code"
    env_home = "ECTYPE_CLAUDE_HOME"
    default_home = "~/.claude/projects"
    writable = True
    native_format = "Claude Code .jsonl"

    def discover(self) -> list[SessionRef]:
        out = []
        for proj in sorted(self.home().iterdir()):
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                if f.stem.startswith("agent-"):
                    continue          # subagent side files, not sessions
                st = f.stat()
                out.append(SessionRef(self.name, f.stem, f,
                                      datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                                      st.st_size, project=_project_label(proj.name),
                                      title=self._custom_title(proj / f.stem) or self._peek_title(f)))
        return out

    @staticmethod
    def _custom_title(session_dir: Path) -> str | None:
        p = session_dir / "custom-title.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8-sig")).get("customTitle") or None
        except (OSError, ValueError):
            return None

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
                    if r.get("isMeta"):
                        continue
                    c = (r.get("message") or {}).get("content")
                    text = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
                    text = text.strip()
                    if text and not text.startswith(("<", "Caveat:")):
                        return text.splitlines()[0][:80]
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
        for _, rec in iter_jsonl(ref.path, bad):
            t = rec.get("type")
            if t == "ai-title":
                title = rec.get("aiTitle") or title
                continue
            if t == "summary":
                title = title or rec.get("summary")
                continue
            if t not in ("user", "assistant"):
                continue
            ts = parse_ts(rec.get("timestamp"))
            started = started or ts
            ended = ts or ended
            cwd = cwd or rec.get("cwd")
            version = version or rec.get("version")
            branch = branch or rec.get("gitBranch")
            m = rec.get("message") or {}
            blocks = self._blocks(m.get("content"), t)
            if not blocks:
                continue
            for b in blocks:
                self._restore_spill(b, side)
            usage = m.get("usage") or {}
            tok = None
            if usage:
                tok = TokenUsage(
                    input=usage.get("input_tokens"),
                    output=usage.get("output_tokens"),
                    cached=usage.get("cache_read_input_tokens"),
                    thinking=(usage.get("output_tokens_details") or {}).get("thinking_tokens"),
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
            msgs.append(Message(len(msgs), role, ts, blocks, id=rec.get("uuid"),
                                model=m.get("model"), tokens=tok, meta=meta))
        return Session(self.name, ref.id, ref.path, msgs, title=custom_title or title, project=ref.project,
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
