"""Running the target agent's own CLI, for the two things only it can tell us.

**Mint a template.** A cross-agent conversion has to produce a file the target will accept, and
the envelope (version strings, flags, whatever the agent stamps on a new session) is the agent's
business, not ours. `--template` already lets you hand over a real session of the target so the
envelope is genuine; minting is the same thing without the hunting: run the agent once, headless,
with the cheapest model, and use the session it just wrote. The envelope then matches the release
that is actually installed rather than one that happened to be lying around.

**Verify a resume.** Writing a file the agent parses is not the same as writing one it will
resume. The three writable targets were each proven by hand on 2026-09-04 by installing a
converted session and resuming it for real; `--verify` is that check, automated, so it can be
re-run after an agent upgrade instead of trusted from a dated table.

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
        # Gemini resumes by position, not by id: `latest` is the only stable handle, so a verify
        # here proves the newest session resumes, which after an install is the one just written.
        "resume": lambda sid: ["--resume", "latest", "-p", "ok"],
        "note": "resumes by index (latest), not by id",
    },
}

TIMEOUT = 180          # a cold agent start plus one token of output; anything longer is a hang


class AgentUnavailable(RuntimeError):
    """The target's CLI is not installed, or ectype does not know how to drive it."""


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
    p = _run([spec["bin"], *spec["mint"]], workspace)
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
