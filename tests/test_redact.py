"""Raw-text redaction must keep JSON valid and must not let an identifier through after an escape.

Two real cases from 2026-09-09: an e-mail right after an escaped newline ("\\n323…@x") lost the
`n` of the escape and left a bare backslash, so the copied record was no longer JSON; and the
username right after "\\n" was invisible to the alnum boundary of the username rule.
"""
from __future__ import annotations

import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ectype.transform import Redactor   # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def main() -> int:
    user = getpass.getuser()
    red = Redactor.defaults()
    line = json.dumps({"text": f"identity\n12345678+someone@users.noreply.github.com\nnext {user} line\t{user}\r{user} end \"{user}\""})
    out = red.text(line)
    ok = True
    try:
        back = json.loads(out)
    except ValueError as e:
        ok = False
        back = {"text": f"(invalid JSON after redaction: {e})"}
    check(ok, "a redacted JSON line is still valid JSON")
    check("someone@users.noreply.github.com" not in out and "user@example.com" in out, "the e-mail after an escaped newline is replaced")
    check("\\n" in out and "\\t" in out and "\\r" in out, "the escapes themselves survive")
    check(user not in back["text"], f"the username is gone after \\n, \\t, \\r and a quote ({back['text'][:60]!r})")
    plain = red.text(f"{user}\n{user} at /home/{user}/x")
    check(user not in plain, "decoded text: the username is gone at line starts and inside paths")
    check(red.text("keep \"name@example.org\" here") == "keep \"user@example.com\" here", "an e-mail that starts after a quote is replaced whole")
    print("all redaction checks passed" if not FAILS else "FAILED: " + "; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
