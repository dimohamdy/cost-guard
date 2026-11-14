---
description: Apply cost-guard's suggestions (disable unused plugins, move unused skills or agents aside, remove unused MCP servers), with undo
argument-hint: "[undo [last|all|<id>]]"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*), AskUserQuestion
---
Base command: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" fix`

If "$ARGUMENTS" starts with "undo": run the base command with `undo <rest of the arguments, default last>`, show the output, and stop.

Otherwise:
1. Run the base command with `--list --json`. If the list is empty, say there is nothing to fix automatically and point to /cost-guard:insights for manual tips. Stop.
2. Use AskUserQuestion (multiSelect) to ask which fixes to apply. Offer the up-to-4 items with the biggest `usd`; label each with its `what` (short), and put its `why` and saving in the description. Mention in the question that every change can be undone with /cost-guard:fix undo.
3. Apply only the items the user picked, by id: run the base command with `apply <id> <id> …`. Never apply an item the user did not pick, and apply nothing if they picked nothing.
4. Show the result and remind the user that open sessions need a restart for the change to take effect, and that `/cost-guard:fix undo` reverses it.
