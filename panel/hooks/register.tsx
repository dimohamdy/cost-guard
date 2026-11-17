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

// ---------- drawings ----------
// An Svg is drawn as an image, so it can't read the app's colour tokens: each one carries
// both themes and picks one with prefers-color-scheme. Status steps are fixed in both.
const GOOD = '#0ca30c'
const WARN = '#fab219'
const CRIT = '#d03b3b'
const STYLE =
  '<style>' +
  'text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}' +
  '.ink{fill:#0b0b0b}.sub{fill:#52514e}.track{stroke:#e8e7e3}.grid{stroke:#e2e1dc}' +
  '.bar{fill:#2a78d6}.bar:hover,.hit:hover+.bar{opacity:.8}.today{fill:#eb6834}.avg{stroke:#52514e}' +
  '@media (prefers-color-scheme:dark){.ink{fill:#f0efec}.sub{fill:#c3c2b7}.track{stroke:#383835}' +
  '.grid{stroke:#383835}.bar{fill:#3987e5}.today{fill:#d95926}.avg{stroke:#c3c2b7}}' +
  '</style>'

export function statusHex(pct: number): string {
  return pct >= 80 ? CRIT : pct >= 50 ? WARN : GOOD
}

function esc(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export type Ring = { label: string; pct: number; sub: string }

/** One to three ring gauges side by side: the % in the middle, the label and a note below. */
export function ringsSvg(rings: Ring[]): string {
  const w = 300
  const h = 150
  const many = rings.length > 2
  const r = many ? 34 : 42
  const sw = many ? 9 : 10
  const big = many ? 18 : 21
  const labelSize = many ? 11.5 : 12.5
  const c = 2 * Math.PI * r
  const slot = w / Math.max(rings.length, 1)
  const body = rings
    .map((g, i) => {
      const cx = slot * i + slot / 2
      const cy = 58
      const shown = Math.max(0, Math.min(g.pct, 100))
      const dash = (shown / 100) * c
      const over = g.pct > 100
      return (
        `<g><title>${esc(`${g.label}: ${Math.round(g.pct)}% · ${g.sub}`)}</title>` +
        `<circle class="track" cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke-width="${sw}"/>` +
        (dash > 0
          ? `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${statusHex(g.pct)}" stroke-width="${sw}" ` +
            `stroke-linecap="round" stroke-dasharray="${dash.toFixed(1)} ${c.toFixed(1)}" ` +
            `transform="rotate(-90 ${cx} ${cy})"/>`
          : '') +
        `<text class="ink" x="${cx}" y="${cy + 7}" font-size="${big}" font-weight="650" text-anchor="middle">` +
        `${Math.round(g.pct)}%</text>` +
        (over ? `<text x="${cx}" y="${cy + 22}" font-size="9" text-anchor="middle" fill="${CRIT}">over</text>` : '') +
        `<text class="ink" x="${cx}" y="${cy + r + 24}" font-size="${labelSize}" font-weight="600" text-anchor="middle">${esc(g.label)}</text>` +
        `<text class="sub" x="${cx}" y="${cy + r + 40}" font-size="${many ? 10 : 11}" text-anchor="middle">${esc(g.sub)}</text></g>`
      )
    })
    .join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${STYLE}${body}</svg>`
}

/** 14 days of spend: bars with a 2px gap, today in the accent, a dashed average line. */
export function dayChart(days: { day: string; usd: number }[]): string {
  const w = 300
  const h = 122
  const top = 16
  const base = h - 20
  const max = Math.max(...days.map(d => d.usd), 0.01)
  const active = days.filter(d => d.usd > 0)
  const avg = active.length ? active.reduce((a, d) => a + d.usd, 0) / active.length : 0
  const slot = w / Math.max(days.length, 1)
  const bw = slot - 2
  const y = (v: number) => base - (v / max) * (base - top)
  const bars = days
    .map((d, i) => {
      const isToday = i === days.length - 1
      const bh = d.usd > 0 ? Math.max(base - y(d.usd), 3) : 0
      const x = i * slot + 1
      const date = new Date(`${d.day}T12:00:00`)
      const tip = esc(`${date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })}: ${money(d.usd)}`)
      const anchor = i === 0 ? 'start' : isToday ? 'end' : 'middle'
      const lx = i === 0 ? x : isToday ? x + bw : x + bw / 2
      const label =
        i === 0 || isToday || i === Math.floor(days.length / 2)
          ? `<text class="sub" x="${lx}" y="${h - 5}" font-size="9.5" text-anchor="${anchor}">` +
            `${isToday ? 'today' : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</text>`
          : ''
      return (
        `<g><title>${tip}</title><rect x="${x}" y="${top - 4}" width="${slot}" height="${base - top + 4}" fill="transparent"/>` +
        (bh ? `<path class="${isToday ? 'today' : 'bar'}" d="M${x},${base}v-${(bh - 3).toFixed(1)}q0,-3 3,-3h${(bw - 6).toFixed(1)}q3,0 3,3v${(bh - 3).toFixed(1)}z"/>` : '') +
        `</g>${label}`
      )
    })
    .join('')
  const avgLine =
    avg > 0
      ? `<line class="avg" x1="0" x2="${w}" y1="${y(avg).toFixed(1)}" y2="${y(avg).toFixed(1)}" stroke-width="1" stroke-dasharray="3 3" opacity=".7"/>` +
        `<text class="sub" x="${w}" y="${(y(avg) - 4).toFixed(1)}" font-size="9.5" text-anchor="end">avg ${money(avg)}/day</text>`
      : ''
  const axis = `<line class="grid" x1="0" x2="${w}" y1="${base + 0.5}" y2="${base + 0.5}" stroke-width="1"/>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${STYLE}${axis}${bars}${avgLine}</svg>`
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

    const card = (title: string, ...children: unknown[]) => (
      <Box
        flexDirection="column"
        gap="sm"
        marginBottom="md"
        padding="md"
        borderStyle="round"
        borderDimColor
        key={`c-${title}`}
      >
        <Text size="caption" weight="semibold" dimColor>
          {title.toUpperCase()}
        </Text>
        {children}
      </Box>
    )

    const meter = (label: string, pct: number, valueText: string, key: string) =>
      hasProgress ? (
        <els.Progress
          key={key}
          label={label}
          value={Math.min(Math.max(pct, 0), 100)}
          max={100}
          valueText={valueText}
          color={levelColor(pct)}
        />
      ) : (
        <Text color={levelColor(pct)} key={key}>
          {label} {'▓'.repeat(Math.round(Math.min(pct, 100) / 10))}
          {'░'.repeat(10 - Math.round(Math.min(pct, 100) / 10))} {valueText}
        </Text>
      )

    const tile = (caption: string, value: string, note: string | null, color?: string) => (
      <Box flexDirection="column" flexGrow={1} minWidth={84} padding="sm" borderStyle="round" borderDimColor key={`t-${caption}`}>
        <Text size="caption" dimColor>
          {caption}
        </Text>
        <Text size="heading" weight="semibold" tabular color={color}>
          {value}
        </Text>
        {note && (
          <Text size="caption" dimColor>
            {note}
          </Text>
        )}
      </Box>
    )

    const blocks: unknown[] = []
    if (s.error) {
      blocks.push(
        <Box marginBottom="md" padding="sm" borderStyle="round" borderColor="error" key="err">
          <Text color="error">Cost Guard data unavailable: {s.error}</Text>
        </Box>,
      )
    }

    // 1. The headline: plan limits, or the caps on an API key.
    const rings: Ring[] = []
    const ringRows: unknown[] = []
    if (limits.length) {
      for (const w of limits) {
        const sub =
          w.kind === 'spend_limit' && w.limit_usd
            ? `${money(w.used_usd)} of ${money(w.limit_usd)}`
            : w.until
              ? `resets in ${w.until}`
              : 'reset time unknown'
        rings.push({ label: w.kind === 'five_hour' ? '5-hour' : w.kind === 'seven_day' ? 'Weekly' : 'Spend limit', pct: w.pct, sub })
        if (!hasSvg) ringRows.push(meter(w.label, w.pct, `${Math.round(w.pct)}% · ${sub}`, `m-${w.kind}`))
        if (w.pace && w.pct < 100) {
          ringRows.push(
            <Text size="footnote" color={w.pace.before_reset ? 'warning' : 'subtle'} key={`pace-${w.kind}`}>
              {w.pace.before_reset ? '▲ ' : '✓ '}
              {w.label}:{' '}
              {w.pace.before_reset
                ? `at this pace it's full around ${w.pace.full_when}, before it resets`
                : `at this pace it lasts until it resets${w.resets_when ? ` (${w.resets_when})` : ''}`}
            </Text>,
          )
        }
      }
    } else if (d) {
      rings.push({ label: 'Today', pct: (d.today / d.caps.daily) * 100, sub: `${money(d.today)} of ${money(d.caps.daily)}` })
      if (d.month !== null) {
        rings.push({ label: 'This month', pct: (d.month / d.caps.monthly) * 100, sub: `${money(d.month)} of ${money(d.caps.monthly)}` })
      }
      if (!hasSvg) for (const g of rings) ringRows.push(meter(g.label, g.pct, g.sub, `m-${g.label}`))
      if (d.month !== null && d.month > d.caps.monthly) {
        ringRows.push(
          <Text size="footnote" color="error" key="over">
            ▲ Over the monthly budget by {money(d.month - d.caps.monthly)}. Cost Guard warns once a day
            {d.caps.monthly_hard_stop ? ' and stops new prompts' : ''}.
          </Text>,
        )
      }
    }
    if (rings.length) {
      blocks.push(
        card(
          limits.length ? (plan ? 'Plan limits' : 'Spend limit') : 'Budget',
          hasSvg && (
            <Box alignItems="center" key="rings">
              <els.Svg
                source={ringsSvg(rings)}
                alt={rings.map(g => `${g.label} ${Math.round(g.pct)}% (${g.sub})`).join('; ')}
                isInteractive
              />
            </Box>
          ),
          ...ringRows,
          !limits.length && d && (
            <Text size="caption" dimColor key="api-note">
              API key: no 5-hour or weekly limits. These are Cost Guard’s own caps.
            </Text>
          ),
        ),
      )
    }

    if (d) {
      // 2. This session at a glance.
      const ctxPct = l.contextPercent ?? (l.contextWindow ? (d.session.ctx / l.contextWindow) * 100 : null)
      const hot = d.session.last_15min >= 7.5
      blocks.push(
        <Box flexDirection="row" gap="sm" flexWrap="wrap" marginBottom="md" key="tiles">
          {tile('Session', money(d.session.usd), plan ? 'API value' : `cap ${money(d.caps.session)}`)}
          {tile('Spend rate', d.session.rate_per_h >= 0.5 ? `${money(d.session.rate_per_h)}/h` : 'idle', hot ? 'running hot' : 'last 30 min', hot ? 'warning' : undefined)}
          {tile(
            'Context',
            ctxPct !== null ? `${Math.round(ctxPct)}%` : `${Math.round(d.session.ctx / 1000)}k`,
            d.session.cache_cold ? 'cache cold' : `${Math.round(d.session.ctx / 1000)}k tokens`,
            ctxPct !== null ? levelColor(ctxPct) : undefined,
          )}
        </Box>,
      )
      if (d.advice.length) {
        blocks.push(
          card(
            'Right now',
            ...d.advice.slice(0, 2).map((a, i) => (
              <Text key={`adv-${i}`} size="footnote">
                <Text color="claude">● </Text>
                {a}
              </Text>
            )),
          ),
        )
      }

      // 3. Fourteen days and where it went.
      const total = d.by_day.reduce((a, x) => a + x.usd, 0)
      blocks.push(
        card(
          plan ? 'Last 14 days · API value' : 'Last 14 days',
          <Box flexDirection="row" gap="md" key="totals">
            <Text size="title" weight="semibold" tabular>
              {money(total)}
            </Text>
            <Text size="footnote" dimColor>
              {d.month !== null ? `${money(d.month)} this month` : 'month still being counted'}
            </Text>
          </Box>,
          hasSvg ? (
            <els.Svg
              key="chart"
              source={dayChart(d.by_day)}
              alt={`Spend per day: ${d.by_day.map(x => `${x.day} ${money(x.usd)}`).join(', ')}`}
              isInteractive
            />
          ) : (
            <Text dimColor key="chart">
              Last 7 days: {d.by_day.slice(-7).map(x => money(x.usd)).join(' · ')}
            </Text>
          ),
        ),
      )
      const monthTotal = d.month ?? d.by_project.reduce((a, p) => a + p.usd, 0)
      if (d.by_project.length) {
        blocks.push(
          card(
            'Top projects this month',
            ...d.by_project.slice(0, 4).map(p => {
              const share = monthTotal > 0 ? (p.usd / monthTotal) * 100 : 0
              return hasProgress ? (
                <els.Progress
                  key={`p-${p.key}`}
                  label={p.name}
                  value={share}
                  max={100}
                  valueText={`${money(p.usd)} · ${Math.round(share)}%`}
                  color="suggestion"
                />
              ) : (
                <Box flexDirection="row" justifyContent="space-between" key={`p-${p.key}`}>
                  <Text wrap="truncate-middle">{p.name}</Text>
                  <Text tabular dimColor>
                    {money(p.usd)} · {Math.round(share)}%
                  </Text>
                </Box>
              )
            }),
          ),
        )
      }

      // 4. What to change.
      const tips: Tip[] = d.tips ?? []
      blocks.push(
        card(
          'Ways to save',
          d.tips === null && (
            <Text dimColor key="t-wait">
              Looking through your last 14 days…
            </Text>
          ),
          d.tips !== null && tips.length === 0 && (
            <Text dimColor key="t-none">
              ✓ Nothing stands out: what goes out with each message gets used.
            </Text>
          ),
          ...tips.map((t, i) => (
            <Box flexDirection="column" gap="xs" key={`tip-${i}`}>
              <Box flexDirection="row" gap="sm" alignItems="center">
                <Text size="caption" weight="semibold" color="success">
                  SAVE ≈{money(t.usd)} / 2 WK
                </Text>
                {t.fixable && (
                  <Text size="caption" dimColor>
                    · one-step fix
                  </Text>
                )}
              </Box>
              <Text size="footnote">{t.text}</Text>
              {t.fixable && (
                <Box key={`fixrow-${i}`}>
                  <Button
                    key={`fix-${t.key}`}
                    label="Review and apply…"
                    variant="secondary"
                    onPress={() => void $.command.run({ command: 'cost-guard:fix' })}
                  />
                </Box>
              )}
            </Box>
          )),
        ),
      )
    }

    const mode = d
      ? d.billing === 'subscription'
        ? 'Pro/Max plan'
        : d.billing === 'gateway'
          ? 'Company gateway'
          : 'API key'
      : null

    return (
      <Box flexDirection="column" padding="sm">
        <Box flexDirection="row" justifyContent="space-between" alignItems="center" marginBottom="md">
          <Text size="footnote" dimColor>
            {mode ? `${mode} · ` : ''}
            {s.isLoading
              ? 'refreshing…'
              : s.updatedAt
                ? `updated ${new Date(s.updatedAt).toTimeString().slice(0, 5)}`
                : 'loading…'}
          </Text>
          <Button key="refresh" label="Refresh" variant="secondary" onPress={() => void refresh($, true)} />
        </Box>
        {blocks}
      </Box>
    )
  })
}
