import { describe, expect, it, vi } from 'vitest'
import {
  RETRYABLE_STATUSES,
  describeSummary,
  isRetryable,
  runBatchRetry,
  selectRetryable,
  summarizeResults,
  type RetryItemResult,
} from '@/utils/batchRetry'
import type { PollOutcome } from '@/utils/taskPolling'
import type { ApiErrorEnvelope, HistoryItem } from '@/api/types'

/**
 * The behaviour these tests guard: a batch retry must be sequential, must stop
 * the moment the user asks, must skip records that are already fine, and must
 * never let one bad item abort the rest. Every one of those is a way to either
 * lose the user's work or spend their money.
 */

/**
 * Build a complete HistoryItem. Deliberately not a loose cast: a missing
 * required field becomes a type error here, so this fixture cannot silently
 * drift from the real API contract.
 */
function item(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return {
    id: 1,
    input_text: '示例文本',
    reply_text: '示例回复',
    llm_provider: 'mock',
    llm_model: 'mock-llm',
    tts_provider: 'mimo',
    tts_voice: '冰糖',
    tts_model: 'mimo-v2.5-tts',
    audio_format: 'mp3',
    audio_bytes: 1000,
    duration_ms: 1000,
    status: 'failed',
    degraded: false,
    error_code: null,
    favorite: false,
    tags: [],
    created_at: '2026-10-16T00:00:00+00:00',
    audio_url: '/audio/1',
    ...overrides,
  }
}

/**
 * Build a poll result for a task that finished with `status`.
 *
 * `error` takes a partial envelope and fills the missing half with an empty
 * string. That keeps the fixture valid against the real `ApiErrorEnvelope`
 * (which requires both fields) while still exercising the fallback branches
 * for a payload that carries only a code or only a message.
 */
function terminal(status: string, error?: Partial<ApiErrorEnvelope>): PollOutcome {
  return {
    kind: 'terminal',
    status: {
      task_id: 't',
      status: status as never,
      stage: 'done',
      progress: 100,
      message: '',
      created_at: null,
      started_at: null,
      finished_at: null,
      elapsed_ms: null,
      error: error
        ? {
            code: error.code ?? '',
            message: error.message ?? '',
            detail: error.detail ?? null,
            request_id: error.request_id ?? null,
          }
        : null,
    },
  }
}

describe('isRetryable / selectRetryable', () => {
  it('retries only failed and interrupted records', () => {
    expect(RETRYABLE_STATUSES).toEqual(['failed', 'interrupted'])
    expect(isRetryable(item({ status: 'failed' }))).toBe(true)
    expect(isRetryable(item({ status: 'interrupted' }))).toBe(true)
  })

  it('skips succeeded, canceled, pending and running records', () => {
    for (const status of ['succeeded', 'canceled', 'pending', 'running'] as const) {
      expect(isRetryable(item({ status }))).toBe(false)
    }
  })

  it('keeps only the retryable ones, preserving order', () => {
    const items = [
      item({ id: 1, status: 'succeeded' }),
      item({ id: 2, status: 'failed' }),
      item({ id: 3, status: 'interrupted' }),
      item({ id: 4, status: 'canceled' }),
    ]
    expect(selectRetryable(items).map((i) => i.id)).toEqual([2, 3])
  })
})

describe('summarizeResults', () => {
  it('counts each outcome and the attempted total', () => {
    const results: RetryItemResult[] = [
      { id: 1, label: 'a', outcome: 'succeeded' },
      { id: 2, label: 'b', outcome: 'succeeded' },
      { id: 3, label: 'c', outcome: 'failed', reason: 'x' },
      { id: 4, label: 'd', outcome: 'skipped', reason: 'y' },
      { id: 5, label: 'e', outcome: 'stopped', reason: 'z' },
    ]
    const s = summarizeResults(results)
    expect(s.succeeded).toBe(2)
    expect(s.failed).toBe(1)
    expect(s.skipped).toBe(1)
    expect(s.stopped).toBe(1)
    // skipped/stopped were never submitted, so they are not "attempted".
    expect(s.attempted).toBe(3)
    expect(s.total).toBe(5)
  })

  it('handles an empty run', () => {
    const s = summarizeResults([])
    expect(s).toMatchObject({ succeeded: 0, failed: 0, skipped: 0, stopped: 0, total: 0 })
  })

  it('mentions skipped and stopped only when they happened', () => {
    const onlySuccess = describeSummary(summarizeResults([{ id: 1, label: 'a', outcome: 'succeeded' }]))
    expect(onlySuccess).toContain('成功 1')
    expect(onlySuccess).not.toContain('跳过')
    expect(onlySuccess).not.toContain('未重试')

    const mixed = describeSummary(
      summarizeResults([
        { id: 1, label: 'a', outcome: 'succeeded' },
        { id: 2, label: 'b', outcome: 'skipped' },
      ]),
    )
    expect(mixed).toContain('跳过 1')
  })
})

describe('runBatchRetry: sequencing', () => {
  it('submits strictly one at a time, never in parallel', async () => {
    let inFlight = 0
    let maxInFlight = 0
    const order: number[] = []

    const items = [
      item({ id: 1, status: 'failed' }),
      item({ id: 2, status: 'failed' }),
      item({ id: 3, status: 'failed' }),
    ]

    await runBatchRetry(items, {
      shouldStop: () => false,
      submit: async (it) => {
        inFlight += 1
        maxInFlight = Math.max(maxInFlight, inFlight)
        order.push(it.id)
        return { task_id: `t${it.id}` }
      },
      poll: async () => {
        inFlight -= 1
        return terminal('succeeded')
      },
    })

    expect(maxInFlight).toBe(1)
    expect(order).toEqual([1, 2, 3])
  })

  it('does not submit a record whose status does not warrant a retry', async () => {
    const submitted: number[] = []
    const items = [
      item({ id: 1, status: 'succeeded' }),
      item({ id: 2, status: 'failed' }),
    ]

    const results = await runBatchRetry(items, {
      shouldStop: () => false,
      submit: async (it) => {
        submitted.push(it.id)
        return { task_id: 't' }
      },
      poll: async () => terminal('succeeded'),
    })

    expect(submitted).toEqual([2])
    expect(results[0]?.outcome).toBe('skipped')
    expect(results[0]?.reason).toContain('已成功')
    expect(results[1]?.outcome).toBe('succeeded')
  })

  it('reports progress after each item', async () => {
    const onProgress = vi.fn()
    const items = [
      item({ id: 1, status: 'failed' }),
      item({ id: 2, status: 'succeeded' }),
    ]

    await runBatchRetry(items, {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('succeeded'),
      onProgress,
    })

    expect(onProgress).toHaveBeenCalledTimes(2)
    // (result, done, total)
    expect(onProgress.mock.calls[0]?.[1]).toBe(1)
    expect(onProgress.mock.calls[0]?.[2]).toBe(2)
    expect(onProgress.mock.calls[1]?.[1]).toBe(2)
  })
})

describe('runBatchRetry: stopping', () => {
  it('submits nothing when stop is already requested', async () => {
    const submitted: number[] = []
    const items = [item({ id: 1 }), item({ id: 2 }), item({ id: 3 })]

    const results = await runBatchRetry(items, {
      shouldStop: () => true,
      submit: async (it) => {
        submitted.push(it.id)
        return { task_id: 't' }
      },
      poll: async () => terminal('succeeded'),
    })

    expect(submitted).toEqual([])
    expect(results.map((r) => r.outcome)).toEqual(['stopped', 'stopped', 'stopped'])
    // The untouched remainder is reported, not silently dropped.
    expect(summarizeResults(results).stopped).toBe(3)
  })

  it('halts the remaining items when stop arrives mid-batch', async () => {
    const submitted: number[] = []
    let stop = false
    const items = [item({ id: 1 }), item({ id: 2 }), item({ id: 3 })]

    const results = await runBatchRetry(items, {
      shouldStop: () => stop,
      submit: async (it) => {
        submitted.push(it.id)
        return { task_id: `t${it.id}` }
      },
      poll: async () => {
        // The user hits stop while the first item is running.
        stop = true
        return terminal('succeeded')
      },
    })

    // Item 1 was already paid for and finished; 2 and 3 must never be sent.
    expect(submitted).toEqual([1])
    expect(results.map((r) => r.outcome)).toEqual(['succeeded', 'stopped', 'stopped'])
  })

  it('treats a stop observed while polling as stopped, not as a failure', async () => {
    const items = [item({ id: 1 }), item({ id: 2 })]
    let stop = false

    const results = await runBatchRetry(items, {
      shouldStop: () => stop,
      submit: async () => ({ task_id: 't' }),
      poll: async () => {
        stop = true
        return { kind: 'stopped' }
      },
    })

    expect(results[0]?.outcome).toBe('stopped')
    expect(results[0]?.reason).toContain('停止')
    expect(results[1]?.outcome).toBe('stopped')
  })
})

describe('runBatchRetry: one failure never aborts the batch', () => {
  it('continues after a submission throws', async () => {
    const items = [
      item({ id: 1, status: 'failed' }),
      item({ id: 2, status: 'failed' }),
      item({ id: 3, status: 'failed' }),
    ]
    const attempted: number[] = []

    const results = await runBatchRetry(items, {
      shouldStop: () => false,
      submit: async (it) => {
        attempted.push(it.id)
        if (it.id === 2) throw new Error('队列已满')
        return { task_id: `t${it.id}` }
      },
      poll: async () => terminal('succeeded'),
    })

    expect(attempted).toEqual([1, 2, 3])
    expect(results.map((r) => r.outcome)).toEqual(['succeeded', 'failed', 'succeeded'])
    expect(results[1]?.reason).toBe('队列已满')
  })

  it('records a task that ends failed, and keeps going', async () => {
    const items = [item({ id: 1 }), item({ id: 2 })]
    const results = await runBatchRetry(items, {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('failed'),
    })
    expect(results.map((r) => r.outcome)).toEqual(['failed', 'failed'])
    expect(results[0]?.reason).toBe('生成失败')
  })

  it("surfaces the backend's own error instead of a generic message", async () => {
    // "生成失败" tells the user nothing actionable; the code and message say
    // exactly what to fix. Verified live: a retry against a bogus provider
    // reported only "生成失败" until this was added.
    const results = await runBatchRetry([item({ id: 1 })], {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () =>
        terminal('failed', {
          code: 'LLM_UNSUPPORTED_PROVIDER',
          message: '不支持的 LLM 服务商 "nope"。',
        }),
    })
    expect(results[0]?.outcome).toBe('failed')
    expect(results[0]?.reason).toContain('不支持的 LLM 服务商')
    expect(results[0]?.reason).toContain('LLM_UNSUPPORTED_PROVIDER')
  })

  it('falls back to the code alone when no message is present', async () => {
    const results = await runBatchRetry([item({ id: 1 })], {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('failed', { code: 'TTS_TEXT_TOO_LONG' }),
    })
    expect(results[0]?.reason).toBe('生成失败（TTS_TEXT_TOO_LONG）')
  })

  it('uses the message alone when no code is present', async () => {
    const results = await runBatchRetry([item({ id: 1 })], {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('failed', { message: '服务商拒绝了请求' }),
    })
    expect(results[0]?.reason).toBe('服务商拒绝了请求')
  })

  it('explains an interrupted result and invites another retry', async () => {
    const results = await runBatchRetry([item({ id: 1 })], {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('interrupted'),
    })
    expect(results[0]?.outcome).toBe('failed')
    expect(results[0]?.reason).toContain('重启')
    expect(results[0]?.reason).toContain('重试')
  })

  it('reports a poll timeout without stopping the batch', async () => {
    const items = [item({ id: 1 }), item({ id: 2 })]
    let call = 0
    const results = await runBatchRetry(items, {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => {
        call += 1
        return call === 1 ? { kind: 'timeout' } : terminal('succeeded')
      },
    })
    expect(results[0]?.outcome).toBe('failed')
    expect(results[0]?.reason).toContain('超时')
    expect(results[1]?.outcome).toBe('succeeded')
  })

  it('treats a canceled task as a failure for that item only', async () => {
    const results = await runBatchRetry([item({ id: 1 }), item({ id: 2 })], {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('canceled'),
    })
    expect(results[0]?.outcome).toBe('failed')
    expect(results[0]?.reason).toBe('任务被取消')
  })
})

describe('runBatchRetry: labels', () => {
  it('uses a short excerpt of the text so the summary is readable', async () => {
    const results = await runBatchRetry(
      [item({ id: 7, input_text: '这是一段很长的输入文本'.repeat(10) })],
      {
        shouldStop: () => false,
        submit: async () => ({ task_id: 't' }),
        poll: async () => terminal('succeeded'),
      },
    )
    expect(results[0]?.label.length).toBeLessThanOrEqual(21)
    expect(results[0]?.label).toContain('…')
  })

  it('falls back to a positional label when the record has no text', async () => {
    const results = await runBatchRetry([item({ id: 7, input_text: '   ' })], {
      shouldStop: () => false,
      submit: async () => ({ task_id: 't' }),
      poll: async () => terminal('succeeded'),
    })
    expect(results[0]?.label).toBe('第 1 条')
  })
})
