# Changelog

## 0.3.0 (2026-09-10)

- **`ectype backup`.** A copy of a session and every sidecar file it owns, in one timestamped
  folder with a manifest of where each file came from. `--list` shows what has been saved,
  `--restore NAME` puts it back, and `--dry-run` names every destination without writing.
- **Installs back themselves up.** When an install would modify a file the store already owns, it
  takes that copy first. Most do not need one, because they only add a file; Codex is the
  exception, since it appends to the store's session index and its migration rewrites the thread
  history. `--no-backup` opts out.
- README: every install route in one block, an explanation of what the single-file `.pyz` is and
  how to run it on each platform, and an *everything mode* screenshot retaken on the same session
  the token tables describe, so the picture and the prose carry the same numbers.

## 0.2.1 (2026-09-10)

- **`ectype summarize <id>`, and `summarize_session` over MCP.** What happened in a session,
  extracted from the transcript with no model, no API key and no network: what was asked, which
  tools ran and how often, which files were touched, which commands ran, what failed, and a
  lowercase keyword line meant to be matched by a regex rather than read. On a 981-message session
  that is a few hundred tokens against 347,517 for the transcript, which is enough to decide
  whether to open it at all. `--start` and `--end` narrow it to a range.

## 0.2.0 (2026-09-10)

- **`ectype mcp`: an MCP server on stdio**, no dependencies, so any MCP client can let an agent
  read its own past sessions. Four read-only tools: `list_sessions`, `session_budget` (what a
  session costs before you read it), `show_session` (at a depth the agent chooses) and
  `list_agents`. A fifth tool installs a session into a live store and is not offered at all
  unless the server is started with `--allow-write`. Client wiring and a Claude Code slash
  command are in `integrations/`.
- **Rename a session in the web UI**, by double-clicking its name. Claude Code takes the name
  into its own store (the `custom-title.json` it writes itself, so the name shows up there too);
  every other store keeps it on ectype's side rather than having a title field invented inside
  it. An empty name restores the generated one.
- **`convert --mint-template`** runs the target agent once on its cheapest model and uses the
  session it writes as the template, so the envelope matches the release you have installed.
  **`convert --install --verify`** resumes the installed session in the target agent and reports
  whether it answered. Both spend a small API call, so both are opt-in.
- **A single-file build.** Every release now carries `ectype.pyz`: the whole program in one file,
  runnable with nothing installed but Python (`python ectype.pyz gui`).
- **Fixed:** clicking a row in the session list set the session and rendered it, but never
  highlighted the row, because a `//` comment on the same line swallowed the two statements that
  move the selection. The page's script is syntax-checked by the test suite now.

## 0.1.2 (2026-09-10)

- **Open WebUI tool calls were dropped, in every chat.** The adapter read the assistant node's
  `output` parts for reasoning only, and looked for tool activity in a message-level `tool_calls`
  key that Open WebUI does not write. A real tool run against 0.11.3 showed where it lives:
  `output` also carries a `function_call` part (name and arguments) and a `function_call_output`
  part, interleaved with the reasoning in the order they happened. Both are read now and tied
  together by `call_id`, a list-of-parts output is flattened to its text, and any status other
  than `completed` marks the result as an error.
- **The distribution is now `ectype-cli` on PyPI.** Only the distribution name: the import
  package is `ectype`, the command is `ectype`, and `pip install ectype-cli` gives you both.
- **A disclaimer in the README.** Redaction is best effort and an export should be read before it
  is shared; installing writes into a live agent store; a conversion carries the conversation but
  not the state the original ran with.

## 0.1.1 (2026-09-10)

- **The preview keeps its role colours at any size.** Past 500,000 characters it used to be
  painted as plain text, which is every full render of a real session. The cost was never the
  text, it was the node count: one span per line. The painter now emits one span per run of
  same-class lines, so a 40-line tool result is one node instead of 40. On a 981-message session
  rendered whole (1.24 MB, 10,466 lines) that is 1,574 nodes and 168 ms including layout, where
  the same render previously lost its colours entirely.
- **Packaging.** The distribution description no longer calls all thirteen stores coding agents:
  ten are, three are chat apps. `[project.urls]` points at the repository and this changelog.
- The README is half its former size and starts with Install, and the measured cost of each view
  is now one worked example on one session instead of a table of every option.

## 0.1.0 (2026-09-09), first public release

- **Thirteen agents, one shape.** Claude Code, Codex CLI, Gemini CLI, Antigravity, VS Code
  Copilot Chat, Cursor, Cline, Roo Code, Continue, Aider, LM Studio, Open WebUI and SillyTavern
  are read into the same `Session → Message → ContentBlock` model. Sidecar files that agents keep
  beside a transcript are read back in.
- **Continue a session.** A same-agent export is a lossless copy: tool records, thinking and
  sidecar files all survive, and `--install` files it under the agent's store ready to resume.
  Claude Code, Codex CLI and Gemini CLI were each verified by installing a converted session and
  resuming it live; the other ten are read-only, each for a stated reason.
- **Cross-agent conversion**, reported rather than silent: every conversion writes a
  `*.fidelity.md` counting what was kept, folded and dropped. Convert to an agent *and* pick a
  generic format to get the same fold as text you feed in yourself.
- **A token budget you can defend.** Two numbers, *max context* (everything on, nothing capped)
  and *selected* (exactly what the export costs), and a table whose total equals *selected*.
- **Tool output in three modes.** names only (the tool's name, nothing else), capped, everything.
  The mode touches nothing but tool calls and results.
- **Merge.** Back-to-back turns of the same actor become one turn, a tool result riding with the
  call that produced it, or the whole transcript becomes one user / system / assistant message in
  any format. On a 981-message session, names only is 13,073 tokens against 347,517 whole.
- **Six export formats** (text, markdown, html, json, jsonl, csv) plus each writable agent's own
  resumable file.
- **Redaction** computed from the running machine, with every match shown next to its
  replacement, and a hide-timezone rule that leaves the offset out without shifting the clock.
- **Import notice** appended as the last message, generic or itemised, previewed with real values.
- **Local web UI** with a file-manager session list (sortable, resizable, column menu,
  collapsible sidebar), a preview coloured by role, and settings for store paths per agent and
  per OS. It binds `127.0.0.1` and additionally requires a loopback `Host` and, when the browser
  sends one, a same-origin `Origin`, so another page open in the same browser cannot drive it and
  a DNS-rebound host cannot read your transcripts.
- **Command line** for all of it: `list`, `show`, `export`, `convert`, `fixture`, `settings`,
  `gui`.

Known limits are listed in the README, under *Known limitations*.
