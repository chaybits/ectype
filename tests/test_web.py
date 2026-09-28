"""Web UI endpoint test: runs the server on a free port and drives every endpoint.

    python3 tests/test_web.py

Uses a THROWAWAY settings file (ECTYPE_CONFIG in a temp dir), never the user's. Loads one session
of the first enabled, available agent; prefer a probe session (ECTYPE_PROBE_IDS or a title/id
containing "probe"), else the smallest file. Point an agent at a fixture to keep it private:

    ECTYPE_CLAUDE_HOME=<fixture dir> python3 tests/test_web.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ectype-test-")
os.environ["ECTYPE_CONFIG"] = str(Path(_TMP) / "settings.json")      # before ectype imports anything
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ectype.web import make_server      # noqa: E402
from ectype.render.formats import FORMATS  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


class Client:
    def __init__(self, port: int):
        self.port = port

    def _req(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        r = c.getresponse()
        raw = r.read()
        hdr = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdr, raw

    def get(self, path):
        st, h, raw = self._req("GET", path)
        return st, json.loads(raw)

    def post(self, path, body):
        st, h, raw = self._req("POST", path, body)
        return st, h, raw


def pick(sessions: list[dict]) -> dict | None:
    probe = os.environ.get("ECTYPE_PROBE_IDS", "")
    ids = [p.strip() for p in probe.split(",") if p.strip()]
    for s in sessions:
        if any(s["id"].startswith(i) for i in ids):
            return s
    for s in sessions:
        if "probe" in (s.get("title") or "").lower() or "probe" in s["id"].lower():
            return s
    return min(sessions, key=lambda s: s["size"]) if sessions else None


def main() -> int:
    srv = make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(port)
    print(f"server on 127.0.0.1:{port}, settings file {os.environ['ECTYPE_CONFIG']}")

    st, agents = c.get("/api/agents")
    check(st == 200 and len(agents) == 13, f"/api/agents lists 13 adapters (got {len(agents) if st == 200 else st})")
    check(all({"name", "label", "category", "available", "enabled", "home", "home_source"} <= set(a) for a in agents), "agent rows carry the settings-page fields")
    check(all(a["category"] in ("coding", "chat") for a in agents), "every adapter has a category")
    check(all(not a["enabled"] for a in agents if a["category"] == "chat"), "chat apps are off by default")
    writable = {a["name"] for a in agents if a["writable"]}
    check(writable == {"claude-code", "codex", "gemini-cli", "cline"}, f"exactly the verified-resumable agents are writable ({', '.join(sorted(writable))})")
    check(all(a["native_format"] for a in agents if a["writable"]) and all(a["native_format"] is None for a in agents if not a["writable"]),
          "every writable agent names its resume format, and no source-only agent claims one")

    st, cfg = c.get("/api/settings")
    check(st == 200 and cfg["settings"]["export"]["mode"] == "download" and cfg["formats"] == list(FORMATS), "/api/settings returns defaults + formats")
    check({"agents", "categories", "modes", "workspaces", "columns"} <= set(cfg), "settings carry the catalogues the page needs")
    check([m["key"] for m in cfg["modes"]] == ["brief", "custom", "full"], "the three context modes are offered")
    check(all(k in writable for k in cfg["workspaces"]), "workspaces are listed only for writable agents")
    st, h, raw = c.post("/api/settings", {"settings": {"export": {"folder": _TMP}}})   # keep folder-mode files in our temp dir
    check(st == 200 and json.loads(raw)["export_dir"] == _TMP, "export folder points at the temp dir")
    st, cfg = c.get("/api/settings")

    st, sessions = c.get("/api/sessions")
    check(st == 200, "/api/sessions ok")
    listed_agents = {s["agent"] for s in sessions}
    enabled = {a["name"] for a in agents if a["enabled"] and a["available"]}
    check(listed_agents <= enabled, f"only enabled agents are listed ({', '.join(sorted(listed_agents)) or 'none'})")
    s = pick(sessions)
    if not s:
        print("no session available for an enabled agent; nothing more to test")
        return 1 if FAILS else 0
    print(f"  using {s['agent']} {s['id'][:8]} ({s['size']:,} B)")
    key = {"agent": s["agent"], "id": s["id"]}

    def render(opts):
        st, h, raw = c.post("/api/render", {**key, "opts": opts})
        j = json.loads(raw)
        if st != 200:
            raise RuntimeError(j)
        return j

    base = {"thinking": False, "tools": True, "env": False, "cap": 150}
    r = render(base)
    b = r["budget"]
    check(r["tokens"] == b["selected"] > 0, f"render returns tokens == budget.selected ({b['selected']:,})")
    # Compare CONTENT as well as whole renders. Both used to be necessary because a filtered
    # render carried a longer "shown:" header; that line is gone (2026-09-09), so the whole-render
    # comparison holds too and is asserted separately below.
    check(sum(p["tokens"] for p in b["parts"]) <= sum(p["full"] for p in b["parts"]),
          f"no view includes more content than everything ({sum(p['tokens'] for p in b['parts']):,} <= {sum(p['full'] for p in b['parts']):,})")
    parts_sum = sum(p["tokens"] for p in b["parts"]) + b["structure"]
    check(parts_sum == b["selected"], f"parts + structure == selected ({parts_sum:,} vs {b['selected']:,})")
    check(all(p["tokens"] <= p["full"] for p in b["parts"]), "no part exceeds its full count")
    check("messages:" in r["text"], "the header still states the session's message counts")
    check("shown:" not in r["text"], "the header carries no `shown:` summary line (removed 2026-09-09)")
    check(b["messages"] <= b["messages_full"], f"the budget still reports {b['messages']} of {b['messages_full']} messages")
    check(b["selected"] <= b["everything"],
          f"a filtered render never costs more than the ceiling ({b['selected']:,} <= {b['everything']:,})")
    r_all = render({"thinking": True, "tools": True, "env": True, "cap": 0})
    check(r_all["budget"]["selected"] == r_all["budget"]["everything"], "all options on + no cap == everything exactly")
    r_cap = render({**base, "cap": 50})
    check(r_cap["budget"]["selected"] <= b["selected"], "a smaller cap never costs more")
    r_slice = render({**base, "start": 0, "end": 2})
    check(r_slice["budget"]["messages"] <= 2, "message range limits the count")
    # --- merge turns: same content, fewer headers; the ceiling is measured the same way ----------
    r_m, r_nm = render({**base, "collapse": True}), render({**base, "collapse": False})
    check(sum(p["tokens"] for p in r_m["budget"]["parts"]) == sum(p["tokens"] for p in r_nm["budget"]["parts"]),
          "merging turns changes structure only; every part costs the same")
    check(r_m["budget"]["selected"] <= r_nm["budget"]["selected"],
          f"merged turns never cost more ({r_m['budget']['selected']:,} <= {r_nm['budget']['selected']:,})")
    check(r_m["budget"]["messages"] == r_nm["budget"]["messages"], "the shown count still counts the original messages")
    import re as _re
    hdr = lambda t: len(_re.findall(r"^\[(USER|ASSISTANT|TOOL|SYSTEM)\]", t, _re.M))
    check(hdr(r_m["text"]) <= hdr(r_nm["text"]), f"merging leaves no more headers than before ({hdr(r_m['text'])} <= {hdr(r_nm['text'])})")
    r_all_nm = render({"thinking": True, "tools": True, "env": True, "cap": 0, "collapse": False})
    check(r_all_nm["budget"]["selected"] == r_all_nm["budget"]["everything"], "all options on + no cap == everything, with merging off too")
    r_red = render({**base, "redact": True, "redact_terms": ["zzz-not-present"]})
    check(isinstance(r_red["redacted"], str), f"redaction reports what matched ({r_red['redacted']})")
    det = (r_red.get("redaction") or {}).get("details") or []
    check(bool(det) and all({"name", "label", "replacement", "hits", "matches"} <= set(d) for d in det), "redaction details list every rule with its matches")
    check(any(d["name"] == "term:zzz-not-present" and d["hits"] == 0 for d in det), "an ad-hoc term with no hits is reported as such")
    check(all(sum(c for _, c in d["matches"]) == d["hits"] for d in det), "match counts add up to the hits")
    r_n = render({**base, "notice": True})
    check("[Import notice]" in r_n["text"].rsplit("[SYSTEM]", 1)[-1] and r_n["notice"] is True, "import notice is the last message of the render")
    check(any(p["key"] == "notice" and p["on"] and p["tokens"] > 0 for p in r_n["budget"]["parts"]), "the notice has its own budget row")
    r_c = render({**base, "cap": 20})
    check(r_c["budget"]["selected"] <= b["selected"], "a 20-token cap costs no more than a 150-token cap")

    # --- context modes -------------------------------------------------------------------
    r_brief = render({**base, "mode": "brief"})
    r_full = render({**base, "mode": "full"})
    check(sum(p["tokens"] for p in b["parts"]) <= sum(p["tokens"] for p in r_full["budget"]["parts"]),
          f"the cap never includes more content than full ({sum(p['tokens'] for p in b['parts']):,} <= {sum(p['tokens'] for p in r_full['budget']['parts']):,})")
    check(sum(p["tokens"] for p in r_brief["budget"]["parts"]) <= sum(p["tokens"] for p in r_full["budget"]["parts"]),
          f"brief includes no more content than full ({r_brief['budget']['selected']:,} vs {r_full['budget']['selected']:,})")
    check(r_brief["budget"]["mode"] == "brief" and r_brief["budget"]["cap"] == 0, "brief reports itself and applies no cap")
    check(not _re.search(r"^  ● \S+\(", r_brief["text"], _re.M),
          "names only renders no tool call with arguments")
    check(not _re.search(r"^  ↳ \d", r_brief["text"], _re.M) and "result 0 chars" not in r_brief["text"],
          "names only prints no sizes and no empty-result lines")
    calls_brief = next((p["tokens"] for p in r_brief["budget"]["parts"] if p["key"] == "tool_calls"), 0)
    calls_full = next((p["tokens"] for p in r_full["budget"]["parts"] if p["key"] == "tool_calls"), 0)
    if calls_full:
        check(calls_brief * 4 < calls_full, f"brief drops tool arguments, not just output ({calls_brief:,} vs {calls_full:,})")
    tr = [p for p in r_full["budget"]["parts"] if p["key"] == "tool_results"]
    check(not tr or tr[0]["remaining"] == 0, "in full mode nothing is left out of tool results")
    trb = [p for p in r_brief["budget"]["parts"] if p["key"] == "tool_results"]
    if trb and trb[0]["full"]:
        check(trb[0]["remaining"] > 0 and not _re.search(r"^  ↳ result ", r_brief["text"], _re.M),
              f"brief leaves {trb[0]['remaining']:,} tokens of tool output out, and prints no result lines")

    # a tool result continues the turn that called it; no [TOOL] header, no repeated timestamp
    r_full_tools = render({**base, "mode": "full"})
    check("[TOOL]" not in r_full_tools["text"], "tool results carry no header of their own by default")
    import re as _re
    stamps = _re.findall(r"^\[(?:USER|ASSISTANT|SYSTEM)\] \d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?P<off>[^\n]*)$",
                         r_full_tools["text"], _re.M)
    check(bool(stamps) and all(not s.strip().startswith(("+", "-")) for s in stamps),
          "the UTC offset is not repeated on every message header")
    check(_re.search(r"^started:.*[+-]\d{4}", r_full_tools["text"], _re.M) is not None,
          "the session header still states the offset once")

    # --- hidden roles and folded tool turns ----------------------------------------------
    r_hu = render({**base, "hide_roles": ["user"]})
    check(r_hu["budget"]["selected"] < b["selected"] and "[USER]" not in r_hu["text"], "hiding your turns removes them from the render")
    check(any(p["key"] == "user" and not p["on"] for p in r_hu["budget"]["parts"]), "the hidden role is marked off in the table")
    check(sum(p["tokens"] for p in r_hu["budget"]["parts"]) + r_hu["budget"]["structure"] == r_hu["budget"]["selected"],
          "the table still adds up exactly with a role hidden")
    # "hide timezone" removes the offset; it must NOT shift the clock
    r_tz = render({**base, "redact": True, "redact_rules": {"timezone": True}})
    r_notz = render({**base, "redact": True, "redact_rules": {"timezone": False}})
    started_tz = _re.search(r"^started:\s+(\S+ \S+)", r_tz["text"], _re.M)
    started_no = _re.search(r"^started:\s+(\S+ \S+)", r_notz["text"], _re.M)
    check(_re.search(r"^started:.*[+-]\d{4}", r_tz["text"], _re.M) is None,
          "hiding the timezone leaves no UTC offset anywhere")
    check(started_tz and started_no and started_tz.group(1) == started_no.group(1),
          f"and does not move the clock ({started_tz.group(1) if started_tz else '?'})")
    check(r_tz["text"] != r_notz["text"], "the toolbar toggle changes the render, not just the saved setting")

    # timestamp granularity, and the whole transcript as one message
    r_stamp_none = render({**base, "stamps": "none"})
    r_stamp_time = render({**base, "stamps": "time"})
    none_t, time_t, full_t = (r_stamp_none["budget"]["selected"], r_stamp_time["budget"]["selected"], b["selected"])
    check(none_t <= time_t <= full_t, f"shorter timestamps never cost more ({none_t:,} <= {time_t:,} <= {full_t:,})")
    check(_re.search(r"^\[(USER|ASSISTANT)\]\s*$", r_stamp_none["text"], _re.M) is not None,
          "stamps=none leaves a bare role marker")
    check(_re.search(r"^\[(USER|ASSISTANT)\] \d\d:\d\d:\d\d", r_stamp_time["text"], _re.M) is not None,
          "stamps=time keeps the time and drops the date")
    st, h, raw = c.post("/api/export", {**key, "opts": {**base, "wrap": "user"}, "format": "jsonl"})
    recs = [json.loads(l) for l in raw.decode("utf-8").strip().split("\n")]
    msgs = [r for r in recs if r.get("type") != "session"]
    check(st == 200 and len(msgs) == 1 and msgs[0]["role"] == "user",
          f"wrap puts the whole transcript in one user message ({len(msgs)} message record)")
    check(sum(len(bl.get("text", "")) for bl in msgs[0]["blocks"]) > 200,
          "and that message actually holds the transcript")

    # --- per-render redaction overrides ---------------------------------------------------
    r_off = render({**base, "redact": True, "redact_rules": {k: False for k in ("home", "media", "username", "hostname", "email", "keys", "ip")}})
    check((r_off.get("redaction") or {}).get("report") == "nothing matched", "switching every rule off for one render redacts nothing")

    # --- a hidden assistant keeps the tool calls it made; a lone tool turn gets a header ----------
    r_ha = render({**base, "hide_roles": ["user", "assistant"]})
    calls_all = sum(p["full_blocks"] for p in b["parts"] if p["key"] == "tool_calls")
    kept = sum(p["blocks"] for p in r_ha["budget"]["parts"] if p["key"] == "tool_calls")
    check(kept == calls_all, f"hiding the assistant keeps the tool calls it made ({kept} of {calls_all})")
    check(calls_all == 0 or _re.search(r"^\[TOOL\]", r_ha["text"], _re.M) is not None,
          "a tool turn with nothing above it carries its own [TOOL] header")
    check(calls_all == 0 or _re.search(r"^  ● ", r_ha["text"], _re.M) is not None,
          "hiding the assistant still renders the calls it made")
    r_th = render({**base, "thinking": True, "mode": "brief"})
    check(all(p["remaining"] == 0 for p in r_th["budget"]["parts"] if p["key"] == "thinking"),
          "the tool-output mode leaves thinking alone: never mentioned-only, never capped")
    # --- everything into one message, in the text formats too --------------------------------------
    r_w = render({**base, "wrap": "user"})
    check("(the whole transcript as one message:" in r_w["text"] and r_w["budget"]["selected"] == r_w["tokens"],
          "wrap applies to the text preview: one outer turn holding the conversation")
    r_w_all = render({"thinking": True, "tools": True, "env": True, "cap": 0, "wrap": "user"})
    check(r_w_all["budget"]["selected"] == r_w_all["budget"]["everything"], "all on + no cap == everything, wrapped too")
    st, h, raw = c.post("/api/export", {**key, "opts": {**base, "wrap": "system"}, "name": "wrap-md", "format": "markdown"})
    check(st == 200 and b"the whole transcript as one message" in raw and raw.count(b"\n### ") == 1,
          "wrap applies to markdown: exactly one section, fenced")
    # --- the import notice, with real values ---------------------------------------------
    st, h, raw = c.post("/api/notice", {**key})
    j = json.loads(raw)
    check(st == 200 and s["id"][:8] in j.get("text", "") and "<agent>" not in j.get("text", ""),
          "/api/notice returns the notice with this session's own values, not placeholders")

    for fmt in FORMATS:
        st, h, raw = c.post("/api/export", {**key, "opts": base, "name": f"t-{fmt}", "format": fmt})
        ok = st == 200 and "attachment" in h.get("content-disposition", "") and len(raw) > 0
        check(ok, f"export {fmt}: download with Content-Disposition ({len(raw):,} B)")
        if fmt == "csv":
            check(raw.startswith(b"\xef\xbb\xbf"), "csv starts with a UTF-8 BOM")
        if fmt == "html":
            check(raw.startswith(b"<!doctype html>") and b"<section class='msg" in raw, "html is a standalone page")
        if fmt == "jsonl":
            first = json.loads(raw.decode("utf-8").splitlines()[0])
            check(first.get("type") == "session", "jsonl starts with a session header line")
    st, h, raw = c.post("/api/export", {**key, "opts": {**base, "notice": True}, "name": "conv-test", "format": "native", "convert": "claude-code"})
    # the newest real session decides the shape: a transcript alone is the .jsonl (starts with "{");
    # one with spill files comes as the store-layout zip the fidelity note promises (X-Bundle: zip)
    shape_ok = (raw.startswith(b"{") and h.get("x-bundle") == "file") or (raw.startswith(b"PK") and h.get("x-bundle") == "zip")
    check(st == 200 and "attachment" in h.get("content-disposition", "") and shape_ok and "x-fidelity" in h,
          f"convert to claude-code: download with fidelity header ({len(raw):,} B, bundle {h.get('x-bundle')})")
    check("x-target-relpath" in h and h["x-target-relpath"].endswith(".jsonl") and "x-target-home" in h, "conversion says where the file belongs under the target home")
    st, h, raw = c.post("/api/export", {**key, "opts": base, "format": "native", "convert": "codex", "mode": "folder"})
    j = json.loads(raw)
    check(st == 200 and j.get("mode") == "folder" and "kept:" in j.get("report", "") and Path(j["path"]).exists(), "convert to codex in folder mode writes the file and a report")
    # --- convert + a generic format: the fold without the envelope ---------------------------------
    st, h, raw = c.post("/api/export", {**key, "opts": {**base, "notice": True}, "name": "as-codex", "format": "text", "convert": "codex"})
    body = raw.decode("utf-8", "replace")
    check(st == 200 and h.get("x-folded-for") == "codex" and "x-fidelity" in h and "x-target-relpath" not in h,
          "convert + a generic format is a folded export, not the target's file")
    check("[Imported from" in body and "[Import notice]" in body and ("[tool call:" in body or "tool calls: 0" in body),
          "the folded text carries the banner, the notice and the folded tool notes")
    check("session-context-manager" not in body, "the banner names this tool, not the legacy one")
    st, h, raw = c.post("/api/export", {**key, "opts": base, "format": "markdown", "convert": "codex", "mode": "folder"})
    j = json.loads(raw)
    check(st == 200 and j.get("folded_for") == "codex" and Path(j["path"]).suffix == ".md" and Path(j["report_path"]).exists(),
          "a folded export in folder mode writes the file and its report")
    st, mx = c.get("/api/convert-matrix")
    check(st == 200 and len(mx["carry"]) >= 10 and set(mx["targets"]) == {"claude-code", "codex", "gemini-cli", "cline"} and "disclaimer" in mx, "conversion matrix lists elements, targets and a disclaimer")

    # --- the pair decides: same agent in and out is a copy, so nothing is lost -------------
    st, same = c.get("/api/convert-matrix?source=claude-code&target=claude-code")
    fates = {row["fate"] for row in same["carry"]}
    check(st == 200 and same["native"] is True, "claude-code → claude-code is reported as native")
    check("dropped" not in fates and "folded" not in fates,
          f"nothing is dropped or folded when the agent exports to itself (fates: {', '.join(sorted(fates))})")
    check(any(r["element"] == "thinking" and r["fate"] == "carried" for r in same["carry"]), "thinking is carried on a native copy")
    st, cross = c.get("/api/convert-matrix?source=codex&target=claude-code")
    check(cross["native"] is False and "dropped" in {r["fate"] for r in cross["carry"]}, "a cross-agent pair still reports what it loses")

    # --- native export: needs a session of a WRITABLE agent, which may not be the one above ----
    nat = min((x for x in sessions if x["agent"] in writable), key=lambda x: x["size"], default=None)
    if nat:
        print(f"  native/install checks use {nat['agent']} {nat['id'][:8]} ({nat['size']:,} B)")
        s, key = nat, {"agent": nat["agent"], "id": nat["id"]}
        st, h, raw = c.post("/api/export", {**key, "opts": {**base, "notice": True}, "format": "native"})
        check(st == 200 and h.get("x-native") == "1" and raw.startswith(b"{"), f"native export writes {s['agent']}'s own format ({len(raw):,} B)")
        rep = h.get("x-fidelity", "")
        check("native copy" in rep.replace("%20", " ").replace("%0A", "\n") or "native" in rep, "the report says it was a copy")
        st, h, raw = c.post("/api/export", {**key, "opts": base, "format": "native", "mode": "folder"})
        j = json.loads(raw)
        check(st == 200 and j.get("native") is True and Path(j["path"]).exists(), "native export in folder mode writes the file")
        back = Path(j["path"]).read_text(encoding="utf-8", errors="replace")
        check(s["id"] not in back, "the copy carries a fresh session id, so it cannot collide with the original")

        # install mode writes into the agent's LIVE store; point that store at the temp dir first,
        # so the test never touches the real one. The server runs in this process, so the env var
        # it reads is the one set here.
        env = {"claude-code": "ECTYPE_CLAUDE_HOME", "codex": "CODEX_HOME", "gemini-cli": "ECTYPE_GEMINI_HOME"}[s["agent"]]
        prev, store = os.environ.get(env), str(Path(_TMP) / "live-store")
        os.environ[env] = store
        try:
            st, h, raw = c.post("/api/export", {**key, "opts": base, "format": "native", "mode": "install",
                                                "workspace": "/tmp/ectype-workspace"})
            j = json.loads(raw)
            ok = st == 200 and j.get("mode") == "install" and Path(j["path"]).exists() and str(j["path"]).startswith(store)
            check(ok, f"install writes into the agent's own store ({j.get('path', j.get('error'))})")
            check("resume" in (j.get("resume") or ""), f"install says how to resume it ({j.get('resume')})")
            check(Path(j["report_path"]).exists() and not Path(j["report_path"]).is_relative_to(store),
                  "the fidelity report is kept OUT of the live store")
            check("ectype-workspace" in str(j["path"]), "the chosen workspace decides where the agent files it")
        finally:
            os.environ.pop(env, None) if prev is None else os.environ.__setitem__(env, prev)
    st, h, raw = c.post("/api/export", {**key, "opts": base, "name": "folder-test", "format": "text", "mode": "folder"})
    j = json.loads(raw)
    check(st == 200 and j.get("mode") == "folder" and Path(j["path"]).exists() and Path(j["path"]).parent == Path(_TMP),
          f"folder mode writes into the configured folder ({j.get('path')})")

    # settings round trip: disable the agent we used -> it disappears from the list; then restore
    new = json.loads(json.dumps(cfg["settings"]))
    new["agents"][s["agent"]] = {"enabled": False, "home": None}
    new["export"]["mode"] = "folder"
    new["export"]["folder"] = _TMP
    new["redact"]["ip"] = True
    new["redact"]["timezone"] = True
    new["redact"]["terms"] = ["Alice=User"]
    new["notice"]["detailed"] = True
    new["notice"]["items"] = ["paths", "time"]
    new["view"]["table"]["columns"] = ["blocks", "tokens", "share"]      # "tokens" is a retired name
    new["view"]["mode"] = "brief"
    st, h, raw = c.post("/api/settings", {"settings": new})
    saved = json.loads(raw)
    check(st == 200 and saved["settings"]["agents"][s["agent"]]["enabled"] is False, "settings saved")
    sv = saved.get("settings", {})
    check(sv.get("redact", {}).get("ip") is True and sv["redact"]["terms"] == ["Alice=User"] and sv["notice"]["items"] == ["paths", "time"],
          "redaction and notice settings round-trip")
    check(sv["view"]["table"]["columns"] == ["blocks", "share"] and sv["view"]["mode"] == "brief",
          "retired table columns are dropped instead of rejected, and the context mode is stored")
    st, h, raw = c.post("/api/settings", {"settings": {"view": {"mode": "sideways"}}})
    check(st == 400 and "mode" in json.loads(raw).get("error", ""), "an unknown context mode is rejected")

    check(Path(os.environ["ECTYPE_CONFIG"]).exists(), "settings file written to ECTYPE_CONFIG")
    st, sessions2 = c.get("/api/sessions")
    check(all(x["agent"] != s["agent"] for x in sessions2), "a disabled agent is gone from /api/sessions")
    st, h, raw = c.post("/api/settings", {"settings": {"export": {"mode": "sideways"}}})
    check(st == 400 and "mode" in json.loads(raw).get("error", ""), "invalid settings are rejected with a message")
    new["agents"][s["agent"]] = {"enabled": True, "home": None}
    c.post("/api/settings", {"settings": new})
    st, sessions3 = c.get("/api/sessions")
    check(any(x["agent"] == s["agent"] for x in sessions3), "re-enabled agent is listed again")
    st, h, raw = c.post("/api/settings", {"settings": {"view": {"context_window": 123456}}})
    check(st == 200 and json.loads(raw)["settings"]["view"]["reference"] == 123456, "legacy view.context_window is accepted as reference")
    st, h, raw = c.post("/api/settings", {"settings": {"notice": {"items": ["nonsense"]}}})
    check(st == 400 and "nonsense" in json.loads(raw).get("error", ""), "unknown notice item is rejected")
    # NB: a POST replaces the whole document, so these partial posts reset everything else; keep
    # them after the checks that depend on saved state.
    st, h, raw = c.post("/api/settings", {"settings": {"view": {"table": {"columns": ["blocks", "full", "removed"]}}}})
    check(st == 200 and json.loads(raw)["settings"]["view"]["table"]["columns"] == ["blocks", "remaining"],
          "the retired full/removed columns migrate to `remaining` instead of leaving an empty table")

    # The preview used to give up role colours past 500 KB, which is every full render of a real
    # session; colouring is one span per RUN of same-class lines now and has no size cut-off.
    page = (Path(__file__).resolve().parents[1] / "ectype" / "web" / "index.html").read_text(encoding="utf-8")
    check("text.length>500000" not in page, "the preview has no size at which it drops role colours")
    check("run.push(l)" in page and "const flush=" in page, "paint() groups a run of same-class lines into one span")
    # A `//` comment runs to the end of its line. Selecting a row used to sit AFTER one, on the same
    # line, so a click set `cur` and rendered but never highlighted the row until something else
    # redrew the list. Valid JavaScript, silently dead, which is why the syntax check below cannot
    # see it and this one names the line.
    import re as _re
    check(_re.search(r"^\s*\$\$\('\.s\.sel'\)", page, _re.M) is not None,
          "the row-selection line starts a line of its own, not after a comment")
    # The page is one big inline script served as a string, so nothing else type-checks it: a
    # stray brace ships and the list simply never draws.
    import shutil as _shutil
    import subprocess as _sub
    node = _shutil.which("node")
    if node:
        body = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        js = Path(_TMP) / "page.js"
        js.write_text(body, encoding="utf-8")
        r = _sub.run([node, "--check", str(js)], capture_output=True, text=True)
        check(r.returncode == 0, f"the page's script parses ({(r.stderr or '').strip().splitlines()[-1] if r.returncode else 'ok'})")
    else:
        print("  skip node --check (no node)")

    srv.shutdown()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else "all web checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
