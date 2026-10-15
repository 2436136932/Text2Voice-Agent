/**
 * Pure helpers for the metrics dashboard (`GET /meta/metrics`).
 *
 * Kept out of `MetricsPanel.vue` so they can be unit tested in the node
 * environment established by `vite.config.ts` (no DOM, no Vue).
 */

/** One parsed `<kind>:<provider>:<ok|error>` counter row. */
export interface ProviderCallRow {
  /** Raw counter key exactly as the backend sent it; unique per row. */
  key: string
  /** `llm` / `tts`, or `''` when the key did not carry one. */
  kind: string
  /** Provider id, or `''` when the key did not carry one. */
  provider: string
  /** Chinese label for the kind, for display. */
  kindLabel: string
  /** Provider shown in the table (falls back to a placeholder). */
  providerLabel: string
  ok: number
  error: number
  /** `ok + error`. */
  total: number
}

/** Placeholder used anywhere a figure is unknown / not computable. */
export const EMPTY_VALUE = '—'

const KIND_LABELS: Record<string, string> = {
  llm: '大模型',
  tts: '语音合成',
}

/** A sentinel that can never collide with a real provider id or counter key. */
const MISSING = '\u0000'

/**
 * Human label for a provider-call kind. Unknown kinds pass through as-is so a
 * new backend kind shows up in the table instead of disappearing.
 */
export function providerKindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? (kind || '其他')
}

/**
 * Parse the backend's `"<kind>:<provider>:<ok|error>"` counter keys.
 *
 * Robust by design: the key format is a plain string contract that the backend
 * may extend (a provider id containing `:` would add segments), so this never
 * throws and never drops a counter.
 *
 * Behaviours:
 *   - `"llm:deepseek:ok"`   -> kind `llm`, provider `deepseek`, ok +1
 *   - the result tag is the LAST segment: `ok`/`success` -> ok, `error`/anything
 *     else -> error (the backend only ever emits `ok` / `error`, but a future
 *     value must still be counted rather than thrown away)
 *   - the provider is everything between the kind and the tag, so
 *     `"tts:custom:http://x/y:ok"` keeps its full provider string
 *   - `< 2` segments (e.g. `"llm"`, `""`) -> one row with `kind`/`provider`
 *     empty and the whole key recorded, counted as an error
 *   - `ok` / `error` counters of the same kind+provider are merged into one row
 *
 * @returns one row per distinct kind+provider, sorted by total calls descending.
 */
export function parseProviderCalls(calls: Record<string, number> | null | undefined): ProviderCallRow[] {
  const rows = new Map<string, ProviderCallRow>()

  for (const [key, rawCount] of Object.entries(calls ?? {})) {
    const segments = String(key ?? '').split(':')
    const hasTag = segments.length >= 2
    const tag = hasTag ? (segments[segments.length - 1] ?? '').trim().toLowerCase() : ''
    const kind = hasTag ? (segments[0] ?? '').trim() : ''
    const provider = hasTag ? segments.slice(1, -1).join(':').trim() : ''

    // A non-numeric or negative count is not a real counter; treat it as 0
    // rather than rendering "NaN" or a negative total.
    const count =
      typeof rawCount === 'number' && Number.isFinite(rawCount) && rawCount > 0
        ? Math.trunc(rawCount)
        : 0
    const isOk = tag === 'ok' || tag === 'success'

    const id = `${kind}\u0000${provider}`
    let row = rows.get(id)
    if (!row) {
      row = {
        // The first key seen wins as the raw label; a merged row records the
        // rest so the information is still visible in the tooltip.
        key,
        kind,
        provider,
        kindLabel: providerKindLabel(kind),
        providerLabel: provider || '（未知服务商）',
        ok: 0,
        error: 0,
        total: 0,
      }
      rows.set(id, row)
    } else if (!row.key.includes(key)) {
      row.key = `${row.key}${MISSING}${key}`
    }

    if (isOk) row.ok += count
    else row.error += count
    row.total = row.ok + row.error
  }

  return [...rows.values()].sort((a, b) => {
    if (b.total !== a.total) return b.total - a.total
    // Stable, deterministic tie-break: an unstable order would make the table
    // flicker between refreshes with equal totals.
    return a.key.split(MISSING).join(' ').localeCompare(b.key.split(MISSING).join(' '))
  })
}

/** The `key` field of {@link ProviderCallRow} can hold several merged raw keys. */
export function providerCallKeys(row: ProviderCallRow): string {
  return row.key.split(MISSING).join('、')
}

/**
 * `generations_succeeded / generations_total` as a percentage.
 *
 * Returns `null` when it cannot be computed (missing or zero total), so the
 * caller renders {@link EMPTY_VALUE} instead of `NaN` / `Infinity` / `0%`.
 */
export function successRate(succeeded: number | null | undefined, total: number | null | undefined): number | null {
  if (!Number.isFinite(total) || !total || total <= 0) return null
  if (!Number.isFinite(succeeded)) return null
  return ((succeeded as number) / (total as number)) * 100
}

/**
 * Format `successRate` for display, e.g. `"92%"`; `"—"` when undefined.
 *
 * `maximumFractionDigits` is left at the default so `92%` stays `92%` and only
 * a genuine fraction such as `33.3%` shows decimals.
 */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY_VALUE
  return `${value.toFixed(1).replace(/\.0$/, '')}%`
}

/** Convenience: success rate of a generation counter pair, formatted. */
export function formatSuccessRate(
  succeeded: number | null | undefined,
  total: number | null | undefined,
): string {
  return formatPercent(successRate(succeeded, total))
}

/**
 * Format a second count human-readably in Chinese:
 *   `45` -> `45秒`, `3900` -> `1小时5分`, `90000` -> `1天1小时`
 *
 * Only the two most significant units are shown, which keeps the overview row
 * compact. A non-finite / negative input renders as {@link EMPTY_VALUE}.
 */
export function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return EMPTY_VALUE
  }
  const total = Math.floor(seconds)

  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60

  if (days > 0) return hours > 0 ? `${days}天${hours}小时` : `${days}天`
  if (hours > 0) return minutes > 0 ? `${hours}小时${minutes}分` : `${hours}小时`
  if (minutes > 0) return secs > 0 ? `${minutes}分${secs}秒` : `${minutes}分`
  return `${secs}秒`
}
