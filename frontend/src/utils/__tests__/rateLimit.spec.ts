import { describe, expect, it, vi } from 'vitest'
import {
  FALLBACK_WAIT_MS,
  MAX_WAIT_MS,
  MIN_WAIT_MS,
  WAIT_MARGIN_MS,
  isRateLimited,
  rateLimitWaitMs,
  withRateLimitRetry,
} from '@/utils/rateLimit'
import { ApiError } from '@/api/http'

/**
 * Why these tests matter: the platform's own long-text feature submits more
 * requests than its own rate limit allows (10 per 60s), with no pacing. That
 * was reproduced live -- a 12-item batch failed one item with
 * "请求过于频繁". The fix must wait exactly as long as the server asks, retry,
 * and never weaken the limit itself.
 */

/** A 429 exactly as the backend produces it. */
function rateLimited(retryAfter: number, extra: Record<string, unknown> = {}): ApiError {
  return new ApiError({
    code: 'RATE_LIMITED',
    message: `请求过于频繁，请在 ${retryAfter.toFixed(0)} 秒后重试。`,
    detail: { bucket: 'generate', limit: 10, window_seconds: 60, retry_after: retryAfter, ...extra },
    status: 429,
  })
}

/** Records sleeps instead of performing them. */
function fakeSleep() {
  const waits: number[] = []
  return {
    waits,
    sleep: async (ms: number) => {
      waits.push(ms)
    },
  }
}

describe('isRateLimited', () => {
  it('recognises the backend code', () => {
    expect(isRateLimited(rateLimited(6))).toBe(true)
  })

  it('recognises a bare 429 even without the documented code', () => {
    const bare = new ApiError({ code: 'HTTP_429', message: 'too many', status: 429 })
    expect(isRateLimited(bare)).toBe(true)
  })

  it('does not treat other failures as rate limiting', () => {
    for (const code of ['TTS_AUTH_FAILED', 'LLM_TIMEOUT', 'TASK_QUEUE_FULL', 'NETWORK_ERROR']) {
      expect(isRateLimited(new ApiError({ code, message: 'x' }))).toBe(false)
    }
    expect(isRateLimited(new Error('plain'))).toBe(false)
    expect(isRateLimited(null)).toBe(false)
    expect(isRateLimited(undefined)).toBe(false)
  })
})

describe('rateLimitWaitMs', () => {
  it("uses the server's retry_after, plus a small margin", () => {
    expect(rateLimitWaitMs(rateLimited(6))).toBe(6000 + WAIT_MARGIN_MS)
    expect(rateLimitWaitMs(rateLimited(1))).toBe(1000 + WAIT_MARGIN_MS)
  })

  it('derives the interval from window/limit when retry_after is absent', () => {
    const error = new ApiError({
      code: 'RATE_LIMITED',
      message: 'x',
      detail: { limit: 10, window_seconds: 60 },
      status: 429,
    })
    // 60/10 = 6s
    expect(rateLimitWaitMs(error)).toBe(6000 + WAIT_MARGIN_MS)
  })

  it('falls back to a constant when the detail carries nothing usable', () => {
    const noDetail = new ApiError({ code: 'RATE_LIMITED', message: 'x', status: 429 })
    expect(rateLimitWaitMs(noDetail)).toBe(FALLBACK_WAIT_MS + WAIT_MARGIN_MS)
  })

  it('never returns a busy-loop or a stall', () => {
    for (const retryAfter of [0, -5, Number.NaN, 1e9, 0.0001]) {
      const wait = rateLimitWaitMs(rateLimited(retryAfter))
      expect(wait).toBeGreaterThanOrEqual(MIN_WAIT_MS)
      expect(wait).toBeLessThanOrEqual(MAX_WAIT_MS + WAIT_MARGIN_MS)
      expect(Number.isFinite(wait)).toBe(true)
    }
  })

  it('ignores non-numeric detail values', () => {
    const error = new ApiError({
      code: 'RATE_LIMITED',
      message: 'x',
      detail: { retry_after: 'soon', limit: 'ten', window_seconds: null },
      status: 429,
    })
    expect(rateLimitWaitMs(error)).toBe(FALLBACK_WAIT_MS + WAIT_MARGIN_MS)
  })
})

describe('withRateLimitRetry', () => {
  it('returns the result untouched when nothing is rate limited', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi.fn().mockResolvedValue('ok')

    await expect(withRateLimitRetry(fn, { sleep })).resolves.toBe('ok')
    expect(fn).toHaveBeenCalledTimes(1)
    expect(waits).toEqual([])
  })

  it('waits and retries after a 429, then succeeds', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi
      .fn()
      .mockRejectedValueOnce(rateLimited(6))
      .mockResolvedValueOnce('ok')

    await expect(withRateLimitRetry(fn, { sleep })).resolves.toBe('ok')
    expect(fn).toHaveBeenCalledTimes(2)
    expect(waits).toEqual([6000 + WAIT_MARGIN_MS])
  })

  it('retries repeatedly while the budget stays spent', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi
      .fn()
      .mockRejectedValueOnce(rateLimited(6))
      .mockRejectedValueOnce(rateLimited(6))
      .mockResolvedValueOnce('ok')

    await expect(withRateLimitRetry(fn, { sleep })).resolves.toBe('ok')
    expect(fn).toHaveBeenCalledTimes(3)
    expect(waits).toHaveLength(2)
  })

  it('does NOT retry any other failure', async () => {
    const { sleep, waits } = fakeSleep()
    const boom = new ApiError({ code: 'TTS_AUTH_FAILED', message: '密钥无效' })
    const fn = vi.fn().mockRejectedValue(boom)

    await expect(withRateLimitRetry(fn, { sleep })).rejects.toBe(boom)
    // One attempt only: waiting is appropriate for exactly one condition.
    expect(fn).toHaveBeenCalledTimes(1)
    expect(waits).toEqual([])
  })

  it('gives up after maxAttempts and rethrows the 429', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi.fn().mockRejectedValue(rateLimited(1))

    await expect(
      withRateLimitRetry(fn, { sleep, maxAttempts: 3 }),
    ).rejects.toMatchObject({ code: 'RATE_LIMITED' })

    // 3 attempts, so 2 waits: it does not wait after the final failure.
    expect(fn).toHaveBeenCalledTimes(3)
    expect(waits).toHaveLength(2)
  })

  it('stops without waiting when shouldStop is already true', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi.fn().mockRejectedValue(rateLimited(6))

    await expect(
      withRateLimitRetry(fn, { sleep, shouldStop: () => true }),
    ).rejects.toMatchObject({ code: 'RATE_LIMITED' })

    // Never sleep then submit: that would spend money the user just cancelled.
    expect(waits).toEqual([])
  })

  it('aborts if the user stops while waiting', async () => {
    let stop = false
    const waits: number[] = []
    const fn = vi.fn().mockRejectedValue(rateLimited(6))

    await expect(
      withRateLimitRetry(fn, {
        sleep: async (ms: number) => {
          waits.push(ms)
          // The user hits stop during the wait.
          stop = true
        },
        shouldStop: () => stop,
      }),
    ).rejects.toMatchObject({ code: 'RATE_LIMITED' })

    // Waited once, then refused to submit again.
    expect(waits).toHaveLength(1)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('does not wait beyond maxTotalWaitMs', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi.fn().mockRejectedValue(rateLimited(10))

    await expect(
      withRateLimitRetry(fn, { sleep, maxAttempts: 10, maxTotalWaitMs: 12_000 }),
    ).rejects.toMatchObject({ code: 'RATE_LIMITED' })

    // One 10s wait fits in a 12s budget; a second would not.
    expect(waits).toHaveLength(1)
    expect(waits.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(12_000)
  })

  it('reports each wait so the UI can explain the pause', async () => {
    const { sleep } = fakeSleep()
    const onWait = vi.fn()
    const fn = vi.fn().mockRejectedValueOnce(rateLimited(6)).mockResolvedValueOnce('ok')

    await withRateLimitRetry(fn, { sleep, onWait })
    expect(onWait).toHaveBeenCalledWith(6000 + WAIT_MARGIN_MS, 1)
  })

  it('treats maxAttempts below 1 as a single attempt', async () => {
    const { sleep, waits } = fakeSleep()
    const fn = vi.fn().mockRejectedValue(rateLimited(1))

    await expect(
      withRateLimitRetry(fn, { sleep, maxAttempts: 0 }),
    ).rejects.toMatchObject({ code: 'RATE_LIMITED' })
    expect(fn).toHaveBeenCalledTimes(1)
    expect(waits).toEqual([])
  })
})
