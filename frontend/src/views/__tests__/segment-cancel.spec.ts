import { describe, expect, it } from 'vitest'
import {
  canRunSegment,
  cancelRun,
  newRunToken,
  segmentCancelSummary,
  startRun,
  type SegmentRunToken,
} from '@/utils/taskStatus'

/**
 * The bug these tests guard against: pressing 停止生成 on a long-text segmented
 * run cancelled only the segment that happened to be in flight, and the loop
 * then submitted every remaining segment — the user kept paying for vendor
 * calls after asking the app to stop.
 *
 * The loop's continue/stop decision is a pure function of a run token
 * (`canRunSegment`), so the whole control flow is testable here in the node
 * environment without a DOM, without mounting GenerateView.vue, and without a
 * network stub. The simulation below mirrors `runSegmented()`: the checkpoint
 * before each submit and the re-read after each await are exactly the two
 * places the real loop consults the signal.
 */

/** The piece of `runSegmented()` under test: a submit-loop driven by a token. */
function simulateRun(options: {
  pieces: number
  /** Called after each successful segment, may mutate the shared signal. */
  afterSegment?: (index: number, signal: SegmentRunToken, token: number) => SegmentRunToken
  /** Called after the submit resolves and before the (simulated) await. */
  afterSubmit?: (index: number, signal: SegmentRunToken, token: number) => SegmentRunToken
  /** The signal before the loop begins. */
  beforeLoop?: (signal: SegmentRunToken, token: number) => SegmentRunToken
  /** Extra signal check to model a cancel landing during an await. */
  duringAwait?: (index: number, signal: SegmentRunToken, token: number) => SegmentRunToken
}): { submitted: number[]; completed: number; canceled: boolean; signal: SegmentRunToken; token: number } {
  let signal = startRun(newRunToken())
  const token = signal.current
  const submitted: number[] = []
  let completed = 0
  let canceled = false

  if (options.beforeLoop) signal = options.beforeLoop(signal, token)

  for (let index = 0; index < options.pieces; index += 1) {
    // Checkpoint 1 — before submitting: this is the fix. Without it the loop
    // spends money it was explicitly told not to spend.
    if (!canRunSegment(signal, token)) {
      canceled = true
      break
    }

    submitted.push(index + 1)
    if (options.afterSubmit) signal = options.afterSubmit(index, signal, token)

    // Models `await waitForTask(...)`: cancellation can land here.
    if (options.duringAwait) signal = options.duringAwait(index, signal, token)
    const stoppedMidSegment = !canRunSegment(signal, token)

    if (stoppedMidSegment) {
      canceled = true
      break
    }
    completed += 1
    if (options.afterSegment) signal = options.afterSegment(index, signal, token)
  }

  return { submitted, completed, canceled, signal, token }
}

describe('segmented run cancellation (the 停止生成 bug)', () => {
  it('submits nothing when the run is canceled before the loop starts', () => {
    const { submitted, canceled } = simulateRun({
      pieces: 5,
      beforeLoop: (signal, token) => cancelRun(signal, token),
    })

    expect(submitted).toEqual([])
    expect(canceled).toBe(true)
  })

  it('stops after exactly the segment that was in flight when cancel was pressed', () => {
    // Cancel lands during segment 1's poll (the realistic case: user watches
    // 第 1 / 8 段 and clicks stop).
    const { submitted, completed, canceled } = simulateRun({
      pieces: 8,
      duringAwait: (index, signal, token) => (index === 0 ? cancelRun(signal, token) : signal),
    })

    // Segment 1 was already paid for; segments 2..8 must never be submitted.
    expect(submitted).toEqual([1])
    expect(completed).toBe(0)
    expect(canceled).toBe(true)
  })

  it('submits segment 1 and none after when cancel follows a completed segment', () => {
    const { submitted, completed, canceled } = simulateRun({
      pieces: 6,
      afterSegment: (index, signal, token) => (index === 0 ? cancelRun(signal, token) : signal),
    })

    expect(submitted).toEqual([1])
    expect(completed).toBe(1)
    expect(canceled).toBe(true)
  })

  it('does not let a stale run resume submitting after a newer run began', () => {
    // Run A starts, is parked in an await. The user cancels, then starts run B.
    // Run B bumps the token, so A's token is superseded: when A's await finally
    // resolves it must NOT submit (this is what a plain boolean got wrong, since
    // run B clears the flag and A would read it as "not canceled").
    let signal = startRun(newRunToken())
    const staleToken = signal.current

    signal = cancelRun(signal, staleToken)
    signal = startRun(signal) // run B — a fresh token, canceled flag cleared
    const newToken = signal.current

    expect(newToken).toBeGreaterThan(staleToken)
    // The stale run cannot submit anything.
    expect(canRunSegment(signal, staleToken)).toBe(false)
    // The new run is unaffected and may proceed.
    expect(canRunSegment(signal, newToken)).toBe(true)
  })

  it('cancels only the run owning the current token', () => {
    // A cancel aimed at an old token must not stop the live run, otherwise a
    // late click from a previous run silently kills the new one.
    let signal = startRun(newRunToken())
    const oldToken = signal.current
    signal = startRun(signal)

    const afterStaleCancel = cancelRun(signal, oldToken)
    expect(afterStaleCancel.canceled).toBe(false)
    expect(canRunSegment(afterStaleCancel, signal.current)).toBe(true)
  })

  it('is a clean stop, not a failure: the run reports canceled with results kept', () => {
    const { submitted, completed, canceled } = simulateRun({
      pieces: 4,
      afterSegment: (index, signal, token) => (index === 1 ? cancelRun(signal, token) : signal),
    })

    expect(submitted).toEqual([1, 2])
    expect(completed).toBe(2)
    // `canceled` is the flag the loop uses to pick the info message instead of
    // throwing an ApiError — cancel must never reach the error alert.
    expect(canceled).toBe(true)
  })
})

describe('honest reporting on a canceled segmented run', () => {
  const successText = '段全部生成完成'

  it('never uses the all-segments-done wording when canceled', () => {
    for (const completed of [0, 1, 3, 7]) {
      const summary = segmentCancelSummary(completed, 8)
      expect(summary).not.toContain(successText)
      expect(summary).toContain('已取消')
    }
  })

  it('reports how many segments completed out of the total', () => {
    const summary = segmentCancelSummary(3, 12)
    expect(summary).toContain('3')
    expect(summary).toContain('12')
  })

  it('says the remaining segments were not submitted', () => {
    const summary = segmentCancelSummary(3, 12)
    expect(summary).toContain('不再提交')
  })

  it('says nothing was submitted when cancel landed before any segment', () => {
    const summary = segmentCancelSummary(0, 5)
    expect(summary).toContain('未提交')
    expect(summary).not.toContain(successText)
  })

  it('tells the user the completed segments are still in history', () => {
    const summary = segmentCancelSummary(2, 6)
    expect(summary).toContain('历史记录')
  })

  it('produces a summary distinguishable from the success toast', () => {
    // The success path is `${pieces.length} 段全部生成完成`; the canceled path
    // must never be able to produce that same string.
    for (let completed = 0; completed <= 8; completed += 1) {
      expect(segmentCancelSummary(completed, 8)).not.toBe(`8 ${successText}`)
    }
  })
})
