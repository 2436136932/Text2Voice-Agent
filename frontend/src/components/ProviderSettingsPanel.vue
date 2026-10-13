<script setup lang="ts">
/**
 * Provider configuration panel — the single place where LLM/TTS providers,
 * credentials, models and voices are configured.
 *
 * This used to be duplicated between the generate page cards and the settings
 * page; it now lives here only, and both pages consume the same `config` store.
 *
 * Credential safety: keys stay in the Pinia store (memory) and reach
 * localStorage only when the user explicitly enables "自动保存配置". The panel
 * never sends a key to `/settings`.
 */
import { computed, ref } from 'vue'
import {
  ChatDotRound,
  Connection,
  Download,
  Key,
  Link,
  Microphone,
  QuestionFilled,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fetchDebugCapture, fetchModels, probePaths, validateProvider } from '@/api'
import { ApiError } from '@/api/http'
import type {
  DebugCaptureRecord,
  FetchedModel,
  ProbePathItem,
  ProviderKind,
  ValidateProviderRequest,
} from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'
import { genderLabel } from '@/utils/format'

const props = defineProps<{ kind: ProviderKind }>()

const configStore = useConfigStore()
const settingsStore = useSettingsStore()

const isLLM = computed(() => props.kind === 'llm')
const title = computed(() => (isLLM.value ? '大模型服务商' : '语音合成服务商'))
const icon = computed(() => (isLLM.value ? ChatDotRound : Microphone))

const validating = ref(false)
const fetchingModels = ref(false)
const lastLatency = ref<number | null>(null)
const fetchedModels = ref<FetchedModel[]>([])
const advancedOpen = ref<string[]>([])
const probingPaths = ref(false)
const probeResults = ref<ProbePathItem[]>([])
const suggestedPath = ref<string | null>(null)
const capturingDebug = ref(false)
const debugRecords = ref<DebugCaptureRecord[]>([])
const debugEnabled = ref(false)

/* ------------------------------ bound fields ----------------------------- */

const providerId = computed<string>({
  get: () => (isLLM.value ? configStore.llmProviderId : configStore.ttsProviderId),
  set: (value) => {
    if (isLLM.value) configStore.selectLLMProvider(value)
    else configStore.selectTTSProvider(value)
    lastLatency.value = null
    fetchedModels.value = []
  },
})

const apiKey = computed<string>({
  get: () => (isLLM.value ? configStore.llmApiKey : configStore.ttsApiKey),
  set: (value) => {
    if (isLLM.value) configStore.llmApiKey = value
    else configStore.ttsApiKey = value
  },
})

const baseUrl = computed<string>({
  get: () => (isLLM.value ? configStore.llmBaseUrl : configStore.ttsBaseUrl),
  set: (value) => {
    if (isLLM.value) configStore.llmBaseUrl = value
    else configStore.ttsBaseUrl = value
  },
})

const model = computed<string>({
  get: () => configStore.llmModel,
  set: (value) => {
    configStore.llmModel = value
  },
})

const voice = computed<string>({
  get: () => configStore.ttsVoice,
  set: (value) => {
    configStore.ttsVoice = value
  },
})

/* ------------------------------- metadata -------------------------------- */

const options = computed(() => (isLLM.value ? configStore.llmProviders : configStore.ttsProviders))
const current = computed(() => (isLLM.value ? configStore.llmProvider : configStore.ttsProvider))

const supportsBaseUrl = computed(() => current.value?.supports_base_url ?? false)
const requiresBaseUrl = computed(() => current.value?.requires_base_url ?? false)
const docsUrl = computed(() => current.value?.docs_url ?? null)
const apiKeyPlaceholder = computed(() => current.value?.api_key_hint || '请输入 API Key')

/** True when the key field can be left empty (local/offline providers). */
const keyOptional = computed(() => (current.value as { requires_api_key?: boolean } | null)?.requires_api_key === false)

const isCustomProvider = computed(() => providerId.value === 'custom')

/** Model list for the LLM selector (fetched list wins when present). */
const modelChoices = computed(() => {
  if (fetchedModels.value.length > 0) {
    return fetchedModels.value.map((item) => ({ id: item.id, name: item.name }))
  }
  return configStore.llmModels
})

/** Voice list for the TTS selector. */
const voiceChoices = computed(() => configStore.ttsVoices)

/**
 * For a custom TTS endpoint we surface the fetched list, marking which models
 * declare TTS support so the user can pick the right one. For built-in
 * providers we use whatever audio models the backend advertises.
 */
const ttsProviderModels = computed<{ id: string; name: string }[]>(() => {
  const models = (current.value as { models?: { id: string; name: string }[] } | null)?.models
  return models ?? []
})

const ttsModelOptions = computed<({ id: string; name: string; supports_tts?: boolean })[]>(() => {
  if (isCustomProvider.value && fetchedModels.value.length > 0) return fetchedModels.value
  return ttsProviderModels.value
})

/** Providers that advertise voice-clone support. */
const cloneCapable = computed(
  () => (current.value as { supports_voice_clone?: boolean } | null)?.supports_voice_clone === true,
)

const cloneHint = computed(
  () => (current.value as { clone_hint?: string } | null)?.clone_hint ?? '',
)

/**
 * Domains each preset provider's models actually live on.
 *
 * Used to detect the common mistake of pasting a *third-party* base URL into a
 * preset provider (e.g. a MiMo URL under "OpenAI TTS"), where the model list
 * would silently keep showing the preset's own models and the user never
 * realises they should have picked 「自定义」 instead.
 */
const PROVIDER_DOMAINS: Record<string, string[]> = {
  openai: ['openai.com', 'azure.com'],
  anthropic: ['anthropic.com'],
  qwen: ['aliyuncs.com', 'dashscope'],
  deepseek: ['deepseek.com'],
  gemini: ['googleapis.com', 'google.com'],
  zhipu: ['bigmodel.cn'],
  moonshot: ['moonshot.cn'],
  ollama: ['localhost', '127.0.0.1'],
  elevenlabs: ['elevenlabs.io'],
  azure: ['speech.microsoft.com', 'microsoft.com'],
  volcengine: ['volcengine.com', 'bytedance.com'],
  minimax: ['minimax.chat', 'minimaxi.com'],
  edge: ['bing.com', 'microsoft.com'],
}

/**
 * True when a preset provider is pointed at a host that is clearly not its own
 * — i.e. the user is trying to reach a different vendor through the wrong
 * provider entry.
 */
const foreignBaseUrl = computed<boolean>(() => {
  if (isCustomProvider.value) return false
  const raw = baseUrl.value.trim()
  if (!raw) return false
  const allowed = PROVIDER_DOMAINS[providerId.value]
  if (!allowed) return false

  let host = raw.toLowerCase()
  try {
    host = new URL(raw.includes('://') ? raw : `https://${raw}`).hostname
  } catch {
    return false
  }
  return !allowed.some((domain) => host.endsWith(domain))
})

/* ------------------------------- validation ------------------------------ */

/**
 * The 获取模型 button is available for every provider, not just custom ones.
 *
 * Preset lists are hand-written annotations, not a source of truth: a vendor
 * can add or retire a model at any time, so the user must be able to pull the
 * live list. `custom` additionally requires a base URL; built-ins fall back to
 * their own default endpoint.
 */
const canFetchModels = computed(() => {
  if (apiKey.value.trim().length === 0) return false
  if (isCustomProvider.value) return baseUrl.value.trim().length > 0
  if (requiresBaseUrl.value) return baseUrl.value.trim().length > 0
  return true
})

const canValidate = computed(() => {
  if (!providerId.value) return false
  if (!keyOptional.value && apiKey.value.trim().length === 0) return false
  if (requiresBaseUrl.value && baseUrl.value.trim().length === 0) return false
  if (isLLM.value && model.value.trim().length === 0) return false
  if (!isLLM.value && voice.value.trim().length === 0) return false
  return true
})

const validateDisabledReason = computed(() => {
  if (!providerId.value) return '请先选择服务商'
  if (!keyOptional.value && apiKey.value.trim().length === 0) return '请先填写 API Key'
  if (requiresBaseUrl.value && baseUrl.value.trim().length === 0) return '该服务商必须填写 Base URL'
  if (isLLM.value && model.value.trim().length === 0) return '请先选择或输入模型名称'
  if (!isLLM.value && voice.value.trim().length === 0) return '请先选择发音人或克隆音色'
  return ''
})

const keyHint = computed(() =>
  settingsStore.autoSaveCredentials
    ? '已开启「自动保存配置」：Key 会以明文保存在本机 localStorage，仅本机可见。'
    : 'Key 仅保存在浏览器内存中，刷新页面即失效，绝不会写入后端数据库或日志。',
)

/* -------------------------------- actions -------------------------------- */

async function handleFetchModels(): Promise<void> {
  if (!canFetchModels.value) {
    ElMessage.warning(
      isCustomProvider.value ? '请先填写 Base URL 和 API Key' : '请先填写 API Key',
    )
    return
  }
  fetchingModels.value = true
  try {
    const result = await fetchModels({
      provider: providerId.value,
      // Omitting base_url lets built-in providers use their own default.
      base_url: baseUrl.value.trim(),
      api_key: apiKey.value,
      kind: props.kind,
    })
    fetchedModels.value = result.models
    if (result.models.length === 0) {
      ElMessage.warning('上游返回了空模型列表')
      return
    }
    const fresh = result.models.filter((item) => !item.note).length
    const ttsModels = result.models.filter((item) => item.supports_tts)
    const extra = fresh > 0 ? `，其中 ${fresh} 个是本地未标注的新模型` : ''
    if (!isLLM.value && ttsModels.length > 0) {
      ElMessage.success(
        `已从上游获取 ${result.models.length} 个模型${extra}，${ttsModels.length} 个支持语音合成`,
      )
    } else {
      ElMessage.success(`已从上游获取 ${result.models.length} 个模型${extra}`)
    }
  } catch (error) {
    // Fall back to the built-in list, and say so rather than going blank.
    reportError(error, '获取模型失败，当前仍显示内置列表')
    ElMessage.warning('未能从上游获取模型列表，继续使用内置列表（可手动输入模型名）')
  } finally {
    fetchingModels.value = false
  }
}

/** Enable/disable the backend request capture. */
async function handleToggleCapture(enabled: boolean): Promise<void> {
  capturingDebug.value = true
  try {
    const result = await fetchDebugCapture({ enabled })
    debugEnabled.value = result.enabled
    debugRecords.value = result.records
    if (!enabled) ElMessage.info('已停止抓包')
  } catch (error) {
    reportError(error, '切换抓包失败')
  } finally {
    capturingDebug.value = false
  }
}

/**
 * Turn capture on, fire one validation probe, then show what was actually sent.
 *
 * This is the fastest way to diagnose "the endpoint 404s and I do not know
 * why": it shows the real URL, headers and body the backend used.
 */
async function handleCaptureDebug(): Promise<void> {
  capturingDebug.value = true
  try {
    await fetchDebugCapture({ enabled: true })
    debugEnabled.value = true

    // Trigger one real outbound call so there is something to inspect.
    if (canValidate.value) {
      await validateProvider({
        kind: props.kind,
        provider: providerId.value,
        api_key: apiKey.value,
        base_url: baseUrl.value.trim() || null,
        model: isLLM.value ? model.value : configStore.ttsModel || null,
        voice: isLLM.value ? null : voice.value,
      }).catch(() => undefined)
    }

    const result = await fetchDebugCapture({})
    debugRecords.value = result.records
    if (result.records.length === 0) {
      ElMessage.warning('没有捕捉到请求。请先填写完整配置（Base URL / Key / 模型）')
    } else {
      ElMessage.success(`已捕捉 ${result.records.length} 条请求`)
    }
  } catch (error) {
    reportError(error, '抓包失败')
  } finally {
    capturingDebug.value = false
  }
}

/** Render one captured call as copyable plain text. */
function formatCapture(item: DebugCaptureRecord): string {
  const lines: string[] = []
  lines.push(`URL: ${item.method} ${item.url}`)
  const headerEntries = Object.entries(item.headers ?? {})
  if (headerEntries.length > 0) {
    lines.push(`Headers: ${headerEntries.map(([k, v]) => `${k}: ${v}`).join(', ')}`)
  }
  if (item.request_body !== null && item.request_body !== undefined) {
    lines.push(`Request: ${JSON.stringify(item.request_body)}`)
  }
  if (item.response_body) lines.push(`Response: ${item.response_body}`)
  if (item.error) lines.push(`Error: ${item.error}`)
  lines.push(`Elapsed: ${item.elapsed_ms}ms`)
  return lines.join('\n')
}

async function handleProbePaths(): Promise<void> {
  if (!canFetchModels.value) {
    ElMessage.warning('请先填写 Base URL 和 API Key')
    return
  }
  probingPaths.value = true
  suggestedPath.value = null
  try {
    const result = await probePaths({
      base_url: baseUrl.value.trim(),
      api_key: apiKey.value,
    })
    probeResults.value = result.results
    suggestedPath.value = result.suggested_path
    const alive = result.results.filter((item) => item.alive)
    if (alive.length === 0) {
      ElMessage.warning('所有常见路径都没有响应，请查阅服务商文档确认接口地址')
    } else {
      ElMessage.success(`发现 ${alive.length} 个可用路径${result.suggested_path ? `，建议 ${result.suggested_path}` : ''}`)
    }
  } catch (error) {
    reportError(error, '探测路径失败')
  } finally {
    probingPaths.value = false
  }
}

async function handleValidate(): Promise<void> {
  if (!canValidate.value) {
    ElMessage.warning(validateDisabledReason.value)
    return
  }
  validating.value = true
  lastLatency.value = null

  const payload: ValidateProviderRequest = {
    kind: props.kind,
    provider: providerId.value,
    api_key: apiKey.value,
    base_url: baseUrl.value.trim() || null,
    model: isLLM.value ? model.value : null,
    voice: isLLM.value ? null : voice.value,
  }

  try {
    const result = await validateProvider(payload)
    lastLatency.value = result.latency_ms
    if (result.valid) {
      const latency = result.latency_ms !== null ? `（${result.latency_ms} ms）` : ''
      ElMessage.success(`连接成功：${result.provider}${latency}`)
    } else {
      ElMessage.error(result.message || '连接校验未通过')
    }
  } catch (error) {
    reportError(error, '连接校验失败')
  } finally {
    validating.value = false
  }
}

/** Surface `detail.suggestion` when the backend provided actionable advice. */
function reportError(error: unknown, fallback: string): void {
  if (error instanceof ApiError) {
    const suggestion = (error.detail as { suggestion?: string } | undefined)?.suggestion
    ElMessage.error({
      message: `${error.message}${suggestion ? `\n${suggestion}` : ''}${error.code ? `\n[${error.code}]` : ''}`,
      duration: 7000,
      showClose: true,
    })
  } else {
    ElMessage.error(fallback)
  }
}
</script>

<template>
  <el-card class="provider-panel" shadow="never">
    <template #header>
      <div class="provider-panel__header">
        <div class="provider-panel__header-left">
          <el-icon class="provider-panel__icon"><component :is="icon" /></el-icon>
          <span>{{ title }}</span>
        </div>
        <div class="provider-panel__header-right">
          <el-tag v-if="lastLatency !== null" type="success" size="small" effect="plain">
            {{ lastLatency }} ms
          </el-tag>
          <el-tooltip v-if="docsUrl" :content="`打开 ${current?.name ?? ''} 官方文档`" placement="top">
            <el-button
              tag="a"
              :href="docsUrl"
              target="_blank"
              rel="noopener noreferrer"
              link
              size="small"
              :icon="Link"
            >
              文档
            </el-button>
          </el-tooltip>
        </div>
      </div>
    </template>

    <el-form label-position="top" @submit.prevent>
      <el-form-item label="服务商">
        <el-select
          v-model="providerId"
          class="provider-panel__control"
          placeholder="请选择服务商"
          :loading="configStore.providersLoading"
        >
          <el-option v-for="item in options" :key="item.id" :label="item.name" :value="item.id">
            <span class="provider-panel__option">
              <span>{{ item.name }}</span>
              <span class="provider-panel__option-id t2v-mono">{{ item.id }}</span>
            </span>
          </el-option>
        </el-select>
        <div v-if="current?.notes" class="provider-panel__hint">{{ current.notes }}</div>
      </el-form-item>

      <el-form-item>
        <template #label>
          <span class="provider-panel__label">
            API Key
            <el-tag v-if="keyOptional" size="small" type="info" effect="plain">可留空</el-tag>
            <el-tooltip :content="keyHint" placement="top">
              <el-icon class="provider-panel__label-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </span>
        </template>
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          clearable
          autocomplete="off"
          spellcheck="false"
          :placeholder="apiKeyPlaceholder"
          :prefix-icon="Key"
          class="provider-panel__control"
        />
      </el-form-item>

      <!-- LLM: model picker -->
      <el-form-item v-if="isLLM" label="模型">
        <div class="provider-panel__row">
          <el-select
            v-model="model"
            class="provider-panel__control provider-panel__grow"
            placeholder="请选择或输入模型名称"
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
          >
            <el-option v-for="item in modelChoices" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-button
            :icon="Download"
            :loading="fetchingModels"
            :disabled="!canFetchModels"
            @click="handleFetchModels"
          >
            获取模型
          </el-button>
        </div>
        <div class="provider-panel__hint">
          <template v-if="fetchedModels.length > 0">
            列表来自上游实时接口（{{ fetchedModels.length }} 个）
            <template v-if="fetchedModels.some((m) => !m.note)">
              ，其中 <b>{{ fetchedModels.filter((m) => !m.note).length }}</b> 个是新增的未标注模型
            </template>
            。此前的内置列表仅作为兜底。
          </template>
          <template v-else>
            当前显示的是<b>内置列表</b>。上游可能已增删模型，
            点「获取模型」可拉取真实列表（新模型会自动出现，下线的不再可选）。
          </template>
        </div>
      </el-form-item>

      <!-- TTS: voice / model picker -->
      <template v-else>
        <el-form-item label="发音人 / 音色">
          <div class="provider-panel__row">
            <el-select
              v-model="voice"
              class="provider-panel__control provider-panel__grow"
              placeholder="请选择发音人"
              filterable
              allow-create
            >
              <el-option v-for="item in voiceChoices" :key="item.id" :label="item.name" :value="item.id">
                <span class="provider-panel__option">
                  <span>{{ item.name }}</span>
                  <span class="provider-panel__option-tags">
                    <el-tag v-if="item.cloned" size="small" type="warning" effect="plain">克隆</el-tag>
                    <el-tag size="small" effect="plain" type="info">{{ genderLabel(item.gender) }}</el-tag>
                    <el-tag size="small" effect="plain" type="info">{{ item.language }}</el-tag>
                  </span>
                </span>
              </el-option>
            </el-select>
          </div>
          <div v-if="cloneHint" class="provider-panel__hint">{{ cloneHint }}</div>
        </el-form-item>

        <!-- TTS model: always fetchable, with TTS capability markers -->
        <el-form-item v-if="isCustomProvider || ttsProviderModels.length > 0 || fetchedModels.length > 0" label="TTS 模型">
          <div class="provider-panel__row">
            <el-select
              v-model="configStore.ttsModel"
              class="provider-panel__control provider-panel__grow"
              placeholder="点「获取模型」拉取，或直接输入模型名"
              filterable
              allow-create
              clearable
            >
              <el-option
                v-for="item in ttsModelOptions"
                :key="item.id"
                :label="item.name"
                :value="item.id"
              >
                <span class="provider-panel__option">
                  <span>{{ item.name }}</span>
                  <span class="provider-panel__option-tags">
                    <el-tag
                      v-if="'supports_tts' in item && item.supports_tts"
                      size="small"
                      type="success"
                      effect="dark"
                    >
                      TTS
                    </el-tag>
                    <el-tag
                      v-if="'note' in item && item.note && fetchedModels.length > 0"
                      size="small"
                      type="info"
                      effect="plain"
                    >
                      内置说明
                    </el-tag>
                  </span>
                </span>
              </el-option>
            </el-select>
            <el-button
              :icon="Download"
              :loading="fetchingModels"
              :disabled="!canFetchModels"
              @click="handleFetchModels"
            >
              获取模型
            </el-button>
          </div>
          <div class="provider-panel__hint">
            <template v-if="fetchedModels.length > 0">
              列表来自上游实时接口（{{ fetchedModels.length }} 个）。
              带 <el-tag size="small" type="success" effect="dark">TTS</el-tag> 的模型支持语音合成。
            </template>
            <template v-else>
              当前显示<b>内置列表</b>。点「获取模型」拉取上游真实列表，避免选到已下线的模型。
            </template>
          </div>
        </el-form-item>
      </template>

      <el-collapse v-model="advancedOpen" class="provider-panel__advanced">
        <el-collapse-item name="advanced">
          <template #title><span>高级设置</span></template>

          <el-form-item>
            <template #label>
              <span class="provider-panel__label">
                Base URL
                <el-tag v-if="requiresBaseUrl" size="small" type="warning" effect="plain">必填</el-tag>
                <el-tag v-else-if="!supportsBaseUrl" size="small" type="info" effect="plain">不支持</el-tag>
              </span>
            </template>
            <el-input
              v-model="baseUrl"
              class="provider-panel__control"
              :disabled="!supportsBaseUrl"
              clearable
              spellcheck="false"
              placeholder="例如 https://api.example.com/v1"
            />
            <div class="provider-panel__hint">
              使用自建 / 代理网关时填写，需包含版本路径（通常是 /v1）。
            </div>
            <el-alert
              v-if="foreignBaseUrl"
              class="provider-panel__warn"
              type="warning"
              show-icon
              :closable="false"
              title="这个 Base URL 看起来不属于当前服务商"
            >
              <template #default>
                当前选的是「{{ current?.name }}」，它只提供
                {{ isLLM ? '自家的大模型' : '自家的音色与模型' }}，改 Base URL 不会让模型列表变成对方的。
                要接入第三方（例如 mimo），请把服务商切换为
                <b>{{ isLLM ? '「自定义 (OpenAI 兼容)」' : '「自定义 TTS (OpenAI 兼容)」' }}</b>，
                这样才能用「获取模型」拉取对方真实的模型列表。
              </template>
            </el-alert>
          </el-form-item>

          <!-- Custom TTS: synthesis path is configurable because gateways differ -->
          <el-form-item v-if="isCustomProvider && !isLLM" label="合成接口路径">
            <el-input
              v-model="configStore.ttsSpeechPath"
              class="provider-panel__control"
              clearable
              spellcheck="false"
              placeholder="/audio/speech"
            >
              <template #append>
                <el-button :loading="probingPaths" :disabled="!canFetchModels" @click="handleProbePaths">
                  探测可用路径
                </el-button>
              </template>
            </el-input>
            <div class="provider-panel__hint">
              默认 <span class="t2v-mono">/audio/speech</span>（OpenAI 标准）。
              若服务商使用其他路径（如 <span class="t2v-mono">/tts</span>），点右侧按钮自动探测。
            </div>

            <div v-if="probeResults.length > 0" class="provider-panel__probe">
              <div class="provider-panel__probe-title">
                探测结果（400/401/422 也表示路径存在，只有 404 才是没有）
              </div>
              <el-table :data="probeResults" size="small" max-height="240">
                <el-table-column prop="path" label="路径" min-width="150">
                  <template #default="{ row }">
                    <span class="t2v-mono">{{ row.path }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态" width="70" />
                <el-table-column label="结论" min-width="200">
                  <template #default="{ row }">
                    <el-tag :type="row.alive ? 'success' : 'info'" size="small" effect="plain">
                      {{ row.alive ? '存在' : '无' }}
                    </el-tag>
                    <span class="provider-panel__probe-note">{{ row.note }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="" width="80">
                  <template #default="{ row }">
                    <el-button
                      v-if="row.alive"
                      link
                      type="primary"
                      size="small"
                      @click="configStore.ttsSpeechPath = row.path"
                    >
                      使用
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
              <el-alert
                v-if="suggestedPath"
                class="provider-panel__probe-suggest"
                type="success"
                show-icon
                :closable="false"
                :title="`建议使用 ${suggestedPath}`"
              >
                <template #default>
                  <el-button size="small" type="primary" @click="configStore.ttsSpeechPath = suggestedPath">
                    填入该路径
                  </el-button>
                </template>
              </el-alert>
              <el-alert
                v-else
                class="provider-panel__probe-suggest"
                type="warning"
                show-icon
                :closable="false"
                title="没有探测到可用路径，请用「抓包」看后端实际发了什么"
                description="服务商可能不是 OpenAI 兼容格式。开启抓包后点一次「测试连接」，即可看到完整的请求地址、请求头与请求体，便于对照官方文档。"
              >
                <template #default>
                  <el-button size="small" :loading="capturingDebug" @click="handleCaptureDebug">
                    抓取最近一次请求
                  </el-button>
                </template>
              </el-alert>

              <div v-if="debugRecords.length > 0" class="provider-panel__debug">
                <div class="provider-panel__probe-title">
                  抓包结果（{{ debugRecords.length }} 条，密钥已脱敏）
                </div>
                <div v-for="item in debugRecords" :key="item.seq" class="provider-panel__debug-item">
                  <div class="provider-panel__debug-line">
                    <el-tag size="small" :type="item.status && item.status < 400 ? 'success' : 'danger'">
                      {{ item.status ?? item.error ?? '—' }}
                    </el-tag>
                    <span class="t2v-mono">{{ item.method }} {{ item.url }}</span>
                  </div>
                  <pre class="provider-panel__debug-body">{{ formatCapture(item) }}</pre>
                </div>
                <el-button size="small" @click="handleToggleCapture(false)">停止抓包</el-button>
              </div>
            </div>
          </el-form-item>

          <!--温度、max tokens 与系统提示词 are per-generation settings and now
               live on the generate page; keeping a second copy here is what made
               the same value editable in two places. -->

          <el-form-item v-if="!isLLM" label="支持的音频格式">
            <div class="provider-panel__tags">
              <el-tag v-for="item in configStore.ttsFormats" :key="item" size="small" effect="plain">
                {{ item }}
              </el-tag>
            </div>
          </el-form-item>

          <el-form-item v-if="!isLLM && cloneCapable" label="声音克隆">
            <el-tag type="success" effect="plain" size="small">该服务商支持声音克隆</el-tag>
          </el-form-item>
        </el-collapse-item>
      </el-collapse>

      <div class="provider-panel__actions">
        <el-tooltip
          :content="validateDisabledReason || '校验凭据是否可用'"
          placement="top"
        >
          <span>
            <el-button
              type="primary"
              plain
              :icon="Connection"
              :loading="validating"
              :disabled="!canValidate"
              @click="handleValidate"
            >
              测试连接
            </el-button>
          </span>
        </el-tooltip>
      </div>
    </el-form>
  </el-card>
</template>

<style scoped>
.provider-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.provider-panel__header-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.provider-panel__header-right {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.provider-panel__icon {
  color: var(--t2v-color-primary);
}

.provider-panel__control {
  width: 100%;
}

.provider-panel__row {
  display: flex;
  gap: 8px;
  width: 100%;
  align-items: flex-start;
}

.provider-panel__grow {
  flex: 1;
}

.provider-panel__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.provider-panel__option-id {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.provider-panel__option-tags {
  display: inline-flex;
  gap: 4px;
}

.provider-panel__label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.provider-panel__label-icon {
  color: var(--el-text-color-secondary);
  cursor: help;
}

.provider-panel__hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.provider-panel__warn {
  margin-top: 8px;
  line-height: 1.6;
}

.provider-panel__probe {
  margin-top: 12px;
}

.provider-panel__probe-title {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}

.provider-panel__probe-note {
  margin-left: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.provider-panel__probe-suggest {
  margin-top: 10px;
}

.provider-panel__debug {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.provider-panel__debug-item {
  margin-bottom: 10px;
}

.provider-panel__debug-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  word-break: break-all;
  margin-bottom: 4px;
}

.provider-panel__debug-body {
  margin: 0;
  padding: 8px 10px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow: auto;
}

.provider-panel__tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.provider-panel__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
