/** One plan or spend window, as `cost_guard.py panel-json` reports it. */
export type LimitWindow = {
  kind: 'five_hour' | 'seven_day' | 'spend_limit' | string
  label: string
  pct: number
  resets_at: number | null
  resets_in: string | null
  resets_when: string | null
  used_usd?: number
  limit_usd?: number
  period?: string
  text: string
  pace: { per_h: number; full_at: number; full_when: string; before_reset: boolean } | null
}

export type Tip = { key: string; usd: number; text: string; fixable: boolean }

/** `cost_guard.py panel-json`, version 1. */
export type PanelData = {
  v: number
  generated_at: number
  billing: 'api' | 'subscription' | 'gateway' | string
  limits: LimitWindow[]
  session: {
    usd: number
    rate_per_h: number
    last_15min: number
    ctx: number
    model: string | null
    cache_cold: boolean
  }
  today: number
  month: number | null
  caps: { session: number; daily: number; monthly: number; warn_at_pct: number; monthly_hard_stop: boolean }
  by_day: { day: string; usd: number }[]
  by_project: { key: string; name: string; usd: number }[]
  by_model: { key: string; usd: number }[]
  tips: Tip[] | null
  advice: string[]
}

/** What the engine itself reports, between two `panel-json` runs. */
export type Live = {
  limits: { kind: string; percentUsed: number; resetsAt?: string }[]
  contextPercent: number | null
  contextWindow: number | null
}

export type Status = { updatedAt: number; error: string | null; isLoading: boolean }

declare module 'claude-code' {
  interface PluginState {
    'cost-guard-panel': {
      data: Shaped<PanelData | null>
      live: Shaped<Live>
      status: Shaped<Status>
    }
  }
}
