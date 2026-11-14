---
description: Show where Claude Code money went, by session, project, model or day
argument-hint: "[days | week | month] [by project|model|day] [json]"
allowed-tools: Bash(CLAUDE_PLUGIN_DATA=*)
---
Build the flags from "$ARGUMENTS":
- a number N → `--days N` (default `--days 1`); "week" → `--week`; "month" → `--month`
- "by project" / "by model" / "by day" / "by session" → `--by project` etc. (default by session)
- "json" → `--json`

Run with the Bash tool:

`CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_guard.py" report <flags>`

Show the output, then in two or three sentences point out what stands out (the most expensive sessions, projects or models). Session paths are project folders with "/" written as "-"; subagent transcripts sit under their parent session id.
