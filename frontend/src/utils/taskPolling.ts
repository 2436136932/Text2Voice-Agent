/**
 * Polling a task until it reaches a terminal state.
 *
 * There were two pollers in the app (the single-generation one and the
 * segmented `waitForTask()`), and each carried its own idea of which statuses
 * end the wait. That duplication is precisely how `interrupted` slipped
 * through and left the UI polling a dead task for five minutes before blaming
 * a timeout.
 *
 * The rule itself now lives in `utils/taskStatus.ts` (`isTerminalStatus`, which
 * fails closed). This module adds the *loop* around it, and is kept separate
 * from that module because it touches the API layer while `taskStatus.ts` stays
 * dependency-free and trivially testable.
 *
 * The network calls and the sleep are injectable so the whole loop can be
 * unit-tested in the node environment without a DOM, a server, or real time.
 */
import { fetchTask } from '@/api'
import type { TaskStatusResponse } from '@/api/types'
import { isTerminalStatus } from '@/utils/taskStatus'

/** How long to wait between polls. */
export const POLL_INTERVAL_MS = 900

/**
 * Upper bound on polls for one task (~4 minutes at the default interval).
 *
 * A cap is needed because a task can legitimately sit in the queue behind
 * others; giving up produces an explicit timeout rather than an endless spinner.
 */
export const POLL_MAX_ATTEMPTS = 260

/** Why polling stopped. */
export type PollOutcome =
  /** The task reached a terminal status (this includes `interrupted`). */
  | { kind: 'terminal'; status: TaskStatusResponse }
  /** The caller asked to stop (e.g. the user cancelled the batch). */
  | { kind: 'stopped' }
  /** The attempt cap was reached without a terminal status. */
  | { kind: 'timeout' }

export interface PollOptions {
  intervalMs?: number
  maxAttempts?: number
  /**
   * Checked before every poll. Returning true ends the wait with
   * `{ kind: 'stopped' }`, so a cancel is honoured on the next tick rather than
   * after the whole task finishes.
   */
  shouldStop?: () => boolean
  /** Injected for tests; defaults to the real `GET /tasks/{id}`. */
  fetchStatus?: (taskId: string) => Promise<TaskStatusResponse>
  /** Injected for tests; defaults to a real timer. */
  sleep?: (ms: number) => Promise<void>
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, ms)
  })

/**
 * Poll `taskId` until it is terminal, the caller stops, or the cap is reached.
 *
 * Returns a discriminated union rather than throwing, because "timed out",
 * "cancelled" and "finished with an error status" are three different things
 * and the caller must be able to tell them apart. Errors from the status
 * request itself propagate: a network failure is not a task outcome.
 */
export async function pollUntilTerminal(
  taskId: string,
  options: PollOptions = {},
): Promise<PollOutcome> {
  const intervalMs = options.intervalMs ?? POLL_INTERVAL_MS
  const maxAttempts = options.maxAttempts ?? POLL_MAX_ATTEMPTS
  const fetchStatus = options.fetchStatus ?? fetchTask
  const sleep = options.sleep ?? defaultSleep
  const shouldStop = options.shouldStop

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (shouldStop?.()) return { kind: 'stopped' }

    const status = await fetchStatus(taskId)
    if (isTerminalStatus(status.status)) return { kind: 'terminal', status }

    await sleep(intervalMs)
  }

  // Re-check once more: a stop that arrived during the final sleep is still a
  // stop, not a timeout.
  if (shouldStop?.()) return { kind: 'stopped' }
  return { kind: 'timeout' }
}
