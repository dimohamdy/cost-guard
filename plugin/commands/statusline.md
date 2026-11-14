---
description: Show session cost, daily cost and context size in the status line
argument-hint: "[--force]"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Run with the Bash tool:

`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" install-statusline $ARGUMENTS`

If it reports an existing status line, tell the user and ask before running it again with --force. Otherwise say the status line appears in new terminal sessions, for example: `Opus 5.5 · session $12.34 · today $40.10 · ctx 612k · cache cold`.
