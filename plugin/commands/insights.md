---
description: Find plugins, skills, MCP servers and hooks that burn tokens on every message without being used
argument-hint: "[days, default 14]"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Run with the Bash tool, using the number of days in "$ARGUMENTS" (default 14):

`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" insights --days <days>`

Show the output as is. Then, in at most three sentences, name the two or three changes worth making first. Do not disable plugins, move skills or edit settings yourself; the user decides. If they then ask you to make a change, do only that change.
