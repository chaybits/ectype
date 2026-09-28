"""Running the target agent's own CLI, for the two things only it can tell us.

**Mint a template.** A cross-agent conversion has to produce a file the target will accept, and
the envelope (version strings, flags, whatever the agent stamps on a new session) is the agent's
business, not ours. `--template` already lets you hand over a real session of the target so the
envelope is genuine; minting is the same thing without the hunting: run the agent once, headless,
with the cheapest model, and use the session it just wrote. The envelope then matches the release
that is actually installed rather than one that happened to be lying around.

**Verify a resume.** Writing a file the agent parses is not the same as writing one it will
resume. The first three writable targets were each proven by hand on 2026-09-04 by installing a
converted session and resuming it for real, and Cline on 2026-09-18 through its terminal UI;
`--verify` is that check, automated, so it can be re-run after an agent upgrade instead of
trusted from a dated table.

Both spend a real API call on the user's own account, and both need the target's CLI installed and
signed in. That is why neither is ever the default: they are flags, and a missing CLI is reported
rather than worked around.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

# Per target: how to make it write one throwaway session, and how to make it resume one. The
# prompt is deliberately trivial and the model deliberately the cheapest available, because the
# only thing being bought is a file.
AGENT_CLI: dict[str, dict] = {
    "claude-code": {
        "bin": "claude",
        "mint": ["-p", "ok", "--model", "haiku"],
        "resume": lambda sid: ["--resume", sid, "-p", "ok", "--model", "haiku"],
        "note": "resumes by session id",
    },
    "codex": {
        "bin": "codex",
        "mint": ["exec", "--skip-git-repo-check", "ok"],
        "resume": lambda sid: ["exec", "resume", sid, "--skip-git-repo-check", "ok"],
        "note": "resumes a rollout by id",
    },
    "gemini-cli": {
        "bin": "gemini",
        "mint": ["-p", "ok"],
        # by full session id: 0.58.0's `--resume` takes "latest", an index or a UUID (resolveSession /
        # findSession in its bundle). `latest` picks the newest START time, which an installed older
        # session never is, so a verify by `latest` proved a different conversation.
        "resume": lambda sid: ["--resume", sid, "-p", "ok"],
        "note": "resumes by session id",
    },
    "cline": {
        "bin": "cline",
        "mint": ["-t", "120", "Reply with exactly the word ok"],
        # `cline --id` opens the session in the CLI's terminal UI and nothing else (3.0.62 refuses
        # to run it headless, with or without --json or piped input), so a verify drives that UI
        # through a pseudo-terminal and reads the session's own messages file for the reply.
        "tui_resume": lambda sid: ["--id", sid],
        "replied": lambda sid: _cline_assistant_messages(sid),
        "note": "resumes by id in its terminal UI, driven through a pseudo-terminal",
    },
}

TIMEOUT = 180          # a cold agent start plus one token of output; anything longer is a hang
TUI_SETTLE = 8         # seconds a terminal UI gets to draw and load the session before the prompt is typed
TUI_QUIET = 10         # seconds of no output after activity that count as "the reply is in"
TUI_MIN_OUTPUT = 2000  # bytes a UI must have drawn before silence can end the wait: the Cline 3.0.62 UI redraws the
                       # session and its prompt in more than this, so less means it has not loaded yet
TUI_MIN_RUN = 15       # seconds before silence can end the wait: settle plus one model round trip at the least


def _cline_assistant_messages(sid: str) -> tuple[int, str]:
    """(number of assistant messages, text of the last one) in a Cline CLI session's messages file."""
    import json
    from . import adapters
    p = adapters.get("cline").home() / sid / f"{sid}.messages.json"
    try:
        msgs = json.loads(p.read_text(encoding="utf-8-sig")).get("messages") or []
    except (OSError, ValueError):
        return 0, ""
    replies = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]
    last = replies[-1] if replies else {}
    text = "".join(c.get("text", "") for c in (last.get("content") or []) if isinstance(c, dict) and c.get("type") == "text")
    return len(replies), text


class AgentUnavailable(RuntimeError):
    """The target's CLI is not installed, or ectype does not know how to drive it."""


class AgentTimedOut(AgentUnavailable):
    """The target's CLI ran but did not finish within TIMEOUT: a hang, reported rather than raised
    as a traceback (a caller that handles AgentUnavailable handles this too)."""


def _cli(target: str) -> dict:
    spec = AGENT_CLI.get(target)
    if not spec:
        raise AgentUnavailable(f"ectype cannot drive {target!r}: known CLIs are {', '.join(AGENT_CLI)}")
    if not shutil.which(spec["bin"]):
        raise AgentUnavailable(f"{spec['bin']!r} is not on PATH, so {target} cannot be run here")
    return spec


def _run(args: list[str], cwd: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT,
                          cwd=cwd or None, check=False)


def mint_template(target: str, workspace: str | None = None) -> Path:
    """Run `target` once so it writes a session, and return that file.

    The newest session of that agent after the run, compared against the newest before it, so a
    session someone else's process wrote at the same moment cannot be mistaken for ours."""
    from . import adapters
    spec = _cli(target)
    ad = adapters.get(target)
    before = {r.path for r in ad.discover()}
    try:
        p = _run([spec["bin"], *spec["mint"]], workspace)
    except subprocess.TimeoutExpired:
        raise AgentTimedOut(f"{spec['bin']} did not finish within {TIMEOUT}s while minting a template") from None
    after = [r for r in ad.discover() if r.path not in before]
    if not after:
        raise AgentUnavailable(
            f"{spec['bin']} ran (exit {p.returncode}) but wrote no new session"
            + (f": {(p.stderr or p.stdout or '').strip()[:200]}" if p.returncode else ""))
    return max(after, key=lambda r: r.mtime).path


def verify_resume(target: str, sid: str, workspace: str | None = None) -> tuple[bool, str]:
    """Resume `sid` in `target` and say whether it answered. (ok, one line of detail).

    "Answered" is the agent exiting cleanly with something on stdout: the prompt asks for nothing,
    so any reply is proof it loaded the session and ran. The reply's first line is reported rather
    than judged, because no heuristic can tell a model's answer from the agent's own chatter.
    Observed once, worth knowing: run from INSIDE another agent's session, a nested `claude` can
    exit 0 having printed a permission notice instead of a reply, which passes this check without
    the model ever seeing the session. Verify from a plain terminal."""
    spec = _cli(target)
    if "tui_resume" in spec:
        return _tui_resume(spec, sid, "Reply with exactly the word ok", workspace)
    t0 = time.time()
    try:
        p = _run([spec["bin"], *spec["resume"](sid)], workspace)
    except subprocess.TimeoutExpired:
        return False, f"{spec['bin']} did not finish within {TIMEOUT}s"
    took = time.time() - t0
    if p.returncode != 0:
        return False, f"exit {p.returncode} after {took:.0f}s: {(p.stderr or p.stdout or '').strip().splitlines()[-1][:200] if (p.stderr or p.stdout).strip() else 'no output'}"
    reply = (p.stdout or "").strip()
    if not reply:
        return False, f"exit 0 after {took:.0f}s but no reply, so the session may have loaded empty"
    return True, f"resumed in {took:.0f}s, {spec['note']}, replied {reply.splitlines()[0][:60]!r}"


def _tui_resume(spec: dict, sid: str, prompt: str, workspace: str | None) -> tuple[bool, str]:
    """Resume a session in an agent whose resume exists only in its terminal UI.

    Opens the UI in a pseudo-terminal, lets it settle, types the prompt, waits for the output to
    go quiet after activity, leaves the UI (so it flushes the session), and asks
    `spec["replied"]` whether the session gained an assistant message. The UI's text is not
    parsed: the session file is the only thing that says a reply was made and stored. POSIX only.
    Learned on Cline 3.0.62: typing before the UI has loaded the session, or killing the UI before
    it has written, both look like "no reply" while the file is fine, hence the settle and the
    clean exit sequence.
    """
    import os as _os
    if _os.name == "nt":
        raise AgentUnavailable(f"{spec['bin']} resumes only in its terminal UI, and driving one needs a POSIX pseudo-terminal")
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    before, _ = spec["replied"](sid)
    t0 = time.time()
    pid, fd = pty.fork()
    if pid == 0:                                                     # the child: become the agent
        _os.environ.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"})
        try:
            if workspace:
                _os.chdir(workspace)
            _os.execvp(spec["bin"], [spec["bin"], *spec["tui_resume"](sid)])
        finally:
            _os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    def drain(seconds: float) -> int:
        end = time.time() + seconds
        n = 0
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    n += len(_os.read(fd, 65536))
                except OSError:
                    break
        return n
    exited: int | None = None                # the UI's exit code once it is gone; reaped exactly once

    def running() -> bool:
        nonlocal exited
        if exited is None:
            done, status = _os.waitpid(pid, _os.WNOHANG)
            if done:
                exited = _os.waitstatus_to_exitcode(status)
        return exited is None

    def close_fd() -> None:
        try:
            _os.close(fd)
        except OSError:
            pass

    drain(TUI_SETTLE)
    # A UI that is already gone (a session it could not open, a CLI that refused to start) used to
    # surface as EIO from the write below, or as the full TIMEOUT of silence; it is a plain answer.
    if not running():
        close_fd()
        return False, f"{spec['bin']} exited with code {exited} before the prompt could be typed"
    try:
        _os.write(fd, prompt.encode("utf-8") + b"\r")
    except OSError as e:
        running()
        close_fd()
        return False, f"could not type into {spec['bin']}'s terminal ({e}); exit code {exited}"
    seen = 0
    t1 = last_byte = time.time()
    while time.time() - t1 < TIMEOUT:
        n = drain(1)
        if n:
            seen += n
            last_byte = time.time()
        if not running():
            break                                # gone: waiting for quiet would only add TIMEOUT
        # quiet is measured in seconds since the last byte, not in loop turns (a turn is shorter
        # when the read returns at once)
        if seen > TUI_MIN_OUTPUT and time.time() - last_byte >= TUI_QUIET and time.time() - t1 > TUI_MIN_RUN:
            break
    if running():
        for seq in (b"/exit\r", b"\x03", b"\x03", b"\x04"):         # leave politely, then firmly
            try:
                _os.write(fd, seq)
            except OSError:
                break
            drain(1)
        for _ in range(20):
            if not running():
                break
            time.sleep(0.5)
        else:
            for sig in (signal.SIGTERM, signal.SIGKILL):             # then by force, and reap it
                try:
                    # the whole process group: pty.fork() made the UI a session and group leader, and
                    # a helper it started that ignores signals would otherwise outlive the verify
                    _os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                except PermissionError:
                    _os.kill(pid, sig)
                for _ in range(10):
                    if not running():
                        break
                    time.sleep(0.5)
                if not running():
                    break
    close_fd()
    after, text = spec["replied"](sid)
    took = time.time() - t0
    if after <= before:
        if exited not in (None, 0):
            return False, f"the terminal UI exited with code {exited} after {took:.0f}s without writing a reply"
        return False, f"the terminal UI ran for {took:.0f}s but the session file gained no assistant message"
    return True, f"resumed in {took:.0f}s, {spec['note']}, replied {text.splitlines()[0][:60] if text else '(empty)'!r}"
