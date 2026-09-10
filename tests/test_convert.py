"""Round-trip test: load the newest real session of each agent, convert it to every writable
target into a temp dir, and re-read the result with our own adapter for that target.

    python3 tests/test_convert.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for _s in (sys.stdout, sys.stderr):            # Windows cp1252 consoles vs. → ✓ × in our output
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ectype import adapters                       # noqa: E402
from ectype.convert import WRITERS, convert       # noqa: E402
from ectype.transform import Redactor             # noqa: E402

ENV = {"claude-code": "ECTYPE_CLAUDE_HOME", "codex": "CODEX_HOME", "gemini-cli": "ECTYPE_GEMINI_HOME"}


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for src_name, ad in adapters.ADAPTERS.items():
            if not ad.available() or not ad.discover():
                print(f"{src_name:<12} skip"); continue
            # newest session that actually has an assistant turn (agents leave 1-2 line stubs on failed runs)
            s = None
            PERSONAL = {"sillytavern", "lmstudio", "open-webui"}
            cands = sorted(ad.discover(), key=lambda r: r.mtime, reverse=True)
            probes = [r for r in cands if "probe" in (r.id + " " + (r.title or "")).lower()]
            if probes:
                cands = probes
            elif src_name in PERSONAL:
                print(f"{src_name:<12} skip (no probe session; refusing to read personal chats)"); continue
            raw = chosen = None
            for ref in cands[:8]:
                cand = ad.load(ref)
                if any(m.role == "assistant" for m in cand.messages):
                    raw, chosen = cand, ref
                    s = Redactor.defaults().session(cand); break
            if s is None:
                print(f"{src_name:<12} skip (no session with an assistant turn)"); continue
            extra = [(src, rel) for src, rel in ad.artifacts(chosen) if src != raw.path]
            for tgt in WRITERS:
                out = Path(tmp) / f"{src_name}__{tgt}"
                # same agent in and out must take the NATIVE path: a copy, not a conversion. It
                # needs the unredacted session, because it re-reads the original file.
                native = tgt == src_name
                try:
                    path, rep = convert(raw if native else s, tgt, out, redact=Redactor.defaults().text,
                                        source_path=raw.path, extra=extra if native else None)
                    assert path.exists() and path.stat().st_size > 0
                    if native:
                        import getpass
                        me = getpass.getuser()
                        leaked = [f for f in out.rglob("*") if f.is_file() and me in f.read_text(encoding="utf-8", errors="replace")]
                        assert not leaked, f"redacted native copy still carries the username: {[f.name for f in leaked][:3]}"
                    if native:
                        assert not rep.folded and not rep.dropped, \
                            f"a native copy must lose nothing, but folded={dict(rep.folded)} dropped={dict(rep.dropped)}"
                        assert raw.count("thinking") == 0 or rep.kept.get("thinking") == raw.count("thinking"), \
                            f"native copy kept {rep.kept.get('thinking')} of {raw.count('thinking')} thinking blocks"
                    # re-read through the target adapter with its home pointed at the temp dir
                    old = os.environ.get(ENV[tgt]); os.environ[ENV[tgt]] = str(out)
                    try:
                        tad = adapters.get(tgt)
                        refs = tad.discover()
                        assert len(refs) == 1, f"expected 1 session in {out}, found {len(refs)}"
                        back = tad.load(refs[0])
                    finally:
                        if old is None: os.environ.pop(ENV[tgt], None)
                        else: os.environ[ENV[tgt]] = old
                    users = sum(1 for m in back.messages if m.role == "user" and not m.is_env)
                    asst = sum(1 for m in back.messages if m.role == "assistant")
                    assert users >= 1 and asst >= 1, f"round trip lost turns: user={users} assistant={asst}"
                    print(f"{src_name:<12} → {tgt:<12} ok  kept={dict(rep.kept)} folded={dict(rep.folded)} dropped={dict(rep.dropped)} back: user={users} assistant={asst}")
                except Exception as e:                # noqa: BLE001
                    failed += 1
                    print(f"{src_name:<12} → {tgt:<12} FAIL {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
