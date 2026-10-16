/**
 * Batch retry of failed / interrupted history records.
 *
 * Why this runs in the BROWSER and not on the server
 * --------------------------------------------------
 * The backend deliberately stores only a credential-stripped snapshot of each
 * request (`GenerateRequest.safe_dump`): the API key is never persisted, by
 * design. That means a history record simply cannot be re-run server-side --
 * there is no key to run it with. So a retry is driven by the credentials the
 * user holds locally, and the request is rebuilt through the same
 * `configStore.buildRequest()` path the generate page uses. This module never
 * sees, stores or logs a key.
 *
 * Why sequential
 * --------------
 * Each item is a real, billable vendor call, and the platform is rate limited.
 * Firing a selection of 20 in parallel would spend 20 calls at once, trip the
 * limiter and make the run harder to stop. Items are submitted strictly one
 * after another, and the stop flag is checked before every submission.
 *
 * The orchestration lives here (rather than inline in the view) so the parts
 * that are easy to get wrong -- which records are eligible, what happens when
 * one item fails, and what "stopped" means -- are unit-testable without a DOM.
 */
import type { HistoryItem, TaskStatusResponse } from '@/api/types'
import type { PollOutcome } from '@/utils/taskPolling'

/** Statuses worth re-running. A `pending`/`running` task is still going. */
export const RETRYABLE_STATUSES: readonly string[] = ['failed', 'interrupted']

/** Per-item result. */
export type RetryOutcome =
  /** Re-submitted and the new task succeeded. */
  | 'succeeded'
  /** Re-submitted but the new task ended in a non-success terminal state. */
  | 'failed'
  /** Never submitted: the record's status did not warrant a retry. */
  | 'skipped'
  /** Not submitted: the batch was stopped before reaching it. */
  | 'stopped'

export interface RetryItemResult {
  id: number
  /** Short text of the record, for the summary. */
  label: string
  outcome: RetryOutcome
  /** Human-readable explanation, present for anything that is not success. */
  reason?: string
}

/** Tallies for the end-of-batch summary. */
export interface RetrySummary {
  succeeded: number
  failed: number
  skipped: number
  stopped: number
  /** Everything that was actually submitted (succeeded + failed). */
  attempted: number
  total: number
}

/** Does this record's status warrant a retry? */
export function isRetryable(item: Pick<HistoryItem, 'status'>): boolean {
  return RETRYABLE_STATUSES.includes(item.status)
}

/**
 * Has this failure already been recovered by a later successful run?
 *
 * A retry creates a new record rather than rewriting history, so a failure
 * stays listed after it has been fixed. Without this check the batch kept
 * offering such rows, and running them again would spend money a second time.
 * The backend derives `superseded_by`; this only reads it.
 */
export function isSuperseded(item: Pick<HistoryItem, 'superseded_by'>): boolean {
  return item.superseded_by !== null && item.superseded_by !== undefined
}

/**
 * The records a batch run would actually submit.
 *
 * Excludes anything already recovered, so the count shown on the button and the
 * work the batch does cannot disagree. `isRetryable` alone is the status check.
 */
export function selectRetryable<
  T extends Pick<HistoryItem, 'status' | 'superseded_by'>,
>(items: T[]): T[] {
  return items.filter((item) => isRetryable(item) && !isSuperseded(item))
}

/** Count a run's results. */
export function summarizeResults(results: RetryItemResult[]): RetrySummary {
  let succeeded = 0
  let failed = 0
  let skipped = 0
  let stopped = 0

  for (const result of results) {
    if (result.outcome === 'succeeded') succeeded += 1
    else if (result.outcome === 'failed') failed += 1
    else if (result.outcome === 'skipped') skipped += 1
    else stopped += 1
  }

  return {
    succeeded,
    failed,
    skipped,
    stopped,
    attempted: succeeded + failed,
    total: results.length,
  }
}

/** One-line Chinese summary of a finished batch. */
export function describeSummary(summary: RetrySummary): string {
  const parts = [`成功 ${summary.succeeded}`, `失败 ${summary.failed}`]
  if (summary.skipped > 0) parts.push(`跳过 ${summary.skipped}`)
  if (summary.stopped > 0) parts.push(`未重试 ${summary.stopped}`)
  return `批量重试结束：${parts.join(' / ')}`
}

/** What the caller must supply. All side effects are injected. */
export interface BatchRetryDeps {
  /**
   * Copy the record's parameters into the config store and submit it. Must
   * throw on failure (missing credentials, queue full, ...); the throw is
   * recorded as this item's failure and does not abort the batch.
   */
  submit: (item: HistoryItem) => Promise<{ task_id: string }>
  /** Wait for the submitted task to finish. */
  poll: (taskId: string) => Promise<PollOutcome>
  /** True once the user asked to stop. Checked before each submission. */
  shouldStop: () => boolean
  /** Called after each item settles, for progress display. */
  onProgress?: (result: RetryItemResult, done: number, total: number) => void
}

/** Short label for a record, used in messages. */
function labelFor(item: HistoryItem, index: number): string {
  const text = (item.input_text || '').trim().replace(/\s+/g, ' ')
  if (!text) return `第 ${index + 1} 条`
  return text.length > 20 ? `${text.slice(0, 20)}…` : text
}

/** Map a finished task status onto a retry outcome. */
function outcomeForStatus(status: TaskStatusResponse): { outcome: RetryOutcome; reason?: string } {
  if (status.status === 'succeeded') return { outcome: 'succeeded' }
  if (status.status === 'canceled') return { outcome: 'failed', reason: '任务被取消' }
  if (status.status === 'interrupted') {
    return { outcome: 'failed', reason: '服务重启导致中断，可再次重试' }
  }

  // Prefer the backend's own explanation. A generic "生成失败" tells the user
  // nothing they can act on, while the error code (LLM_UNSUPPORTED_PROVIDER,
  // TTS_TEXT_TOO_LONG, ...) says exactly what to fix.
  const message = status.error?.message?.trim()
  const code = status.error?.code?.trim()
  if (message && code) return { outcome: 'failed', reason: `${message}（${code}）` }
  if (message) return { outcome: 'failed', reason: message }
  if (code) return { outcome: 'failed', reason: `生成失败（${code}）` }
  return { outcome: 'failed', reason: '生成失败' }
}

/**
 * Re-run the given records, sequentially.
 *
 * Takes the FULL selection (not just the eligible part) so the summary can
 * report how many were skipped and why -- the user selected them, so silently
 * ignoring them would be confusing.
 *
 * A failing item never aborts the batch: each failure is recorded and the loop
 * moves on. Only a stop request ends it early, and the untouched remainder is
 * reported as `stopped` rather than dropped.
 */
export async function runBatchRetry(
  items: HistoryItem[],
  deps: BatchRetryDeps,
): Promise<RetryItemResult[]> {
  const results: RetryItemResult[] = []
  const total = items.length

  for (let index = 0; index < total; index += 1) {
    const item = items[index]
    if (item === undefined) continue
    const label = labelFor(item, index)

    // Stop wins over everything: never submit another billable call.
    if (deps.shouldStop()) {
      const result: RetryItemResult = {
        id: item.id,
        label,
        outcome: 'stopped',
        reason: '已停止，未提交',
      }
      results.push(result)
      deps.onProgress?.(result, results.length, total)
      continue
    }

    if (!isRetryable(item)) {
      const result: RetryItemResult = {
        id: item.id,
        label,
        outcome: 'skipped',
        reason: item.status === 'succeeded' ? '已成功，无需重试' : '状态不支持重试',
      }
      results.push(result)
      deps.onProgress?.(result, results.length, total)
      continue
    }

    if (isSuperseded(item)) {
      // Reported rather than dropped: the user selected this row, so silently
      // ignoring it would be confusing. Running it would spend money again for
      // a result that already exists.
      const result: RetryItemResult = {
        id: item.id,
        label,
        outcome: 'skipped',
        reason: `已重试成功（记录 #${item.superseded_by}），无需再次提交`,
      }
      results.push(result)
      deps.onProgress?.(result, results.length, total)
      continue
    }

    let result: RetryItemResult
    try {
      // Rebuilds the request through the store, so the local key is used and
      // the record's own parameters (provider/model/voice/format) are honoured.
      const submitted = await deps.submit(item)
      const polled = await deps.poll(submitted.task_id)

      if (polled.kind === 'stopped') {
        result = { id: item.id, label, outcome: 'stopped', reason: '已停止' }
      } else if (polled.kind === 'timeout') {
        result = {
          id: item.id,
          label,
          outcome: 'failed',
          reason: '等待超时，请到历史记录查看结果',
        }
      } else {
        const mapped = outcomeForStatus(polled.status)
        result = {
          id: item.id,
          label,
          outcome: mapped.outcome,
          ...(mapped.reason ? { reason: mapped.reason } : {}),
        }
      }
    } catch (caught) {
      // Per-item failure must not abort the batch.
      result = {
        id: item.id,
        label,
        outcome: 'failed',
        reason: caught instanceof Error ? caught.message : '提交失败',
      }
    }

    results.push(result)
    deps.onProgress?.(result, results.length, total)
  }

  return results
}
