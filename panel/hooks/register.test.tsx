import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'cost-guard-panel',
  component: 'Pane',
  requestId: 'cost-guard',
  props: {
    title: 'Cost Guard',
    isFocused: false,
    bodyColumns: 48,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const inAnHour = new Date(Date.now() + 100 * 60_000).toISOString()

const PANEL = {
  v: 1,
  generated_at: 0,
  billing: 'subscription',
  limits: [
    {
      kind: 'seven_day', label: 'weekly limit', pct: 41, resets_at: 0, resets_in: '3d 2h',
      resets_when: 'Thu 09:00', text: '',
      pace: { per_h: 2, full_at: 0, full_when: 'Wed 14:00', before_reset: true },
    },
  ],
  session: { usd: 12.5, rate_per_h: 8, last_15min: 2, ctx: 120_000, model: 'claude-opus-5-5', cache_cold: false },
  today: 30,
  month: 210,
  caps: { session: 100, daily: 150, monthly: 300, warn_at_pct: 80, monthly_hard_stop: false },
  by_day: Array.from({ length: 14 }, (_, i) => ({ day: `2026-11-${String(i + 5).padStart(2, '0')}`, usd: i })),
  by_project: [{ key: '/w/app', name: 'app', usd: 40 }],
  by_model: [],
  tips: [
    { key: 'a', usd: 9.5, text: 'Trim CLAUDE.md', fixable: false },
    { key: 'b', usd: 3.9, text: 'Disable plugin x', fixable: true },
    { key: 'c', usd: 1, text: 'Move skill y aside', fixable: true },
  ],
  advice: ['A big tool output is still in context.'],
}

function engine(on: any, calls: string[][], fiveHour = 62) {
  on('command.register', () => ({ value: { command: 'cost-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('clock.every', () => ({ value: {} }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('env.get', () => ({ value: '/home/me' }))
  on('fs.exists', () => ({ value: true }))
  on('session.id', () => ({ value: 's1' }))
  on('session.usage', () => ({
    value: {
      startedAt: 0,
      context: { tokens: 120_000, window: 200_000, percent: 60 },
      rateLimits: [{ kind: 'five_hour', percentUsed: fiveHour, resetsAt: inAnHour }],
    },
  }))
  on('process.run', (_: any, e: any) => {
    calls.push(e.argv)
    const out = e.argv.includes('panel-json') ? JSON.stringify(PANEL) : ''
    return { value: { exitCode: 0, stdout: out, stderr: '' } }
  })
}

test('the pane shows both plan limits, the pace and three tips', async ($, on) => {
  const calls: string[][] = []
  engine(on, calls)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  // The limits are ring gauges in one Svg; its alt text names each one.
  const drawn = JSON.stringify(await ui.drawn())
  expect(drawn).toContain('5-hour 62% (resets in 1h')
  expect(drawn).toContain('Weekly 41% (resets in 3d 2h)')
  expect(drawn).toContain('stroke-dasharray')
  expect(await ui.find({ type: 'Text', text: /Weekly limit: at this pace it's full around Wed 14:00/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /Trim CLAUDE.md/ })).toBeTruthy()
  expect((await ui.findAll({ type: 'Button', text: /Review and apply/ })).length).toBe(2)
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(2)
  expect(await ui.find({ type: 'Text', text: /Top projects this month/i })).toBeTruthy()

  // The live reading is handed to the hooks, then the data is read.
  expect(calls.some(argv => argv.includes('record-limits') && argv.includes('s1'))).toBe(true)
  expect(calls.some(argv => argv.includes('panel-json'))).toBe(true)
  await ui.unmount()
})

test('a failing script shows why, and the pane still draws', async ($, on) => {
  on('command.register', () => ({ value: { command: 'cost-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('clock.every', () => ({ value: {} }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('env.get', () => ({ value: '/home/me' }))
  on('fs.exists', () => ({ value: true }))
  on('session.id', () => ({ value: 's1' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 200_000 }, rateLimits: [] } }))
  on('process.run', () => ({ value: { exitCode: 1, stdout: '', stderr: 'Traceback\nValueError: boom' } }))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /data unavailable: ValueError: boom/ }))).toBeTruthy()
  expect(await ui.find({ type: 'Button', text: /Refresh/ })).toBeTruthy()
  await ui.unmount()
})

/** The drawn tree as plain lines, roughly as a terminal shows it. */
function lines(node: any): string[] {
  const text = (n: any): string =>
    typeof n === 'string' ? n : n?.type === 'Button' ? `[${n.props?.label ?? ''}]` : (n?.children ?? []).map(text).join('')
  const out: string[] = []
  const walk = (n: any) => {
    if (!n || typeof n === 'string') return
    if (n.type === 'Text') out.push(text(n))
    else if (n.type === 'Box' && n.props?.flexDirection === 'row') out.push((n.children ?? []).map(text).join('  '))
    else (n.children ?? []).forEach(walk)
  }
  walk(node)
  return out
}

test('the terminal pane: bars, sparkline, projects and tips', async ($, on) => {
  engine(on, [])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 60 } })
  const shown = lines(await ui.drawn())
  console.log('\n' + shown.join('\n'))
  const all = shown.join('\n')
  expect(all).toContain('◆ Cost Guard · Pro/Max plan')
  expect(all).toMatch(/5-hour  █+░+  62%  resets in 1h/)
  expect(all).toMatch(/Weekly  █+░+  41%  resets in 3d 2h/)
  expect(all).toContain('▲ full around Wed 14:00')
  expect(all).toMatch(/14 days ·[▁▂▃▄▅▆▇]{12}█  \$91\.00 · avg \$7\.00\/day/)
  expect(all).toMatch(/Nov 5 +▲ today/)
  expect(all).toContain('Top projects this month')
  expect(all).toContain('1. ≈$9.50 Trim CLAUDE.md')
  expect(await ui.find({ type: 'Button', text: /Review fixes/ })).toBeTruthy()
  expect(await ui.find({ type: 'Svg' })).toBeFalsy()
  await ui.unmount()
})

test('above the prompt the pane stays compact', async ($, on) => {
  engine(on, [])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, placement: 'inline' } })
  const all = lines(await ui.drawn()).join('\n')
  console.log('\n' + all)
  expect(all).toContain('3 ways to save ≈$14.40 / 2 wk')
  expect(all).not.toContain('14 days')
  await ui.unmount()
})

test('the desktop widget is just the rings', async ($, on) => {
  engine(on, [])
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop', props: { ...PANE.props, placement: 'widget' } })
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(1)
  expect(await ui.find({ type: 'Button' })).toBeFalsy()
  await ui.unmount()
})

const BAND = {
  plugin: 'cost-guard-panel',
  component: 'AbovePrompt',
  requestId: 'band',
  props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 6, bodyColumns: 80, scroll: { offset: 0, bodyRows: 6 } },
} as const

test('a calm session draws no band', async ($, on) => {
  engine(on, [])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /Cost Guard/ })).toBeFalsy()
  await ui.unmount()
})

test('a limit past 80% gets a line above the prompt, until hidden', async ($, on) => {
  engine(on, [], 86)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'cost-panel' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const row = await ui.find({ type: 'Text', text: /Cost Guard: 5-hour limit at 86% · resets in 1h/ })
  expect(row).toBeTruthy()
  console.log('\n' + lines(await ui.drawn()).join('\n'))
  await ui.press({ key: 'hide' })
  await ui.redraw()
  expect(await ui.find({ type: 'Text', text: /Cost Guard/ })).toBeFalsy()
  await ui.unmount()
})
