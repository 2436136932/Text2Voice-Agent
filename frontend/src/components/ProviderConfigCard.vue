<script setup lang="ts">
/**
 * Provider credential card — used twice on the generate page, once for the LLM
 * and once for the TTS provider.
 *
 * The API key is kept in the Pinia `config` store (memory only) and is sent
 * exclusively to `POST /providers/validate` (测试连接) and to `POST /tasks`.
 * It is never logged and never written to `/settings`.
 */
import { computed, ref } from 'vue'
import { ChatDotRound, Connection, Key, Link, Microphone, QuestionFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { validateProvider } from '@/api'
import { ApiError } from '@/api/http'
import type { ProviderKind, ValidateProviderRequest } from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'
import { genderLabel } from '@/utils/format'

const props = defineProps<{
  /** Which provider family this card configures. */
  kind: ProviderKind
}>()

const configStore = useConfigStore()
const settingsStore = useSettingsStore()

const validating = ref(false)
const lastLatency = ref<number | null>(null)

const isLLM = computed<boolean>(() => props.kind === 'llm')

const title = computed<string>(() => (isLLM.value ? '大模型服务商' : '语音合成服务商'))
const icon = computed(() => (isLLM.value ? ChatDotRound : Microphone))

/* --------------------------- selected provider --------------------------- */

const providerId = computed<string>({
  get: () => (isLLM.value ? configStore.llmProviderId : configStore.ttsProviderId),
  set: (value: string) => {
    if (isLLM.value) configStore.selectLLMProvider(value)
    else configStore.selectTTSProvider(value)
  },
})

const apiKey = computed<string>({
  get: () => (isLLM.value ? configStore.llmApiKey : configStore.ttsApiKey),
  set: (value: string) => {
    if (isLLM.value) configStore.llmApiKey = value
    else configStore.ttsApiKey = value
  },
})

const baseUrl = computed<string>({
  get: () => (isLLM.value ? configStore.llmBaseUrl : configStore.ttsBaseUrl),
  set: (value: string) => {
    if (isLLM.value) configStore.llmBaseUrl = value
    else configStore.ttsBaseUrl = value
  },
})

const options = computed(() => (isLLM.value ? configStore.llmProviders : configStore.ttsProviders))

const current = computed(() => (isLLM.value ? configStore.llmProvider : configStore.ttsProvider))

const supportsBaseUrl = computed<boolean>(() => current.value?.supports_base_url ?? false)
const requiresBaseUrl = computed<boolean>(() => current.value?.requires_base_url ?? false)

/** Placeholder for the API key field, taken from the provider metadata. */
const apiKeyPlaceholder = computed<string>(() => current.value?.api_key_hint ?? '请输入 API Key')

/** Docs link for the selected provider. */
const docsUrl = computed<string | null>(() => current.value?.docs_url ?? null)

/* --------------------------------- model --------------------------------- */

const model = computed<string>({
  get: () => configStore.llmModel,
  set: (value: string) => {
    configStore.llmModel = value
  },
})

const modelOptions = computed(() => configStore.llmModels)

/* --------------------------------- voice --------------------------------- */

const voice = computed<string>({
  get: () => configStore.ttsVoice,
  set: (value: string) => {
    configStore.ttsVoice = value
  },
})

const voiceOptions = computed(() => configStore.ttsVoices)

const formats = computed<string[]>(() => configStore.ttsFormats)

/* ------------------------------ advanced UI ------------------------------ */

const advancedOpen = ref<string[]>([])

/* ------------------------------- validation ------------------------------ */

const missingApiKey = computed<boolean>(() => apiKey.value.trim().length === 0)

const apiKeyWarning = computed<string>(() =>
  isLLM.value ? '请填写大模型 API Key' : '请填写语音合成 API Key',
)

/** Whether 测试连接 can run. */
const canValidate = computed<boolean>(() => {
  if (providerId.value.length === 0) return false
  if (missingApiKey.value) return false
  if (requiresBaseUrl.value && baseUrl.value.trim().length === 0) return false
  if (isLLM.value && model.value.trim().length === 0) return false
  if (!isLLM.value && voice.value.trim().length === 0) return false
  return true
})

/** Tooltip explaining why 测试连接 is disabled. */
const validateDisabledReason = computed<string>(() => {
  if (providerId.value.length === 0) return '请先选择服务商'
  if (missingApiKey.value) return apiKeyWarning.value
  if (requiresBaseUrl.value && baseUrl.value.trim().length === 0) return '该服务商必须填写 Base URL'
  if (isLLM.value && model.value.trim().length === 0) return '请先选择或输入模型名称'
  if (!isLLM.value && voice.value.trim().length === 0) return '请先选择发音人'
  return ''
})

/** Run `POST /providers/validate` and surface the result via ElMessage. */
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
    base_url: baseUrl.value.trim().length > 0 ? baseUrl.value.trim() : null,
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
    if (error instanceof ApiError) {
      ElMessage.error(`${error.message}${error.code ? ` [${error.code}]` : ''}`)
    } else {
      ElMessage.error('连接校验失败，请稍后重试')
    }
  } finally {
    validating.value = false
  }
}

function handleProviderChange(): void {
  lastLatency.value = null
}

/** Toggle the inline hint about where the key is stored. */
const keyHint = computed<string>(() =>
  settingsStore.autoSaveCredentials
    ? '已开启“自动保存配置”：该 Key 会以明文保存在本机 localStorage 中。'
    : 'Key 仅保存在浏览器内存中，刷新页面即失效，且绝不会写入后端设置。',
)
</script>

<template>
  <el-card class="provider-card" shadow="never">
    <template #header>
      <div class="provider-card__header">
        <div class="provider-card__header-left">
          <el-icon class="provider-card__icon"><component :is="icon" /></el-icon>
          <span>{{ title }}</span>
        </div>
        <div class="provider-card__header-right">
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

    <el-form label-position="top" class="provider-card__form" @submit.prevent>
      <el-form-item :label="isLLM ? '服务商' : '服务商'">
        <el-select
          v-model="providerId"
          class="provider-card__control"
          placeholder="请选择服务商"
          :loading="configStore.providersLoading"
          @change="handleProviderChange"
        >
          <el-option
            v-for="item in options"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          >
            <span class="provider-card__option">
              <span>{{ item.name }}</span>
              <span class="provider-card__option-id t2v-mono">{{ item.id }}</span>
            </span>
          </el-option>
        </el-select>
      </el-form-item>

      <el-form-item>
        <template #label>
          <span class="provider-card__label">
            API Key
            <el-tooltip :content="keyHint" placement="top">
              <el-icon class="provider-card__label-icon"><QuestionFilled /></el-icon>
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
          class="provider-card__control"
        />
      </el-form-item>

      <el-form-item v-if="isLLM" label="模型">
        <el-select
          v-model="model"
          class="provider-card__control"
          placeholder="请选择或输入模型名称"
          filterable
          allow-create
          default-first-option
          :reserve-keyword="false"
        >
          <el-option
            v-for="item in modelOptions"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item v-else label="发音人">
        <el-select
          v-model="voice"
          class="provider-card__control"
          placeholder="请选择发音人"
          filterable
        >
          <el-option
            v-for="item in voiceOptions"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          >
            <span class="provider-card__option">
              <span>{{ item.name }}</span>
              <span class="provider-card__option-tags">
                <el-tag size="small" effect="plain" type="info">{{ genderLabel(item.gender) }}</el-tag>
                <el-tag size="small" effect="plain" type="info">{{ item.language }}</el-tag>
              </span>
            </span>
          </el-option>
        </el-select>
      </el-form-item>

      <el-collapse v-model="advancedOpen" class="provider-card__advanced">
        <el-collapse-item name="advanced">
          <template #title>
            <span class="provider-card__advanced-title">高级设置</span>
          </template>

          <el-form-item>
            <template #label>
              <span class="provider-card__label">
                Base URL
                <el-tag v-if="requiresBaseUrl" size="small" type="warning" effect="plain">必填</el-tag>
                <el-tag v-else-if="!supportsBaseUrl" size="small" type="info" effect="plain">不支持</el-tag>
              </span>
            </template>
            <el-input
              v-model="baseUrl"
              class="provider-card__control"
              :disabled="!supportsBaseUrl"
              clearable
              spellcheck="false"
              placeholder="留空则使用服务商默认地址，例如 https://api.openai.com/v1"
            />
            <div class="provider-card__hint">
              仅在使用自建 / 代理网关时填写，需包含版本路径（如 /v1）。
            </div>
          </el-form-item>

          <el-form-item v-if="!isLLM" label="支持的音频格式">
            <div class="provider-card__tags">
              <el-tag v-for="item in formats" :key="item" size="small" effect="plain">
                {{ item }}
              </el-tag>
              <span v-if="formats.length === 0" class="t2v-muted">—</span>
            </div>
          </el-form-item>
        </el-collapse-item>
      </el-collapse>

      <div class="provider-card__actions">
        <el-tooltip
          :content="validateDisabledReason || '调用 POST /providers/validate 校验凭据'"
          placement="top"
        >
          <span class="provider-card__validate-wrap">
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
.provider-card {
  height: 100%;
}

.provider-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.provider-card__header-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.provider-card__header-right {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.provider-card__icon {
  color: var(--t2v-color-primary);
}

.provider-card__form :deep(.el-form-item) {
  margin-bottom: 16px;
}

.provider-card__form :deep(.el-form-item:last-of-type) {
  margin-bottom: 0;
}

.provider-card__control {
  width: 100%;
}

.provider-card__label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.provider-card__label-icon {
  color: var(--t2v-text-muted);
  cursor: help;
}

.provider-card__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.provider-card__option-id {
  color: var(--t2v-text-muted);
}

.provider-card__option-tags {
  display: inline-flex;
  gap: 4px;
}

.provider-card__advanced {
  margin-top: 4px;
  border-top: 1px dashed var(--t2v-border-color);
  border-bottom: none;
}

.provider-card__advanced-title {
  font-size: 13px;
  color: var(--t2v-text-secondary);
}

.provider-card__advanced :deep(.el-collapse-item__content) {
  padding-bottom: 4px;
}

.provider-card__hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--t2v-text-muted);
  line-height: 1.5;
}

.provider-card__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.provider-card__actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.provider-card__validate-wrap {
  display: inline-flex;
}
</style>