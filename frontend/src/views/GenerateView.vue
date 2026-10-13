<script setup lang="ts">
/**
 * `/generate` — the generation workbench.
 *
 * Layout (after the provider-config consolidation):
 *   1. a read-only "current configuration" bar that links to /settings
 *   2. a mode switch:  文字转语音 | 声音克隆 | 直接配音
 *   3. mode-specific input (reference clip picker for cloning)
 *   4. text input + per-generation parameters (speed / format)
 *   5. submit + progress + result
 *
 * Provider, key, model and voice selection all live on /settings now; this page
 * only reads them. That removes the duplicated provider/voice controls that used
 * to appear in three different cards here.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Microphone, Setting, VideoCamera, Warning } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import ErrorAlert from '@/components/ErrorAlert.vue'
import MediaPicker from '@/components/MediaPicker.vue'
import ResultCard from '@/components/ResultCard.vue'
import TextInputCard from '@/components/TextInputCard.vue'
import { ApiError } from '@/api/http'
import { fetchCapabilities, fetchTask, fetchTaskResult, submitTask } from '@/api'
import type {
  CapabilitiesResponse,
  GenerateOptions,
  GenerateResponse,
  MediaInfo,
  TaskStatusResponse,
} from '@/api/types'
import { useConfigStore, type GenerationMode } from '@/stores/config'
import { stageLabel } from '@/utils/format'

const configStore = useConfigStore()
const router = useRouter()

/* ------------------------------- polling ------------------------------- */

const POLL_INTERVAL_MS = 900
const POLL_TIMEOUT_MS = 5 * 60 * 1000

const generating = ref(false)
const task = ref<TaskStatusResponse | null>(null)
const result = ref<GenerateResponse | null>(null)
const error = ref<ApiError | null>(null)
const elapsedSeconds = ref(0)
const capabilities = ref<CapabilitiesResponse | null>(null)
const referenceMedia = ref<MediaInfo | null>(null)

let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null
let pollStartedAt = 0

const stageText = computed(() => stageLabel(task.value?.stage ?? 'queued'))
const progress = computed(() => Math.min(100, Math.max(0, task.value?.progress ?? 0)))
const stageMessage = computed(() => task.value?.message ?? '')
const elapsedText = computed(() => `${elapsedSeconds.value}s`)
const busy = computed(() => generating.value)

/* ------------------------------ mode stuff ------------------------------ */

const MODES: { value: GenerationMode; label: string; hint: string; icon: unknown }[] = [
  {
    value: 'tts',
    label: '文字转语音',
    hint: '大模型根据你的输入写一段回复，再把回复读出来',
    icon: Microphone,
  },
  {
    value: 'clone',
    label: '声音克隆',
    hint: '上传一段音频或视频作为音色参考，用这个声音读你输入的文字',
    icon: VideoCamera,
  },
  {
    value: 'text2audio',
    label: '直接配音',
    hint: '跳过语言模型，直接把输入的文字合成为语音',
    icon: Microphone,
  },
]

const mode = computed<GenerationMode>({
  get: () => configStore.mode,
  set: (value) => {
    configStore.mode = value
    error.value = null
  },
})

const activeMode = computed(() => MODES.find((item) => item.value === mode.value) ?? MODES[0])

/** Providers that can actually clone — used to build an actionable warning. */
const cloneCapableNames = computed<string[]>(() => {
  const list = capabilities.value?.voice_clone.supported_providers ?? []
  const reference = capabilities.value?.voice_clone.reference_audio_providers ?? []
  const ids = Array.from(new Set([...list, ...reference]))
  return ids
    .map((id) => configStore.ttsProviders.find((item) => item.id === id)?.name ?? id)
    .filter(Boolean)
})

/**
 * Whether the selected TTS provider can consume a reference clip at all.
 * When it cannot we block submission and explain exactly what to change —
 * this is the "friendly error" the user asked for, surfaced *before* the call
 * instead of after a confusing upstream failure.
 */
const cloneSupported = computed(() => configStore.supportsAnyCloning)

const cloneBlockReason = computed<string | null>(() => {
  if (mode.value !== 'clone') return null
  if (!configStore.ttsProviderId) return '请先在「系统设置」中选择语音合成服务商'
  if (!cloneSupported.value) {
    const names = cloneCapableNames.value.length > 0 ? cloneCapableNames.value.join('、') : 'ElevenLabs、MiniMax、自定义(OpenAI 兼容)'
    return `当前服务商「${configStore.ttsProvider?.name ?? configStore.ttsProviderId}」不支持声音克隆。请在「系统设置」中切换到 ${names}，或改用「文字转语音」模式。`
  }
  if (!referenceMedia.value) return '请先上传一段参考音频或视频（建议 5–30 秒清晰人声）'
  return null
})

/** Blocking reasons that must be shown above the submit button. */
const blockingReasons = computed<string[]>(() => {
  const reasons = [...configStore.disabledReasons]
  if (cloneBlockReason.value) reasons.unshift(cloneBlockReason.value)
  return reasons
})

const canSubmit = computed(() => configStore.canGenerate && !cloneBlockReason.value && !busy.value)

/** 直接配音 skips the model, so its parameters are irrelevant and hidden. */
const needsLlm = computed<boolean>(() => configStore.mode !== 'text2audio')

/* ------------------------------- actions ------------------------------- */

function stopTimers(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  if (tickTimer !== null) {
    clearInterval(tickTimer)
    tickTimer = null
  }
}

function finish(): void {
  stopTimers()
  generating.value = false
}

async function loadResult(taskId: string): Promise<void> {
  try {
    result.value = await fetchTaskResult(taskId)
    ElMessage.success('生成完成')
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : null
    if (!error.value) ElMessage.error('获取结果失败')
  }
}

async function poll(taskId: string): Promise<void> {
  try {
    const status = await fetchTask(taskId)
    task.value = status

    if (status.status === 'succeeded') {
      finish()
      await loadResult(taskId)
      return
    }

    if (status.status === 'failed' || status.status === 'canceled') {
      finish()
      error.value = status.error
        ? new ApiError({
            code: status.error.code,
            message: status.error.message,
            detail: status.error.detail,
            requestId: status.error.request_id,
          })
        : new ApiError({
            code: status.status === 'canceled' ? 'TASK_CANCELED' : 'TASK_FAILED',
            message: status.status === 'canceled' ? '任务已取消' : '任务执行失败',
          })
      return
    }

    if (Date.now() - pollStartedAt > POLL_TIMEOUT_MS) {
      finish()
      error.value = new ApiError({
        code: 'REQUEST_TIMEOUT',
        message: '任务轮询超过 5 分钟仍未完成，请稍后在历史记录中查看结果。',
      })
    }
  } catch (caught) {
    finish()
    error.value = caught instanceof ApiError ? caught : null
    if (!error.value) ElMessage.error('查询任务状态失败')
  }
}

async function runGeneration(overrides?: Partial<GenerateOptions>): Promise<void> {
  error.value = null
  result.value = null
  task.value = null
  elapsedSeconds.value = 0

  let payload
  try {
    payload = configStore.buildRequest(overrides)
  } catch (caught) {
    ElMessage.warning(caught instanceof Error ? caught.message : '配置不完整')
    return
  }

  generating.value = true
  try {
    const submitted = await submitTask(payload)
    task.value = {
      task_id: submitted.task_id,
      status: submitted.status,
      stage: submitted.stage,
      progress: submitted.progress,
      message: submitted.message,
      created_at: null,
      started_at: null,
      finished_at: null,
      elapsed_ms: null,
      error: null,
    }

    pollStartedAt = Date.now()
    pollTimer = setInterval(() => void poll(submitted.task_id), POLL_INTERVAL_MS)
    tickTimer = setInterval(() => {
      elapsedSeconds.value = Math.round((Date.now() - pollStartedAt) / 1000)
    }, 1000)
  } catch (caught) {
    generating.value = false
    if (caught instanceof ApiError) {
      error.value = caught
    } else {
      ElMessage.error('提交任务失败')
    }
  }
}

function handleRetry(): void {
  void runGeneration()
}

function handleRegenerate(): void {
  void runGeneration()
}

/**
 * Re-synthesize only the audio for the reply we already have.
 *
 * `options.reuse_reply_text` makes this a genuine TTS-only run: the LLM is
 * skipped entirely, so the reply text is preserved byte-for-byte.
 */
function handleRegenerateTts(): void {
  const previousReply = result.value?.reply_text?.trim()
  if (!previousReply) {
    ElMessage.warning('没有可复用的回复文本，请使用「重新生成」')
    return
  }
  void runGeneration({ reuse_reply_text: previousReply, fallback_enabled: false })
}

function goToSettings(): void {
  void router.push('/settings')
}

/* ------------------------------ lifecycle ------------------------------ */

onMounted(async () => {
  try {
    capabilities.value = await fetchCapabilities()
  } catch {
    // Capability probing is best-effort; the UI degrades to "try and see".
  }
})

onBeforeUnmount(stopTimers)

const referenceHint = computed<string>(() => {
  const ffmpeg = capabilities.value?.ffmpeg
  if (ffmpeg && !ffmpeg.available) {
    return '当前环境未检测到 ffmpeg：WAV 可直接裁剪，MP3/MP4 请先转为 WAV（或安装 ffmpeg 后自动启用）。'
  }
  return '建议 5–30 秒、单人清晰人声、无明显背景音乐。'
})
</script>

<template>
  <div class="t2v-page generate-view">
    <div class="generate-view__head">
      <div>
        <h2 class="t2v-page-title">语音生成</h2>
        <p class="t2v-page-subtitle">{{ activeMode.hint }}</p>
      </div>
      <el-button :icon="Setting" @click="goToSettings">服务商配置</el-button>
    </div>

    <!-- ---------------- 当前配置（只读） ---------------- -->
    <el-card class="config-bar" shadow="never">
      <div class="config-bar__row">
        <div class="config-bar__group">
          <span class="config-bar__label">大模型</span>
          <el-tag v-if="configStore.llmProvider" type="primary" effect="plain" size="small">
            {{ configStore.llmProvider.name }}
          </el-tag>
          <span class="config-bar__value t2v-mono">{{ configStore.llmModel || '—' }}</span>
          <el-tag v-if="!configStore.llmApiKey" type="danger" size="small" effect="plain">缺少 Key</el-tag>
        </div>
        <el-divider direction="vertical" />
        <div class="config-bar__group">
          <span class="config-bar__label">语音合成</span>
          <el-tag v-if="configStore.ttsProvider" type="success" effect="plain" size="small">
            {{ configStore.ttsProvider.name }}
          </el-tag>
          <span class="config-bar__value t2v-mono">{{ configStore.ttsVoice || '—' }}</span>
          <el-tag v-if="!configStore.ttsApiKey" type="danger" size="small" effect="plain">缺少 Key</el-tag>
          <el-tag v-if="configStore.supportsVoiceClone" type="warning" size="small" effect="plain">
            支持克隆
          </el-tag>
        </div>
        <div class="config-bar__spacer"></div>
        <el-button link type="primary" size="small" :icon="Setting" @click="goToSettings">
          修改配置
        </el-button>
      </div>
    </el-card>

    <!-- ---------------- 模式切换 ---------------- -->
    <el-card class="mode-card" shadow="never">
      <el-radio-group v-model="mode" class="mode-card__group">
        <el-radio-button v-for="item in MODES" :key="item.value" :value="item.value">
          <span class="mode-card__item">
            <el-icon><component :is="item.icon" /></el-icon>
            {{ item.label }}
          </span>
        </el-radio-button>
      </el-radio-group>

      <!-- clone mode: reference clip -->
      <div v-if="mode === 'clone'" class="mode-card__clone">
        <div class="mode-card__clone-title">参考音频 / 视频</div>
        <MediaPicker
          v-model:media-id="configStore.referenceMediaId"
          v-model:media="referenceMedia"
          :allow-trim="true"
          :max-size-mb="capabilities?.upload.max_size_mb ?? 50"
          :accept="capabilities?.upload.accept ?? '.wav,.mp3,.m4a,.mp4,.mov,.webm,.ogg,.flac,.aac'"
          :hint="referenceHint"
        />
      </div>

      <el-alert
        v-if="cloneBlockReason"
        class="mode-card__block"
        type="warning"
        show-icon
        :closable="false"
        :title="cloneBlockReason"
      />
    </el-card>

    <div class="generate-view__grid">
      <div class="generate-view__left">
        <TextInputCard />

        <el-card class="params-card" shadow="never">
          <template #header>
            <div class="params-card__header">
              <span>生成参数</span>
              <el-button link size="small" @click="configStore.resetParams()">恢复默认</el-button>
            </div>
          </template>
          <el-form label-position="top" @submit.prevent>
            <el-form-item :label="`语速（${configStore.speed.toFixed(2)}x）`">
              <el-slider
                v-model="configStore.speed"
                :min="configStore.speedMin"
                :max="configStore.speedMax"
                :step="0.05"
                :disabled="!configStore.supportsSpeed"
                show-input
                :show-input-controls="false"
              />
            </el-form-item>
            <el-form-item label="音频格式">
              <el-radio-group v-model="configStore.format" size="small">
                <el-radio-button
                  v-for="item in configStore.ttsFormats"
                  :key="item"
                  :value="item"
                >
                  {{ item }}
                </el-radio-button>
              </el-radio-group>
            </el-form-item>

            <!-- LLM behaviour lives here, not on the settings page: it shapes a
                 single reply rather than the system as a whole. -->
            <template v-if="needsLlm">
              <el-form-item :label="`温度（${configStore.temperature.toFixed(2)}）`">
                <el-slider
                  v-model="configStore.temperature"
                  :min="0"
                  :max="2"
                  :step="0.05"
                  show-input
                  :show-input-controls="false"
                />
                <div class="params-card__hint">
                  越低越稳定、越高越有创意。朗读场景建议 0.3 – 0.7。
                </div>
              </el-form-item>

              <el-form-item label="最大回复长度 (max tokens)">
                <el-input-number
                  v-model="configStore.maxTokens"
                  :min="1"
                  :max="32768"
                  :step="128"
                  controls-position="right"
                />
                <div class="params-card__hint">
                  语音合成的时长与它成正比，按需调整。
                </div>
              </el-form-item>

              <el-form-item label="系统提示词">
                <el-input
                  v-model="configStore.systemPrompt"
                  type="textarea"
                  :rows="3"
                  maxlength="4000"
                  show-word-limit
                  placeholder="告诉大模型应该如何回答，例如：回答简洁、口语化，适合朗读。"
                />
              </el-form-item>
            </template>

            <el-form-item>
              <el-checkbox v-model="configStore.autoSave">保存到历史记录</el-checkbox>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- ---------------- 提交 ---------------- -->
        <el-card class="submit-card" shadow="never">
          <el-tooltip
            :disabled="canSubmit"
            :content="blockingReasons.join('；')"
            placement="top"
          >
            <span class="submit-card__wrap">
              <el-button
                type="primary"
                size="large"
                :loading="busy"
                :disabled="!canSubmit"
                :icon="activeMode.icon as never"
                @click="runGeneration()"
              >
                {{ busy ? '生成中…' : '开始生成' }}
              </el-button>
            </span>
          </el-tooltip>

          <div v-if="blockingReasons.length > 0" class="submit-card__reasons">
            <div v-for="reason in blockingReasons" :key="reason" class="submit-card__reason">
              <el-icon><Warning /></el-icon>
              <span>{{ reason }}</span>
            </div>
          </div>

          <div v-if="busy" class="submit-card__progress">
            <el-progress :percentage="progress" :stroke-width="10" striped striped-flow />
            <div class="submit-card__stage">
              <span>{{ stageText }}</span>
              <span class="t2v-muted">{{ stageMessage }}</span>
              <span class="t2v-muted">{{ elapsedText }}</span>
            </div>
          </div>
        </el-card>

        <ErrorAlert v-if="error" :error="error" @retry="handleRetry" />
      </div>

      <div class="generate-view__right">
        <ResultCard
          v-if="result"
          :result="result"
          @regenerate="handleRegenerate"
          @regenerate-tts="handleRegenerateTts"
        />
        <el-card v-else class="placeholder-card" shadow="never">
          <el-empty description="生成结果会显示在这里">
            <template #description>
              <p class="t2v-muted">填写文本后点击「开始生成」</p>
            </template>
          </el-empty>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.generate-view__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.generate-view :deep(.el-card) {
  margin-bottom: 16px;
}

.config-bar :deep(.el-card__body) {
  padding: 12px 16px;
}

.config-bar__row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.config-bar__group {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.config-bar__label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.config-bar__value {
  font-size: 13px;
}

.config-bar__spacer {
  flex: 1;
}

.mode-card__group {
  margin-bottom: 4px;
}

.mode-card__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.mode-card__clone {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.mode-card__clone-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
}

.mode-card__block {
  margin-top: 14px;
}

.generate-view__grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

@media (max-width: 1280px) {
  .generate-view__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.params-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.params-card__hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.submit-card__wrap {
  display: block;
}

.submit-card__wrap :deep(.el-button) {
  width: 100%;
}

.submit-card__reasons {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.submit-card__reason {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-color-warning);
}

.submit-card__progress {
  margin-top: 16px;
}

.submit-card__stage {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
  font-size: 12px;
}

.placeholder-card :deep(.el-card__body) {
  padding: 40px 16px;
}
</style>
