/**
 * Typed API client for the Text2Voice Agent backend.
 *
 * Every function rejects with an `ApiError` (see `./http`).
 */
import { del, get, http, post, put } from './http'
import type {
  CapabilitiesResponse,
  CloneVoiceRequest,
  ClonedVoice,
  DebugCaptureResponse,
  DeleteHistoryBulkRequest,
  DeleteHistoryBulkResponse,
  EnhanceMediaResponse,
  FetchModelsRequest,
  FetchModelsResponse,
  GenerateRequest,
  GenerateResponse,
  HealthResponse,
  HistoryDetail,
  HistoryListResponse,
  HistoryQuery,
  MediaInfo,
  ProbePathsRequest,
  ProbePathsResponse,
  ProvidersResponse,
  ServerSettings,
  TaskStatusResponse,
  TaskSubmitResponse,
  TrimMediaRequest,
  TrimMediaResponse,
  UpdateSettingsRequest,
  ValidateProviderRequest,
  ValidateProviderResponse,
  VoiceListResponse,
} from './types'

export * from './types'
export {
  ApiError,
  API_BASE_URL,
  DEFAULT_REQUEST_TIMEOUT_MS,
  createRequestId,
  getRequestTimeout,
  http,
  isApiError,
  isApiErrorCode,
  secondsToTimeoutMs,
  setRequestTimeout,
  toApiError,
} from './http'

/* ------------------------------------------------------------------ */
/* Providers                                                           */
/* ------------------------------------------------------------------ */

/** `GET /providers` — available LLM and TTS providers with their options. */
export function fetchProviders(): Promise<ProvidersResponse> {
  return get<ProvidersResponse>('/providers')
}

/** `POST /providers/validate` — verify credentials / connectivity. */
export function validateProvider(payload: ValidateProviderRequest): Promise<ValidateProviderResponse> {
  return post<ValidateProviderResponse>('/providers/validate', payload)
}

/** `POST /providers/models` — fetch model list from a custom OpenAI-compatible endpoint. */
export function fetchModels(payload: FetchModelsRequest): Promise<FetchModelsResponse> {
  return post<FetchModelsResponse>('/providers/models', payload)
}

/** `POST /providers/probe-paths` — discover which TTS routes a custom endpoint exposes. */
export function probePaths(payload: ProbePathsRequest): Promise<ProbePathsResponse> {
  return post<ProbePathsResponse>('/providers/probe-paths', payload)
}

/**
 * `GET /meta/debug-capture` — read the outbound request capture.
 *
 * `enabled` toggles recording (clearing history when switched on); `clear`
 * empties it. Credentials are redacted by the backend before capture.
 */
export function fetchDebugCapture(
  params: { enabled?: boolean; clear?: boolean } = {},
): Promise<DebugCaptureResponse> {
  return get<DebugCaptureResponse>('/meta/debug-capture', params)
}

/* ------------------------------------------------------------------ */
/* Capabilities                                                        */
/* ------------------------------------------------------------------ */

/** `GET /capabilities` — runtime probe (ffmpeg availability, clone support). */
export function fetchCapabilities(): Promise<CapabilitiesResponse> {
  return get<CapabilitiesResponse>('/capabilities')
}

/* ------------------------------------------------------------------ */
/* Media (upload / inspect / trim)                                     */
/* ------------------------------------------------------------------ */

/** `POST /media/inspect` — upload a file and read its metadata. */
export async function inspectMedia(file: File, onProgress?: (percent: number) => void): Promise<MediaInfo> {
  const form = new FormData()
  form.append('file', file)
  // Do NOT set Content-Type manually: the browser must add the multipart
  // boundary itself, otherwise the server cannot parse the file field.
  const response = await http.post<MediaInfo>('/media/inspect', form, {
    onUploadProgress: (event) => {
      if (!onProgress || !event.total) return
      onProgress(Math.round((event.loaded / event.total) * 100))
    },
  })
  return response.data
}

/** `POST /media/trim` — cut a window out of an uploaded file. */
export function trimMedia(payload: TrimMediaRequest): Promise<TrimMediaResponse> {
  return post<TrimMediaResponse>('/media/trim', payload)
}

/**
 * `POST /media/enhance` — produce a clone-friendly copy of a reference clip
 * (silence trimmed, loudness normalized). Returns a NEW media id.
 */
export function enhanceMedia(mediaId: string): Promise<EnhanceMediaResponse> {
  return post<EnhanceMediaResponse>('/media/enhance', { media_id: mediaId })
}

/** `DELETE /media/{id}` — discard an uploaded file. */
export function deleteMedia(mediaId: string): Promise<void> {
  return del<void>(`/media/${encodeURIComponent(mediaId)}`)
}

/** Absolute URL for previewing uploaded / trimmed media in `<audio>`/`<video>`. */
export function mediaUrl(previewUrl: string): string {
  const base = import.meta.env.VITE_API_BASE_URL || '/api/v1'
  return previewUrl.startsWith('/api/') ? previewUrl : `${base}${previewUrl.replace(/^\/api\/v1/, '')}`
}

/* ------------------------------------------------------------------ */
/* Cloned voices                                                       */
/* ------------------------------------------------------------------ */

/** `GET /voices` — locally stored cloned voices (never contains keys). */
export function fetchVoices(): Promise<VoiceListResponse> {
  return get<VoiceListResponse>('/voices')
}

/** `POST /voices/clone` — create a voice from a reference clip. */
export function cloneVoice(payload: CloneVoiceRequest): Promise<ClonedVoice> {
  return post<ClonedVoice>('/voices/clone', payload)
}

/** `DELETE /voices/{id}` — remove a cloned voice. */
export function deleteVoice(id: number): Promise<void> {
  return del<void>(`/voices/${id}`)
}

/* ------------------------------------------------------------------ */
/* Generation                                                          */
/* ------------------------------------------------------------------ */

/** `POST /tasks` — submit an asynchronous generation task. */
export function submitTask(payload: GenerateRequest): Promise<TaskSubmitResponse> {
  return post<TaskSubmitResponse>('/tasks', payload)
}

/** `GET /tasks/{task_id}` — poll task status / stage / progress. */
export function fetchTask(taskId: string): Promise<TaskStatusResponse> {
  return get<TaskStatusResponse>(`/tasks/${encodeURIComponent(taskId)}`)
}

/** `GET /tasks/{task_id}/result` — fetch the finished result (409 while running). */
export function fetchTaskResult(taskId: string): Promise<GenerateResponse> {
  return get<GenerateResponse>(`/tasks/${encodeURIComponent(taskId)}/result`)
}

/** `DELETE /tasks/{task_id}` — cancel a pending/running task. */
export function cancelTask(taskId: string): Promise<TaskStatusResponse> {
  return del<TaskStatusResponse>(`/tasks/${encodeURIComponent(taskId)}`)
}

/** `POST /generate` — synchronous generation variant. */
export function generateSync(payload: GenerateRequest): Promise<GenerateResponse> {
  return post<GenerateResponse>('/generate', payload)
}

/* ------------------------------------------------------------------ */
/* History                                                             */
/* ------------------------------------------------------------------ */

/** `GET /history` — paginated, searchable history list. */
export function fetchHistory(query: HistoryQuery): Promise<HistoryListResponse> {
  const params: Record<string, unknown> = {
    page: query.page,
    page_size: query.page_size,
  }
  if (query.q) params.q = query.q
  if (query.status) params.status = query.status
  return get<HistoryListResponse>('/history', params)
}

/** `GET /history/{id}` — single record with latency details. */
export function fetchHistoryDetail(id: number): Promise<HistoryDetail> {
  return get<HistoryDetail>(`/history/${id}`)
}

/** `DELETE /history/{id}` — delete one record (HTTP 204). */
export function deleteHistoryItem(id: number): Promise<void> {
  return del<void>(`/history/${id}`)
}

/** `DELETE /history` — delete a set of records or everything. */
export function deleteHistoryBulk(
  payload: DeleteHistoryBulkRequest,
): Promise<DeleteHistoryBulkResponse> {
  return del<DeleteHistoryBulkResponse>('/history', payload)
}

/** `GET /audio/{id}` — absolute URL usable directly as an `<audio src>`. */
export function audioUrl(id: number, download = false): string {
  return `${import.meta.env.VITE_API_BASE_URL || '/api/v1'}/audio/${id}${download ? '?download=1' : ''}`
}

/**
 * `GET /audio/{id}` — fetch the raw bytes as a Blob.
 * Useful when the audio must be handed to `URL.createObjectURL`.
 */
export async function fetchAudioBlob(id: number): Promise<Blob> {
  const response = await http.get<Blob>(`/audio/${id}`, { responseType: 'blob' })
  return response.data
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

/** `GET /settings` — server-side defaults (never contains API keys). */
export function fetchSettings(): Promise<ServerSettings> {
  return get<ServerSettings>('/settings')
}

/** `PUT /settings` — partial update, returns the full settings object. */
export function updateSettings(payload: UpdateSettingsRequest): Promise<ServerSettings> {
  return put<ServerSettings>('/settings', payload)
}

/* ------------------------------------------------------------------ */
/* Health                                                              */
/* ------------------------------------------------------------------ */

/** `GET /health` — backend liveness probe used by the header indicator. */
export function fetchHealth(): Promise<HealthResponse> {
  return get<HealthResponse>('/health')
}