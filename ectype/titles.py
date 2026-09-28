"""Renaming a session, and the names ectype keeps on its own side.

Two tiers, because "rename" means different things to thirteen stores:

* **native** where the agent has a place for a name it did not generate. Claude Code is the clean
  case: `<uuid>/custom-title.json` is the file the agent itself writes when you name a session, so
  writing it is using the store as intended, and the name shows up in the agent too.
* **local** for the rest. The name is kept here instead of being written into another program's
  database, which is the promise the README's disclaimer makes.

Its own file, deliberately NOT `settings.json`: saving settings is a full replace (that is how the
session list's columns were once wiped by an unrelated dialog), and a name the user typed must not
be something a Settings save can destroy.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import settings

_LOCK = threading.RLock()
_cache: dict[str, str] | None = None
_unreadable: bool = False        # the file exists but did not parse: put() keeps it aside before writing


def path() -> Path:
    """`titles.json` beside the settings file, so ECTYPE_CONFIG moves both together."""
    return settings.config_path().with_name("titles.json")


def _key(agent: str, sid: str) -> str:
    return f"{agent}:{sid}"


def names() -> dict[str, str]:
    """Every local name, keyed `agent:id`. Unreadable file = no names, never an exception: a
    rename is a convenience, and losing the file must not stop a session from loading.
    (Was `all()`, which shadowed the builtin inside this module.)"""
    global _cache, _unreadable
    with _LOCK:
        if _cache is None:
            _unreadable = False
            try:
                data = json.loads(path().read_text(encoding="utf-8-sig"))
                _cache = {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}
            except FileNotFoundError:
                _cache = {}
            except (OSError, ValueError, AttributeError):
                _cache = {}
                _unreadable = path().exists()
        return dict(_cache)


def get(agent: str, sid: str) -> str | None:
    return names().get(_key(agent, sid))


def put(agent: str, sid: str, title: str) -> None:
    """Store (or, with an empty title, forget) one local name. Atomic, like the settings write.
    (Was `set()`, which shadowed the builtin inside this module.)"""
    global _cache, _unreadable
    with _LOCK:
        # re-read the file, not the cache: another ectype process (a second GUI, the CLI) may have
        # renamed a session since this one loaded it, and writing the stale cache back erased that name
        _cache = None
        data = names()
        k = _key(agent, sid)
        if not title.strip() and k not in data:
            return          # clearing a name nobody set: do not create an empty file to say so
        if title.strip():
            data[k] = title.strip()
        else:
            data.pop(k, None)
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if _unreadable and p.exists():
            # the user's own names, in a file that no longer parses (a stray comma from a hand edit):
            # kept, never written over with the one new name, as settings.save() does (audit F62)
            bad = p.with_name(p.name + f".bad-{datetime.now():%Y%m%d-%H%M%S}")
            p.replace(bad)
            print(f"ectype: the unreadable {p.name} was kept as {bad.name}", file=sys.stderr)
            _unreadable = False
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        _cache = data


def invalidate() -> None:
    """Forget the cache (the file changed underneath us, or a test wrote its own)."""
    global _cache
    with _LOCK:
        _cache = None
