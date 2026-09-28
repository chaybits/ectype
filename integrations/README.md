# Using ectype from inside an agent

Two ways, and they are not alternatives: the MCP server is what lets an agent decide for itself,
the slash command is what lets you ask for it in one word.

## MCP server (any MCP client)

`ectype mcp` speaks JSON-RPC over stdin and stdout, with no dependencies, so any MCP client can
run it. Six tools, all read-only:

| Tool | What the agent gets |
|---|---|
| `list_sessions` | recent sessions across every store on the machine |
| `search_sessions` | a regex over the summaries ectype keeps: "have I dealt with this before?", with how many sessions have no summary yet |
| `session_budget` | what a session costs, element by element, before reading it |
| `show_session` | the transcript, at a depth it chooses (names only, capped at the cap from Settings unless told otherwise, everything) |
| `summarize_session` | what happened in it: asks, tools, files, commands, errors, a keyword line |
| `list_agents` | which stores exist here and which can be written back to |

Claude Code:

```bash
claude mcp add ectype -- ectype mcp
```

Anything that reads a config file (Codex, Cursor, Cline, Continue):

```json
{ "mcpServers": { "ectype": { "command": "ectype", "args": ["mcp"] } } }
```

**A seventh tool, `install_session`, writes into an agent's live store**, so it does not exist
unless you start the server with `ectype mcp --allow-write`. A tool an agent cannot see is a tool
it cannot be talked into calling.

## The `/ectype` command (Claude Code, Gemini CLI, Codex)

One command in three dialects, all built on `ectype recall`: the agent finds the session (through
`ectype search` when given words), brings it in at the depth the options ask for, and ectype
saves the session's summary so the next `ectype search` finds it. The defaults for depth and for
the summary live in Settings → Agent skill (the dialog shows the exact command they produce), and
start with the conversation alone; options typed after the id win. Every dialect needs the `ectype` command on PATH, not the MCP
server.

```bash
ectype skill install                          # writes the three files below under /ectype
ectype skill install --name bringmedasummary  # any name; Settings → Agent skill remembers it
ectype skill status                           # what is installed, where; `remove` takes them out again
```

| Agent | File it writes | Then |
|---|---|---|
| Claude Code | `~/.claude/commands/<name>.md` (a copy for `/ectype`: [`claude-code/ectype.md`](claude-code/ectype.md)) | `/ectype a1b2c3d4 --mode custom --cap 300` |
| Gemini CLI | `~/.gemini/commands/<name>.toml` ([`gemini-cli/ectype.toml`](gemini-cli/ectype.toml)) | `/ectype a1b2c3d4 --mode brief` |
| Codex | `~/.codex/prompts/<name>.md` ([`codex/ectype.md`](codex/ectype.md)) | `/ectype a1b2c3d4` (or `/prompts:ectype`) |

A file ectype did not write is never overwritten or removed; the copies here are the same text,
rendered for the name `ectype`, for anyone who prefers to copy by hand. When `CLAUDE_CONFIG_DIR`,
`GEMINI_CLI_HOME` or `CODEX_HOME` is set, the file goes under it, because that is where the agent
looks.

Headless notes, from running all three on one session: Claude Code accepts it as
`claude -p "/ectype <id>"` with `--allowedTools "Bash(ectype:*)"`; Gemini CLI accepts it as
`gemini -p "/ectype <id>"` only in a trusted folder (`GEMINI_CLI_TRUST_WORKSPACE=true` or
`--skip-trust`) and with the shell tool approved (`--yolo`); Codex accepts it as
`codex exec -s workspace-write "/ectype <id>"` (a sandbox that cannot write under the settings
folder makes `recall` print a "summary NOT saved" warning and still succeed).

## Adding an agent ectype does not know

An adapter can live in a separate package: it registers itself under the `ectype.adapters`
entry-point group and ectype loads it at start, no change to ectype needed. A complete working
example, and the contract a plug-in has to meet, is in [`adapter-plugin/`](adapter-plugin/).
