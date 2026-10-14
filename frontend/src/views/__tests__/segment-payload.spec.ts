import { describe, expect, it } from 'vitest'
import type { GenerateRequest } from '@/api/types'
import { segmentText } from '@/utils/segment'

/**
 * Verifies the contract the segmented-generation path relies on: when a
 * segment replaces `text`, any `reuse_reply_text` (clone and 直接配音 modes)
 * must be replaced too. Sending the full document as the reply for every
 * segment would read the text N times.
 *
 * Mirrors segmentPayload() in GenerateView.vue; kept here so a regression in
 * that logic fails a fast unit test instead of a slow browser check.
 */
function segmentPayload(payload: GenerateRequest, piece: string): GenerateRequest {
  const options = payload.options.reuse_reply_text
    ? { ...payload.options, reuse_reply_text: piece }
    : payload.options
  return { ...payload, text: piece, options }
}

function baseRequest(text: string, reuse: boolean): GenerateRequest {
  return {
    text,
    llm: {
      provider: 'mock',
      api_key: '',
      model: 'mock-echo-v1',
      base_url: null,
      temperature: 0.7,
      max_tokens: 512,
      system_prompt: '',
    },
    tts: {
      provider: 'mock',
      api_key: '',
      voice: 'mock-neutral',
      model: '',
      base_url: null,
      format: 'wav',
      speed: 1,
      extra: {},
    },
    options: {
      auto_save: false,
      include_audio_base64: false,
      fallback_enabled: true,
      reuse_reply_text: reuse ? text : null,
    },
  }
}

describe('segment payload rewriting', () => {
  it('replaces text with the segment', () => {
    const payload = baseRequest('一二三四五六', false)
    const out = segmentPayload(payload, '一二')
    expect(out.text).toBe('一二')
  })

  it('replaces reuse_reply_text together with text', () => {
    const payload = baseRequest('一二三四五六', true)
    const out = segmentPayload(payload, '一二')
    expect(out.text).toBe('一二')
    expect(out.options.reuse_reply_text).toBe('一二')
  })

  it('leaves reuse_reply_text alone when the mode does not use it', () => {
    const payload = baseRequest('一二三四五六', false)
    const out = segmentPayload(payload, '一二')
    expect(out.options.reuse_reply_text).toBeNull()
  })

  it('does not mutate the original payload', () => {
    const payload = baseRequest('一二三四五六', true)
    segmentPayload(payload, '一二')
    expect(payload.text).toBe('一二三四五六')
    expect(payload.options.reuse_reply_text).toBe('一二三四五六')
  })

  it('keeps every segment and the whole document in sync', () => {
    const document = '这是第一句话。这是第二句话。这是第三句话。'.repeat(20)
    const { segments } = segmentText(document, { limit: 50 })
    const payload = baseRequest(document, true)

    const built = segments.map((piece) => segmentPayload(payload, piece))
    for (const [index, request] of built.entries()) {
      expect(request.text).toBe(segments[index])
      expect(request.options.reuse_reply_text).toBe(segments[index])
    }
    // Concatenating the spoken segments reproduces the document.
    const spoken = built.map((item) => item.options.reuse_reply_text).join('')
    expect(spoken.replace(/\s/g, '')).toBe(document.replace(/\s/g, ''))
  })
})
