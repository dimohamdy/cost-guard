# Cost Guard: a Claude Code plugin

Spending guardrails for Claude Code. Each developer installs it for themselves,
so no admin rights are needed.

| Situation | What happens |
|---|---|
| Session passes $10 / $25 / $50 | A one-time alert in the chat (it does not block) |
| Session passes **$100** | New prompts are **blocked**, and tool calls are **denied mid-turn**, so a long autonomous run also stops |
| This machine passes **$150 today** (all sessions) | Same block |
| You come back to an idle session with a big context | Blocked **once** with the cost of re-caching it ("idle 22 h, re-caches 612k tokens ≈ $4.90"). Re-send to continue, or `/clear` |
| Context passes **150k tokens** | A one-time note on what fills it and what to drop (see below) |
| Status line (optional) | `Opus 5.5 · session $12.34 · today $40.10 · ctx 612k · cache cold` |

To get past a stop, put `#override` in your message. That adds one more cap-sized allowance.
The plugin never blocks the app's own bookkeeping tools (task list, wrap-up, plan mode). You can
change that list with `"never_block": [...]` in `overrides.json`.

## Context advice

Run `/cost-guard:context` at any time. Each alert and stop also includes the top two points.
The advice comes from the session transcript:

- **Large tool output still in context**, such as a full file read or a long command log.
  You get a `/compact drop the raw output of …` line that you can paste as is.
- **The same file read 3 or more times.**
- **Images** still in context.
- **MCP servers that load tool definitions into every message but had no calls in
  14 days.** Turn them off in `/mcp` (in the desktop app, under connector settings).
- **A large context**, split into fixed overhead, tool output, images and conversation.
  Use `/clear` for a new task or `/compact` for the same task.
- **Subagents** that cost more than 30% of the session, and **Opus or Fable** sessions where Sonnet would do.

## Install

From a clone of this repository:

```bash
claude plugin marketplace add /path/to/claude-cost-guard
```

```bash
claude plugin install cost-guard@team-cost-tools
```

Once the repository is on GitHub, teammates can skip the clone and run
`/plugin marketplace add <org>/<repo>`, then `/plugin install cost-guard@team-cost-tools`.

Restart open sessions afterwards. Then, optionally, in Claude Code:

```
/cost-guard:statusline
```

## Change the limits

Either way works. The second one wins over the first:

1. **Plugin options**: `/config` → Cost Guard, or `/plugin configure cost-guard@team-cost-tools`
2. **Command**, in any session:
   ```
   /cost-guard:limits                 show the current limits and where each comes from
   /cost-guard:limits session 50      session cap $50
   /cost-guard:limits daily 200       daily cap $200
   /cost-guard:limits alerts 5,20,40  alert amounts
   /cost-guard:limits cold 2          cold-cache warning threshold
   /cost-guard:limits context 200     context advice at 200k tokens
   /cost-guard:limits reset           back to the plugin options
   ```

See where the money went: `/cost-guard:report 7` (last 7 days, per session).

## Why these checks

Most of the cost of a long session is **cache writes**, not code. The prompt cache
expires after an idle period (5 minutes or 1 hour). After that, the next message
re-writes the whole context at 1.25x to 2x the input price. In one $239.73 session,
$214 (89%) was cache writes.

## Limits of the tool

- **It's an estimate.** It uses list prices from the local transcripts, with
  subagents included. Your company gateway may bill differently. To update prices,
  add `"prices": {"claude-opus-5-5": [input, output, cache_read]}` to
  `~/.claude/plugins/data/cost-guard-team-cost-tools/overrides.json` (USD per 1M tokens).
- **It's per machine and voluntary.** Anyone can disable it. Real enforcement
  (org spend limits, gateway budgets) needs an admin.
- **It runs at prompt and tool-call boundaries.** A single model response can't
  be stopped halfway through, so a session can go slightly over a cap.
- **It fails open.** If the guard crashes, Claude Code keeps working, and the
  error is logged to `~/.claude/plugins/data/cost-guard-team-cost-tools/state/errors.log`.

## Layout

```
.claude-plugin/marketplace.json   marketplace "team-cost-tools"
plugin/.claude-plugin/plugin.json manifest and the limit options
plugin/hooks/hooks.json           SessionStart, UserPromptSubmit, PreToolUse
plugin/commands/                  /cost-guard:limits, :report, :context, :statusline
plugin/scripts/cost_guard.py      all logic (Python 3, standard library only)
```
