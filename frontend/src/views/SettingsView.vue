<script setup lang="ts">
/**
 * `/settings` — server-side defaults (GET/PUT `/settings`) plus a local
 * credential panel that wipes keys from the config store and localStorage.
 *
 * API keys are never part of this payload: the backend settings endpoint has no
 * credential fields at all.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Delete, InfoFilled, Refresh, Select } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ErrorAlert from '@/components/ErrorAlert.vue'
import { ApiError } from '@/api/http'
import type { UpdateSettingsRequest } from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'
import { genderLabel } from '@/utils/format'

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
    default_llm_provider: 'openai',
    default_llm_model: 'gpt-4o-mini',
    default_tts_provider: 'openai',
    default_tts_voice: 'alloy',
    default_speed: 1.0,
    default_system_prompt: '你是一个乐于助人的助手。',
    auto_save_history: true,
    auto_save_credentials: false,
    request_timeout_seconds: 120,
  }
}

const form = reactive<SettingsForm>(emptyForm())
const formError = ref<ApiError | null>(null)
const loading = ref(false)

/** Copy the store's settings into the local form. */
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

/** Build the partial payload sent to `PUT /settings`. */
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
  }
}

onMounted(async () => {
  loading.value = true
  formError.value = null
  const settings = await settingsStore.load()
  if (settings) syncFormFromStore()
  else if (settingsStore.error) {
    formError.value = new ApiError({ code: 'SETTINGS_LOAD_FAILED', message: settingsStore.error })
  }
  loading.value = false
})

/* -------------------------------- actions -------------------------------- */

const saving = ref(false)

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
    // Keep the local credential-persistence mirror aligned with the server value.
    settingsStore.setAutoSaveCredentials(updated.auto_save_credentials)
    if (updated.auto_save_credentials) configStore.persistCredentials()
    else configStore.clearCredentials()
    // Seed the generation form with the new defaults.
    configStore.applyDefaults(updated)
    syncFormFromStore()
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

/** React to the auto-save switch: persist or wipe immediately. */
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
  const provider = llmProviders.value.find((item) => item.id === form.default_llm_provider)
  return provider?.models ?? []
})

const ttsVoices = computed(() => {
  const provider = ttsProviders.value.find((item) => item.id === form.default_tts_provider)
  return provider?.voices ?? []
})

const ttsSpeedRange = computed<{ min: number; max: number }>(() => {
  const provider = ttsProviders.value.find((item) => item.id === form.default_tts_provider)
  return { min: provider?.speed_min ?? 0.25, max: provider?.speed_max ?? 4.0 }
})

const speedLabel = computed<string>(() => `${form.default_speed.toFixed(2)}x`)

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
      <span class="t2v-page__subtitle">配置默认服务商、语音参数与请求行为。</span>
    </div>

    <ErrorAlert
      v-if="formError"
      :error="formError"
      title="设置操作失败"
      :retryable="false"
      @dismiss="formError = null"
    />

    <el-card v-loading="loading" shadow="never">
      <template #header>
        <div class="settings-view__header">
          <span>默认生成参数</span>
          <span v-if="settingsStore.effective.updated_at" class="t2v-muted settings-view__updated">
            最后更新：{{ settingsStore.effective.updated_at }}
          </span>
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
              <el-option
                v-for="item in llmProviders"
                :key="item.id"
                :label="item.name"
                :value="item.id"
              />
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
              <el-option
                v-for="item in llmModels"
                :key="item.id"
                :label="item.name"
                :value="item.id"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="默认语音合成服务商">
            <el-select
              v-model="form.default_tts_provider"
              class="settings-view__control"
              placeholder="请选择服务商"
              @change="handleTTSProviderChange"
            >
              <el-option
                v-for="item in ttsProviders"
                :key="item.id"
                :label="item.name"
                :value="item.id"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="默认发音人">
            <el-select
              v-model="form.default_tts_voice"
              class="settings-view__control"
              placeholder="请选择发音人"
              filterable
            >
              <el-option
                v-for="item in ttsVoices"
                :key="item.id"
                :label="item.name"
                :value="item.id"
              >
                <span class="settings-view__option">
                  <span>{{ item.name }}</span>
                  <el-tag size="small" effect="plain" type="info">
                    {{ genderLabel(item.gender) }}
                  </el-tag>
                </span>
              </el-option>
            </el-select>
          </el-form-item>
        </div>

        <el-form-item label="默认语速">
          <div class="settings-view__slider-row">
            <el-slider
              v-model="form.default_speed"
              class="settings-view__slider"
              :min="ttsSpeedRange.min"
              :max="ttsSpeedRange.max"
              :step="0.05"
            />
            <span class="settings-view__value t2v-mono">{{ speedLabel }}</span>
          </div>
        </el-form-item>

        <el-form-item label="默认系统提示词">
          <el-input
            v-model="form.default_system_prompt"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            resize="vertical"
            placeholder="例如：你是一个乐于助人的助手。"
          />
        </el-form-item>

        <el-divider />

        <div class="t2v-grid t2v-grid--2">
          <el-form-item label="自动保存历史记录">
            <div class="settings-view__switch-row">
              <el-switch v-model="form.auto_save_history" />
              <span class="t2v-muted settings-view__switch-hint">
                开启后，每次生成的结果会自动写入后端历史记录。
              </span>
            </div>
          </el-form-item>

          <el-form-item label="请求超时时间（秒）">
            <el-input-number
              v-model="form.request_timeout_seconds"
              :min="5"
              :max="1800"
              :step="10"
              controls-position="right"
              class="settings-view__number"
            />
            <div class="settings-view__switch-hint t2v-muted">
              同时作用于前端 axios 超时；后端默认 120 秒。
            </div>
          </el-form-item>
        </div>

        <el-form-item label="自动保存配置（API Key）">
          <div class="settings-view__switch-row">
            <el-switch
              v-model="form.auto_save_credentials"
              @change="handleAutoSaveChange(form.auto_save_credentials)"
            />
            <span class="t2v-muted settings-view__switch-hint">
              默认关闭。开启后 API Key 会保存在本机浏览器中，便于下次直接使用。
            </span>
          </div>
        </el-form-item>

        <el-alert
          v-if="form.auto_save_credentials"
          class="settings-view__warning"
          type="warning"
          show-icon
          :closable="false"
          title="安全提示：API Key 将以明文保存在本机 localStorage"
        >
          <ul class="settings-view__warning-list">
            <li>localStorage 中的内容不会被加密，同一台电脑上的其他程序或用户可能读取到该 Key。</li>
            <li>Key 只保存在当前浏览器，不会上传到后端，也不会写入 <code>/settings</code>。</li>
            <li>请勿在公用 / 共享电脑上开启此选项；建议改用环境变量或密钥管理服务。</li>
            <li>关闭该开关或点击下方的“清除本地凭据”即可立即删除。</li>
          </ul>
        </el-alert>

        <div class="settings-view__actions">
          <el-button type="primary" :icon="Select" :loading="saving" @click="handleSave">
            保存
          </el-button>
          <el-button :icon="Refresh" :disabled="saving" @click="handleReset">重置</el-button>
        </div>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="settings-view__header">
          <span>本地凭据</span>
          <el-tag :type="hasCachedCredentials ? 'warning' : 'info'" size="small" effect="plain">
            {{ hasCachedCredentials ? 'localStorage 中存在凭据' : '未保存任何凭据' }}
          </el-tag>
        </div>
      </template>

      <div class="settings-view__credentials">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="大模型 API Key">
            <el-tag :type="keyPresence.llm ? 'success' : 'info'" size="small" effect="plain">
              {{ keyPresence.llm ? '已在内存中填写' : '未填写' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="语音合成 API Key">
            <el-tag :type="keyPresence.tts ? 'success' : 'info'" size="small" effect="plain">
              {{ keyPresence.tts ? '已在内存中填写' : '未填写' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="localStorage 键名">
            <span class="t2v-mono">t2v:credentials</span>
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          class="settings-view__info"
          type="info"
          show-icon
          :closable="false"
          title="API Key 的处理方式"
        >
          <ul class="settings-view__warning-list">
            <li>Key 由你在“语音生成”页输入，仅保存在当前页面内存（Pinia store）中。</li>
            <li>每次请求随 body 发送给后端，用于调用对应服务商。</li>
            <li>绝不会写入 <code>/settings</code>，也不会出现在任何日志中。</li>
          </ul>
        </el-alert>

        <div class="settings-view__actions">
          <el-button
            type="danger"
            plain
            :icon="Delete"
            :disabled="!hasCachedCredentials && !keyPresence.llm && !keyPresence.tts"
            @click="handleClearCredentials"
          >
            清除本地凭据
          </el-button>
          <el-tooltip content="清除后需要重新输入 API Key" placement="top">
            <el-icon class="settings-view__info-icon"><InfoFilled /></el-icon>
          </el-tooltip>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.settings-view__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.settings-view__updated {
  font-size: 12px;
  font-weight: 400;
}

.settings-view__control {
  width: 100%;
}

.settings-view__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.settings-view__slider-row {
  display: flex;
  align-items: center;
  gap: 16px;
  width: 100%;
}

.settings-view__slider {
  flex: 1 1 auto;
  min-width: 0;
}

.settings-view__value {
  flex: 0 0 auto;
  min-width: 52px;
  text-align: right;
  font-weight: 600;
  color: var(--t2v-text-secondary);
}

.settings-view__number {
  width: 180px;
}

.settings-view__switch-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.settings-view__switch-hint {
  font-size: 12px;
  line-height: 1.5;
}

.settings-view__warning,
.settings-view__info {
  margin: 4px 0 16px;
}

.settings-view__warning-list {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.8;
}

.settings-view__actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.settings-view__credentials {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.settings-view__info-icon {
  color: var(--t2v-text-muted);
}
</style>