/**
 * The real per-request character limit for the selected TTS provider.
 *
 * Why this module exists
 * ----------------------
 * Long text is split into segments by `utils/segment.ts`, and each segment is
 * sent as its own request. That split used a hardcoded 5000 characters — the
 * limit the *pipeline* enforces — while every TTS provider additionally
 * declares its own `max_chars`, which is often far tighter:
 *
 *     volcengine 1024   edge 3000   mimo 4000   openai 4096
 *     azure / elevenlabs / minimax / mock 5000
 *
 * Nothing read that value, so for most providers the frontend produced
 * segments the backend was guaranteed to reject. This was reproducible with
 * real credentials: 7500 characters were split at 5000 into pieces of ~4991
 * and ~2509, and the very first request came back
 *
 *     TTS_TEXT_TOO_LONG: 文本长度 4991 超过 小米 MiMo TTS 单次合成上限 4000 字符
 *
 * i.e. a long-text run died on segment 1, after the user had already been
 * charged for the attempt. For volcengine (1024) no 5000-character segment
 * could ever succeed.
 *
 * The provider's own `max_chars` is therefore the source of truth, and the
 * effective limit is the *smaller* of it and the pipeline limit.
 *
 * `max_chars` arrives on `GET /providers` (`ProvidersResponse.tts[].max_chars`).
 * It is deliberately NOT read from `GET /capabilities`, which does not carry it.
 */
import type { ProvidersResponse } from '@/api/types'

/**
 * Pipeline-wide per-request limit, used when a provider declares nothing.
 *
 * Kept in sync with `MAX_TEXT_LENGTH` by the store, which passes the value in;
 * this fallback only exists so the helper is safe to call with no provider.
 */
export const DEFAULT_SEGMENT_LIMIT = 5000

/**
 * Smallest limit we will ever hand to the splitter.
 *
 * A provider reporting 0/negative/missing must never collapse the limit to
 * zero: `segmentText` would then have to cut every character into its own
 * request, turning one click into thousands of vendor calls. Values below this
 * floor are treated as "not reported" and fall back instead.
 */
export const MIN_SEGMENT_LIMIT = 100

/**
 * Resolve the per-segment character limit for the selected TTS provider.
 *
 * @param providers      `GET /providers` payload, or null while it loads.
 * @param ttsProviderId  Selected TTS provider id ('' when unset).
 * @param pipelineLimit  The pipeline-wide limit to clamp against (MAX_TEXT_LENGTH).
 * @returns The effective limit: `min(pipelineLimit, provider.max_chars)`, or
 *          `pipelineLimit` when the provider or its `max_chars` is unusable.
 *
 * Never returns <= 0, so callers can pass the result straight to `segmentText`.
 */
export function effectiveSegmentLimit(
  providers: ProvidersResponse | null | undefined,
  ttsProviderId: string | null | undefined,
  pipelineLimit: number = DEFAULT_SEGMENT_LIMIT,
): number {
  // Clamp the pipeline limit itself: a bad value here must not poison the result.
  const fallback =
    Number.isFinite(pipelineLimit) && Math.floor(pipelineLimit) >= MIN_SEGMENT_LIMIT
      ? Math.floor(pipelineLimit)
      : DEFAULT_SEGMENT_LIMIT

  if (!providers || !ttsProviderId) return fallback

  const provider = providers.tts.find((item) => item.id === ttsProviderId)
  const declared = provider?.max_chars

  // Missing / non-numeric / non-positive / absurdly small all mean "not usable",
  // so fall back rather than trusting a meaningless number.
  if (typeof declared !== 'number' || !Number.isFinite(declared)) return fallback
  if (declared < MIN_SEGMENT_LIMIT) return fallback

  return Math.min(fallback, Math.floor(declared))
}

/**
 * How many segments `text` becomes under the selected provider.
 *
 * Exists so the count shown in the UI ("将自动分为 N 段生成") is derived from
 * the *same* limit that the submit path uses. Reporting one number and sending
 * another is what made the original bug so confusing: the badge promised 2
 * segments while the backend rejected segment 1 outright.
 */
export function segmentCountFor(
  text: string,
  providers: ProvidersResponse | null | undefined,
  ttsProviderId: string | null | undefined,
  pipelineLimit: number = DEFAULT_SEGMENT_LIMIT,
): number {
  const trimmed = text.trim()
  if (trimmed.length === 0) return 0
  const limit = effectiveSegmentLimit(providers, ttsProviderId, pipelineLimit)
  if (trimmed.length <= limit) return 1
  // Ceil-free lower bound; the real splitter may produce a few more pieces
  // because it prefers natural boundaries. The UI uses segmentText() for the
  // exact figure -- this is a cheap estimate for callers that cannot import it.
  return Math.ceil(trimmed.length / limit)
}
