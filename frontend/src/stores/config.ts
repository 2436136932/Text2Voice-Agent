/**
 * Generation configuration store.
 *
 * Holds the provider catalogue, the user-typed API keys (in memory only) and
 * every parameter needed to build a `GenerateRequest`. The typed text lives here
 * too, so navigating between pages never loses it.
 *
 * SECURITY: keys are never logged and never sent anywhere except the endpoints
 * that legitimately need them (`POST /providers/validate`, `POST /tasks`,
 * `POST /generate`). They are written to localStorage ONLY when the user enables
 * `auto_save_credentials`.
 */
import { defineStore } from 'pinia'
import { fetchProviders } from '@/api'
import type {
  GenerateOptions,
  GenerateRequest,
  LLMProvider,
  ProvidersResponse,
  ServerSettings,
  TTSProvider,
  VoiceOption,
} from '@/api/types'

/** localStorage key for the opt-in credential cache. */
export const CREDENTIALS_STORAGE_KEY = 't2v:credentials'

/** Maximum accepted input length (mirrors the backend limit). */
export const MAX_TEXT_LENGTH = 5000

/**
 * Generation modes.
 *
 * * `tts`     — plain text-to-speech: the LLM writes a reply and the TTS
 *               provider speaks it.
 * * `clone`   — voice cloning: a reference clip (audio or video) defines the
 *               timbre, and the typed text is spoken in that voice.
 * * `text2audio` — skip the LLM entirely and speak the typed text verbatim.
 *               Useful when the user already has the exact script.
 */
export type GenerationMode = 'tts' | 'clone' | 'text2audio'

/** Shape of the localStorage configuration cache. */
interface CachedCredentials {
  llmApiKey: string
  ttsApiKey: string
  llmBaseUrl: string
  ttsBaseUrl: string
  /* Non-secret selections, so the whole setup survives a reload. */
  llmProviderId?: string
  llmModel?: string
  ttsProviderId?: string
  ttsVoice?: string
  ttsModel?: string
  ttsSpeechPath?: string
  savedAt?: string
}

export interface ConfigState {
  /* catalogue */
  providers: ProvidersResponse | null
  providersLoading: boolean
  providersLoaded: boolean
  providersError: string | null

  /* LLM */
  llmProviderId: string
  llmApiKey: string
  llmModel: string
  llmBaseUrl: string
  temperature: number
  maxTokens: number
  systemPrompt: string

  /* TTS */
  ttsProviderId: string
  ttsApiKey: string
  ttsVoice: string
  ttsBaseUrl: string
  /** Model id for OpenAI-compatible TTS endpoints (e.g. mimo-v2.5-tts). */
  ttsModel: string
  /**
   * Synthesis route appended to the base URL for custom endpoints. OpenAI uses
   * `/audio/speech`; some gateways differ, so it is user-editable.
   */
  ttsSpeechPath: string
  speed: number
  format: string

  /* generation mode */
  mode: GenerationMode
  /** Reference clip used for voice cloning (media id on the backend). */
  referenceMediaId: string | null

  /* input */
  text: string

  /* options */
  autoSave: boolean
  includeAudioBase64: boolean
  fallbackEnabled: boolean

  /* UI */
  advancedOpen: string[]
}

const DEFAULT_SYSTEM_PROMPT = '你是一个乐于助人的助手。'

export const useConfigStore = defineStore('config', {
  state: (): ConfigState => ({
    providers: null,
    providersLoading: false,
    providersLoaded: false,
    providersError: null,

    llmProviderId: '',
    llmApiKey: '',
    llmModel: '',
    llmBaseUrl: '',
    temperature: 0.7,
    maxTokens: 1024,
    systemPrompt: DEFAULT_SYSTEM_PROMPT,

    ttsProviderId: '',
    ttsApiKey: '',
    ttsVoice: '',
    ttsBaseUrl: '',
    ttsModel: '',
    ttsSpeechPath: '',
    speed: 1.0,
    format: 'mp3',

    mode: 'tts',
    referenceMediaId: null,

    text: '',

    autoSave: true,
    includeAudioBase64: false,
    fallbackEnabled: true,

    advancedOpen: [],
  }),

  getters: {
    /** All LLM providers offered by the backend. */
    llmProviders(state): LLMProvider[] {
      return state.providers?.llm ?? []
    },

    /** All TTS providers offered by the backend. */
    ttsProviders(state): TTSProvider[] {
      return state.providers?.tts ?? []
    },

    /** Currently selected LLM provider descriptor (or `null`). */
    llmProvider(state): LLMProvider | null {
      if (!state.providers) return null
      return state.providers.llm.find((item) => item.id === state.llmProviderId) ?? null
    },

    /** Currently selected TTS provider descriptor (or `null`). */
    ttsProvider(state): TTSProvider | null {
      if (!state.providers) return null
      return state.providers.tts.find((item) => item.id === state.ttsProviderId) ?? null
    },

    /** Models offered by the selected LLM provider. */
    llmModels(): { id: string; name: string }[] {
      return this.llmProvider?.models ?? []
    },

    /** Voices offered by the selected TTS provider. */
    ttsVoices(): VoiceOption[] {
      return this.ttsProvider?.voices ?? []
    },

    /** Audio formats supported by the selected TTS provider. */
    ttsFormats(): string[] {
      return this.ttsProvider?.formats ?? ['mp3']
    },

    /** Lower bound of the speed slider for the selected provider. */
    speedMin(): number {
      return this.ttsProvider?.speed_min ?? 0.25
    },

    /** Upper bound of the speed slider for the selected provider. */
    speedMax(): number {
      return this.ttsProvider?.speed_max ?? 4.0
    },

    /** Whether the selected TTS provider supports speed control. */
    supportsSpeed(): boolean {
      return this.ttsProvider?.supports_speed ?? true
    },

    /** Whether the selected TTS provider exposes a real voice-clone API. */
    supportsVoiceClone(): boolean {
      return this.ttsProvider?.supports_voice_clone ?? false
    },

    /** Whether the selected TTS provider can take a reference clip directly. */
    supportsReferenceAudio(): boolean {
      return this.ttsProvider?.supports_reference_audio ?? false
    },

    /** True when either cloning route is available for the current provider. */
    supportsAnyCloning(): boolean {
      return this.supportsVoiceClone || this.supportsReferenceAudio
    },

    /** Friendly hint telling the user which model to pick for cloning. */
    cloneHint(): string {
      return this.ttsProvider?.clone_hint ?? ''
    },

    /** Models the current TTS provider can use for cloning. */
    cloneModels(): { id: string; name: string }[] {
      return this.ttsProvider?.clone_models ?? []
    },

    /** Effective base URL for the LLM request (`null` when unused). */
    llmBaseUrlOrNull(state): string | null {
      const value = state.llmBaseUrl.trim()
      return value.length > 0 ? value : null
    },

    /** Effective base URL for the TTS request (`null` when unused). */
    ttsBaseUrlOrNull(state): string | null {
      const value = state.ttsBaseUrl.trim()
      return value.length > 0 ? value : null
    },

    /**
     * Whether every required field for the *current mode* is present.
     *
     * `clone` additionally needs a reference clip; `text2audio` does not need
     * an LLM at all.
     */
    canGenerate(state): boolean {
      if (state.text.trim().length === 0) return false
      if (state.text.length > MAX_TEXT_LENGTH) return false
      if (state.ttsProviderId.length === 0) return false
      if (state.ttsVoice.trim().length === 0) return false

      // LLM is required unless we are only voicing the text verbatim.
      const needsLLM = state.mode !== 'text2audio'
      if (needsLLM) {
        if (state.llmProviderId.length === 0) return false
        if (state.llmApiKey.trim().length === 0) return false
        if (state.llmModel.trim().length === 0) return false
      }

      // Cloning needs a reference clip.
      if (state.mode === 'clone' && !state.referenceMediaId) return false

      return true
    },

    /** Human readable reasons explaining why generation is disabled. */
    disabledReasons(state): string[] {
      const reasons: string[] = []
      if (state.text.trim().length === 0) reasons.push('请输入要生成的文本')
      if (state.text.length > MAX_TEXT_LENGTH) reasons.push(`文本长度不能超过 ${MAX_TEXT_LENGTH} 个字符`)
      if (!state.providersLoaded) reasons.push('服务商列表尚未加载完成')

      const needsLLM = state.mode !== 'text2audio'
      if (needsLLM) {
        if (state.llmProviderId.length === 0) reasons.push('请前往「系统设置」选择大模型服务商')
        if (state.llmApiKey.trim().length === 0) reasons.push('请前往「系统设置」填写大模型 API Key')
        if (state.llmModel.trim().length === 0) reasons.push('请前往「系统设置」选择大模型')
      }

      if (state.ttsProviderId.length === 0) reasons.push('请前往「系统设置」选择语音合成服务商')
      if (state.ttsVoice.trim().length === 0) reasons.push('请前往「系统设置」选择音色')

      if (state.mode === 'clone' && !state.referenceMediaId) {
        reasons.push('声音克隆模式需要先上传一段参考音频或视频')
      }
      return reasons
    },
  },

  actions: {
    /** `GET /providers`; applies server defaults for the first load. */
    async loadProviders(force = false): Promise<ProvidersResponse | null> {
      if (this.providersLoaded && !force) return this.providers
      this.providersLoading = true
      this.providersError = null
      try {
        const providers = await fetchProviders()
        this.providers = providers
        this.providersLoaded = true
        this.ensureSelections()
        return providers
      } catch (error) {
        this.providersError = error instanceof Error ? error.message : '加载服务商列表失败'
        return null
      } finally {
        this.providersLoading = false
      }
    },

    /** Fill in provider/model/voice selections that are empty or stale. */
    ensureSelections(): void {
      const providers = this.providers
      if (!providers) return

      if (!providers.llm.some((item) => item.id === this.llmProviderId)) {
        const first = providers.llm[0]
        if (first) {
          this.llmProviderId = first.id
          this.llmModel = first.default_model
        }
      } else {
        const current = providers.llm.find((item) => item.id === this.llmProviderId)
        if (current && this.llmModel.trim().length === 0) this.llmModel = current.default_model
      }

      if (!providers.tts.some((item) => item.id === this.ttsProviderId)) {
        const first = providers.tts[0]
        if (first) {
          this.ttsProviderId = first.id
          this.ttsVoice = first.default_voice
          this.format = first.formats[0] ?? 'mp3'
          this.speed = Math.min(Math.max(1.0, first.speed_min), first.speed_max)
        }
      } else {
        const current = providers.tts.find((item) => item.id === this.ttsProviderId)
        if (current) {
          if (this.ttsVoice.trim().length === 0) this.ttsVoice = current.default_voice
          if (!current.formats.includes(this.format)) this.format = current.formats[0] ?? 'mp3'
        }
      }
    },

    /** Called when the user picks a different LLM provider. */
    selectLLMProvider(providerId: string): void {
      this.llmProviderId = providerId
      const provider = this.providers?.llm.find((item) => item.id === providerId) ?? null
      if (provider) {
        this.llmModel = provider.default_model
        if (!provider.supports_base_url) this.llmBaseUrl = ''
      }
    },

    /** Called when the user picks a different TTS provider. */
    selectTTSProvider(providerId: string): void {
      this.ttsProviderId = providerId
      const provider = this.providers?.tts.find((item) => item.id === providerId) ?? null
      if (provider) {
        this.ttsVoice = provider.default_voice
        this.format = provider.formats[0] ?? 'mp3'
        this.speed = Math.min(Math.max(this.speed, provider.speed_min), provider.speed_max)
        if (!provider.supports_base_url) this.ttsBaseUrl = ''
      }
    },

    /**
     * Apply server defaults without clobbering what the user already typed:
     * only empty selections and untouched keys are filled in.
     */
    applyDefaults(settings: ServerSettings): void {
      if (this.llmProviderId.length === 0) this.llmProviderId = settings.default_llm_provider
      if (this.llmModel.trim().length === 0) this.llmModel = settings.default_llm_model
      if (this.ttsProviderId.length === 0) this.ttsProviderId = settings.default_tts_provider
      if (this.ttsVoice.trim().length === 0) this.ttsVoice = settings.default_tts_voice
      if (this.speed === 1.0 && Number.isFinite(settings.default_speed)) this.speed = settings.default_speed
      if (this.systemPrompt.trim().length === 0) this.systemPrompt = settings.default_system_prompt
      this.autoSave = settings.auto_save_history
    },

    /** Build the `GenerateRequest` body (throws when required fields are missing). */
    buildRequest(overrides?: Partial<GenerateOptions>): GenerateRequest {
      if (!this.canGenerate) {
        throw new Error(this.disabledReasons.join('；') || '配置不完整')
      }

      const options: GenerateOptions = {
        auto_save: this.autoSave,
        include_audio_base64: this.includeAudioBase64,
        fallback_enabled: this.fallbackEnabled,
        ...overrides,
      }

      // `clone` forwards the reference clip; `text2audio` skips the LLM by
      // asking the backend to voice the typed text verbatim.
      if (this.mode === 'clone' && this.referenceMediaId) {
        options.reference_media_id = this.referenceMediaId
      }
      if (this.mode === 'text2audio') {
        options.reuse_reply_text = this.text
      }

      return {
        text: this.text,
        llm: {
          provider: this.llmProviderId,
          api_key: this.llmApiKey,
          model: this.llmModel,
          base_url: this.llmBaseUrlOrNull,
          temperature: this.temperature,
          max_tokens: this.maxTokens,
          system_prompt: this.systemPrompt,
        },
        tts: {
          provider: this.ttsProviderId,
          api_key: this.ttsApiKey,
          voice: this.ttsVoice,
          model: this.ttsModel,
          speed: this.speed,
          format: this.format,
          base_url: this.ttsBaseUrlOrNull,
          // Only custom endpoints need a route override; send it only when set
          // so built-in providers keep their own defaults.
          extra: this.ttsSpeechPath.trim()
            ? { speech_path: this.ttsSpeechPath.trim() }
            : {},
        },
        options,
      }
    },

    /* ---------------- configuration cache ---------------- */

    /**
     * Snapshot the current provider configuration into localStorage.
     *
     * Stores the API keys **and** the non-secret selections, so reopening the
     * app restores the whole setup rather than just the credentials. Called
     * automatically whenever a credential field changes (see the subscribers in
     * `main.ts`), which is what makes "fill it once" actually work — previously
     * this only ran when the user toggled the switch or pressed 保存设置, so a
     * key typed on the generate page was silently lost on reload.
     */
    persistCredentials(): void {
      const payload: CachedCredentials = {
        llmApiKey: this.llmApiKey,
        ttsApiKey: this.ttsApiKey,
        llmBaseUrl: this.llmBaseUrl,
        ttsBaseUrl: this.ttsBaseUrl,
        llmProviderId: this.llmProviderId,
        llmModel: this.llmModel,
        ttsProviderId: this.ttsProviderId,
        ttsVoice: this.ttsVoice,
        ttsModel: this.ttsModel,
        ttsSpeechPath: this.ttsSpeechPath,
        savedAt: new Date().toISOString(),
      }
      try {
        localStorage.setItem(CREDENTIALS_STORAGE_KEY, JSON.stringify(payload))
      } catch {
        /* storage unavailable (private mode / quota) — ignore */
      }
    },

    /**
     * Restore keys and selections from localStorage.
     *
     * Selections are always restored (they are not sensitive); credentials are
     * restored too, because the cache only exists when the user enabled saving
     * — see `App.vue`, which gates this call on `autoSaveCredentials`.
     */
    restoreCredentials(): void {
      try {
        const raw = localStorage.getItem(CREDENTIALS_STORAGE_KEY)
        if (!raw) return
        const parsed = JSON.parse(raw) as Partial<CachedCredentials>
        if (typeof parsed.llmApiKey === 'string') this.llmApiKey = parsed.llmApiKey
        if (typeof parsed.ttsApiKey === 'string') this.ttsApiKey = parsed.ttsApiKey
        if (typeof parsed.llmBaseUrl === 'string') this.llmBaseUrl = parsed.llmBaseUrl
        if (typeof parsed.ttsBaseUrl === 'string') this.ttsBaseUrl = parsed.ttsBaseUrl
        if (typeof parsed.llmProviderId === 'string' && parsed.llmProviderId) {
          this.llmProviderId = parsed.llmProviderId
        }
        if (typeof parsed.llmModel === 'string' && parsed.llmModel) this.llmModel = parsed.llmModel
        if (typeof parsed.ttsProviderId === 'string' && parsed.ttsProviderId) {
          this.ttsProviderId = parsed.ttsProviderId
        }
        if (typeof parsed.ttsVoice === 'string' && parsed.ttsVoice) this.ttsVoice = parsed.ttsVoice
        if (typeof parsed.ttsModel === 'string') this.ttsModel = parsed.ttsModel
        if (typeof parsed.ttsSpeechPath === 'string') this.ttsSpeechPath = parsed.ttsSpeechPath
      } catch {
        /* malformed payload — ignore */
      }
    },

    /** When the cache was last written (for the settings page). */
    cachedCredentialsSavedAt(): string | null {
      try {
        const raw = localStorage.getItem(CREDENTIALS_STORAGE_KEY)
        if (!raw) return null
        const parsed = JSON.parse(raw) as Partial<CachedCredentials>
        return typeof parsed.savedAt === 'string' ? parsed.savedAt : null
      } catch {
        return null
      }
    },

    /** Wipe keys from memory and from localStorage. */
    clearCredentials(): void {
      this.llmApiKey = ''
      this.ttsApiKey = ''
      this.llmBaseUrl = ''
      this.ttsBaseUrl = ''
      try {
        localStorage.removeItem(CREDENTIALS_STORAGE_KEY)
      } catch {
        /* ignore */
      }
    },

    /** Whether a credential cache currently exists in localStorage. */
    hasCachedCredentials(): boolean {
      try {
        return localStorage.getItem(CREDENTIALS_STORAGE_KEY) !== null
      } catch {
        return false
      }
    },

    /** Reset every generation parameter (keeps provider selections). */
    resetParams(): void {
      this.text = ''
      this.temperature = 0.7
      this.maxTokens = 1024
      this.systemPrompt = DEFAULT_SYSTEM_PROMPT
      this.speed = 1.0
      this.format = this.ttsFormats[0] ?? 'mp3'
      this.autoSave = true
      this.includeAudioBase64 = false
      this.fallbackEnabled = true
    },
  },
})