import { describe, expect, it, vi } from 'vitest'
import { pollUntilTerminal } from '@/utils/taskPolling'
import type { TaskStatusResponse } from '@/api/types'

/**
 * These tests guard the shared poller. It exists because there were two of them
 * (the single-generation one and the segmented waitForTask loop), each with its
 * own hand-written list of terminal statuses -- which is exactly how
 * `interrupted` slipped through and left the UI polling a dead task for five
 * minutes before blaming a timeout.
 */

function status(value: string): TaskStatusResponse {
  return {
    task_id: 't1',
    status: value as never,
    stage: 'done',
    progress: 100,
    message: '',
    created_at: null,
    started_at: null,
    finished_at: null,
    elapsed_ms: null,
    error: null,
  }
}

/** Never actually waits: the loop must be testable without real time. */
const noSleep = async (): Promise<void> => {}

describe('pollUntilTerminal', () => {
  it('returns the status as soon as it is terminal', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('succeeded'))
    const outcome = await pollUntilTerminal('t1', { fetchStatus, sleep: noSleep })

    expect(outcome.kind).toBe('terminal')
    if (outcome.kind === 'terminal') {
      expect(outcome.status.status).toBe('succeeded')
    }
    expect(fetchStatus).toHaveBeenCalledTimes(1)
  })

  it('keeps polling while the task is pending or running', async () => {
    const fetchStatus = vi
      .fn()
      .mockResolvedValueOnce(status('pending'))
      .mockResolvedValueOnce(status('running'))
      .mockResolvedValueOnce(status('succeeded'))

    const outcome = await pollUntilTerminal('t1', { fetchStatus, sleep: noSleep })
    expect(outcome.kind).toBe('terminal')
    expect(fetchStatus).toHaveBeenCalledTimes(3)
  })

  it('treats interrupted as terminal, so a restarted server does not hang the poll', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('interrupted'))
    const outcome = await pollUntilTerminal('t1', { fetchStatus, sleep: noSleep })
    expect(outcome.kind).toBe('terminal')
    expect(fetchStatus).toHaveBeenCalledTimes(1)
  })

  it('treats an unknown status as terminal rather than looping forever', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('some_future_status'))
    const outcome = await pollUntilTerminal('t1', { fetchStatus, sleep: noSleep })
    expect(outcome.kind).toBe('terminal')
    expect(fetchStatus).toHaveBeenCalledTimes(1)
  })

  it('gives up at the attempt cap with an explicit timeout', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('running'))
    const outcome = await pollUntilTerminal('t1', {
      fetchStatus,
      sleep: noSleep,
      maxAttempts: 5,
    })

    expect(outcome.kind).toBe('timeout')
    expect(fetchStatus).toHaveBeenCalledTimes(5)
  })

  it('stops before polling again once shouldStop is true', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('running'))
    // Stop is ALREADY requested when the poll begins.
    const stop = true

    const outcome = await pollUntilTerminal('t1', {
      fetchStatus,
      sleep: noSleep,
      shouldStop: () => stop,
    })

    // Nothing was ever polled: the stop is checked first.
    expect(outcome.kind).toBe('stopped')
    expect(fetchStatus).not.toHaveBeenCalled()
  })

  it('honours a stop that arrives while waiting', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('running'))
    let stop = false

    const outcome = await pollUntilTerminal('t1', {
      fetchStatus,
      shouldStop: () => stop,
      sleep: async () => {
        // The user hits stop during the interval between polls.
        stop = true
      },
    })

    expect(outcome.kind).toBe('stopped')
    // One poll happened, then the loop noticed the stop.
    expect(fetchStatus).toHaveBeenCalledTimes(1)
  })

  it('reports a stop that lands during the final sleep as stopped, not timeout', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('running'))
    let stop = false

    const outcome = await pollUntilTerminal('t1', {
      fetchStatus,
      maxAttempts: 2,
      shouldStop: () => stop,
      sleep: async () => {
        stop = true
      },
    })

    expect(outcome.kind).toBe('stopped')
  })

  it('propagates a status-request failure instead of hiding it', async () => {
    const fetchStatus = vi.fn().mockRejectedValue(new Error('network down'))
    await expect(
      pollUntilTerminal('t1', { fetchStatus, sleep: noSleep }),
    ).rejects.toThrow('network down')
  })

  it('passes the task id through to the fetcher', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(status('succeeded'))
    await pollUntilTerminal('abc123', { fetchStatus, sleep: noSleep })
    expect(fetchStatus).toHaveBeenCalledWith('abc123')
  })
})
