"""Smoke test against whatever real stores exist on this machine (no fixtures needed).

    python3 tests/test_smoke.py

For every available agent: discover, load the newest session, check the invariants the
canonical model promises. Skips agents whose store is absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for _s in (sys.stdout, sys.stderr):            # Windows cp1252 consoles vs. → ✓ × in our output
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ectype import adapters                      # noqa: E402
from ectype.render.text import RenderOptions, render  # noqa: E402

ROLES = {"user", "assistant", "system", "tool"}
KINDS = {"text", "thinking", "tool_call", "tool_result", "system", "info", "error", "image"}


def check(agent: str) -> str:
    ad = adapters.get(agent)
    if not ad.available():
        return "skip (store absent)"
    refs = ad.discover()
    if not refs:
        return "skip (no sessions)"
    # PRIVACY: never load a real user conversation. Prefer a session we generated ourselves
    # (title/id contains "probe"); otherwise skip apps whose store is personal chat history.
    PERSONAL = {"sillytavern", "lmstudio", "open-webui"}
    known = set(filter(None, (os.environ.get("ECTYPE_PROBE_IDS") or "").split(",")))
    probes = [r for r in refs if "probe" in (r.id + " " + (r.title or "")).lower() or any(r.id.startswith(k) for k in known)]
    if probes:
        cands = sorted(probes, key=lambda r: r.mtime, reverse=True)
    elif agent in PERSONAL:
        return "skip (no probe session; refusing to read personal chats)"
    else:
        cands = sorted(refs, key=lambda r: r.mtime, reverse=True)
    # The newest file can be a session that was opened and never used (Copilot Chat writes one per
    # window). An empty session is not a parse failure, so try the next-newest, up to five.
    skipped = 0
    for ref in cands[:5]:
        s = ad.load(ref)
        assert s.agent == agent and s.id == ref.id
        if s.messages:
            break
        skipped += 1
    else:
        raise AssertionError(f"no messages parsed in the {min(5, len(cands))} newest sessions")
    for m in s.messages:
        assert m.role in ROLES, m.role
        assert m.timestamp is None or m.timestamp.tzinfo is not None, "naive timestamp"
        for b in m.blocks:
            assert b.kind in KINDS, b.kind
            if b.kind == "tool_call":
                assert b.name, "tool_call without name"
    calls = s.count("tool_call"); results = s.count("tool_result")
    text = render(s, RenderOptions(thinking=True))
    assert text.startswith(f"=== {agent} ·")
    return (f"ok  {ref.short}  msgs={len(s.messages)} calls={calls} results={results} render={len(text):,} chars"
            + (f"  (skipped {skipped} empty)" if skipped else ""))


if __name__ == "__main__":
    failed = False
    for name in adapters.ADAPTERS:
        try:
            print(f"{name:<12} {check(name)}")
        except Exception as e:                # noqa: BLE001
            failed = True
            print(f"{name:<12} FAIL {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
