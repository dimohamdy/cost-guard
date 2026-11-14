# Cost Guard: a Claude Code plugin

Spending guardrails for Claude Code. Each developer installs it for themselves,
so no admin rights are needed.

| Situation | What happens |
|---|---|
| Session passes $10 / $25 / $50 | A one-time alert in the chat (it does not block) |
| A session, the day or the month reaches **80%** of its cap | A one-time warning that includes the pace, for example "at ≈$40/h the session cap is reached in ~25 min" |
| Session passes **$100** | New prompts are **blocked**, and tool calls are **denied mid-turn**, so a long autonomous run also stops |
| This machine passes **$150 today** (all sessions) | Same block |
| This machine passes the **$300 monthly budget** | A warning once a day. Turn on `monthly_hard_stop` to block instead |
| A session spends **more than $15 in 15 minutes** | The **runaway breaker** pauses the next tool call once. That much spend usually means a loop or a huge context |
| You come back to an idle session with a big context | Blocked **once** with the cost of re-caching it ("idle 22 h, re-caches 612k tokens ≈ $4.90"). Re-send to continue, or `/clear` |
| You start a session (once a day) | The top 3 cost cuts from your last 14 days, each with its fix. `/cost-guard:fix` can apply them |
| Context passes **150k tokens** | A one-time note on what fills it and what to drop |
| A stop or a cap warning, while you're away | A **desktop notification** (macOS, or Linux with `notify-send`) |
| Status line (optional) | `Opus 5.5 · session $12.34 · today $40.10 · month $210.00 · $18.20/h · ctx 612k · cache cold` |

To get past a stop, put `#override` in your message. That adds one more cap-sized allowance.
The plugin never blocks the app's own bookkeeping tools (task list, wrap-up, plan mode). You can
change that list with `"never_block": [...]` in `overrides.json`.

## What burns tokens without being used

At the start of a session, once a day, the plugin shows the top 3 items with the fix for each.
To change how often: `/cost-guard:limits tips 7` (weekly) or `tips 0` (off). To stop a
suggestion you want to keep: `/cost-guard:limits ignore claude-mem`. Any part of a plugin,
skill, server, file or hook name works.

`/cost-guard:insights [days]` (14 days by default) shows the full list. It reads every recent
transcript, subagents included, and finds what goes out with **every** message and how often it
was used:

- **Plugins whose skills were never used.** The report warns you when the plugin also
  provides an MCP server you do use.
- **Personal skills and custom agents that were never used**, and descriptions that are too long.
- **MCP servers** that were never called, servers that only wrap a CLI you already have
  (`gh`, `kubectl`, …), and servers that load more than 20 tools.
- **CLAUDE.md and MEMORY.md files** over 300 lines or 2.5k tokens.
- **Large skills that run often**, and **hooks** that inject text at session start.

### Apply a tip with one step, and undo it

`/cost-guard:fix` lists what it can change automatically, asks which items to apply, and applies
only those:

| Fix | What it does | Undo |
|---|---|---|
| Unused plugin | `claude plugin disable <plugin>` | `claude plugin enable <plugin>` |
| Unused personal skill or agent | moves it to `~/.claude/skills-off/` or `~/.claude/agents-off/` | moves it back |
| Unused MCP server from `~/.claude.json` | `claude mcp remove` (its config is saved first) | `claude mcp add-json` with the saved config |

`/cost-guard:fix undo` reverses the last change, and `/cost-guard:fix undo all` reverses all of
them. App connectors and hooks can't be changed automatically, so they keep manual instructions.

## Context advice

Run `/cost-guard:context` at any time. Each alert and stop also includes the top two points:

- **Large tool output still in context**, with a `/compact drop the raw output of …` line you
  can paste as is.
- **Wasteful reads**: lock files, generated or minified files, and full test-run logs (the
  advice is to run only the failing test or pipe through `| tail -50`).
- **The same file read 3 or more times**, and **images** still in context.
- **What is loaded on every message**: MCP servers, CLAUDE.md files and agents, as above.
- **A large context**, broken down into its parts. Use `/clear` for a new task or `/compact`
  for the same task.
- **Subagents** that cost more than 30% of the session, and **Opus or Fable** sessions
  where Sonnet would do.

## Reports

```
/cost-guard:report                  today, by session
/cost-guard:report week by project  last 7 days, per project folder
/cost-guard:report month by model   this month, per model
/cost-guard:report 30 by day json   JSON, for scripts or a spreadsheet
```

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

The plugin options set defaults for everyone (`/config` → Cost Guard, or
`/plugin configure cost-guard@team-cost-tools`). The command overrides them on your machine,
and project limits override both inside their folder:

```
/cost-guard:limits                   show the current limits and where each comes from
/cost-guard:limits session 50        session cap $50
/cost-guard:limits daily 200         daily cap $200
/cost-guard:limits monthly 300       monthly budget $300
/cost-guard:limits stop-monthly on   block at the monthly budget instead of warning
/cost-guard:limits pct 80            warning at 80% of each cap
/cost-guard:limits burst 15          runaway breaker at $15 per 15 minutes (0 = off)
/cost-guard:limits notify off        no desktop notifications
/cost-guard:limits project session 20   a $20 session cap in this folder only
/cost-guard:limits alerts 5,20,40    alert amounts
/cost-guard:limits cold 2            cold-cache warning threshold
/cost-guard:limits context 200       context advice at 200k tokens
/cost-guard:limits reset             back to the plugin options (keeps ignores and project limits)
/cost-guard:limits ignore clear      show every tip again
```

## Why these checks

Most of the cost of a long session is **cache writes**, not code. The prompt cache
expires after an idle period (5 minutes or 1 hour). After that, the next message
re-writes the whole context at 1.25x to 2x the input price. In one $239.73 session,
$214 (89%) was cache writes. A run like that also spends fast, so the runaway breaker
stops it well before the session cap.

## Limits of the tool

- **It's an estimate.** It uses list prices from the local transcripts, with
  subagents included. Your company gateway may bill differently. To update prices,
  add `"prices": {"claude-opus-5-5": [input, output, cache_read]}` to
  `~/.claude/plugins/data/cost-guard-team-cost-tools/overrides.json` (USD per 1M tokens).
- **It's per machine and voluntary.** Anyone can disable it. Real enforcement
  (org spend limits, gateway budgets) needs an admin.
- **It runs at prompt and tool-call boundaries.** A single model response can't
  be stopped halfway through, so a session can go slightly over a cap.
- **The first scan of a month runs in the background.** Until it finishes, the monthly
  figure is left out; the plugin never guesses it.
- **One data folder per machine.** Hooks from parallel sessions share it; writes are atomic,
  and totals are rebuilt from transcript offsets. State files are private to your user (0600).
- **It fails open.** If the guard crashes, Claude Code keeps working, and the
  error is logged to `~/.claude/plugins/data/cost-guard-team-cost-tools/state/errors.log`.

## Development

```bash
python3 -m unittest discover -s tests -v
```

```bash
claude plugin validate plugin
```

The tests use a temporary HOME and synthetic transcripts, so they never touch real data.

```
.claude-plugin/marketplace.json   marketplace "team-cost-tools"
plugin/.claude-plugin/plugin.json manifest and the limit options
plugin/hooks/hooks.json           SessionStart, UserPromptSubmit, PreToolUse
plugin/commands/                  /cost-guard:limits, :report, :context, :insights, :fix, :statusline
plugin/scripts/cost_guard.py      all logic (Python 3, standard library only)
tests/test_cost_guard.py          unit tests
```

## Credits

Ideas were adapted from other open tools: claude-session-cost (transcript-based cost tracking
and private state files), cost-gate (warning at a percentage of the budget), ccusage (burn rate,
reports by period, project and model, JSON output), claude-code-tokenbudget (monthly quotas),
context-budget, unclog and token-optimizer (setup audits of CLAUDE.md files, agents, unused MCP
servers and wrapper servers). Cost Guard adds the cold-cache stop, the runaway breaker and
undoable fixes.
