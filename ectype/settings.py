"""User settings: one JSON file per platform (override with $ECTYPE_CONFIG).

    Linux    $XDG_CONFIG_HOME/ectype/settings.json   (default ~/.config/ectype/settings.json)
    macOS    ~/Library/Application Support/ectype/settings.json
    Windows  %APPDATA%\\ectype\\settings.json

Store-location precedence, highest first:

    $ECTYPE_<AGENT>_HOME  >  settings["agents"][name]["home"]  >  the adapter's default

The env var stays on top so a one-off run against a fixture never depends on the file.
A missing file means all defaults; nothing personal is hard-coded here.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

APP = "ectype"
TABLE_COLUMNS = ("blocks", "chars", "remaining", "share")   # the budget table's optional columns
# the session list's columns, in display order; `name` is always shown and cannot be turned off
LIST_COLUMNS = ("name", "agent", "id", "modified", "size", "project")
LIST_SORTS = ("name", "agent", "modified", "size", "project")
LEGACY_COLUMNS = {"full", "removed", "tokens"}              # v0.2 names; tokens is always shown now
MODES = ("brief", "custom", "full")                         # how much of a tool result to include
STAMPS = ("full", "time", "none")                           # per-message timestamp: 18 / 11 / 5 tokens
WRAP_ROLES = ("", "user", "system", "assistant")            # emit the transcript as one message of this role
HIDE_ROLES = ("user", "assistant")                          # the sides a view can leave out (the toolbar's two checkboxes)
NOTICE_ITEMS = ("paths", "time", "os", "tools", "state")                     # see ectype/notice.py
REDACT_RULES = ("home", "media", "username", "hostname", "email", "keys", "ip")

DEFAULTS: dict = {
    # name -> {"enabled": bool | None, "home": str | None}; None = leave the adapter's default
    "agents": {},
    "view": {
        "color_roles": True,        # colour [USER] / [ASSISTANT] / [TOOL] headers in the preview
        "reference": 200000,        # the number the budget bar compares "selected" against (e.g. a model's context window)
        "mode": "custom",           # brief | custom | full: how much of a tool result is included
        "cap": 150,                 # TOKENS kept per tool result / thinking block in CUSTOM mode
        "tool_headers": False,      # give tool turns their own "[TOOL] <timestamp>" line (~21 tokens each)
        "stamps": "full",           # per-message timestamp: full date+time | time only | none
        "markers": True,            # print the [USER] / [ASSISTANT] line at all
        "collapse": True,           # merge back-to-back turns of the same actor: one header per run of calls, not one per call
        "wrap": "",                 # "", or a role: json/jsonl/csv become ONE message of that role
        "thinking": False,
        "tools": True,
        "env": False,
        "notices": False,           # the agent's own notices (API errors, refusals, away summaries), where the store keeps them
        "questions": True,          # a question the agent put to you, and your answer, shown as the turns they are
        "peers": True,              # messages other sessions sent in (Claude Code's peer messages), under their own label
        "table": {"columns": ["blocks", "remaining", "share"], "hide_empty": True},
        # the session list, as a file manager shows one: which columns, and how it is sorted
        "list": {"columns": ["name", "agent", "modified", "size"], "sort": "modified", "desc": True},
    },
    "redact": {                     # which rules the "redact" checkbox applies
        "home": True, "media": True, "username": True, "hostname": True,
        "email": True, "keys": True, "ip": False,
        "timezone": False,          # leave the UTC offset (+0300 …) out of the session header; the clock itself is not shifted
        "terms": [],                # persistent extra literals: "text" or "text=Replacement"
    },
    "export": {
        "mode": "download",         # "download" = browser download; "folder" = written server-side
        "folder": "",               # "" = the --export-dir given to `gui`, else ~/ectype-exports
        "format": "text",
    },
    "notice": {                     # the import notice appended as the last message (ectype/notice.py)
        "enabled": True,
        "detailed": False,          # False = one generic sentence; True = one bullet per item below
        "items": list(NOTICE_ITEMS),
    },
    "convert": {"warned": False},   # the one-time conversion warning has been shown
    "gui": {
        "port": 8765,               # `ectype gui` listens here unless --port says otherwise; the host is always 127.0.0.1
    },
    "backup": {                     # ectype backup, and the copies an install takes by itself
        "keep_last": 0,             # keep at most this many backup folders (0 = all of them)
        "keep_days": 0,             # delete backup folders older than this many days (0 = never)
    },
    "skill": {                      # how `ectype recall` (the /ectype slash command) brings a session in: every view option, its own defaults
        "command": "ectype",        # the slash command's name: /ectype, or whatever `ectype skill install` wrote
        "mode": "brief",            # brief | custom | full: the tool-output mode, once tool calls are on
        "cap": 150,                 # tokens per tool result in custom mode
        "thinking": False,
        "tools": False,             # tool calls and results at all; off = the conversation alone (the user, 2026-09-27)
        "env": False,               # injected system/environment messages
        "notices": False,           # the agent's own notices
        "questions": True,          # questions put to the user, and the answers, as turns
        "peers": True,              # messages other sessions sent in
        "collapse": True,           # merge back-to-back turns of the same actor
        "wrap": "",                 # "", or a role: the whole transcript as one message of it
        "stamps": "full",           # per-message timestamp: full | time | none
        "markers": True,            # the [USER] / [ASSISTANT] lines
        "tool_headers": False,      # a tool turn's own [TOOL] line
        "hide_roles": [],           # "user" and/or "assistant": leave that side's turns out
        "redact": False,            # apply Settings -> Redaction to what is brought in
        "notice": False,            # append the import notice
        "summarize": True,          # also save the session's summary into ectype's own store (ledger)
    },
    "summary": {                    # ectype summarize, and the summaries kept for search (ectype/summary.py)
        # an agent's closing summary is the text it wrote between two of one of these, in one of its
        # own turns: literal strings, several allowed, an empty list switches the section off. The
        # default holds no character Markdown gives a meaning to, so a rendered reply shows the marker
        # as written; `-*-summary-*-`, the default until 2026-09-27, showed as `--summary--` because
        # its asterisks became italics (ARCHITECTURE D24)
        "markers": ["%%SUMMARY%%"],
    },
}

_cache: dict | None = None
_STAMP: tuple | None = None      # (path, mtime_ns, size) of the file the cache was read from
_LOAD_ERROR: str | None = None   # set when the file exists but could not be read as settings
_LOCK = threading.RLock()        # load() and save() are called from the web server's request threads


def load_error() -> str | None:
    """Why the settings file was ignored, or None. A file that fails to parse is NOT treated as
    absent in silence: the defaults apply, this says so, and `save()` keeps the broken file."""
    load()                      # stamp-aware: a file broken after start-up is noticed here too
    return _LOAD_ERROR


def config_path() -> Path:
    env = os.environ.get("ECTYPE_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP / "settings.json"


def _migrate_columns(cols: list) -> list[str]:
    """v0.2 had `tokens`, `full` and `removed` columns. `tokens` is now always shown, and `full`
    and `removed` were replaced by one `remaining` column, so a saved choice keeps its meaning
    instead of quietly leaving the table with nothing in it."""
    out = [c for c in cols if c in TABLE_COLUMNS]
    if any(c in ("full", "removed") for c in cols) and "remaining" not in out:
        out.append("remaining")
    return out


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _file_stamp() -> tuple:
    p = config_path()
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), None, None)


def load(fresh: bool = False) -> dict:
    """Settings merged over DEFAULTS. Cached, and re-read when the file changed on disk (or
    `fresh=True`): the GUI is one long-lived process, and a cache that never looked again made a
    hand edit or an `ectype skill install --name` invisible to it, then overwrote them with the next
    small save, and bypassed the `.bad-<stamp>` protection for a file broken after start-up."""
    global _cache, _LOAD_ERROR, _STAMP
    with _LOCK:
        stamp = _file_stamp()
        if _cache is not None and not fresh and stamp == _STAMP:
            return copy.deepcopy(_cache)
        _STAMP = stamp
        data: dict = {}
        p = config_path()
        _LOAD_ERROR = None
        if p.exists():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    _LOAD_ERROR = f"{p}: not a JSON object"
            except (OSError, ValueError) as e:
                _LOAD_ERROR = f"{p}: {e}"
            if _LOAD_ERROR:
                print(f"ectype: settings file ignored, defaults in effect ({_LOAD_ERROR})", file=sys.stderr)
        return _apply(data)


def _apply(data: dict) -> dict:
    """Merge a loaded document over DEFAULTS (with the v0.2 migrations) into the cache."""
    global _cache
    view = data.get("view") or {}
    if isinstance(view, dict):
        if "reference" not in view and "context_window" in view:                # v0.2.0 name
            view["reference"] = view.pop("context_window")
        table = view.get("table")
        if isinstance(table, dict) and isinstance(table.get("columns"), list):  # v0.2.0 columns
            table["columns"] = _migrate_columns(table["columns"])
    _cache = _merge(DEFAULTS, data)
    return copy.deepcopy(_cache)


def _bool(section: str, key: str, v) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{section}.{key} must be true or false")
    return v


def _int(section: str, key: str, v, lo: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) < lo:
        raise ValueError(f"{section}.{key} must be an integer >= {lo}")
    return int(v)


def _strlist(section: str, key: str, v, allowed: tuple[str, ...] | None = None) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{section}.{key} must be a list of strings")
    if allowed is not None:
        bad = [x for x in v if x not in allowed]
        if bad:
            raise ValueError(f"{section}.{key}: unknown {', '.join(bad)} (allowed: {', '.join(allowed)})")
    return [x.strip() for x in v if x.strip()] if allowed is None else list(dict.fromkeys(v))


def validate(data: dict) -> dict:
    """Coerce an incoming dict to the schema. Unknown keys are dropped; bad values raise ValueError."""
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    out = copy.deepcopy(DEFAULTS)

    agents = data.get("agents") or {}
    if not isinstance(agents, dict):
        raise ValueError("agents must be an object")
    for name, cfg in agents.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"agents.{name} must be an object")
        en = cfg.get("enabled")
        if en is not None and not isinstance(en, bool):
            raise ValueError(f"agents.{name}.enabled must be true or false")
        home = cfg.get("home")
        if home is not None and not isinstance(home, str):
            raise ValueError(f"agents.{name}.home must be a string")
        home = (home or "").strip() or None
        if en is None and home is None:
            continue
        out["agents"][str(name)] = {"enabled": en, "home": home}

    view = data.get("view") or {}
    if not isinstance(view, dict):
        raise ValueError("view must be an object")
    if "reference" not in view and "context_window" in view:
        view = {**view, "reference": view["context_window"]}
    for key in ("color_roles", "thinking", "tools", "env", "notices", "questions", "peers", "tool_headers", "markers", "collapse"):
        if key in view:
            out["view"][key] = _bool("view", key, view[key])
    if "stamps" in view:
        if view["stamps"] not in STAMPS:
            raise ValueError(f"view.stamps must be one of {', '.join(STAMPS)}")
        out["view"]["stamps"] = view["stamps"]
    if "wrap" in view:
        if view["wrap"] not in WRAP_ROLES:
            raise ValueError(f"view.wrap must be empty or one of {', '.join(r for r in WRAP_ROLES if r)}")
        out["view"]["wrap"] = view["wrap"]
    if "reference" in view:
        out["view"]["reference"] = _int("view", "reference", view["reference"], 1)
    if "cap" in view:
        out["view"]["cap"] = _int("view", "cap", view["cap"], 0)
    if "mode" in view:
        if view["mode"] not in MODES:
            raise ValueError(f"view.mode must be one of {', '.join(MODES)}")
        out["view"]["mode"] = view["mode"]
    table = view.get("table") or {}
    if not isinstance(table, dict):
        raise ValueError("view.table must be an object")
    if "columns" in table:
        # a column list is a display preference: names retired since v0.2 migrate, never fail
        if not isinstance(table["columns"], list) or not all(isinstance(x, str) for x in table["columns"]):
            raise ValueError("view.table.columns must be a list of strings")
        out["view"]["table"]["columns"] = _migrate_columns(table["columns"])

    lst = view.get("list") or {}
    if not isinstance(lst, dict):
        raise ValueError("view.list must be an object")
    if "columns" in lst:
        cols = _strlist("view.list", "columns", lst["columns"], LIST_COLUMNS)
        out["view"]["list"]["columns"] = ["name"] + [c for c in cols if c != "name"]   # name is fixed
    if "sort" in lst:
        if lst["sort"] not in LIST_SORTS:
            raise ValueError(f"view.list.sort must be one of {', '.join(LIST_SORTS)}")
        out["view"]["list"]["sort"] = lst["sort"]
    if "desc" in lst:
        out["view"]["list"]["desc"] = _bool("view.list", "desc", lst["desc"])
    if "hide_empty" in table:
        out["view"]["table"]["hide_empty"] = _bool("view.table", "hide_empty", table["hide_empty"])

    red = data.get("redact") or {}
    if not isinstance(red, dict):
        raise ValueError("redact must be an object")
    for key in (*REDACT_RULES, "timezone"):
        if key in red:
            out["redact"][key] = _bool("redact", key, red[key])
    if "terms" in red:
        out["redact"]["terms"] = _strlist("redact", "terms", red["terms"])

    exp = data.get("export") or {}
    if not isinstance(exp, dict):
        raise ValueError("export must be an object")
    if "mode" in exp:
        if exp["mode"] not in ("download", "folder"):
            raise ValueError("export.mode must be 'download' or 'folder'")
        out["export"]["mode"] = exp["mode"]
    if "folder" in exp:
        if not isinstance(exp["folder"], str):
            raise ValueError("export.folder must be a string")
        out["export"]["folder"] = exp["folder"].strip()
    if "format" in exp:
        if not isinstance(exp["format"], str) or not exp["format"]:
            raise ValueError("export.format must be a non-empty string")
        out["export"]["format"] = exp["format"]

    notice = data.get("notice") or {}
    if not isinstance(notice, dict):
        raise ValueError("notice must be an object")
    for key in ("enabled", "detailed"):
        if key in notice:
            out["notice"][key] = _bool("notice", key, notice[key])
    if "items" in notice:
        out["notice"]["items"] = _strlist("notice", "items", notice["items"], NOTICE_ITEMS)

    conv = data.get("convert") or {}
    if not isinstance(conv, dict):
        raise ValueError("convert must be an object")
    if "warned" in conv:
        out["convert"]["warned"] = _bool("convert", "warned", conv["warned"])

    gui = data.get("gui") or {}
    if not isinstance(gui, dict):
        raise ValueError("gui must be an object")
    if "port" in gui:
        port = _int("gui", "port", gui["port"], 1)
        if port > 65535:
            raise ValueError("gui.port must be between 1 and 65535")
        out["gui"]["port"] = port

    bk = data.get("backup") or {}
    if not isinstance(bk, dict):
        raise ValueError("backup must be an object")
    for key in ("keep_last", "keep_days"):
        if key in bk:
            out["backup"][key] = _int("backup", key, bk[key], 0)

    skill = data.get("skill") or {}
    if not isinstance(skill, dict):
        raise ValueError("skill must be an object")
    if "command" in skill:
        from .skill import valid_name
        out["skill"]["command"] = valid_name(skill["command"])
    if "mode" in skill:
        if skill["mode"] not in MODES:
            raise ValueError(f"skill.mode must be one of {', '.join(MODES)}")
        out["skill"]["mode"] = skill["mode"]
    if "cap" in skill:
        out["skill"]["cap"] = _int("skill", "cap", skill["cap"], 0)
    for key in ("thinking", "tools", "env", "notices", "questions", "peers", "collapse", "markers", "tool_headers",
                "redact", "notice", "summarize"):
        if key in skill:
            out["skill"][key] = _bool("skill", key, skill[key])
    if "stamps" in skill:
        if skill["stamps"] not in STAMPS:
            raise ValueError(f"skill.stamps must be one of {', '.join(STAMPS)}")
        out["skill"]["stamps"] = skill["stamps"]
    if "wrap" in skill:
        if skill["wrap"] not in WRAP_ROLES:
            raise ValueError(f"skill.wrap must be empty or one of {', '.join(r for r in WRAP_ROLES if r)}")
        out["skill"]["wrap"] = skill["wrap"]
    if "hide_roles" in skill:
        out["skill"]["hide_roles"] = _strlist("skill", "hide_roles", skill["hide_roles"], HIDE_ROLES)

    summ = data.get("summary") or {}
    if not isinstance(summ, dict):
        raise ValueError("summary must be an object")
    if "markers" in summ:
        v = summ["markers"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError("summary.markers must be a list of strings")
        marks: list[str] = []
        for x in v:                 # trimmed, no blanks, no duplicates; the order is kept
            x = x.strip()
            if x and x not in marks:
                marks.append(x)
        out["summary"]["markers"] = marks
    return out


def save(data: dict) -> dict:
    """Validate, write, and return the stored settings.

    The write is atomic (temp file, then rename), so a crash cannot leave a truncated file. A file
    that `load()` could not read is renamed to `settings.json.bad-<stamp>` first: it is the user's
    own text, and the defaults being saved over it is how a hand edit used to disappear."""
    global _cache, _LOAD_ERROR
    with _LOCK:
        v = validate(data)
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        load()                                   # cheap when unchanged; recomputes _LOAD_ERROR when the file moved
        if _LOAD_ERROR and p.exists():
            bad = p.with_name(p.name + f".bad-{datetime.now():%Y%m%d-%H%M%S}")
            p.replace(bad)
            print(f"ectype: the unreadable settings file was kept as {bad.name}", file=sys.stderr)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(v, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        _cache = v
        _LOAD_ERROR = None
        global _STAMP
        _STAMP = _file_stamp()
        return copy.deepcopy(v)


def patch(section_values: dict) -> dict:
    """Save a partial document merged over the settings as they are on disk NOW. The page's small
    saves (the list's sort and columns, a dialog's options) use this: posting the whole document the
    page loaded at start-up erased whatever changed on disk since (audit 2026-09-25, F55)."""
    with _LOCK:
        return save(_merge(load(fresh=True), section_values))


def _agents() -> dict:
    """The `agents` section WITHOUT the defensive deep copy `load()` makes.

    `load()` copies so a caller can edit what it gets back. The two readers below want one scalar
    and never mutate, and they are on the hottest path in the program: every `home()`,
    `available()` and `enabled()` call goes through them, and `discover()` calls `home()` again
    per store. Deep-copying the whole settings document to read one boolean cost more than
    listing the sessions did; `/api/agents` spent most of its time here."""
    if _cache is None:
        load()
    return (_cache or {}).get("agents", {})


def agent_home(name: str) -> str | None:
    """The store path the user saved for this agent, or None."""
    return (_agents().get(name) or {}).get("home") or None


def agent_enabled(name: str, default: bool) -> bool:
    v = (_agents().get(name) or {}).get("enabled")
    return default if v is None else bool(v)
