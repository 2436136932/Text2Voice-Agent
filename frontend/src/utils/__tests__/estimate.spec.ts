import { describe, expect, it } from 'vitest'
import {
  CHAT_OVERHEAD_TOKENS,
  DEFAULT_MAX_TOKENS,
  countCharacters,
  describeTokenEstimate,
  estimateTokens,
  estimateUsage,
  formatNumber,
  usesLlm,
  type EstimateInput,
} from '@/utils/estimate'
import { segmentText } from '@/utils/segment'

/** Convenience builder so each test states only what it cares about. */
function base(overrides: Partial<EstimateInput> = {}): EstimateInput {
  return { text: '', limit: 5000, mode: 'tts', ...overrides }
}

describe('countCharacters', () => {
  it('reports zeros for empty and whitespace-only text without crashing', () => {
    for (const text of ['', '   ', '\n\t  \n', '\u3000\u3000']) {
      const c = countCharacters(text)
      expect(c.total).toBeGreaterThanOrEqual(0)
      expect(c.nonWhitespace).toBe(0)
      expect(c.cjk).toBe(0)
      expect(c.latin).toBe(0)
      expect(c.other).toBe(0)
    }
    expect(countCharacters('').total).toBe(0)
  })

  it('separates CJK from Latin from other', () => {
    const c = countCharacters('中文abc!')
    expect(c.cjk).toBe(2)
    expect(c.latin).toBe(4) // a, b, c, !
    expect(c.other).toBe(0)
    expect(c.total).toBe(6)
    expect(c.nonWhitespace).toBe(6)
  })

  it('counts whitespace in total but not in the class buckets', () => {
    const c = countCharacters('中 文')
    expect(c.total).toBe(3)
    expect(c.nonWhitespace).toBe(2)
    expect(c.cjk).toBe(2)
  })

  it('counts an astral-plane emoji once, not twice', () => {
    // '😀' is a surrogate pair in UTF-16 but a single code point.
    const c = countCharacters('😀')
    expect(c.total).toBe(1)
    expect(c.other).toBe(1)
  })
})

describe('estimateTokens', () => {
  it('is zero for empty text', () => {
    expect(estimateTokens('')).toBe(0)
    expect(estimateTokens('   ')).toBe(0)
  })

  it('differentiates CJK from Latin, because they differ by ~4x per char', () => {
    // Same character count, very different token cost.
    const cjk = estimateTokens('中文字符测试内容')
    const latin = estimateTokens('abcdefgh')
    expect(cjk).toBeGreaterThan(latin)
    // 8 CJK chars ~ 8 tokens; 8 Latin chars ~ 2 tokens.
    expect(cjk).toBe(8)
    expect(latin).toBe(2)
  })

  it('rounds up rather than down, so it never under-promises', () => {
    // 5 Latin chars = 1.25 tokens -> 2.
    expect(estimateTokens('abcde')).toBe(2)
  })
})

describe('usesLlm', () => {
  it('only the tts mode calls the model', () => {
    expect(usesLlm('tts')).toBe(true)
    expect(usesLlm('clone')).toBe(false)
    expect(usesLlm('text2audio')).toBe(false)
  })

  it('reusing an existing reply skips the model even in tts mode', () => {
    expect(usesLlm('tts', 'some reply')).toBe(false)
    // Whitespace is not a reply.
    expect(usesLlm('tts', '   ')).toBe(true)
    expect(usesLlm('tts', '')).toBe(true)
    expect(usesLlm('tts', null)).toBe(true)
  })
})

describe('estimateUsage: LLM skipped is reported as not-applicable, never 0', () => {
  it('leaves the LLM figures null in clone mode', () => {
    const e = estimateUsage(base({ text: '中文内容', mode: 'clone' }))
    expect(e.usesLlm).toBe(false)
    expect(e.llmInputTokens).toBeNull()
    expect(e.llmOutputTokensCeiling).toBeNull()
    expect(e.llmOutputTokensEstimate).toBeNull()
    // Not zero — zero would read as "free", which is a different claim.
    expect(e.llmInputTokens).not.toBe(0)
  })

  it('leaves the LLM figures null in text2audio mode', () => {
    const e = estimateUsage(base({ text: '中文内容', mode: 'text2audio' }))
    expect(e.usesLlm).toBe(false)
    expect(e.llmInputTokens).toBeNull()
  })

  it('leaves the LLM figures null when a reply is reused in tts mode', () => {
    const e = estimateUsage(
      base({ text: '中文内容', mode: 'tts', reuseReplyText: '已经有回复了' }),
    )
    expect(e.usesLlm).toBe(false)
    expect(e.llmInputTokens).toBeNull()
  })

  it('says so in the summary line instead of printing a token count', () => {
    const e = estimateUsage(base({ text: '中文内容', mode: 'clone' }))
    const line = describeTokenEstimate(e)
    expect(line).toContain('跳过语言模型')
    expect(line).not.toContain('token 约')
  })

  it('reports a real estimate in tts mode', () => {
    const e = estimateUsage(base({ text: '中文内容', mode: 'tts' }))
    expect(e.usesLlm).toBe(true)
    expect(e.llmInputTokens).toBe(e.textTokens + CHAT_OVERHEAD_TOKENS)
    expect(e.llmInputTokens).toBeGreaterThan(0)
  })
})

describe('estimateUsage: the system prompt counts towards the input', () => {
  it('adds system-prompt tokens', () => {
    const without = estimateUsage(base({ text: '你好', mode: 'tts' }))
    const withPrompt = estimateUsage(
      base({ text: '你好', mode: 'tts', systemPrompt: '你是一个专业的播音员' }),
    )
    expect(withPrompt.llmInputTokens).toBeGreaterThan(without.llmInputTokens as number)
  })
})

describe('estimateUsage: output estimate respects the ceiling', () => {
  it('caps the estimate at max_tokens', () => {
    const longText = '中'.repeat(5000)
    const e = estimateUsage(base({ text: longText, mode: 'tts', maxTokens: 100 }))
    expect(e.llmOutputTokensCeiling).toBe(100)
    expect(e.llmOutputTokensEstimate).toBe(100)
  })

  it('falls back to the backend default for a missing or silly ceiling', () => {
    for (const bad of [undefined, null, 0, -5, Number.NaN]) {
      const e = estimateUsage(base({ text: '你好', mode: 'tts', maxTokens: bad }))
      expect(e.llmOutputTokensCeiling).toBe(DEFAULT_MAX_TOKENS)
    }
  })
})

describe('estimateUsage: segmentation agrees with the real splitter', () => {
  const longText = '这是一段用于验证分段与预览一致性的中文内容。'.repeat(300)

  it('reports exactly the pieces segmentText would send', () => {
    for (const limit of [1024, 3000, 4000, 5000]) {
      const e = estimateUsage(base({ text: longText, limit }))
      const actual = segmentText(longText, { limit }).segments
      expect(e.segments).toEqual(actual)
      expect(e.segmentCount).toBe(actual.length)
      expect(e.segmentLengths).toEqual(actual.map((p) => p.length))
    }
  })

  it('never produces a segment longer than the limit', () => {
    for (const limit of [1024, 3000, 4000, 5000]) {
      const e = estimateUsage(base({ text: longText, limit }))
      for (const len of e.segmentLengths) {
        expect(len).toBeLessThanOrEqual(limit)
      }
    }
  })

  it('uses the limit it is given, so the provider limit really drives it', () => {
    // This is the coordination point with the provider max_chars fix: a tighter
    // provider limit must yield more segments for the same text.
    const tight = estimateUsage(base({ text: longText, limit: 1024 }))
    const loose = estimateUsage(base({ text: longText, limit: 5000 }))
    expect(tight.segmentCount).toBeGreaterThan(loose.segmentCount)
  })

  it('loses no non-whitespace content when splitting', () => {
    // Segments are joined with "\n" separators (each is spoken separately, so a
    // pause between them is wanted), which is why the raw sum of lengths can
    // EXCEED the input. The invariant that matters is content conservation, so
    // compare with whitespace removed.
    const strip = (s: string) => s.replace(/\s+/gu, '')
    for (const limit of [1024, 4000, 5000]) {
      const e = estimateUsage(base({ text: longText, limit }))
      expect(strip(e.segments.join(''))).toBe(strip(longText))
    }
  })

  it('reports one segment and no segmentation for short text', () => {
    const e = estimateUsage(base({ text: '很短的文本' }))
    expect(e.segmentCount).toBe(1)
    expect(e.needsSegmentation).toBe(false)
  })

  it('survives a zero or nonsensical limit without looping or crashing', () => {
    for (const bad of [0, -100, Number.NaN, Number.POSITIVE_INFINITY]) {
      const e = estimateUsage(base({ text: '一些文本', limit: bad }))
      expect(e.segmentCount).toBeGreaterThan(0)
      expect(Number.isFinite(e.segmentCount)).toBe(true)
      for (const len of e.segmentLengths) {
        expect(len).toBeGreaterThanOrEqual(0)
      }
    }
  })
})

describe('estimateUsage: the total ceiling is flagged', () => {
  const CEILING = 1000

  it('flags text beyond the ceiling', () => {
    const e = estimateUsage(
      base({ text: '中'.repeat(CEILING + 1), totalCeiling: CEILING }),
    )
    expect(e.exceedsTotalCeiling).toBe(true)
  })

  it('does not flag text within the ceiling', () => {
    const e = estimateUsage(base({ text: '中'.repeat(10), totalCeiling: CEILING }))
    expect(e.exceedsTotalCeiling).toBe(false)
  })

  it('does not flag when no ceiling is given', () => {
    const e = estimateUsage(base({ text: '中'.repeat(50) }))
    expect(e.exceedsTotalCeiling).toBe(false)
  })
})

describe('non-ASCII edge cases never crash or go negative', () => {
  const cases: Record<string, string> = {
    emoji: '😀🎉🚀'.repeat(20),
    'emoji + text': '你好😀世界🎉',
    'combining marks': 'e\u0301'.repeat(50), // é as e + combining acute
    'zero-width joiner': '👨\u200d👩\u200d👧',
    'mixed scripts': '中文 English 日本語 한국어 12345 !@#$%',
    'full-width punctuation': '你好，世界！这是测试。',
    newlines: '第一行\n\n第二行\r\n第三行',
    'astral CJK ext': '\u{20000}\u{20001}',
  }

  for (const [name, text] of Object.entries(cases)) {
    it(`handles ${name}`, () => {
      const e = estimateUsage(base({ text, limit: 1024, mode: 'tts' }))
      expect(e.total).toBeGreaterThan(0)
      expect(e.nonWhitespace).toBeGreaterThanOrEqual(0)
      expect(e.textTokens).toBeGreaterThanOrEqual(0)
      expect(e.llmInputTokens as number).toBeGreaterThanOrEqual(0)
      expect(e.segmentCount).toBeGreaterThan(0)
      for (const len of e.segmentLengths) {
        expect(len).toBeGreaterThanOrEqual(0)
      }
      // And the summary renders without throwing.
      expect(() => describeTokenEstimate(e)).not.toThrow()
    })
  }

  it('counts a ZWJ emoji sequence by code point, not by UTF-16 unit', () => {
    // '👨‍👩‍👧' = 3 emoji + 2 ZWJ = 5 code points (8 UTF-16 units).
    expect(countCharacters('👨\u200d👩\u200d👧').total).toBe(5)
  })
})

describe('formatNumber', () => {
  it('groups thousands deterministically', () => {
    expect(formatNumber(0)).toBe('0')
    expect(formatNumber(999)).toBe('999')
    expect(formatNumber(1000)).toBe('1,000')
    expect(formatNumber(1234567)).toBe('1,234,567')
  })

  it('handles non-finite input without printing NaN', () => {
    expect(formatNumber(Number.NaN)).toBe('0')
    expect(formatNumber(Number.POSITIVE_INFINITY)).toBe('0')
  })
})

describe('describeTokenEstimate', () => {
  it('labels the figure as an estimate, never as exact', () => {
    const e = estimateUsage(base({ text: '中文内容', mode: 'tts' }))
    const line = describeTokenEstimate(e)
    expect(line).toContain('预估')
    expect(line).toContain('约')
  })
})
