---
description: Show what fills this session's context, and what would cut its cost
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Run with the Bash tool:

`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" context --session "${CLAUDE_SESSION_ID}"`

Show the output as is. Then, in at most three sentences, say which one or two pieces of advice fit what the user is doing right now. If a `/compact ...` line is suggested, give it in a code block so it can be copied. Do not run /compact or /clear yourself.
