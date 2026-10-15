import { describe, expect, it } from 'vitest'
import {
  EMPTY_VALUE,
  formatPercent,
  formatSuccessRate,
  formatUptime,
  parseProviderCalls,
  providerCallKeys,
  providerKindLabel,
  successRate,
} from '@/utils/metrics'

/**
 * `GET /meta/metrics` counts provider calls in a `"<kind>:<provider>:<ok|error>"`
 * keyed dict (backend/app/services/metrics.py:record_provider_call). The panel
 * turns those keys into a per-provider success/failure table, so a parsing
 * regression would silently show wrong operational numbers.
 *
 * These are pure-logic tests: the vitest environment is `node`, no DOM.
 */

describe('provider_calls key parsing', () => {
  it('parses the normal "llm:deepseek:ok" form', () => {
    const [row] = parseProviderCalls({ 'llm:deepseek:ok': 12 })

    expect(row.kind).toBe('llm')
    expect(row.provider).toBe('deepseek')
    expect(row.ok).toBe(12)
    expect(row.error).toBe(0)
    expect(row.total).toBe(12)
  })

  it('parses an error counter into the failure column', () => {
    const [row] = parseProviderCalls({ 'tts:openai:error': 3 })

    expect(row.kind).toBe('tts')
    expect(row.provider).toBe('openai')
    expect(row.ok).toBe(0)
    expect(row.error).toBe(3)
    expect(row.total).toBe(3)
  })

  it('merges the ok and error counters of one kind+provider into a single row', () => {
    const rows = parseProviderCalls({
      'llm:deepseek:ok': 12,
      'llm:deepseek:error': 3,
      'tts:openai:ok': 5,
    })

    expect(rows).toHaveLength(2)
    const deepseek = rows.find((row) => row.provider === 'deepseek')
    expect(deepseek).toMatchObject({ kind: 'llm', ok: 12, error: 3, total: 15 })

    const openai = rows.find((row) => row.provider === 'openai')
    expect(openai).toMatchObject({ kind: 'tts', ok: 5, error: 0, total: 5 })
  })

  it('sorts rows by total calls descending', () => {
    const rows = parseProviderCalls({
      'llm:a:ok': 1,
      'llm:b:ok': 9,
      'tts:c:ok': 4,
      'tts:d:error': 4,
    })

    expect(rows.map((row) => row.total)).toEqual([9, 4, 4, 1])
  })

  it('tolerates a malformed key without throwing', () => {
    const calls = {
      'llm': 1, // only one segment
      '': 2, // empty key
      ':': 3, // separators only
      'llm::ok': 4, // empty provider
      'llm:deepseek': 5, // missing the ok/error tag
      'llm:deepseek:weird': 6, // unknown tag
      'llm:deepseek:ok': 7,
      'tts:custom:http://x/y:ok': 8, // colons inside the provider
      'llm:provider:OK': 9, // upper-case tag
      'llm:provider: success ': 10, // padded tag
    }

    expect(() => parseProviderCalls(calls)).not.toThrow()

    const rows = parseProviderCalls(calls)
    // Nothing is dropped: every counter lands in some row.
    const grandTotal = rows.reduce((sum, row) => sum + row.total, 0)
    const expectedTotal = Object.values(calls).reduce((sum, value) => sum + value, 0)
    expect(grandTotal).toBe(expectedTotal)

    // Every row is still a usable shape for the table.
    for (const row of rows) {
      expect(typeof row.kind).toBe('string')
      expect(typeof row.providerLabel).toBe('string')
      expect(row.providerLabel.length).toBeGreaterThan(0)
      expect(Number.isFinite(row.total)).toBe(true)
      expect(row.total).toBe(row.ok + row.error)
    }
  })

  it('keeps the full provider string when it contains a colon', () => {
    const [row] = parseProviderCalls({ 'tts:custom:http://x/y:ok': 8 })

    expect(row.kind).toBe('tts')
    expect(row.provider).toBe('custom:http://x/y')
    expect(row.ok).toBe(8)
  })

  it('counts an unknown tag as an error rather than dropping it', () => {
    const [row] = parseProviderCalls({ 'llm:deepseek:weird': 6 })

    expect(row.error).toBe(6)
    expect(row.ok).toBe(0)
  })

  it('treats an untagged key as a row instead of throwing', () => {
    // `llm:deepseek` — the last segment occupies the tag slot, so `deepseek`
    // is read as the tag (not recognised as `ok`) and the provider is empty.
    // The point is that it degrades gracefully instead of throwing.
    const [row] = parseProviderCalls({ 'llm:deepseek': 5 })

    expect(row.kind).toBe('llm')
    expect(row.provider).toBe('')
    // No `ok`/`error` segment -> not a confirmed success.
    expect(row.ok).toBe(0)
    expect(row.error).toBe(5)
    expect(row.total).toBe(5)
    // The raw key stays visible so the odd row is still traceable.
    expect(providerCallKeys(row)).toBe('llm:deepseek')
  })

  it('handles an empty or missing provider_calls payload', () => {
    expect(parseProviderCalls({})).toEqual([])
    expect(parseProviderCalls(null)).toEqual([])
    expect(parseProviderCalls(undefined)).toEqual([])
  })

  it('never renders a non-numeric count as NaN', () => {
    const rows = parseProviderCalls({
      'llm:num:ok': 4,
      'llm:nan:ok': Number.NaN,
      'llm:inf:ok': Number.POSITIVE_INFINITY,
    })

    for (const row of rows) {
      expect(Number.isNaN(row.total)).toBe(false)
      expect(Number.isFinite(row.total)).toBe(true)
    }
  })

  it('labels the llm and tts kinds in Chinese', () => {
    expect(providerKindLabel('llm')).toBe('大模型')
    expect(providerKindLabel('tts')).toBe('语音合成')
    // An unknown kind must stay visible rather than become an empty cell.
    expect(providerKindLabel('image')).toBe('image')
  })

  it('exposes every merged raw key for a merged row', () => {
    const [row] = parseProviderCalls({ 'llm:deepseek:ok': 1, 'llm:deepseek:error': 2 })
    const keys = providerCallKeys(row)

    expect(keys).toContain('llm:deepseek:ok')
    expect(keys).toContain('llm:deepseek:error')
  })
})

describe('success rate', () => {
  it('computes the percentage from succeeded and total', () => {
    expect(successRate(92, 100)).toBe(92)
    expect(successRate(1, 3)).toBeCloseTo(33.333, 3)
    expect(formatSuccessRate(92, 100)).toBe('92%')
  })

  it('returns null (not NaN or Infinity) when the total is 0', () => {
    const rate = successRate(0, 0)

    // The contract is a real `null`, so the caller's `=== null` check and the
    // `—` placeholder both work. `Number(null)` is 0, so assert the raw value
    // rather than a numeric view of it.
    expect(rate).toBeNull()
    expect(rate).not.toBe(Number.NaN)
    expect(rate).not.toBe(Number.POSITIVE_INFINITY)
    expect(rate).not.toBe(0)
    expect(typeof rate).not.toBe('number')
    expect(rate === null ? EMPTY_VALUE : rate).toBe(EMPTY_VALUE)
    expect(formatPercent(rate)).toBe(EMPTY_VALUE)
  })

  it('never produces NaN or Infinity for any division-by-zero input', () => {
    // Whatever the caller renders, the string must contain no NaN/Infinity.
    for (const total of [0, null, undefined, Number.NaN]) {
      const rendered = formatSuccessRate(7, total as number)
      expect(rendered).toBe(EMPTY_VALUE)
      expect(rendered).not.toContain('NaN')
      expect(rendered).not.toContain('Infinity')
    }
  })

  it('returns null for a missing or non-finite total', () => {
    expect(successRate(5, null)).toBeNull()
    expect(successRate(5, undefined)).toBeNull()
    expect(successRate(5, Number.NaN)).toBeNull()
    expect(successRate(5, Number.POSITIVE_INFINITY)).toBeNull()
  })

  it('renders a placeholder for any non-computable rate', () => {
    expect(formatPercent(null)).toBe(EMPTY_VALUE)
    expect(formatPercent(undefined)).toBe(EMPTY_VALUE)
    expect(formatPercent(Number.NaN)).toBe(EMPTY_VALUE)
    expect(formatPercent(Number.POSITIVE_INFINITY)).toBe(EMPTY_VALUE)
  })

  it('reports 100% when every generation succeeded and 0% when none did', () => {
    expect(formatSuccessRate(10, 10)).toBe('100%')
    expect(formatSuccessRate(0, 10)).toBe('0%')
  })
})

describe('uptime formatting', () => {
  it('formats seconds-only durations', () => {
    expect(formatUptime(45)).toBe('45秒')
    expect(formatUptime(0)).toBe('0秒')
    expect(formatUptime(59)).toBe('59秒')
  })

  it('formats hour/minute durations', () => {
    expect(formatUptime(3900)).toBe('1小时5分')
    expect(formatUptime(3600)).toBe('1小时')
    expect(formatUptime(90)).toBe('1分30秒')
    expect(formatUptime(120)).toBe('2分')
  })

  it('formats day/hour durations', () => {
    expect(formatUptime(90000)).toBe('1天1小时')
    expect(formatUptime(86400)).toBe('1天')
    expect(formatUptime(1_000_000)).toBe('11天13小时')
  })

  it('renders a placeholder for missing or invalid values', () => {
    expect(formatUptime(null)).toBe(EMPTY_VALUE)
    expect(formatUptime(undefined)).toBe(EMPTY_VALUE)
    expect(formatUptime(Number.NaN)).toBe(EMPTY_VALUE)
    expect(formatUptime(-1)).toBe(EMPTY_VALUE)
  })

  it('is driven by the backend contract example', () => {
    // The endpoint returns a float (metrics.py rounds to 3 decimals).
    expect(formatUptime(3900.482)).toBe('1小时5分')
  })
})
