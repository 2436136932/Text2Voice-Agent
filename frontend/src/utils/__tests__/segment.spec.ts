import { describe, expect, it } from 'vitest'
import { describeSegments, segmentText } from '@/utils/segment'

const LIMIT = 100

/** Every segment must respect the backend's hard limit. */
function expectWithinLimit(segments: string[], limit = LIMIT): void {
  for (const segment of segments) {
    expect(segment.length).toBeLessThanOrEqual(limit)
    expect(segment.length).toBeGreaterThan(0)
  }
}

describe('segmentText', () => {
  it('returns a single segment for short text', () => {
    const result = segmentText('你好，世界。', { limit: LIMIT })
    expect(result.single).toBe(true)
    expect(result.segments).toEqual(['你好，世界。'])
  })

  it('returns nothing for blank input', () => {
    expect(segmentText('   \n  ', { limit: LIMIT }).segments).toEqual([])
  })

  it('trims surrounding whitespace', () => {
    expect(segmentText('  你好  ', { limit: LIMIT }).segments).toEqual(['你好'])
  })

  it('splits at sentence boundaries, keeping punctuation', () => {
    const sentence = '这是一句话。' // 6 chars
    const text = sentence.repeat(30) // 180 chars
    const result = segmentText(text, { limit: LIMIT })

    expect(result.single).toBe(false)
    expect(result.segments.length).toBeGreaterThan(1)
    expectWithinLimit(result.segments)
    // Punctuation stays with its sentence.
    for (const segment of result.segments) {
      expect(segment.endsWith('。')).toBe(true)
    }
  })

  it('never exceeds the limit for a single unbroken run', () => {
    // No punctuation at all: forces the hard-cut path.
    const text = 'a'.repeat(LIMIT * 3 + 17)
    const result = segmentText(text, { limit: LIMIT })

    expectWithinLimit(result.segments)
    expect(result.segments.join('')).toHaveLength(text.length)
  })

  it('loses no characters across the split', () => {
    const text = '第一段内容。第二段内容！第三段内容？'.repeat(12)
    const result = segmentText(text, { limit: LIMIT })
    // Reassembling must account for every non-whitespace character.
    const stripped = (value: string) => value.replace(/\s/g, '')
    expect(stripped(result.segments.join(''))).toBe(stripped(text))
  })

  it('prefers paragraph breaks for prose', () => {
    const paragraph = '这是一个段落，用来测试分段逻辑。'.repeat(4)
    const text = `${paragraph}\n\n${paragraph}`
    const result = segmentText(text, { limit: LIMIT })
    expectWithinLimit(result.segments)
    expect(result.segments.length).toBeGreaterThan(1)
  })

  it('packs small pieces together instead of emitting one segment each', () => {
    // 40 sentences of 6 chars: naive per-sentence splitting would give 40
    // segments; merging should give far fewer.
    const text = '这是句子。'.repeat(40)
    const result = segmentText(text, { limit: LIMIT })
    expectWithinLimit(result.segments)
    expect(result.segments.length).toBeLessThan(10)
  })

  it('handles CJK sentence enders and closers', () => {
    const text = `他说：“好的！”${'然后继续说明。'.repeat(20)}`
    const result = segmentText(text, { limit: LIMIT })
    expectWithinLimit(result.segments)
  })

  it('handles latin punctuation', () => {
    const text = 'Hello world. This is a test! Is it working? '.repeat(12)
    const result = segmentText(text, { limit: LIMIT })
    expectWithinLimit(result.segments)
  })

  it('respects a tiny limit', () => {
    const result = segmentText('abcdefghij', { limit: 3 })
    expectWithinLimit(result.segments, 3)
    expect(result.segments.join('')).toBe('abcdefghij')
  })

  it('works with natural splitting disabled', () => {
    const text = 'abc。'.repeat(50)
    const result = segmentText(text, { limit: 10, preferNatural: false })
    expectWithinLimit(result.segments, 10)
    expect(result.segments.join('')).toBe(text)
  })
})

describe('describeSegments', () => {
  it('describes a single-segment result', () => {
    expect(describeSegments({ segments: ['短文本'], single: true })).toContain('单次请求')
  })

  it('counts segments for a split result', () => {
    const result = segmentText('内容。'.repeat(100), { limit: 20 })
    const description = describeSegments(result)
    expect(description).toContain(`${result.segments.length}`)
    expect(description).toContain('段')
  })
})
