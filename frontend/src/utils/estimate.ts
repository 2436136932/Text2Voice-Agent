/**
 * Pre-submit usage preview.
 *
 * Answers "how big is this going to be?" before the user presses 生成语音:
 * characters, how many requests the text will become, and a rough token
 * estimate for the LLM step.
 *
 * Deliberately NO MONEY. The project has no pricing data, and inventing a
 * per-vendor price table would be both guesswork and stale within weeks, so
 * the preview reports only quantities that can actually be computed. Anything
 * derived from a heuristic is labelled as an estimate in the UI.
 *
 * Everything here is pure so it can be unit-tested in the node vitest
 * environment (no DOM, no store, no network).
 */
import { segmentText } from '@/utils/segment'
import type { GenerationMode } from '@/stores/config'

/**
 * Tokeniser approximations.
 *
 * These are rules of thumb for common tokenisers (BPE families used by the
 * major vendors), NOT a real tokeniser. A real one would mean shipping a
 * vocabulary and a WASM runtime for a hint line, which is not worth it.
 *
 *   - CJK / kana / hangul: roughly one token per character. These scripts
 *     carry far more information per glyph, and BPE vocabularies typically
 *     hold whole common words, so ~1 token/char is the usual ballpark.
 *   - Latin letters, digits and punctuation: roughly one token per four
 *     characters. Note this includes printable ASCII PUNCTUATION, not just
 *     letters: the familiar "~4 characters per token" rule of thumb is measured
 *     on ordinary prose, punctuation included, and punctuation is usually
 *     absorbed into neighbouring tokens rather than standing alone.
 *   - Everything else (emoji, symbols, combining marks): two tokens each. This
 *     matches the observed behaviour of splitting such code points into
 *     multiple byte-level tokens, and it is deliberately pessimistic so the
 *     estimate errs high rather than low.
 */
export const CJK_TOKENS_PER_CHAR = 1
export const LATIN_CHARS_PER_TOKEN = 4
export const OTHER_TOKENS_PER_CHAR = 2

/**
 * Small fixed allowance for the chat framing every LLM request carries
 * (role markers, separators). Immaterial next to real text, but it keeps tiny
 * inputs from estimating as exactly zero.
 */
export const CHAT_OVERHEAD_TOKENS = 8

/**
 * Output-token ceiling used when the caller does not supply one.
 *
 * Mirrors the backend schema default, so the preview matches what a request
 * would actually be capped at.
 */
export const DEFAULT_MAX_TOKENS = 1024

/** Character breakdown of the input text. */
export interface CharacterBreakdown {
  /** Every character, whitespace included. */
  total: number
  /** Characters that are not whitespace — what actually gets spoken. */
  nonWhitespace: number
  /** CJK ideographs, kana and hangul. */
  cjk: number
  /** Latin letters, digits and punctuation. */
  latin: number
  /** Anything else: emoji, symbols, combining marks. */
  other: number
}

/** Inputs for {@link estimateUsage}. */
export interface EstimateInput {
  /** The raw text the user typed. */
  text: string
  /**
   * Per-request character limit.
   *
   * Callers MUST pass the provider-derived limit (`configStore.segmentLimit`)
   * and not a constant: providers declare their own `max_chars`, and a preview
   * that divides by a different number than the submit path uses would promise
   * one thing and send another — the exact mismatch behind the max_chars bug.
   */
  limit: number
  /** Generation mode. Only `tts` uses the LLM. */
  mode: GenerationMode
  /**
   * Set when the run reuses an existing reply (`reuse_reply_text`), which skips
   * the LLM entirely even in `tts` mode.
   */
  reuseReplyText?: string | null
  /** Optional system prompt; its tokens count towards the input. */
  systemPrompt?: string | null
  /** Configured max_tokens, used as the output ceiling. */
  maxTokens?: number | null
  /** Total ceiling for one segmented run, used for the over-limit warning. */
  totalCeiling?: number | null
}

/** The preview figures. Nullable fields are null when they do not apply. */
export interface UsageEstimate extends CharacterBreakdown {
  /** Tokens for the user's text alone (excludes the system prompt). */
  textTokens: number
  /** True when the LLM step runs at all. */
  usesLlm: boolean
  /** Input tokens including system prompt and framing; null when LLM is skipped. */
  llmInputTokens: number | null
  /** Configured output ceiling; null when LLM is skipped. */
  llmOutputTokensCeiling: number | null
  /** Rough reply size, capped by the ceiling; null when LLM is skipped. */
  llmOutputTokensEstimate: number | null
  /** How many requests the text becomes. */
  segmentCount: number
  /** The actual pieces, exactly as the submit path would send them. */
  segments: string[]
  /** Length of each piece, for a per-segment view. */
  segmentLengths: number[]
  /** True when the text needs more than one request. */
  needsSegmentation: boolean
  /** True when the text exceeds the total ceiling for a single run. */
  exceedsTotalCeiling: boolean
}

const WHITESPACE = /\s/u
const CJK =
  /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]/u
/**
 * Printable ASCII (0x21-0x7E: letters, digits, punctuation, symbols) plus
 * Latin-1/Extended letters. Punctuation belongs here rather than in the
 * "other" bucket: the ~4-chars-per-token rule is measured on prose that
 * includes it, and the "other" bucket costs 2 tokens per character, which
 * would badly over-estimate any normal English sentence.
 */
const LATIN = /[\x21-\x7e\u00c0-\u024f]/

/**
 * Count characters by class.
 *
 * Iterates by code POINT (`Array.from`), not by UTF-16 code unit, so an emoji
 * or any other astral-plane character counts once instead of twice.
 */
export function countCharacters(text: string): CharacterBreakdown {
  let total = 0
  let nonWhitespace = 0
  let cjk = 0
  let latin = 0
  let other = 0

  for (const char of Array.from(text)) {
    total += 1
    if (WHITESPACE.test(char)) continue
    nonWhitespace += 1
    if (CJK.test(char)) cjk += 1
    else if (LATIN.test(char)) latin += 1
    else other += 1
  }

  return { total, nonWhitespace, cjk, latin, other }
}

/**
 * Approximate token count for a string.
 *
 * Always rounds UP: an estimate that is too low is worse than one that is too
 * high, because the user is deciding whether the run is affordable.
 */
export function estimateTokens(text: string): number {
  if (!text) return 0
  const { cjk, latin, other } = countCharacters(text)
  const raw =
    cjk * CJK_TOKENS_PER_CHAR +
    latin / LATIN_CHARS_PER_TOKEN +
    other * OTHER_TOKENS_PER_CHAR
  return Math.ceil(raw)
}

/** Whether this configuration calls the LLM at all. */
export function usesLlm(mode: GenerationMode, reuseReplyText?: string | null): boolean {
  // Only `tts` asks the model to write a reply; `clone` and `text2audio` voice
  // the typed text verbatim. Reusing an existing reply skips it even in `tts`.
  if (mode !== 'tts') return false
  return !(reuseReplyText && reuseReplyText.trim().length > 0)
}

/**
 * Group a number for display: `1234` -> `1,234`.
 *
 * Hand-rolled rather than `toLocaleString()` so the output is identical in the
 * browser and under vitest's node environment (locale data differs).
 */
export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  const rounded = Math.trunc(Math.abs(value))
  const sign = value < 0 ? '-' : ''
  return sign + String(rounded).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** One-line summary of the token estimate for the UI. */
export function describeTokenEstimate(estimate: UsageEstimate): string {
  if (!estimate.usesLlm) return '跳过语言模型，不消耗 token'
  return `预估输入 token 约 ${formatNumber(estimate.llmInputTokens ?? 0)}`
}

/**
 * Compute the preview.
 *
 * Segmentation reuses `segmentText()` rather than reimplementing it, and is
 * given the caller's limit, so the piece count and the pieces themselves are
 * exactly what the submit path will produce.
 */
export function estimateUsage(input: EstimateInput): UsageEstimate {
  const raw = input.text ?? ''
  const breakdown = countCharacters(raw)
  const trimmed = raw.trim()
  const textTokens = estimateTokens(trimmed)

  // Clamp the limit: a zero/NaN limit would make segmentText cut per character
  // and report a nonsense segment count.
  const limit =
    Number.isFinite(input.limit) && Math.floor(input.limit) > 0
      ? Math.floor(input.limit)
      : 1

  const segmentation = segmentText(raw, { limit })
  const segments = segmentation.segments
  const segmentLengths = segments.map((piece) => piece.length)

  const llm = usesLlm(input.mode, input.reuseReplyText)

  const ceilingRaw = input.maxTokens
  const ceiling =
    typeof ceilingRaw === 'number' && Number.isFinite(ceilingRaw) && ceilingRaw > 0
      ? Math.floor(ceilingRaw)
      : DEFAULT_MAX_TOKENS

  // Output size heuristic: a spoken reply is typically about as long as the
  // text it is derived from, so reuse the text's token count as the guess and
  // let the configured ceiling cap it. Labelled as an estimate in the UI.
  const outputGuess = textTokens

  const ceilingTotal = input.totalCeiling
  const exceedsTotalCeiling =
    typeof ceilingTotal === 'number' &&
    Number.isFinite(ceilingTotal) &&
    ceilingTotal > 0 &&
    raw.length > ceilingTotal

  return {
    ...breakdown,
    textTokens,
    usesLlm: llm,
    llmInputTokens: llm
      ? textTokens + estimateTokens(input.systemPrompt ?? '') + CHAT_OVERHEAD_TOKENS
      : null,
    llmOutputTokensCeiling: llm ? ceiling : null,
    llmOutputTokensEstimate: llm ? Math.min(ceiling, outputGuess) : null,
    segmentCount: segments.length,
    segments,
    segmentLengths,
    needsSegmentation: !segmentation.single && segments.length > 1,
    exceedsTotalCeiling,
  }
}
