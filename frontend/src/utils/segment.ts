/**
 * Long-text segmentation.
 *
 * The pipeline caps a single request at MAX_TEXT_LENGTH (5000) characters.
 * Rather than rejecting longer input, split it into speakable chunks at
 * natural boundaries and let the existing task queue handle them one by one —
 * each chunk becomes its own history record, so a single failed segment can be
 * regenerated without redoing the whole document.
 *
 * Boundary preference (highest first):
 *   1. blank line   — paragraph break, the strongest signal
 *   2. sentence end — 。！？；.!?; followed by optional quotes/brackets
 *   3. clause end   — ，、,  (only if a chunk would otherwise overflow)
 *   4. hard cut     — absolute fallback, at the limit
 *
 * The splitter never returns an empty chunk and never exceeds the limit, which
 * is what the backend enforces.
 */

/** Sentence terminators for CJK and Latin text, with trailing closers. */
const SENTENCE_END = /([。！？；!?;]+[”"’'）)\]】]*)/g
/** Softer breaks used only when a sentence alone exceeds the limit. */
const CLAUSE_END = /([，、,]+)/g

export interface SegmentOptions {
  /** Hard per-chunk limit; must match the backend's max_text_length. */
  limit: number
  /**
   * When true (default) the splitting prefers paragraph/sentence boundaries.
   * Set false to force a plain character split (used by tests).
   */
  preferNatural?: boolean
}

export interface SegmentResult {
  segments: string[]
  /** True when the input was short enough to send as a single request. */
  single: boolean
}

/** Split `text` into pieces no longer than `limit` characters. */
export function segmentText(text: string, options: SegmentOptions): SegmentResult {
  const limit = Math.max(1, Math.floor(options.limit))
  const trimmed = text.trim()

  if (trimmed.length === 0) return { segments: [], single: true }
  if (trimmed.length <= limit) return { segments: [trimmed], single: true }

  const preferNatural = options.preferNatural !== false
  const segments: string[] = []

  // Stage 1: paragraphs, then sentences.
  const blocks = preferNatural ? splitByBoundary(trimmed, SENTENCE_END) : [trimmed]

  for (const block of blocks) {
    if (block.length <= limit) {
      segments.push(block)
      continue
    }
    // Stage 2: the block is still too long — break at clauses.
    const clauses = preferNatural ? splitByBoundary(block, CLAUSE_END) : [block]
    for (const clause of clauses) {
      if (clause.length <= limit) {
        segments.push(clause)
        continue
      }
      // Stage 3: hard cut at the limit (a single unbroken run of characters).
      for (let i = 0; i < clause.length; i += limit) {
        segments.push(clause.slice(i, i + limit))
      }
    }
  }

  // Stage 4: greedily merge neighbours while they still fit, so we do not send
  // 200 tiny requests for text that naturally groups into large chunks.
  const merged = mergeUpTo(segments, limit)

  return { segments: merged, single: false }
}

/**
 * Split on a boundary regex, keeping the delimiter attached to the left piece
 * (so punctuation stays with its sentence) and dropping empty results.
 */
function splitByBoundary(text: string, pattern: RegExp): string[] {
  const parts: string[] = []
  let cursor = 0
  // Reset lastIndex: the regexps are module-level and stateful with /g.
  pattern.lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(text)) !== null) {
    const end = match.index + match[0].length
    const piece = text.slice(cursor, end).trim()
    if (piece) parts.push(piece)
    cursor = end
  }

  const tail = text.slice(cursor).trim()
  if (tail) parts.push(tail)

  return parts.length > 0 ? parts : [text.trim()]
}

/** Greedily pack pieces together while the result stays within `limit`. */
function mergeUpTo(pieces: string[], limit: number): string[] {
  const out: string[] = []
  let current = ''

  for (const piece of pieces) {
    if (current.length === 0) {
      current = piece
      continue
    }
    // +1 for the newline we join with.
    if (current.length + 1 + piece.length <= limit) {
      current = `${current}\n${piece}`
    } else {
      out.push(current)
      current = piece
    }
  }
  if (current.length > 0) out.push(current)

  return out
}

/** Human-readable summary of a segmentation, for the UI hint. */
export function describeSegments(result: SegmentResult): string {
  if (result.single) return '文本长度在单次请求范围内'
  const total = result.segments.reduce((sum, item) => sum + item.length, 0)
  return `已拆分为 ${result.segments.length} 段（共 ${total} 字符），将依次生成`
}
