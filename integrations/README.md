# Using ectype from inside an agent

Two ways, and they are not alternatives: the MCP server is what lets an agent decide for itself,
the slash command is what lets you ask for it in one word.

## MCP server (any MCP client)

`ectype mcp` speaks JSON-RPC over stdin and stdout, with no dependencies, so any MCP client can
run it. Four tools, all read-only:

| Tool | What the agent gets |
|---|---|
| `list_sessions` | recent sessions across every store on the machine |
| `session_budget` | what a session costs, element by element, before reading it |
| `show_session` | the transcript, at a depth it chooses (names only, capped, everything) |
| `list_agents` | which stores exist here and which can be written back to |

Claude Code:

```bash
claude mcp add ectype -- ectype mcp
```

Anything that reads a config file (Codex, Cursor, Cline, Continue):

```json
{ "mcpServers": { "ectype": { "command": "ectype", "args": ["mcp"] } } }
```

**A fifth tool, `install_session`, writes into an agent's live store**, so it does not exist
unless you start the server with `ectype mcp --allow-write`. A tool an agent cannot see is a tool
it cannot be talked into calling.

## Slash command (Claude Code)

```bash
cp integrations/claude-code/ectype.md ~/.claude/commands/ectype.md
```

Then `/ectype`, or `/ectype some words from the title`. It lists sessions, checks what one costs
before reading it, and reads only as deep as the question needs. It needs the `ectype` command on
PATH, not the MCP server.
