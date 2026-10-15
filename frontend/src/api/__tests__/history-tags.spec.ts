/**
 * Pure helpers behind the history favourites/tags UI.
 *
 * These mirror the backend rules in `app/schemas/generation.py`
 * (`normalize_tags`, MAX_TAG_COUNT, MAX_TAG_LENGTH) so the tag editor shows the
 * user exactly what the server will store.
 */
import { describe, expect, it } from 'vitest'
import { MAX_TAG_COUNT, MAX_TAG_LENGTH, normalizeTags, sameTags } from '@/api/types'

describe('normalizeTags', () => {
  it('trims surrounding whitespace', () => {
    expect(normalizeTags(['  工作 ', '日报  '])).toEqual(['工作', '日报'])
  })

  it('drops empty and whitespace-only entries', () => {
    expect(normalizeTags(['工作', '', '   ', '日报'])).toEqual(['工作', '日报'])
  })

  it('dedupes while keeping the first-seen order', () => {
    expect(normalizeTags(['b', 'a', 'b', ' a '])).toEqual(['b', 'a'])
  })

  it('drops tags longer than the backend limit', () => {
    expect(normalizeTags(['ok', 'x'.repeat(MAX_TAG_LENGTH + 1)])).toEqual(['ok'])
  })

  it('keeps a tag exactly at the limit', () => {
    const tag = 'x'.repeat(MAX_TAG_LENGTH)
    expect(normalizeTags([tag])).toEqual([tag])
  })

  it('caps the tag count', () => {
    const many = Array.from({ length: MAX_TAG_COUNT + 5 }, (_, index) => `t${index}`)
    const result = normalizeTags(many)
    expect(result).toHaveLength(MAX_TAG_COUNT)
    expect(result[0]).toBe('t0')
  })

  it('returns an empty array for no tags', () => {
    expect(normalizeTags([])).toEqual([])
  })

  it('does not mutate its input', () => {
    const input = ['  a  ', 'a', '']
    normalizeTags(input)
    expect(input).toEqual(['  a  ', 'a', ''])
  })
})

describe('sameTags', () => {
  it('treats identical lists as equal', () => {
    expect(sameTags(['a', 'b'], ['a', 'b'])).toBe(true)
  })

  it('treats equal-length different lists as different', () => {
    expect(sameTags(['a', 'b'], ['a', 'c'])).toBe(false)
  })

  it('treats a different order as different', () => {
    expect(sameTags(['a', 'b'], ['b', 'a'])).toBe(false)
  })

  it('treats different lengths as different', () => {
    expect(sameTags(['a'], ['a', 'b'])).toBe(false)
  })

  it('treats two empty lists as equal', () => {
    expect(sameTags([], [])).toBe(true)
  })
})
