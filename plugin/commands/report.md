---
description: Show where Claude Code money went, per session
argument-hint: "[days, default 1]"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Run with the Bash tool, using the number of days in "$ARGUMENTS" (default 1):

`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" report --days <days>`

Show the output, then in two or three sentences point out the most expensive sessions. Folders are project paths with "/" written as "-"; subagent transcripts sit under their parent session id.
