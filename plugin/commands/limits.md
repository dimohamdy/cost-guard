---
description: Show or change cost-guard limits (session cap, daily cap, alerts)
argument-hint: "[session|daily|alerts|cold|word] [value]  or  reset"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Show or change the cost-guard limits. The user's request: "$ARGUMENTS"

Map the request to one command and run it with the Bash tool:

- Empty: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits`
- Session cap ("session 50"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits session_cap_usd 50`
- Daily cap ("daily 200"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits daily_cap_usd 200`
- Alerts ("alerts 5,20,40"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits warn_at_usd 5,20,40`
- Cold-cache warning ("cold 2"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits cold_cache_warn_usd 2`
- Override word ("word #goahead"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits override_word '#goahead'`
- "reset": `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits reset` (go back to the plugin options)

Amounts are in US dollars; drop any $ sign. Then show the table the command prints, in a short reply. Mention that the defaults for everyone can also be changed under /config (Cost Guard), and that values set here win over those.
