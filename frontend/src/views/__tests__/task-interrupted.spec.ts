import { describe, expect, it } from 'vitest'
import type { TaskStatusResponse } from '@/api/types'
import { statusLabel, statusTagType } from '@/utils/format'
import {
  NON_TERMINAL_STATUSES,
  TASK_INTERRUPTED_CODE,
  isTerminalStatus,
  taskErrorFields,
} from '@/utils/taskStatus'

/**
 * `interrupted` is the status an orphaned task carries after the server is
 * restarted mid-generation (backend task-1). It is terminal *and* it is a
 * failure: the in-memory request payload (which holds the provider keys) is
 * gone, so the work can never resume and the user must resubmit.
 *
 * The bug these tests guard against: both pollers used to hard-code
 * `failed | canceled` as "terminal", so an interrupted task was polled until
 * the 5-minute cap and then reported as "轮询超过 5 分钟" — a wrong diagnosis
 * that hid the real reason and kept the UI busy for minutes.
 */

/** A task-status response as returned by `GET /tasks/{id}`. */
function interruptedStatus(
  error: TaskStatusResponse['error'] = null,
): TaskStatusResponse {
  return {
    task_id: 'task-restart-1',
    status: 'interrupted',
    stage: 'done',
    // Progress is kept from the killed run; it must not reset to 0.
    progress: 60,
    message: '服务已重启，该任务已中断',
    created_at: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:01Z',
    finished_at: '2026-01-01T00:05:00Z',
    elapsed_ms: 299000,
    error,
  }
}

/** The exact payload shape frozen by backend task-1. */
const BACKEND_ERROR = {
  code: 'TASK_INTERRUPTED',
  message: '服务已重启，该任务已中断，无法继续执行，请重新提交。',
  detail: {
    suggestion:
      '任务在服务重启时被中断，未完成的进度无法恢复（密钥不会落盘，因此无法自动续跑）。请返回生成页重新提交任务。',
    retryable: true,
  },
  request_id: 'req-abc-123',
}

describe('interrupted status presentation', () => {
  it('labels the status 已中断', () => {
    expect(statusLabel('interrupted')).toBe('已中断')
  })

  it('renders the status as a danger tag, not neutral info', () => {
    expect(statusTagType('interrupted')).toBe('danger')
  })

  it('keeps the other statuses on their existing labels and tag types', () => {
    // Regression guard: the new case must not have reordered the old ones.
    expect(statusLabel('canceled')).toBe('已取消')
    expect(statusLabel('failed')).toBe('失败')
    expect(statusTagType('failed')).toBe('danger')
    expect(statusTagType('running')).toBe('warning')
    expect(statusTagType('succeeded')).toBe('success')
  })
})

describe('terminal-status rule used by poll() and waitForTask()', () => {
  it('treats interrupted as terminal, so polling stops immediately', () => {
    expect(isTerminalStatus('interrupted')).toBe(true)
  })

  it('treats only pending and running as non-terminal', () => {
    expect(NON_TERMINAL_STATUSES).toEqual(['pending', 'running'])
    expect(isTerminalStatus('pending')).toBe(false)
    expect(isTerminalStatus('running')).toBe(false)
  })

  it('treats every other known status as terminal', () => {
    for (const status of ['succeeded', 'failed', 'canceled', 'interrupted']) {
      expect(isTerminalStatus(status)).toBe(true)
    }
  })

  it('treats an unknown or missing status as terminal (never poll forever)', () => {
    // A future backend status must fail closed, not reintroduce the hang.
    expect(isTerminalStatus('some_new_status')).toBe(true)
    expect(isTerminalStatus(null)).toBe(true)
    expect(isTerminalStatus(undefined)).toBe(true)
  })

  it('does not let interrupted fall through to the 5-minute timeout branch', () => {
    // Mirrors poll(): the timeout branch is only reachable while the status is
    // non-terminal, which is exactly the condition the fix establishes.
    const reachesTimeoutBranch = !isTerminalStatus('interrupted')
    expect(reachesTimeoutBranch).toBe(false)
  })
})

describe('interrupted error normalisation', () => {
  it('passes the backend envelope through unchanged when present', () => {
    const fields = taskErrorFields(interruptedStatus(BACKEND_ERROR))

    expect(fields.code).toBe('TASK_INTERRUPTED')
    expect(fields.message).toBe(BACKEND_ERROR.message)
    expect(fields.requestId).toBe('req-abc-123')
    expect(fields.detail?.suggestion).toBe(BACKEND_ERROR.detail.suggestion)
    expect(fields.detail?.retryable).toBe(true)
  })

  it('still produces a TASK_INTERRUPTED code and a non-empty message without an error payload', () => {
    const fields = taskErrorFields(interruptedStatus(null))

    expect(fields.code).toBe(TASK_INTERRUPTED_CODE)
    expect(fields.code).toBe('TASK_INTERRUPTED')
    expect(fields.message.length).toBeGreaterThan(0)
    expect(fields.message).toBe('任务已中断')
    // The real symptom of the bug was the misleading timeout text.
    expect(fields.message).not.toContain('轮询超过 5 分钟')
  })

  it('supplies a resubmit suggestion when the payload omits one', () => {
    const fields = taskErrorFields(
      interruptedStatus({ code: 'TASK_INTERRUPTED', message: '已中断', detail: null, request_id: null }),
    )

    const suggestion = fields.detail?.suggestion
    expect(typeof suggestion).toBe('string')
    // The instruction must explain that nothing is recoverable and to resubmit.
    expect(String(suggestion)).toContain('重新提交')
    expect(fields.detail?.retryable).toBe(true)
  })

  it('does not invent a suggestion for ordinary failures', () => {
    const fields = taskErrorFields({
      status: 'failed',
      error: { code: 'TTS_AUTH_FAILED', message: '鉴权失败', detail: null, request_id: 'r-1' },
    })

    expect(fields.code).toBe('TTS_AUTH_FAILED')
    expect(fields.detail).toBeNull()
  })

  it('keeps the existing fallbacks for failed and canceled', () => {
    expect(taskErrorFields({ status: 'failed', error: null }).code).toBe('TASK_FAILED')
    expect(taskErrorFields({ status: 'failed', error: null }).message).toBe('任务执行失败')
    expect(taskErrorFields({ status: 'canceled', error: null }).message).toBe('任务已取消')
  })
})
