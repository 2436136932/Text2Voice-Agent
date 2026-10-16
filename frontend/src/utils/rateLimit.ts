/**
 * Rate-limit-aware submission.
 *
 * Why this exists
 * ---------------
 * `POST /tasks` (and `POST /generate`) sit behind a "generate" budget of 10
 * requests per 60 seconds -- deliberately strict, because each call spends
 * money upstream. But the platform's own long-text feature splits a document
 * into many requests and submits them back to back: at 4,000 characters per
 * segment (MiMo), a 100,000-character document becomes ~25 submissions. Both
 * the segmented path and the batch-retry path had no pacing at all, so they
 * could exceed the platform's own limit and fail part-way through with
 * "请求过于频繁" -- reproduced live with a 12-item batch.
 *
 * The limit itself is good design and is not weakened here. Instead the client
 * respects it: when the server answers 429 it says exactly how long to wait
 * (`detail.retry_after`, computed from the token bucket's refill rate), so the
 * submission is retried after that wait instead of being reported as a failure.
 *
 * This is reactive rather than a fixed client-side delay on purpose. A blanket
 * "sleep 6s between requests" would slow every run down to the worst case even
 * when the budget is unused, and would still be wrong if the server's limits
 * were configured differently. Waiting only when told keeps the happy path fast
 * and stays correct for any limit.
 */
import { isApiError } from '@/api/http'

/** Error code the backend uses for a spent rate-limit budget. */
export const RATE_LIMITED_CODE = 'RATE_LIMITED'

/** HTTP status that means the same thing when the body is unparsable. */
export const RATE_LIMITED_STATUS = 429

/** How many times one submission may be retried after waiting. */
export const DEFAULT_MAX_ATTEMPTS = 5

/**
 * Total time one submission may spend waiting.
 *
 * Per submission, not per batch: the wait a single 429 asks for is a few
 * seconds, so this is generous. It exists so a server that keeps answering 429
 * produces an error rather than an unbounded hang.
 */
export const DEFAULT_MAX_TOTAL_WAIT_MS = 60_000

/** Never wait less than this; the server floors its figure at 1s too. */
export const MIN_WAIT_MS = 1_000

/** Never wait longer than this, even if the server asks. */
export const MAX_WAIT_MS = 30_000

/**
 * Added to the server's figure. `retry_after` is the exact time until one token
 * exists, so retrying at precisely that moment races the refill; a small buffer
 * avoids a second 429.
 */
export const WAIT_MARGIN_MS = 250

/** Fallback when the response carries no usable timing information. */
export const FALLBACK_WAIT_MS = 5_000

/** True when the error means "you spent your budget, wait and try again". */
export function isRateLimited(error: unknown): boolean {
  if (isApiError(error)) {
    return error.code === RATE_LIMITED_CODE || error.status === RATE_LIMITED_STATUS
  }
  return false
}

function positiveNumber(value: unknown): number | null {
  const num = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(num) && num > 0 ? num : null
}

/**
 * How long to wait before retrying, in milliseconds.
 *
 * Prefers the server's `detail.retry_after` (seconds). If that is missing it
 * derives the interval from `window_seconds / limit`, which is the bucket's
 * refill rate, and finally falls back to a constant. The result is clamped so a
 * nonsense value can neither busy-loop nor stall the UI.
 */
export function rateLimitWaitMs(error: unknown): number {
  let raw: number | null = null

  if (isApiError(error) && error.detail) {
    const detail = error.detail
    const retryAfter = positiveNumber(detail.retry_after)
    if (retryAfter !== null) {
      raw = retryAfter * 1000
    } else {
      // No explicit figure: one token takes window/limit seconds to accrue.
      const window = positiveNumber(detail.window_seconds)
      const limit = positiveNumber(detail.limit)
      if (window !== null && limit !== null) raw = (window / limit) * 1000
    }
  }

  const base = raw ?? FALLBACK_WAIT_MS
  return Math.min(MAX_WAIT_MS, Math.max(MIN_WAIT_MS, Math.ceil(base)) + WAIT_MARGIN_MS)
}

export interface RateLimitRetryOptions {
  /** Maximum attempts for one logical submission (including the first). */
  maxAttempts?: number
  /** Ceiling on total waiting time for one logical submission. */
  maxTotalWaitMs?: number
  /** Injected for tests; defaults to a real timer. */
  sleep?: (ms: number) => Promise<void>
  /** Called before each wait, so the UI can explain the pause. */
  onWait?: (waitMs: number, attempt: number) => void
  /** Checked before waiting: a cancelled batch must not sleep then submit. */
  shouldStop?: () => boolean
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, ms)
  })

/**
 * Run `fn`, waiting and retrying while the server reports a spent budget.
 *
 * Any non-rate-limit error propagates on the first attempt: this only handles
 * one specific, explicitly-retryable condition. `Retryable` is not inferred
 * from the error's own hint, because waiting is only appropriate here when the
 * server said exactly how long to wait.
 */
export async function withRateLimitRetry<T>(
  fn: () => Promise<T>,
  options: RateLimitRetryOptions = {},
): Promise<T> {
  const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS
  const maxTotalWaitMs = options.maxTotalWaitMs ?? DEFAULT_MAX_TOTAL_WAIT_MS
  const sleep = options.sleep ?? defaultSleep
  const attempts = Math.max(1, maxAttempts)

  let waited = 0

  for (let attempt = 1; ; attempt += 1) {
    try {
      return await fn()
    } catch (error) {
      const canRetry = isRateLimited(error) && attempt < attempts
      if (!canRetry) throw error

      // A stop request must win over the wait: sleeping and then submitting
      // would spend money the user just asked us not to spend.
      if (options.shouldStop?.()) throw error

      const waitMs = rateLimitWaitMs(error)
      if (waited + waitMs > maxTotalWaitMs) throw error

      options.onWait?.(waitMs, attempt)
      waited += waitMs
      await sleep(waitMs)

      // Re-check after sleeping: the user may have stopped while we waited.
      if (options.shouldStop?.()) throw error
    }
  }
}
