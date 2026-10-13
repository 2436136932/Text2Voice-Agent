<script setup lang="ts">
/**
 * `/settings` — system-level configuration only.
 *
 * Sections:
 *   1. 服务商与密钥  — who to call, and with which key (plus voice cloning)
 *   2. 请求与保存    — history persistence, credential persistence, timeout
 *   3. 本机凭据      — what this browser holds, and how to wipe it
 *
 * Per-generation parameters (speed, format, system prompt, model, voice) live
 * on the generate page instead. They used to be duplicated here as 「默认生成
 * 参数」, which meant configuring the same model/voice twice and risking the two
 * copies disagreeing.
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
import { ApiError } from '@/api/http'
import type { UpdateSettingsRequest } from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'

const settingsStore = useSettingsStore()
const configStore = useConfigStore()

/* --------------------------------- form ---------------------------------- */

interface SettingsForm {
  auto_save_history: boolean
  auto_save_credentials: boolean
  request_timeout_seconds: number
}

function emptyForm(): SettingsForm {
  return {
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
  form.auto_save_history = settings.auto_save_history
  form.auto_save_credentials = settings.auto_save_credentials
  form.request_timeout_seconds = settings.request_timeout_seconds
}

function buildPatch(): UpdateSettingsRequest {
  return {
    auto_save_history: form.auto_save_history,
    auto_save_credentials: form.auto_save_credentials,
    request_timeout_seconds: form.request_timeout_seconds,
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
</script>

<template>
  <div class="t2v-page settings-view">
    <div class="t2v-page__title">
      <h2>系统设置</h2>
      <span class="t2v-page__subtitle">
        这里配置服务商、密钥与运行行为；语速、格式、提示词等每次生成要调的参数在「语音生成」页。
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
    </el-card>

    <!-- ================= 2. 请求与保存 ================= -->
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
