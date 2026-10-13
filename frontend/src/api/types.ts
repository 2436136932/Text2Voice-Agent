/**
 * TypeScript contracts for the Text2Voice Agent backend API.
 *
 * Every exported signature in `src/api` uses these types — no `any`.
 */

/* ------------------------------------------------------------------ */
/* Errors                                                              */
/* ------------------------------------------------------------------ */

/** The fixed error envelope returned by the backend with a non-2xx status. */
export interface ApiErrorEnvelope {
  code: string
  message: string
  detail: Record<string, unknown> | null
  request_id: string | null
}

/** Machine-readable error codes documented by the backend. */
export type ApiErrorCode =
  | 'TTS_AUTH_FAILED'
  | 'LLM_AUTH_FAILED'
  | 'LLM_RATE_LIMITED'
  | 'TASK_NOT_FINISHED'
  | 'TASK_NOT_FOUND'
  | 'VALIDATION_ERROR'
  | 'PROVIDER_NOT_SUPPORTED'
  | 'UPSTREAM_TIMEOUT'
  | 'NETWORK_ERROR'
  | 'REQUEST_TIMEOUT'
  | 'UNKNOWN_ERROR'
  | (string & {})

/* ------------------------------------------------------------------ */
/* Providers                                                           */
/* ------------------------------------------------------------------ */

export type ProviderKind = 'llm' | 'tts'

export interface ModelOption {
  id: string
  name: string
}

export type VoiceGender = 'male' | 'female' | 'neutral' | (string & {})

export interface VoiceOption {
  id: string
  name: string
  gender: VoiceGender
  language: string
  description?: string
  /** True for voices created in the voice-clone flow (stored locally). */
  cloned?: boolean
}

export interface LLMProvider {
  id: string
  name: string
  default_model: string
  models: ModelOption[]
  requires_base_url: boolean
  supports_base_url: boolean
  api_key_hint: string
  docs_url: string | null
  /** Free-form Chinese description shown under the provider selector. */
  notes?: string
  /** False for locally hosted providers where the key may be left empty. */
  requires_api_key?: boolean
}

export interface TTSProvider {
  id: string
  name: string
  default_voice: string
  voices: VoiceOption[]
  requires_base_url: boolean
  supports_base_url: boolean
  supports_speed: boolean
  speed_min: number
  speed_max: number
  formats: string[]
  api_key_hint: string
  docs_url: string | null
  notes?: string
  requires_api_key?: boolean
  /** Vendor exposes a real voice-clone API. */
  supports_voice_clone?: boolean
  /** Can take a reference clip at synthesis time (prompt-based cloning). */
  supports_reference_audio?: boolean
  /** Models that can be used for cloning. */
  clone_models?: ModelOption[]
  /** Friendly Chinese text telling the user which model to pick for cloning. */
  clone_hint?: string
  models?: ModelOption[]
  max_chars?: number
}

export interface ProvidersResponse {
  llm: LLMProvider[]
  tts: TTSProvider[]
}

/* ------------------------------------------------------------------ */
/* Runtime capabilities                                                */
/* ------------------------------------------------------------------ */

export interface FfmpegCapability {
  available: boolean
  path: string | null
  version: string | null
  message: string
}

export interface VoiceCloneCapability {
  supported_providers: string[]
  reference_audio_providers: string[]
  note: string
}

export interface UploadCapability {
  accept: string
  max_size_mb: number
  trimmable_formats: string[]
  video_extraction_available: boolean
}

export interface CapabilitiesResponse {
  ffmpeg: FfmpegCapability
  voice_clone: VoiceCloneCapability
  upload: UploadCapability
}

/* ------------------------------------------------------------------ */
/* Media (upload / inspect / trim)                                     */
/* ------------------------------------------------------------------ */

export type MediaKind = 'audio' | 'video'

export interface MediaInfo {
  media_id: string
  filename: string
  content_type: string
  kind: MediaKind
  size_bytes: number
  duration_ms: number | null
  sample_rate: number | null
  channels: number | null
  format: string
  trimmable: boolean
  preview_url: string
  warnings?: string[]
}

export interface TrimMediaRequest {
  media_id: string
  start_ms: number
  end_ms: number
  output_format?: string
}

export interface TrimMediaResponse {
  media_id: string
  preview_url: string
  duration_ms: number | null
  size_bytes: number
  format: string
}

/* ------------------------------------------------------------------ */
/* Cloned voices                                                       */
/* ------------------------------------------------------------------ */

export interface ClonedVoice {
  id: number
  voice_id: string
  provider: string
  name: string
  model: string
  duration_ms: number | null
  created_at: string
}

export interface VoiceListResponse {
  items: ClonedVoice[]
}

export interface CloneVoiceRequest {
  provider: string
  api_key: string
  base_url?: string | null
  name: string
  media_id: string
  model?: string | null
  extra?: Record<string, unknown>
}

/* ------------------------------------------------------------------ */
/* Provider validation                                                 */
/* ------------------------------------------------------------------ */

export interface ValidateProviderRequest {
  kind: ProviderKind
  provider: string
  api_key: string
  base_url?: string | null
  model?: string | null
  voice?: string | null
}

export interface ValidateProviderResponse {
  valid: boolean
  message: string
  latency_ms: number | null
  provider: string
  kind: ProviderKind
}

/* ------------------------------------------------------------------ */
/* Fetch models from custom endpoint                                    */
/* ------------------------------------------------------------------ */

export interface FetchModelsRequest {
  base_url: string
  api_key: string
  kind: 'llm' | 'tts'
}

export interface FetchedModel {
  id: string
  name: string
  supports_tts: boolean
  owned_by: string
}

export interface FetchModelsResponse {
  models: FetchedModel[]
  total: number
  base_url: string
}

/* ------------------------------------------------------------------ */
/* Endpoint path discovery                                             */
/* ------------------------------------------------------------------ */

export interface ProbePathsRequest {
  base_url: string
  api_key: string
}

export interface ProbePathItem {
  path: string
  status: number | null
  alive: boolean
  note: string
}

export interface ProbePathsResponse {
  base_url: string
  results: ProbePathItem[]
  suggested_path: string | null
}

/* ------------------------------------------------------------------ */
/* Generation                                                          */
/* ------------------------------------------------------------------ */

export interface LLMRequestConfig {
  provider: string
  api_key: string
  model: string
  base_url: string | null
  temperature: number
  max_tokens: number
  system_prompt: string
}

export interface TTSRequestConfig {
  provider: string
  api_key: string
  voice: string
  /** TTS model id — required by OpenAI-compatible endpoints (e.g. mimo-v2.5-tts). */
  model: string
  speed: number
  format: string
  base_url: string | null
  extra: Record<string, unknown>
}

export interface GenerateOptions {
  auto_save: boolean
  include_audio_base64: boolean
  fallback_enabled: boolean
  /**
   * When set, the backend skips the LLM entirely and synthesizes this text
   * verbatim. Used by the "仅重新合成语音" action and the 直接配音 mode.
   */
  reuse_reply_text?: string | null
  /**
   * Media id of a reference clip (uploaded audio/video) used for voice
   * cloning. Only meaningful for providers that declare clone or
   * reference-audio support.
   */
  reference_media_id?: string | null
}

export interface GenerateRequest {
  text: string
  llm: LLMRequestConfig
  tts: TTSRequestConfig
  options: GenerateOptions
}

export type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'canceled'

export type TaskStage = 'queued' | 'llm' | 'tts' | 'persist' | 'done'

/** 202 Accepted response of `POST /tasks`. */
export interface TaskSubmitResponse {
  task_id: string
  status: TaskStatus
  stage: TaskStage
  progress: number
  message: string
}

/** `GET /tasks/{task_id}` */
export interface TaskStatusResponse {
  task_id: string
  status: TaskStatus
  stage: TaskStage
  progress: number
  message: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  elapsed_ms: number | null
  error: ApiErrorEnvelope | null
}

export interface LLMUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface GenerateLLMInfo {
  provider: string
  model: string
  latency_ms: number
  usage: LLMUsage | null
}

export interface GenerateTTSInfo {
  provider: string
  voice: string
  latency_ms: number
}

/** `POST /generate` and `GET /tasks/{task_id}/result`. */
export interface GenerateResponse {
  record_id: number | null
  reply_text: string
  input_text: string
  audio_url: string
  audio_format: string
  audio_bytes: number
  audio_base64: string | null
  duration_ms: number | null
  degraded: boolean
  degraded_reason: string | null
  llm: GenerateLLMInfo | null
  tts: GenerateTTSInfo | null
  total_latency_ms: number | null
  created_at: string | null
}

/* ------------------------------------------------------------------ */
/* History                                                             */
/* ------------------------------------------------------------------ */

export interface HistoryItem {
  id: number
  input_text: string
  reply_text: string
  llm_provider: string
  llm_model: string
  tts_provider: string
  tts_voice: string
  audio_format: string
  audio_bytes: number | null
  duration_ms: number | null
  status: TaskStatus
  degraded: boolean
  error_code: string | null
  created_at: string
  audio_url: string
}

/** `GET /history/{id}` — list item plus detail-only fields. */
export interface HistoryDetail extends HistoryItem {
  total_latency_ms: number | null
  llm_latency_ms: number | null
  tts_latency_ms: number | null
  error_message: string | null
}

export interface HistoryQuery {
  page: number
  page_size: number
  q?: string
  status?: TaskStatus | ''
}

export interface HistoryListResponse {
  items: HistoryItem[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface DeleteHistoryBulkRequest {
  ids?: number[]
  all?: boolean
}

export interface DeleteHistoryBulkResponse {
  deleted: number
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

export interface ServerSettings {
  default_llm_provider: string
  default_llm_model: string
  default_tts_provider: string
  default_tts_voice: string
  default_speed: number
  default_system_prompt: string
  auto_save_history: boolean
  auto_save_credentials: boolean
  request_timeout_seconds: number
  updated_at: string | null
}

/** Partial body accepted by `PUT /settings`. */
export type UpdateSettingsRequest = Partial<
  Pick<
    ServerSettings,
    | 'default_llm_provider'
    | 'default_llm_model'
    | 'default_tts_provider'
    | 'default_tts_voice'
    | 'default_speed'
    | 'default_system_prompt'
    | 'auto_save_history'
    | 'auto_save_credentials'
    | 'request_timeout_seconds'
  >
>

/* ------------------------------------------------------------------ */
/* Health                                                              */
/* ------------------------------------------------------------------ */

export interface HealthResponse {
  status: string
  version: string
  uptime_seconds: number
}