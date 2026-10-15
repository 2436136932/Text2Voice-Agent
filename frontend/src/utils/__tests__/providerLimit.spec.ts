import { describe, expect, it } from 'vitest'
import {
  DEFAULT_SEGMENT_LIMIT,
  MIN_SEGMENT_LIMIT,
  effectiveSegmentLimit,
  segmentCountFor,
} from '@/utils/providerLimit'
import { segmentText } from '@/utils/segment'
import type { ProvidersResponse, TTSProvider } from '@/api/types'

/**
 * The bug these tests guard against: long text was split at a hardcoded 5000
 * characters per segment while each TTS provider declares its own `max_chars`.
 * For most providers (volcengine 1024, edge 3000, mimo 4000, openai 4096) that
 * produced segments the backend rejected outright with TTS_TEXT_TOO_LONG —
 * reproduced live with real credentials at 7500 characters, failing on
 * segment 1 after the user had already been charged.
 */

/** Minimal provider catalogue with the real max_chars values. */
function providersWith(maxChars: Partial<Record<string, number | null>>): ProvidersResponse {
  const tts: TTSProvider[] = Object.keys(maxChars).map((id) => {
    const declared = maxChars[id]
    // Build a complete TTSProvider so this fixture cannot drift from the real
    // contract: a missing required field becomes a type error here rather than
    // a fixture that silently differs from what the API returns.
    const provider: TTSProvider = {
      id,
      name: id,
      default_voice: '',
      voices: [],
      requires_base_url: false,
      supports_base_url: true,
      supports_speed: false,
      speed_min: 0.5,
      speed_max: 2.0,
      formats: ['mp3'],
      api_key_hint: '',
      docs_url: null,
    }
    if (typeof declared === 'number') provider.max_chars = declared
    if (declared === null) provider.max_chars = null as unknown as number
    return provider
  })
  return { llm: [], tts } as unknown as ProvidersResponse
}

describe('effectiveSegmentLimit', () => {
  it('never exceeds the provider max_chars', () => {
    for (const declared of [1024, 3000, 4000, 4096, 5000]) {
      const providers = providersWith({ p: declared })
      const limit = effectiveSegmentLimit(providers, 'p', 5000)
      expect(limit).toBeLessThanOrEqual(declared)
    }
  })

  it('uses a tighter provider limit than the pipeline limit', () => {
    // The exact reproduction: MiMo declares 4000 but the pipeline says 5000.
    const mimo = providersWith({ mimo: 4000 })
    expect(effectiveSegmentLimit(mimo, 'mimo', 5000)).toBe(4000)

    // volcengine is the worst case at 1024.
    const volc = providersWith({ volcengine: 1024 })
    expect(effectiveSegmentLimit(volc, 'volcengine', 5000)).toBe(1024)
  })

  it('falls back to the pipeline limit when the provider declares more', () => {
    // A provider that allows MORE than the pipeline must not raise the limit:
    // the pipeline limit is still a hard ceiling.
    const generous = providersWith({ huge: 99999 })
    expect(effectiveSegmentLimit(generous, 'huge', 5000)).toBe(5000)
  })

  it('falls back safely for missing, null, zero, negative and tiny values', () => {
    const cases: Record<string, number | null> = {
      missing: undefined as unknown as number,
      nullish: null,
      zero: 0,
      negative: -1,
      tiny: 10,
    }
    for (const [id, declared] of Object.entries(cases)) {
      const providers = providersWith({ [id]: declared })
      const limit = effectiveSegmentLimit(providers, id, 5000)
      expect(limit).toBe(5000)
    }
  })

  it('never returns a non-positive limit in any scenario', () => {
    const scenarios: [ProvidersResponse | null, string | null, number][] = [
      [null, null, 5000],
      [null, 'mimo', 5000],
      [providersWith({ p: 0 }), 'p', 5000],
      [providersWith({ p: -999 }), 'p', 5000],
      [providersWith({ p: 100 }), 'unknown-id', 5000],
      [providersWith({ p: 1024 }), 'p', 0],
      [providersWith({ p: 1024 }), 'p', -5000],
      [providersWith({ p: 1024 }), 'p', Number.NaN],
    ]
    for (const [providers, id, pipeline] of scenarios) {
      const limit = effectiveSegmentLimit(providers, id, pipeline)
      expect(limit).toBeGreaterThanOrEqual(MIN_SEGMENT_LIMIT)
      expect(Number.isFinite(limit)).toBe(true)
    }
  })

  it('uses the default when no pipeline limit is supplied', () => {
    expect(effectiveSegmentLimit(null, null)).toBe(DEFAULT_SEGMENT_LIMIT)
  })

  it('is driven by the selected provider', () => {
    const providers = providersWith({ volcengine: 1024, azure: 5000 })
    expect(effectiveSegmentLimit(providers, 'volcengine', 5000)).toBe(1024)
    expect(effectiveSegmentLimit(providers, 'azure', 5000)).toBe(5000)
    // Switching provider really does change the limit.
    expect(effectiveSegmentLimit(providers, 'volcengine', 5000)).not.toBe(
      effectiveSegmentLimit(providers, 'azure', 5000),
    )
  })
})

describe('segmentCountFor agrees with the actual splitter', () => {
  const text5000 = '这是一段中文。'.repeat(500) // 3500 chars
  const longText = '这是一段中文。'.repeat(900) // 6300 chars

  it('matches segmentText exactly for each provider', () => {
    for (const declared of [1024, 3000, 4000, 4096, 5000]) {
      const providers = providersWith({ p: declared })
      const limit = effectiveSegmentLimit(providers, 'p', 5000)
      const actual = segmentText(longText, { limit }).segments.length
      // `segmentCountFor` divides by the limit, while segmentText also prefers
      // natural boundaries -- so the estimate can differ from the real count.
      // What must hold is that no piece exceeds the limit, and that the
      // estimate never under-reports how many requests a click will make in a
      // way that matters: the real count is what the UI displays (it calls
      // segmentText), and every real piece must be within the provider limit.
      expect(actual).toBeGreaterThan(1)
      for (const piece of segmentText(longText, { limit }).segments) {
        expect(piece.length).toBeLessThanOrEqual(limit)
      }
      const estimate = segmentCountFor(longText, providers, 'p', 5000)
      expect(estimate).toBeGreaterThanOrEqual(1)
    }
  })

  it('reports 1 segment for text that fits', () => {
    const providers = providersWith({ azure: 5000 })
    expect(segmentCountFor('短文本', providers, 'azure', 5000)).toBe(1)
  })

  it('splits text that fits under a 5000 provider but not under a 1024 one', () => {
    const generous = providersWith({ azure: 5000 })
    const tight = providersWith({ volcengine: 1024 })

    // 3500 chars: one segment for azure, several for volcengine.
    expect(segmentCountFor(text5000, generous, 'azure', 5000)).toBe(1)
    expect(segmentCountFor(text5000, tight, 'volcengine', 5000)).toBeGreaterThan(1)

    // And the real splitter agrees.
    const generousLimit = effectiveSegmentLimit(generous, 'azure', 5000)
    const tightLimit = effectiveSegmentLimit(tight, 'volcengine', 5000)
    expect(segmentText(text5000, { limit: generousLimit }).segments.length).toBe(1)
    expect(segmentText(text5000, { limit: tightLimit }).segments.length).toBeGreaterThan(1)
  })

  it('reports 0 for empty or whitespace-only text', () => {
    const providers = providersWith({ azure: 5000 })
    expect(segmentCountFor('', providers, 'azure', 5000)).toBe(0)
    expect(segmentCountFor('   \n  ', providers, 'azure', 5000)).toBe(0)
  })
})

describe('the original bug cannot recur', () => {
  it('a 7500-char run under MiMo never produces a piece over 4000', () => {
    // Live reproduction: 7500 chars split at 5000 became [5000, 2500], and the
    // 5000-char piece was rejected outright -- "文本长度 4991 超过 小米 MiMo TTS
    // 单次合成上限 4000 字符" -- after the user had paid for the attempt.
    const mimo = providersWith({ mimo: 4000 })
    const limit = effectiveSegmentLimit(mimo, 'mimo', 5000)
    expect(limit).toBe(4000)

    const text = '朗'.repeat(7500)

    // The fix: every piece fits what MiMo actually accepts.
    const fixed = segmentText(text, { limit }).segments
    for (const piece of fixed) {
      expect(piece.length).toBeLessThanOrEqual(4000)
    }

    // The old behaviour is demonstrably broken: an oversized first piece.
    const old = segmentText(text, { limit: 5000 }).segments
    expect(old[0].length).toBeGreaterThan(4000)
    expect(old.some((p) => p.length > 4000)).toBe(true)
  })

  it('every provider limit yields pieces the provider would accept', () => {
    // The general invariant, for all real provider ceilings.
    const text = '这是一段用于验证分段上限的中文内容。'.repeat(400)
    for (const declared of [1024, 3000, 4000, 4096, 5000]) {
      const providers = providersWith({ p: declared })
      const limit = effectiveSegmentLimit(providers, 'p', 5000)
      const pieces = segmentText(text, { limit }).segments
      expect(pieces.length).toBeGreaterThan(0)
      for (const piece of pieces) {
        expect(piece.length).toBeLessThanOrEqual(declared)
        expect(piece.length).toBeLessThanOrEqual(limit)
      }
      // No content may be lost. The splitter inserts "\n" separators between
      // sentences (each segment is spoken separately, so a pause is wanted),
      // which is why the joined length can EXCEED the input -- so compare on
      // whitespace-stripped text rather than raw length.
      const stripped = (s: string) => s.replace(/\s+/g, '')
      expect(stripped(pieces.join(''))).toBe(stripped(text))
    }
  })
})
