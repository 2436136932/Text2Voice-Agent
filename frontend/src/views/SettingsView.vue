<script setup lang="ts">
/**
 * `/settings` — the single place where everything is configured.
 *
 * Page order mirrors the user's mental model:
 *   1. 服务商与密钥  — LLM + TTS provider, key, model, voice, clone manager
 *   2. 默认参数      — the defaults the generate page starts from
 *   3. 请求与保存    — history / credential persistence / timeout
 *   4. 本机凭据      — what is currently held in this browser, and how to wipe it
 *
 * Credentials are never sent to `PUT /settings` (the backend schema has no
 * credential field at all); they travel only to the endpoints that call a
 * vendor, and reach localStorage only when the user opts in below.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Delete, InfoFilled, Refresh, Setting } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ErrorAlert from '@/components/ErrorAlert.vue'
import ProviderSettingsPanel from '@/components/ProviderSettingsPanel.vue'
import VoiceClonePanel from '@/components/VoiceClonePanel.vue'
import { ApiError } from '@/api/http'
import type { UpdateSettingsRequest } from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'

const settingsStore = useSettingsStore()
const configStore = useConfigStore()

/* --------------------------------- form ---------------------------------- */

interface SettingsForm {
  default_llm_provider: string
  default_llm_model: string
  default_tts_provider: string
  default_tts_voice: string
  default_speed: number
  default_system_prompt: string
  auto_save_history: boolean
  auto_save_credentials: boolean
  request_timeout_seconds: number
}

function emptyForm(): SettingsForm {
  return {
    default_llm_provider: '',
    default_llm_model: '',
    default_tts_provider: '',
    default_tts_voice: '',
    default_speed: 1.0,
    default_system_prompt: '你是一个乐于助人的中文助手，回答简洁、口语化，适合朗读。',
    auto_save_history: true,
    auto_save_credentials: true,
    request_timeout_seconds: 120,
  }
}

const form = reactive<SettingsForm>(emptyForm())
const formError = ref<ApiError | null>(null)
const loading = ref(false)
const saving = ref(false)

function syncFormFromStore(): void {
  const settings = settingsStore.effective
  form.default_llm_provider = settings.default_llm_provider
  form.default_llm_model = settings.default_llm_model
  form.default_tts_provider = settings.default_tts_provider
  form.default_tts_voice = settings.default_tts_voice
  form.default_speed = settings.default_speed
  form.default_system_prompt = settings.default_system_prompt
  form.auto_save_history = settings.auto_save_history
  form.auto_save_credentials = settings.auto_save_credentials
  form.request_timeout_seconds = settings.request_timeout_seconds
}

function buildPatch(): UpdateSettingsRequest {
  return {
    default_llm_provider: form.default_llm_provider,
    default_llm_model: form.default_llm_model,
    default_tts_provider: form.default_tts_provider,
    default_tts_voice: form.default_tts_voice,
    default_speed: form.default_speed,
    default_system_prompt: form.default_system_prompt,
    auto_save_history: form.auto_save_history,
    auto_save_credentials: form.auto_save_credentials,
    request_timeout_seconds: form.request_timeout_seconds,
    // Staged from the provider panels via "设为默认".
    ...settingsStore.defaultsPatch(),
  }
}

onMounted(async () => {
  loading.value = true
  formError.value = null
  await configStore.loadProviders()
  const settings = await settingsStore.load()
  if (settings) syncFormFromStore()
  else if (settingsStore.error) {
    formError.value = new ApiError({ code: 'SETTINGS_LOAD_FAILED', message: settingsStore.error })
  }
  loading.value = false
})

/* -------------------------------- actions -------------------------------- */

async function handleSave(): Promise<void> {
  if (form.request_timeout_seconds < 5 || form.request_timeout_seconds > 1800) {
    ElMessage.warning('请求超时时间需在 5 – 1800 秒之间')
    return
  }
  saving.value = true
  formError.value = null
  try {
    const updated = await settingsStore.save(buildPatch())
    if (!updated) {
      formError.value = new ApiError({
        code: 'SETTINGS_SAVE_FAILED',
        message: settingsStore.error ?? '保存设置失败',
      })
      ElMessage.error('保存设置失败')
      return
    }
    settingsStore.setAutoSaveCredentials(updated.auto_save_credentials)
    if (updated.auto_save_credentials) configStore.persistCredentials()
    else configStore.clearCredentials()

    settingsStore.clearPendingDefaults()
    configStore.applyDefaults(updated)
    syncFormFromStore()
    hasCachedCredentials.value = configStore.hasCachedCredentials()
    ElMessage.success('设置已保存')
  } finally {
    saving.value = false
  }
}

async function handleReset(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要放弃当前修改并重新加载服务端设置吗？', '重置确认', {
      type: 'warning',
      confirmButtonText: '重置',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  loading.value = true
  settingsStore.clearPendingDefaults()
  const settings = await settingsStore.load()
  if (settings) syncFormFromStore()
  loading.value = false
  ElMessage.info('已恢复为服务端设置')
}

/* --------------------------- local credentials --------------------------- */

const hasCachedCredentials = ref<boolean>(configStore.hasCachedCredentials())

const keyPresence = computed<{ llm: boolean; tts: boolean }>(() => ({
  llm: configStore.llmApiKey.trim().length > 0,
  tts: configStore.ttsApiKey.trim().length > 0,
}))

async function handleClearCredentials(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将清除浏览器中保存的 API Key（内存 + localStorage），确定继续吗？',
      '清除本地凭据',
      { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  configStore.clearCredentials()
  hasCachedCredentials.value = false
  ElMessage.success('本地凭据已清除')
}

function handleAutoSaveChange(value: boolean): void {
  settingsStore.setAutoSaveCredentials(value)
  if (value) {
    configStore.persistCredentials()
    hasCachedCredentials.value = configStore.hasCachedCredentials()
  } else {
    configStore.clearCredentials()
    hasCachedCredentials.value = false
  }
}

/* ------------------------------- selectors ------------------------------- */

const llmProviders = computed(() => configStore.llmProviders)
const ttsProviders = computed(() => configStore.ttsProviders)

const llmModels = computed(() => {
  const provider = llmProviders.value.find((item) => item.id === configStore.llmProviderId)
  return provider?.models ?? []
})

const ttsVoices = computed(() => {
  const provider = ttsProviders.value.find((item) => item.id === configStore.ttsProviderId)
  return provider?.voices ?? []
})

const ttsSpeedRange = computed<{ min: number; max: number }>(() => {
  const provider = ttsProviders.value.find((item) => item.id === configStore.ttsProviderId)
  return { min: provider?.speed_min ?? 0.25, max: provider?.speed_max ?? 4.0 }
})

const speedLabel = computed<string>(() => `${form.default_speed.toFixed(2)}x`)

/** Push the currently selected provider/model/voice into the defaults form. */
function useCurrentAsDefaults(): void {
  form.default_llm_provider = configStore.llmProviderId
  form.default_llm_model = configStore.llmModel
  form.default_tts_provider = configStore.ttsProviderId
  form.default_tts_voice = configStore.ttsVoice
  form.default_system_prompt = configStore.systemPrompt
  ElMessage.success('已同步当前配置到默认参数，别忘了点「保存设置」')
}

function handleLLMProviderChange(value: string): void {
  const provider = llmProviders.value.find((item) => item.id === value)
  if (provider) form.default_llm_model = provider.default_model
}

function handleTTSProviderChange(value: string): void {
  const provider = ttsProviders.value.find((item) => item.id === value)
  if (provider) form.default_tts_voice = provider.default_voice
}
</script>

<template>
  <div class="t2v-page settings-view">
    <div class="t2v-page__title">
      <h2>系统设置</h2>
      <span class="t2v-page__subtitle">
        服务商、密钥、模型与音色都在这里配置；「语音生成」页只负责输入文本和点击生成。
      </span>
    </div>

    <ErrorAlert
      v-if="formError"
      :error="formError"
      title="设置操作失败"
      :retryable="false"
      @dismiss="formError = null"
    />

    <!-- ================= 1. 服务商与密钥 ================= -->
    <el-card shadow="never" class="settings-view__section">
      <template #header>
        <div class="settings-view__section-head">
          <el-icon><Setting /></el-icon>
          <span>服务商与密钥</span>
          <span class="t2v-muted settings-view__section-note">
            密钥仅保存在本机浏览器，不会写入后端数据库
          </span>
        </div>
      </template>

      <div class="t2v-grid t2v-grid--2">
        <ProviderSettingsPanel kind="llm" />
        <ProviderSettingsPanel kind="tts" />
      </div>

      <el-divider />

      <VoiceClonePanel />
    </el-card>

    <!-- ================= 2. 默认参数 ================= -->
    <el-card v-loading="loading" shadow="never" class="settings-view__section">
      <template #header>
        <div class="settings-view__header">
          <span>默认生成参数</span>
          <div class="settings-view__header-right">
            <span v-if="settingsStore.effective.updated_at" class="t2v-muted settings-view__updated">
              最后更新：{{ settingsStore.effective.updated_at }}
            </span>
            <el-button link size="small" @click="useCurrentAsDefaults">用当前配置填充</el-button>
          </div>
        </div>
      </template>

      <el-form label-position="top" @submit.prevent>
        <div class="t2v-grid t2v-grid--2">
          <el-form-item label="默认大模型服务商">
            <el-select
              v-model="form.default_llm_provider"
              class="settings-view__control"
              placeholder="请选择服务商"
              @change="handleLLMProviderChange"
            >
              <el-option v-for="item in llmProviders" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
          </el-form-item>

          <el-form-item label="默认大模型">
            <el-select
              v-model="form.default_llm_model"
              class="settings-view__control"
              placeholder="请选择或输入模型"
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
            >
              <el-option v-for="item in llmModels" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
          </el-form-item>

          <el-form-item label="默认语音合成服务商">
            <el-select
              v-model="form.default_tts_provider"
              class="settings-view__control"
              placeholder="请选择服务商"
              @change="handleTTSProviderChange"
            >
              <el-option v-for="item in ttsProviders" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
          </el-form-item>

          <el-form-item label="默认音色">
            <el-select
              v-model="form.default_tts_voice"
              class="settings-view__control"
              placeholder="请选择音色"
              filterable
            >
              <el-option v-for="item in ttsVoices" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item :label="`默认语速（${speedLabel}）`">
          <el-slider
            v-model="form.default_speed"
            :min="ttsSpeedRange.min"
            :max="ttsSpeedRange.max"
            :step="0.05"
            show-input
            :show-input-controls="false"
            class="settings-view__slider"
          />
        </el-form-item>

        <el-form-item label="默认系统提示词">
          <el-input
            v-model="form.default_system_prompt"
            type="textarea"
            :rows="3"
            maxlength="4000"
            show-word-limit
            placeholder="告诉大模型应该如何回答，例如：回答简洁、口语化，适合朗读。"
          />
        </el-form-item>
      </el-form>
    </el-card>

    <!-- ================= 3. 请求与保存 ================= -->
    <el-card shadow="never" class="settings-view__section">
      <template #header>
        <span>请求与保存</span>
      </template>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="请求超时（秒）">
          <el-input-number
            v-model="form.request_timeout_seconds"
            :min="5"
            :max="1800"
            :step="10"
            controls-position="right"
          />
          <div class="settings-view__hint">
            大模型 + 语音合成两步串行，长文本建议 120 秒以上。
          </div>
        </el-form-item>

        <el-form-item label="自动保存历史记录">
          <el-switch v-model="form.auto_save_history" />
        </el-form-item>

        <el-form-item label="自动保存配置（记住 API Key 与服务商选择）">
          <el-switch
            v-model="form.auto_save_credentials"
            @change="(value: boolean) => handleAutoSaveChange(value)"
          />
          <div class="settings-view__hint">
            开启后，Key、服务商、模型、音色变更时会自动保存到本机浏览器，下次打开无需重填。
          </div>
        </el-form-item>

        <el-alert
          v-if="form.auto_save_credentials"
          type="info"
          show-icon
          :closable="false"
          title="API Key 以明文保存在本机 localStorage"
          description="仅本机浏览器可读，后端始终不会接收或保存密钥。若使用公用电脑，请关闭此开关。"
        />
      </el-form>
    </el-card>

    <!-- ================= 4. 本机凭据 ================= -->
    <el-card shadow="never" class="settings-view__section">
      <template #header>
        <span>本机凭据</span>
      </template>

      <el-descriptions :column="1" border size="small">
        <el-descriptions-item label="大模型 Key">
          <el-tag :type="keyPresence.llm ? 'success' : 'info'" size="small" effect="plain">
            {{ keyPresence.llm ? '已填写（仅内存）' : '未填写' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="语音合成 Key">
          <el-tag :type="keyPresence.tts ? 'success' : 'info'" size="small" effect="plain">
            {{ keyPresence.tts ? '已填写（仅内存）' : '未填写' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="localStorage 缓存">
          <el-tag :type="hasCachedCredentials ? 'warning' : 'info'" size="small" effect="plain">
            {{ hasCachedCredentials ? '存在已保存的 Key' : '无' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <div class="settings-view__actions">
        <el-button :icon="Delete" type="danger" plain @click="handleClearCredentials">
          清除本地凭据
        </el-button>
        <el-tooltip content="密钥只在调用服务商时随请求发送，后端不落库、不写日志" placement="top">
          <el-icon class="settings-view__info"><InfoFilled /></el-icon>
        </el-tooltip>
      </div>
    </el-card>

    <!-- ================= 保存 / 重置 ================= -->
    <div class="settings-view__footer">
      <el-button type="primary" size="large" :loading="saving" @click="handleSave">
        保存设置
      </el-button>
      <el-button size="large" :icon="Refresh" :disabled="saving" @click="handleReset">
        重置
      </el-button>
      <span class="t2v-muted">密钥不参与保存，只会保存在浏览器中。</span>
    </div>
  </div>
</template>

<style scoped>
.settings-view__section {
  margin-bottom: 16px;
}

.settings-view__section-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.settings-view__section-note {
  font-weight: 400;
  font-size: 12px;
}

.settings-view__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.settings-view__header-right {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.settings-view__updated {
  font-size: 12px;
}

.settings-view__control {
  width: 100%;
}

.settings-view__slider {
  width: 100%;
}

.settings-view__hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.settings-view__actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.settings-view__info {
  color: var(--el-text-color-secondary);
  cursor: help;
}

.settings-view__footer {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 0 24px;
}
</style>
