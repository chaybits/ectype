---
description: Read a past session from any AI agent and bring it into this conversation
argument-hint: "[session id, or words from its title]"
allowed-tools: Bash(ectype:*)
---

Bring a past session into this conversation. `$ARGUMENTS` is either empty, a session id (any
prefix), or words to search for.

1. **Find it.** With no argument, run `ectype list -n 20` and show the table, then ask which one.
   With an argument, run `ectype list -n 60` and pick the row whose id starts with it or whose
   title contains it. If several match, show only those and ask; never guess between two.

2. **Price it before reading it.** Sessions run to hundreds of thousands of tokens, and most of
   that is tool output. Run `ectype show <id> --mode brief` first: it keeps every message and
   every tool call by name, and drops the arguments and the output. On a 981-message session that
   is about 13 K tokens instead of 347 K.

3. **Go deeper only where it matters.** If the brief view does not answer the question, re-read
   the part that does, rather than the whole session:
   - `ectype show <id> --cap 300` keeps the first 300 tokens of each tool result;
   - `ectype show <id> --thinking` adds the reasoning, which is large;
   - `ectype show <id> --no-tools` is the conversation alone.

4. **Say what you took.** End by telling the user which session, which view, and roughly what it
   cost, so the context they are now paying for is visible rather than implied.

Do not run `ectype convert` or `ectype export --install` unless the user asks in so many words:
those write into an agent's own store.
