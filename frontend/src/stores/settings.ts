/**
 * Server-side settings store.
 *
 * Only the local `autoSaveCredentials` mirror is persisted to localStorage
 * (through `pinia-plugin-persistedstate`); the server settings themselves are
 * always re-read from `GET /settings`. API keys never pass through this store.
 */
import { defineStore } from 'pinia'
import { fetchSettings, secondsToTimeoutMs, updateSettings } from '@/api'
import { DEFAULT_REQUEST_TIMEOUT_MS, setRequestTimeout } from '@/api/http'
import type { ServerSettings, UpdateSettingsRequest } from '@/api/types'

/** Local-storage key used by the persisted plugin. */
export const SETTINGS_PERSIST_KEY = 't2v:settings'

export interface SettingsState {
  /** Last settings object returned by the backend (never contains keys). */
  server: ServerSettings | null
  /** Local mirror of `auto_save_credentials`, persisted across reloads. */
  autoSaveCredentials: boolean
  loading: boolean
  saving: boolean
  loaded: boolean
  error: string | null
  /**
   * Defaults staged from the provider panels ("设为默认").
   *
   * Kept separate from `server` so the user can stage several changes and then
   * commit them with a single 保存 — nothing is sent until they do.
   */
  pendingDefaults: PendingDefaults
}

export interface PendingDefaults {
  llmProvider: string | null
  llmModel: string | null
  ttsProvider: string | null
  ttsVoice: string | null
}

export const useSettingsStore = defineStore('settings', {
  state: (): SettingsState => ({
    server: null,
    autoSaveCredentials: false,
    loading: false,
    saving: false,
    loaded: false,
    error: null,
    pendingDefaults: {
      llmProvider: null,
      llmModel: null,
      ttsProvider: null,
      ttsVoice: null,
    },
  }),

  getters: {
    /** Effective settings, falling back to documented defaults. */
    effective(state): ServerSettings {
      return (
        state.server ?? {
          default_llm_provider: 'openai',
          default_llm_model: 'gpt-4o-mini',
          default_tts_provider: 'openai',
          default_tts_voice: 'alloy',
          default_speed: 1.0,
          default_system_prompt: '你是一个乐于助人的助手。',
          auto_save_history: true,
          auto_save_credentials: state.autoSaveCredentials,
          request_timeout_seconds: DEFAULT_REQUEST_TIMEOUT_MS / 1000,
          updated_at: null,
        }
      )
    },

    /** Request timeout in milliseconds derived from the server setting. */
    requestTimeoutMs(state): number {
      return state.server
        ? secondsToTimeoutMs(state.server.request_timeout_seconds)
        : DEFAULT_REQUEST_TIMEOUT_MS
    },

    /** Whether history is auto-saved by the backend. */
    autoSaveHistory(): boolean {
      return this.effective.auto_save_history
    },
  },

  actions: {
    /** `GET /settings`, then push the timeout into the axios instance. */
    async load(): Promise<ServerSettings | null> {
      this.loading = true
      this.error = null
      try {
        const settings = await fetchSettings()
        this.applyServerSettings(settings)
        return settings
      } catch (error) {
        this.error = error instanceof Error ? error.message : '加载设置失败'
        setRequestTimeout(DEFAULT_REQUEST_TIMEOUT_MS)
        return null
      } finally {
        this.loading = false
        this.loaded = true
      }
    },

    /** `PUT /settings` with a partial body; returns the full settings object. */
    async save(patch: UpdateSettingsRequest): Promise<ServerSettings | null> {
      this.saving = true
      this.error = null
      try {
        const settings = await updateSettings(patch)
        this.applyServerSettings(settings)
        return settings
      } catch (error) {
        this.error = error instanceof Error ? error.message : '保存设置失败'
        return null
      } finally {
        this.saving = false
      }
    },

    /** Store a settings payload and sync dependent state. */
    applyServerSettings(settings: ServerSettings): void {
      this.server = settings
      this.autoSaveCredentials = settings.auto_save_credentials
      setRequestTimeout(secondsToTimeoutMs(settings.request_timeout_seconds))
    },

    /** Toggle the local credential-persistence mirror (does not touch the API). */
    setAutoSaveCredentials(value: boolean): void {
      this.autoSaveCredentials = value
    },

    /**
     * Merge the staged defaults into a `PUT /settings` patch.
     *
     * Returns only the fields the user actually staged, so an unrelated save
     * never overwrites a default they did not touch.
     */
    defaultsPatch(): UpdateSettingsRequest {
      const patch: UpdateSettingsRequest = {}
      if (this.pendingDefaults.llmProvider) patch.default_llm_provider = this.pendingDefaults.llmProvider
      if (this.pendingDefaults.llmModel) patch.default_llm_model = this.pendingDefaults.llmModel
      if (this.pendingDefaults.ttsProvider) patch.default_tts_provider = this.pendingDefaults.ttsProvider
      if (this.pendingDefaults.ttsVoice) patch.default_tts_voice = this.pendingDefaults.ttsVoice
      return patch
    },

    /** Drop every staged default (after a successful save or a reset). */
    clearPendingDefaults(): void {
      this.pendingDefaults = {
        llmProvider: null,
        llmModel: null,
        ttsProvider: null,
        ttsVoice: null,
      }
    },
  },

  persist: {
    key: SETTINGS_PERSIST_KEY,
    // ONLY the local credential-storage preference is persisted.
    paths: ['autoSaveCredentials'],
  },
})