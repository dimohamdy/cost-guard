import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Live, LimitWindow, PanelData, Status, Tip } from '../types'

const PANE = 'cost-guard'
const TITLE = 'Cost Guard'
const DATA_DIR = '.claude/plugins/data/cost-guard-team-cost-tools'
const REFRESH_MS = 60_000 // keep reset countdowns right while the pane is open
const MIN_GAP_MS = 30_000 // at most one panel-json run per 30 s from engine events

// Change a tag when that value's fields change: it declines what an older version stored.
const data = atom({ plugin: 'cost-guard-panel', key: 'data' } as const, null as PanelData | null, {
  shape: 'panel-1',
})
const live = atom(
  { plugin: 'cost-guard-panel', key: 'live' } as const,
  { limits: [], contextPercent: null, contextWindow: null } as Live,
  { shape: 'live-1' },
)
const status = atom(
  { plugin: 'cost-guard-panel', key: 'status' } as const,
  { updatedAt: 0, error: null, isLoading: false } as Status,
  { shape: 'status-1' },
)

const LABELS: Record<string, string> = {
  five_hour: '5-hour limit',
  seven_day: 'Weekly limit',
  spend_limit: 'Company spend',
}

export function money(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return '—'
  return usd >= 100 ? `$${Math.round(usd).toLocaleString('en-US')}` : `$${usd.toFixed(2)}`
}

export function levelColor(pct: number): 'success' | 'warning' | 'error' {
  return pct >= 80 ? 'error' : pct >= 50 ? 'warning' : 'success'
}

export function untilText(resetsAt: string | undefined, now: number): string | null {
  if (!resetsAt) return null
  const ms = Date.parse(resetsAt) - now
  if (!(ms > 0)) return null
  const min = Math.floor(ms / 60_000)
  if (min < 60) return `${min} min`
  const h = Math.floor(min / 60)
  return h < 48 ? `${h}h${String(min % 60).padStart(2, '0')}` : `${Math.floor(h / 24)}d ${h % 24}h`
}

/** The windows to draw: the engine's live reading wins, panel-json adds pace and dollars. */
export function mergeLimits(d: PanelData | null, l: Live, now: number): (LimitWindow & { until: string | null })[] {
  const fromScript = new Map((d?.limits ?? []).map(w => [w.kind, w]))
  const kinds = new Set([...l.limits.map(w => w.kind), ...fromScript.keys()])
  const out: (LimitWindow & { until: string | null })[] = []
  for (const kind of ['five_hour', 'seven_day', 'spend_limit']) {
    if (!kinds.has(kind)) continue
    const s = fromScript.get(kind)
    const e = l.limits.find(w => w.kind === kind)
    const pct = e ? e.percentUsed : (s?.pct ?? 0)
    const until = e ? untilText(e.resetsAt, now) : (s?.resets_in ?? null)
    out.push({
      kind,
      label: LABELS[kind] ?? kind,
      pct,
      resets_at: s?.resets_at ?? null,
      resets_in: s?.resets_in ?? null,
      resets_when: s?.resets_when ?? null,
      used_usd: s?.used_usd,
      limit_usd: s?.limit_usd,
      period: s?.period,
      text: s?.text ?? '',
      pace: s?.pace ?? null,
      until,
    })
  }
  return out
}

/** A 14-day bar chart as one SVG document, today in the accent colour. */
export function dayChart(days: { day: string; usd: number }[]): string {
  const w = 300
  const h = 96
  const top = 14
  const base = h - 16
  const max = Math.max(...days.map(d => d.usd), 0.01)
  const slot = w / Math.max(days.length, 1)
  const bars = days
    .map((d, i) => {
      const bh = Math.max((d.usd / max) * (base - top), d.usd > 0 ? 2 : 0)
      const x = i * slot + slot * 0.18
      const isToday = i === days.length - 1
      const fill = isToday ? '#d97757' : '#8b93a7'
      const label =
        i % 2 === days.length % 2 || isToday
          ? `<text x="${x + slot * 0.32}" y="${h - 3}" font-size="9" text-anchor="middle" fill="#8b93a7">${d.day.slice(8)}</text>`
          : ''
      return (
        `<rect x="${x.toFixed(1)}" y="${(base - bh).toFixed(1)}" width="${(slot * 0.64).toFixed(1)}" ` +
        `height="${bh.toFixed(1)}" rx="2" fill="${fill}" opacity="${isToday ? 1 : 0.75}"><title>${d.day}: ${money(d.usd)}</title></rect>` +
        label
      )
    })
    .join('')
  const peak = `<text x="0" y="10" font-size="9" fill="#8b93a7">peak ${money(max)}</text>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${peak}${bars}</svg>`
}

let script = ''
let scriptOption = ''
let lastRun = 0
const crossed = new Set<string>()

async function findScript($: any): Promise<string> {
  if (script) return script
  const set = scriptOption
  const home = (await $.env.get('HOME')) ?? ''
  const candidate = set || `${home}/${DATA_DIR}/cost_guard.py`
  script = (await $.fs.exists(candidate)) ? candidate : ''
  return script
}

async function readLive($: any) {
  try {
    const u = await $.session.usage()
    await update($, live, () => ({
      limits: (u.rateLimits ?? []).map((w: any) => ({
        kind: w.kind,
        percentUsed: w.percentUsed,
        resetsAt: w.resetsAt,
      })),
      contextPercent: u.context?.percent ?? null,
      contextWindow: u.context?.window ?? null,
    }))
    return u.rateLimits ?? []
  } catch {
    return []
  }
}

async function refresh($: any, force = false) {
  const now = Date.now()
  if (!force && now - lastRun < MIN_GAP_MS) return
  lastRun = now
  await update($, status, s => ({ ...s, isLoading: true }))
  const rateLimits = await readLive($)
  const path = await findScript($)
  if (!path) {
    await update($, status, () => ({
      updatedAt: now,
      isLoading: false,
      error: 'Cost Guard is not set up yet: start a new session once with the cost-guard plugin enabled.',
    }))
    return
  }
  try {
    if (rateLimits.length) {
      // The hooks can't see plan limits in the app: hand them the reading.
      await $.process.run(['python3', path, 'record-limits'], {
        stdin: JSON.stringify(rateLimits),
        timeoutMs: 5000,
      })
    }
    const sid = await $.session.id()
    const r = await $.process.run(['python3', path, 'panel-json', '--session', sid], { timeoutMs: 10_000 })
    if (r.exitCode !== 0) throw new Error((r.stderr || r.stdout || 'no output').trim().split('\n').pop())
    const parsed = JSON.parse(r.stdout) as PanelData
    await update($, data, () => parsed)
    await update($, status, () => ({ updatedAt: Date.now(), isLoading: false, error: null }))
  } catch (err) {
    await update($, status, s => ({ ...s, isLoading: false, error: String((err as Error)?.message ?? err) }))
  }
}

function toastCrossings($: any, limits: { kind: string; percentUsed: number; resetsAt?: string }[]) {
  for (const w of limits) {
    const key = `${w.kind}:${w.resetsAt ?? ''}`
    if (w.percentUsed >= 80 && !crossed.has(key)) {
      crossed.add(key)
      $.ui.toast(`${LABELS[w.kind] ?? w.kind} at ${Math.round(w.percentUsed)}%`)
    }
  }
}

export const register: Register = (on, options) => {
  scriptOption = String((options as any)?.script_path ?? '').trim()
  script = ''

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'cost-panel', description: 'Open the Cost Guard pane' })
    void $.ui.open({ id: PANE, title: TITLE })
    void refresh($, true)
    $.clock.every(REFRESH_MS, () => {
      void refresh($, true)
    })
    return next(e)
  })

  on('command.run', { command: 'cost-panel' }, async $ => {
    await $.ui.open({ id: PANE, title: TITLE })
    await refresh($, true)
    return { text: 'Cost Guard pane opened.' }
  })

  on('session.measure', async ($, e, next) => {
    if (e.changed.includes('rateLimits')) toastCrossings($, e.rateLimits)
    if (e.changed.includes('rateLimits') || e.changed.includes('cost')) void refresh($)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e) as any
    const { Box, Text, Button } = els
    const hasProgress = 'Progress' in els
    const hasSvg = e.surface !== 'terminal' && 'Svg' in els // the terminal draws Svg as an empty box
    const d = await read($, data)
    const l = await read($, live)
    const s = await read($, status)
    const now = Date.now()
    const limits = mergeLimits(d, l, now)
    const plan = d?.billing === 'subscription' || limits.some(w => w.kind !== 'spend_limit')

    const section = (title: string, ...children: unknown[]) => (
      <Box flexDirection="column" gap="xs" marginBottom="md" key={`s-${title}`}>
        <Text size="caption" weight="semibold" dimColor>
          {title.toUpperCase()}
        </Text>
        {children}
      </Box>
    )

    const meter = (label: string, pct: number, valueText: string, key: string) =>
      hasProgress ? (
        <els.Progress key={key} label={label} value={Math.min(pct, 100)} max={100} valueText={valueText} color={levelColor(pct)} />
      ) : (
        <Text color={levelColor(pct)}>
          {label} {'▓'.repeat(Math.round(Math.min(pct, 100) / 10))}
          {'░'.repeat(10 - Math.round(Math.min(pct, 100) / 10))} {valueText}
        </Text>
      )

    const limitRows = limits.map(w => {
      const value =
        w.kind === 'spend_limit' && w.limit_usd
          ? `${money(w.used_usd)} of ${money(w.limit_usd)}${w.period ? ` ${w.period}` : ''} · ${Math.round(w.pct)}%`
          : `${Math.round(w.pct)}%${w.until ? ` · resets in ${w.until}` : ''}`
      return (
        <Box flexDirection="column" key={w.kind}>
          {meter(w.label, w.pct, value, `m-${w.kind}`)}
          {w.pace && w.pct < 100 && (
            <Text size="footnote" color={w.pace.before_reset ? 'warning' : 'subtle'}>
              {w.pace.before_reset
                ? `At this pace it's full around ${w.pace.full_when}, before it resets`
                : `At this pace it lasts until it resets${w.resets_when ? ` (${w.resets_when})` : ''}`}
            </Text>
          )}
        </Box>
      )
    })

    const ctxPct = l.contextPercent ?? (d && l.contextWindow ? (d.session.ctx / l.contextWindow) * 100 : null)
    const tips: Tip[] = d?.tips ?? []

    const blocks: unknown[] = []
    if (s.error) {
      blocks.push(
        <Box marginBottom="md" key="err">
          <Text color="error">Cost Guard data unavailable: {s.error}</Text>
        </Box>,
      )
    }
    if (limits.length > 0) {
      blocks.push(section(plan ? 'Plan limits' : 'Spend limit', ...limitRows))
    } else if (d) {
      blocks.push(
        section(
          'Limits',
          <Text size="footnote" dimColor>
            {d.billing === 'api'
              ? 'Pay-per-token account: no 5-hour or weekly limits. The caps below are Cost Guard’s own.'
              : 'No limit reading yet: it appears after the first reply in this session.'}
          </Text>,
        ),
      )
    }
    if (d) {
      blocks.push(
        section(
          'This session',
          <Box flexDirection="row" gap="md" flexWrap="wrap">
            <Text weight="semibold" tabular>
              {money(d.session.usd)}
              {plan ? ' API value' : ''}
            </Text>
            {d.session.rate_per_h >= 1 && (
              <Text tabular color={d.session.last_15min >= 7.5 ? 'warning' : 'subtle'}>
                {money(d.session.rate_per_h)}/h
              </Text>
            )}
            {d.session.cache_cold && <Text color="warning">cache cold</Text>}
            {d.session.model && <Text dimColor>{d.session.model.replace('claude-', '')}</Text>}
          </Box>,
          ctxPct !== null &&
            meter('Context', ctxPct, `${Math.round(d.session.ctx / 1000)}k tokens · ${Math.round(ctxPct)}%`, 'ctx'),
          ...d.advice.slice(0, 2).map((a, i) => (
            <Text key={`adv-${i}`} size="footnote" dimColor>
              • {a}
            </Text>
          )),
        ),
      )
      if (!plan) {
        blocks.push(
          section(
            'Budget',
            meter('Today', (d.today / d.caps.daily) * 100, `${money(d.today)} of ${money(d.caps.daily)}`, 'today'),
            d.month !== null &&
              meter('Month', (d.month / d.caps.monthly) * 100, `${money(d.month)} of ${money(d.caps.monthly)}`, 'month'),
          ),
        )
      }
      blocks.push(
        section(
          plan ? 'Usage, 14 days (API value)' : 'Spend, 14 days',
          hasSvg ? (
            <els.Svg
              source={dayChart(d.by_day)}
              alt={`Spend per day: ${d.by_day.map(x => `${x.day} ${money(x.usd)}`).join(', ')}`}
              isInteractive
            />
          ) : (
            <Text dimColor>Last 7 days: {d.by_day.slice(-7).map(x => money(x.usd)).join(' · ')}</Text>
          ),
          ...d.by_project.slice(0, 4).map(p => (
            <Box flexDirection="row" justifyContent="space-between" key={`p-${p.key}`}>
              <Text wrap="truncate-middle">{p.name}</Text>
              <Text tabular dimColor>
                {money(p.usd)}
              </Text>
            </Box>
          )),
        ),
      )
      blocks.push(
        section(
          'Top ways to save',
          d.tips === null && <Text dimColor>Looking through your last 14 days…</Text>,
          d.tips !== null && tips.length === 0 && <Text dimColor>Nothing stands out.</Text>,
          ...tips.map((t, i) => (
            <Box flexDirection="column" key={`tip-${i}`} gap="xs">
              <Text size="footnote">
                <Text weight="semibold">≈{money(t.usd)}/2 wk </Text>
                {t.text}
              </Text>
              {t.fixable && (
                <Button
                  key={`fix-${t.key}`}
                  label="Review and apply…"
                  variant="secondary"
                  onPress={() => void $.command.run({ command: 'cost-guard:fix' })}
                />
              )}
            </Box>
          )),
        ),
      )
    }
    const mode = d
      ? ` · ${d.billing === 'subscription' ? 'Pro/Max plan' : d.billing === 'gateway' ? 'company gateway' : 'API key'}`
      : ''

    return (
      <Box flexDirection="column" padding="sm">
        {blocks}
        <Box flexDirection="row" gap="sm" alignItems="center">
          <Button
            key="refresh"
            label={s.isLoading ? 'Refreshing…' : 'Refresh'}
            variant="secondary"
            onPress={() => void refresh($, true)}
          />
          <Text size="caption" dimColor>
            {s.updatedAt ? `Updated ${new Date(s.updatedAt).toTimeString().slice(0, 5)}` : 'Loading…'}
            {mode}
          </Text>
        </Box>
      </Box>
    )
  })
}
