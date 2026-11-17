---
description: Show or change cost-guard limits (session cap, daily cap, alerts)
argument-hint: "[session|daily|monthly|stop-monthly|pct|burst|notify|project|billing|alerts|cold|context|tips|ignore|word] [value]  or  reset"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Show or change the cost-guard limits. The user's request: "$ARGUMENTS"

Map the request to one command and run it with the Bash tool:

- Empty: `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits`
- Session cap ("session 50"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits session_cap_usd 50`
- Daily cap ("daily 200"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits daily_cap_usd 200`
- Monthly budget ("monthly 300"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits monthly_cap_usd 300`
- Stop (not just warn) at the monthly budget ("stop-monthly on" / "off"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits monthly_hard_stop true` (or false)
- Warning percentage ("pct 80"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits warn_at_pct 80`
- Runaway breaker ("burst 15", "burst 0" = off): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits burst_usd_per_15min 15`
- Desktop notifications ("notify off" / "on"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits desktop_notify false` (or true)
- A limit for this project folder only ("project session 50", "project daily 80", "project reset"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits project session_cap_usd 50` (map session/daily/monthly/burst/alerts to their keys as above; "project reset" removes this folder's limits)
- Alerts ("alerts 5,20,40"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits warn_at_usd 5,20,40`
- Cold-cache warning ("cold 2"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits cold_cache_warn_usd 2`
- Context advice ("context 200"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits context_warn_k 200`
- Cost tips at session start ("tips 7" = at most weekly, "tips 0" = off): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits insights_every_days 7`
- Never suggest something again ("ignore claude-mem"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits insights_ignore claude-mem` (adds to the list)
- Billing ("billing plan" for Pro/Max/Ultra, "billing api", "billing gateway", "billing auto"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits billing subscription` (auto detects it from the limits Claude Code reports; on a plan the dollar caps never block)
- Override word ("word #goahead"): `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits override_word '#goahead'`
- "reset": `CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" limits reset` (go back to the plugin options)

Amounts are in US dollars; drop any $ sign. Then show the table the command prints, in a short reply. Mention that the defaults for everyone can also be changed under /config (Cost Guard), and that values set here win over those.
