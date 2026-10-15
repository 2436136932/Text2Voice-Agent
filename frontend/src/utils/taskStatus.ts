/**
 * Terminal / non-terminal task-status semantics, shared by every poller.
 *
 * The backend contract is deliberately *closed*: only `pending` and `running`
 * are non-terminal. Every other status — `succeeded`, `failed`, `canceled`
 * and `interrupted` — is terminal, so polling must stop the moment one is
 * observed.
 *
 * This lives in its own dependency-free module (rather than inline in
 * GenerateView.vue) so the rule is unit-testable in the node vitest
 * environment, and so the single-generation poller and the segmented
 * `waitForTask()` loop can never drift apart: they previously each carried
 * their own hand-written status list, which is exactly how `interrupted`
 * slipped through and made the UI poll a dead task until the 5-minute
 * timeout, showing "轮询超过 5 分钟" instead of the real reason.
 */
import type { TaskStatus } from '@/api/types'

/** The only statuses that still mean "keep polling". */
export const NON_TERMINAL_STATUSES: readonly TaskStatus[] = ['pending', 'running']

/** Backend error code attached to a task that died with the server. */
export const TASK_INTERRUPTED_CODE = 'TASK_INTERRUPTED'

/** Fallback message when an interrupted status arrives without an error body. */
export const TASK_INTERRUPTED_MESSAGE = '任务已中断'

/**
 * Fallback advice mirroring the backend's `detail.suggestion`.
 *
 * The point of this message is the "keys are never stored" explanation: the
 * user must understand that this task cannot be resumed and has to be
 * resubmitted, rather than waiting for it to finish.
 */
export const TASK_INTERRUPTED_SUGGESTION =
  '任务在服务重启时被中断，未完成的进度无法恢复（密钥不会落盘，因此无法自动续跑）。请返回生成页重新提交任务。'

/**
 * Whether polling should stop for `status`.
 *
 * Non-terminal is *only* `pending` / `running`; anything unknown is treated as
 * terminal so a future status can never reintroduce an infinite poll.
 */
export function isTerminalStatus(status: string | null | undefined): boolean {
  if (!status) return true
  return !NON_TERMINAL_STATUSES.includes(status as TaskStatus)
}

/* ------------------- segmented-run cancellation (run token) ------------------- */

/**
 * The cancellation signal for a segmented (long-text) run.
 *
 * A plain boolean is not enough. `runSegmented()` awaits the network between
 * segments, and a user can press 停止生成 and immediately start a *new* run
 * while the old one is still parked inside `await waitForTask(...)`. When that
 * old await resolves it would read "canceled === false" (the new run cleared
 * the flag) and carry on submitting paid segments.
 *
 * A monotonic token fixes that: every run records the token it owns at start
 * and the only thing that can invalidate it is a *newer* run (or an explicit
 * cancel). A stale run's token can never match again.
 */
export interface SegmentRunToken {
  /** Token owned by the run that is live right now. */
  readonly current: number
  /** Explicit cancel: set by 停止生成, cleared when a fresh run starts. */
  readonly canceled: boolean
}

/** A fresh signal: token 0, not canceled. */
export function newRunToken(): SegmentRunToken {
  return { current: 0, canceled: false }
}

/** Start a run: takes the next token and clears any previous cancel. */
export function startRun(signal: SegmentRunToken): SegmentRunToken {
  return { current: signal.current + 1, canceled: false }
}

/** Cancel the run identified by `token` (a no-op if a newer run already owns it). */
export function cancelRun(signal: SegmentRunToken, token: number): SegmentRunToken {
  if (signal.current !== token) return signal
  return { current: signal.current, canceled: true }
}

/**
 * Whether `token` may still submit its next segment.
 *
 * False when the run was canceled, and false when its token was superseded by
 * a newer run — the two ways the loop must stop spending money.
 */
export function canRunSegment(signal: SegmentRunToken, token: number): boolean {
  return signal.current === token && !signal.canceled
}

/**
 * The honest summary shown when a segmented run stops early.
 *
 * Deliberately never the "全部生成完成" success wording: the completed
 * segments are kept (they are already separate records in history) but the
 * remaining ones were never submitted, and saying otherwise would hide that
 * the user did not get the whole document.
 */
export function segmentCancelSummary(completed: number, total: number): string {
  if (completed <= 0) {
    return `已取消：0 / ${total} 段已完成，本次未提交任何分段。`
  }
  return `已取消：已完成 ${completed} / ${total} 段，剩余分段不再提交。已完成的分段已保存在历史记录中。`
}

/** The subset of the task-status response this module needs. */
export interface TerminalTaskLike {
  status: string
  error?: {
    code?: string | null
    message?: string | null
    detail?: Record<string, unknown> | null
    request_id?: string | null
  } | null
}

/** Normalised error fields ready to construct an `ApiError`. */
export interface TaskErrorFields {
  code: string
  message: string
  detail: Record<string, unknown> | null
  requestId: string | null
}

/**
 * Turn a terminal task status into displayable error fields.
 *
 * A well-formed backend response already carries the full envelope, so it is
 * passed through untouched. The fallbacks exist because the interrupted status
 * is *also* reachable from a partially-populated payload (e.g. a cached or
 * older record): the user must never see a blank message, and must never see
 * the misleading 5-minute timeout text, so both the code and the message are
 * defaulted for `interrupted`.
 */
export function taskErrorFields(status: TerminalTaskLike): TaskErrorFields {
  const error = status.error ?? null
  const suggestion = error?.detail?.suggestion
  const interrupted = status.status === 'interrupted'

  const code = error?.code || (interrupted ? TASK_INTERRUPTED_CODE : 'TASK_FAILED')
  const message =
    error?.message ||
    (interrupted
      ? TASK_INTERRUPTED_MESSAGE
      : status.status === 'canceled'
        ? '任务已取消'
        : '任务执行失败')

  // Keep the backend detail as-is, but guarantee the actionable suggestion is
  // present for an interrupted task so ErrorAlert can always render the
  // "resubmit" instruction as visible text.
  let detail = error?.detail ?? null
  if (interrupted && (typeof suggestion !== 'string' || suggestion.length === 0)) {
    detail = { ...(detail ?? {}), suggestion: TASK_INTERRUPTED_SUGGESTION, retryable: true }
  }

  return {
    code,
    message,
    detail,
    requestId: error?.request_id ?? null,
  }
}
