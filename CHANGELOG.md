# Changelog

All notable changes to ectype are recorded here, newest first, in the shape of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the version numbers follow
[Semantic Versioning](https://semver.org/) while the program is pre-1.0.

## [0.3.1] - 2026-09-28

### Added

- **`ectype search <regex>`, `ectype summarize --all`, and `summarize <id> --save`.** ectype now
  keeps one summary per session on its own side, remembers which version of each file it saw so a
  second `--all` run only touches what changed, and answers "have I dealt with this before?" with
  a regex over every session's summary: a search first summarises whatever is new or changed since
  the last one, and says how many sessions it covered. Over MCP the same search is
  `search_sessions`. Nothing here writes into an agent's store.
- **The closing summary.** When the agent wrote a summary of the session between a pair of
  markers in one of its own turns (`%%SUMMARY%%` unless Settings → Summary says otherwise;
  several markers are allowed), `ectype summarize` quotes every such block first, in order,
  `search` finds them, `recall` carries them, and the web UI shows them under a new **Summary**
  button. `summarize --marker TEXT` looks for another marker once; kept summaries are redone with
  the current markers on the next search. `ectype summary-rule` (and Settings → Summary) writes
  the paragraph that asks an agent for one into the instruction files you pick, inside a block it
  owns, and takes it out again. Only the assistant's turns are read: a summary pasted into a prompt is not
  taken for the new session's.
- **`ectype recall <id>`**, what the `/ectype` command runs: `show` with its defaults taken from
  Settings → Agent skill, which holds every option a view has and starts with the conversation
  alone (`--tools` adds the tool calls), and, by default, the session's summary kept for `search`
  afterwards. Options typed after the id win over
  the settings, and the Settings dialog shows the exact command they produce. The command exists
  for Claude Code, Gemini CLI and Codex (`integrations/`), and each was run once to prove it.
- **Adapter plug-ins.** An adapter for an agent ectype does not know can live in its own package:
  it registers under the `ectype.adapters` entry-point group and ectype loads it at start. A
  plug-in that fails to load is reported and skipped, never allowed to break the listing;
  `ectype agents` marks the ones that came from a plug-in. A complete working example and the
  contract are in `integrations/adapter-plugin/`.
- **The slash command under any name.** `ectype skill install` writes the `/ectype` command for
  Claude Code, Gemini CLI and Codex; `--name bringmedasummary` calls it that instead, and Settings
  → Agent skill remembers the name, shows what is installed, and installs or removes the files.
  A command file ectype did not write is never overwritten or removed, and one it wrote records a
  hash of its text: `ectype skill status` tells one nobody touched (updated by a plain `install`)
  from one you edited (kept unless `--force`, which saves it as `.bak`).
- **Backup retention.** Settings → Backups (or `backup.keep_last` / `backup.keep_days`) limits how
  many backup folders are kept or for how long; 0, the default, keeps everything. `ectype backup
  --prune` applies the rule on demand; `--list` shows sizes.
- **Cline is a writable target.** `ectype convert --to cline --install` writes a session the
  Cline CLI (3.0.x) resumes: its two JSON files plus the row in its own index, which is what
  `cline history` lists. Proven by installing a converted session and resuming it. The CLI resumes
  only in its terminal UI, so `--verify` drives that UI through a pseudo-terminal.
- **Agent notices.** Claude Code's own notes to itself (API errors, a refusal answered by a
  fallback model, the summary it wrote while you were away) are read now, and so are Gemini CLI's
  info and error records and Antigravity's system steps. Off by default in every view; the *agent
  notices* checkbox, Settings → View, `--notices`, or the MCP tools' `notices` flag switch them on.
  `ectype summarize` lists the API errors under *errors* either way. The budget table shows a
  store's own system messages and the agent notices as two rows.
- **`ectype show --mode brief|custom|full`.** The shipped slash command called this flag before it
  existed; every command now exposes the same three modes.
- **Settings → Web UI → port.** `ectype gui` listens where the settings say unless `--port` is
  given; a port that is already taken is reported in one line instead of a traceback.
- **Help → About**: the version, the GitHub and PyPI pages, the licence.
- **A Windows portable build**, `ectype-<version>-windows-x64.zip`, attached to every release.
  Unzip it anywhere and double-click `ectype-gui.bat`. It carries its own copy of Python, so it
  needs neither a Python installation nor a terminal; it keeps its settings inside its own folder,
  writes nothing to PATH or the registry, and is uninstalled by deleting the folder. About 10 MB.
  It is attached to a release only after a Windows machine has run it, and every attached file
  comes with its SHA-256 checksum and a build-provenance attestation (checked with
  `gh attestation verify <file> --owner chaybits`).
- **`ectype --version`**, and `-V`. The version was already reported to MCP clients and carried in
  the package; the command line was the one place that could not answer for it.
- **A question the agent put to you, and your answer, as conversation.** Claude Code's
  `AskUserQuestion`, Gemini CLI's `ask_user`, Codex's `request_user_input`, Cline's and Roo Code's
  `ask_followup_question`: the question shows in the agent's turn with its choices and your answer
  as your turn, even with tool calls off. On by default; the *questions to you* checkbox, Settings
  → View, `--no-questions`, or the MCP tools' `questions` flag turn it off. `ectype summarize`
  lists your answers under *answered*.

### Changed

- `summarize --all` covers the agents switched on in Settings → Agents; a chat app is included when
  you switch it on or name it with `-a`.
- `ectype show` and `export` follow Settings → View for every option you leave out, as the web UI
  does.
- `ectype skill install` puts the command files where each agent looks (it follows
  `CLAUDE_CONFIG_DIR`, `GEMINI_CLI_HOME` and `CODEX_HOME`), and never overwrites or removes a
  command file you edited.
- `backup --restore` saves what it overwrites first, so a restore can be undone.
- A summary shows local time and keeps every keyword, in any language.
- In a CSV export, a cell a spreadsheet would run as a formula starts with an apostrophe.
- A text export's header counts the turns it shows, and names tool calls and thinking only when it
  shows some; it used to count the whole session, which told a model reading an import about
  messages it was never given.
- A message another Claude Code session sent in shows as a turn of its own, `[PEER] from <name>`,
  instead of being hidden as injected context; the *other sessions' messages* checkbox, Settings
  → View and `--no-peers` turn them off.

### Fixed

- A message you typed in Claude Code while the agent was working was missing from every view
  and export; it shows as yours now.
- Redaction runs before long tool output is shortened, so a name can no longer slip through in
  cut-off text. It also catches more kinds of API keys, and your home folder and username in more
  spellings.
- A same-agent copy of a Claude Code session resumes with its last answer.
- A session installed into Gemini CLI goes where Gemini looks for it, and resumes by its id.
- Restoring a backup never deletes that backup or rolls back other sessions, and a backup of an
  agent's database includes its latest changes.
- An install that fails halfway leaves the agent's store as it was, and installs from the web UI
  or over MCP take the same backup as the command line.
- One unreadable session or store no longer hides the others.
- The MCP server keeps running after a malformed request, and uses the cap from Settings when a
  call gives none.
- The web UI answers only its own page, never another local app.
- Downloading a Cline session from the web UI gives both of its files.
- Tool output an agent saved to a separate file is read back, Gemini CLI's included.
- A renamed Claude Code session shows its new name, and the project column shows the right project.

## [0.3.0] - 2026-09-10

### Added

- **`ectype backup`.** A copy of a session and every sidecar file it owns, in one timestamped
  folder with a manifest of where each file came from. `--list` shows what has been saved,
  `--restore NAME` puts it back, and `--dry-run` names every destination without writing.
- **Installs back themselves up.** When an install would modify a file the store already owns, it
  takes that copy first. Most do not need one, because they only add a file; Codex is the
  exception, since it appends to the store's session index and its migration rewrites the thread
  history. `--no-backup` opts out.

## [0.2.1] - 2026-09-10

### Added

- **`ectype summarize <id>`, and `summarize_session` over MCP.** What happened in a session,
  extracted from the transcript with no model, no API key and no network: what was asked, which
  tools ran and how often, which files were touched, which commands ran, what failed, and a
  lowercase keyword line meant to be matched by a regex rather than read. On a 981-message session
  that is a few hundred tokens against 347,517 for the transcript, which is enough to decide
  whether to open it at all. `--start` and `--end` narrow it to a range.

## 0.2.0 - 2026-09-10

### Added

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

### Fixed

- Clicking a row in the session list set the session and rendered it, but never highlighted the
  row, because a `//` comment on the same line swallowed the two statements that move the
  selection. The page's script is syntax-checked by the test suite now.

## 0.1.2 - 2026-09-10

### Changed

- **The distribution is now `ectype-cli` on PyPI.** Only the distribution name: the import
  package is `ectype`, the command is `ectype`, and `pip install ectype-cli` gives you both.

### Fixed

- **Open WebUI tool calls were dropped, in every chat.** The adapter read the assistant node's
  `output` parts for reasoning only, and looked for tool activity in a message-level `tool_calls`
  key that Open WebUI does not write. A real tool run against 0.11.3 showed where it lives:
  `output` also carries a `function_call` part (name and arguments) and a `function_call_output`
  part, interleaved with the reasoning in the order they happened. Both are read now and tied
  together by `call_id`, a list-of-parts output is flattened to its text, and any status other
  than `completed` marks the result as an error.

## 0.1.1 - 2026-09-10

### Fixed

- **The preview keeps its role colours at any size.** Past 500,000 characters it used to be
  painted as plain text, which is every full render of a real session. The cost was never the
  text, it was the node count: one span per line. The painter now emits one span per run of
  same-class lines, so a 40-line tool result is one node instead of 40. On a 981-message session
  rendered whole (1.24 MB, 10,466 lines) that is 1,574 nodes and 168 ms including layout, where
  the same render previously lost its colours entirely.

## 0.1.0 - 2026-09-09

The first public release.

### Added

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

[0.3.1]: https://github.com/chaybits/ectype/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/chaybits/ectype/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/chaybits/ectype/releases/tag/v0.2.1
