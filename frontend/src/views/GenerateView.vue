<script setup lang="ts">
/**
 * `/generate` — configure providers, type text, submit an async task and poll
 * `GET /tasks/{id}` until it settles.
 *
 * Polling: every 900 ms, hard timeout after ~5 minutes.
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { Clock, Refresh, Warning } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import ErrorAlert from '@/components/ErrorAlert.vue'
import ProviderConfigCard from '@/components/ProviderConfigCard.vue'
import ResultCard from '@/components/ResultCard.vue'
import TextInputCard from '@/components/TextInputCard.vue'
import VoiceParamsCard from '@/components/VoiceParamsCard.vue'
import { ApiError } from '@/api/http'
import { fetchTask, fetchTaskResult, submitTask } from '@/api'
import type { GenerateOptions, GenerateResponse, TaskStatusResponse } from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { stageLabel } from '@/utils/format'

const configStore = useConfigStore()

/* ------------------------------- polling ------------------------------- */

const POLL_INTERVAL_MS = 900
const POLL_TIMEOUT_MS = 5 * 60 * 1000

const generating = ref(false)
const task = ref<TaskStatusResponse | null>(null)
const result = ref<GenerateResponse | null>(null)
const error = ref<ApiError | null>(null)
const elapsedSeconds = ref(0)

let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null
let pollStartedAt = 0

/** Current stage label in Chinese. */
const stageText = computed<string>(() => stageLabel(task.value?.stage ?? 'queued'))

/** Progress percentage (0-100). */
const progress = computed<number>(() => {
  const value = task.value?.progress ?? 0
  return Math.min(100, Math.max(0, value))
})

/** Server-supplied status message, when present. */
const stageMessage = computed<string>(() => task.value?.message ?? '')

/** Elapsed seconds since submission. */
const elapsedText = computed<string>(() => `${elapsedSeconds.value}s`)

/** Whether the primary button must stay disabled. */
const busy = computed<boolean>(() => generating.value)

/* ------------------------------ generation ----------------------------- */

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

/** Fetch the final payload once the task reports success. */
async function loadResult(taskId: string): Promise<void> {
  try {
    result.value = await fetchTaskResult(taskId)
    ElMessage.success('生成完成')
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : null
    if (!error.value) ElMessage.error('获取结果失败')
  }
}

/** Poll `GET /tasks/{id}` until it settles, times out, or is canceled. */
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
      if (status.error) {
        error.value = new ApiError({
          code: status.error.code,
          message: status.error.message,
          detail: status.error.detail,
          requestId: status.error.request_id,
        })
      } else {
        error.value = new ApiError({
          code: status.status === 'canceled' ? 'TASK_CANCELED' : 'TASK_FAILED',
          message: status.status === 'canceled' ? '任务已取消' : '任务执行失败',
        })
      }
      return
    }

    if (Date.now() - pollStartedAt > POLL_TIMEOUT_MS) {
      finish()
      error.value = new ApiError({
        code: 'REQUEST_TIMEOUT',
        message: '任务轮询超过 5 分钟仍未完成，请稍后在历史记录中查看结果。',
        isTimeout: true,
      })
    }
  } catch (caught) {
    finish()
    error.value = caught instanceof ApiError ? caught : null
    if (!error.value) ElMessage.error('轮询任务状态失败')
  }
}

/** Submit a task and start polling. `overrides` tweaks the generation options. */
async function runGeneration(overrides?: Partial<GenerateOptions>): Promise<void> {
  if (busy.value) return

  let payload
  try {
    payload = configStore.buildRequest(overrides)
  } catch (caught) {
    ElMessage.warning(caught instanceof Error ? caught.message : '配置不完整')
    return
  }

  stopTimers()
  generating.value = true
  error.value = null
  result.value = null
  task.value = null
  elapsedSeconds.value = 0

  // Keep the opt-in credential cache in sync with what the user just typed.
  if (configStore.llmApiKey.trim().length > 0 && configStore.hasCachedCredentials()) {
    configStore.persistCredentials()
  }

  try {
    const submitted = await submitTask(payload)
    task.value = {
      task_id: submitted.task_id,
      status: submitted.status,
      stage: submitted.stage,
      progress: submitted.progress,
      message: null,
      created_at: null,
      started_at: null,
      finished_at: null,
      elapsed_ms: null,
      error: null,
    }

    pollStartedAt = Date.now()
    tickTimer = setInterval(() => {
      elapsedSeconds.value = Math.floor((Date.now() - pollStartedAt) / 1000)
    }, 1000)

    // Poll immediately, then on a fixed interval.
    void poll(submitted.task_id)
    pollTimer = setInterval(() => {
      void poll(submitted.task_id)
    }, POLL_INTERVAL_MS)
  } catch (caught) {
    finish()
    error.value = caught instanceof ApiError ? caught : null
    if (!error.value) ElMessage.error('提交任务失败')
  }
}

/** Re-run the whole pipeline with the current parameters. */
function handleRegenerate(): void {
  void runGeneration()
}

/**
 * Re-synthesize only the voice for the reply we already have.
 *
 * The backend's `options.reuse_reply_text` makes this a genuine TTS-only run:
 * the LLM is skipped entirely, so the reply text is preserved byte-for-byte and
 * no tokens are spent. Only the voice/speed/format settings are re-applied.
 * `fallback_enabled` is forced off so a failed synthesis is reported instead of
 * silently producing a placeholder track.
 */
function handleRegenerateTts(): void {
  const previousReply = result.value?.reply_text?.trim()
  if (!previousReply) {
    ElMessage.warning('没有可复用的回复文本，请使用“重新生成”')
    return
  }
  void runGeneration({ reuse_reply_text: previousReply, fallback_enabled: false })
}

function handleRetry(): void {
  void runGeneration()
}

onBeforeUnmount(() => {
  stopTimers()
})
</script>

<template>
  <div class="t2v-page generate-view">
    <div class="t2v-page__title">
      <h2>语音生成</h2>
      <span class="t2v-page__subtitle">
        配置大模型与语音合成服务商，输入文本后一键生成回复与音频。
      </span>
    </div>

    <div class="t2v-grid t2v-grid--2">
      <ProviderConfigCard kind="llm" />
      <ProviderConfigCard kind="tts" />
    </div>

    <div class="t2v-grid t2v-grid--2">
      <TextInputCard
        :generating="busy"
        :can-generate="configStore.canGenerate"
        @generate="handleRegenerate"
      />
      <VoiceParamsCard />
    </div>

    <!-- ----------------------------- progress ----------------------------- -->
    <el-card v-if="generating || task" class="generate-view__progress" shadow="never">
      <template #header>
        <div class="generate-view__progress-header">
          <div class="generate-view__progress-title">
            <el-icon class="generate-view__progress-icon"><Clock /></el-icon>
            <span>生成进度</span>
          </div>
          <div class="generate-view__progress-meta">
            <el-tag size="small" effect="plain" type="info" class="t2v-mono">
              {{ task?.task_id ?? '—' }}
            </el-tag>
            <span class="t2v-mono t2v-muted">已用时 {{ elapsedText }}</span>
          </div>
        </div>
      </template>

      <el-progress
        :percentage="progress"
        :stroke-width="14"
        :status="task?.status === 'failed' ? 'exception' : task?.status === 'succeeded' ? 'success' : undefined"
        striped
        :striped-flow="generating"
      />
      <div class="generate-view__stage">
        <span class="generate-view__stage-label">{{ stageText }}</span>
        <span v-if="stageMessage" class="t2v-muted">{{ stageMessage }}</span>
      </div>

      <el-steps
        class="generate-view__steps"
        :active="['queued', 'llm', 'tts', 'persist', 'done'].indexOf(task?.stage ?? 'queued')"
        align-center
        finish-status="success"
        process-status="process"
      >
        <el-step title="排队中" />
        <el-step title="调用大模型" />
        <el-step title="合成语音" />
        <el-step title="保存结果" />
        <el-step title="完成" />
      </el-steps>

      <div v-if="generating" class="generate-view__cancel">
        <el-button :icon="Refresh" size="small" @click="stopTimers(); generating = false">
          停止轮询
        </el-button>
        <span class="t2v-muted generate-view__cancel-hint">
          停止后任务仍会在后端继续执行，可稍后在历史记录中查看。
        </span>
      </div>
    </el-card>

    <!-- ------------------------------- error ------------------------------ -->
    <ErrorAlert
      v-if="error"
      :error="error"
      title="生成失败"
      retryable
      @retry="handleRetry"
      @dismiss="error = null"
    />

    <!-- ------------------------------- result ----------------------------- -->
    <ResultCard
      v-if="result"
      :result="result"
      @regenerate="handleRegenerate"
      @regenerate-tts="handleRegenerateTts"
    />

    <el-alert
      v-if="!configStore.canGenerate && !generating && !result"
      class="generate-view__notice"
      type="info"
      show-icon
      :closable="false"
      title="开始之前"
    >
      <ul class="generate-view__notice-list">
        <li v-for="reason in configStore.disabledReasons" :key="reason">{{ reason }}</li>
      </ul>
    </el-alert>

    <div v-if="configStore.providersError" class="generate-view__provider-error">
      <el-icon><Warning /></el-icon>
      <span>{{ configStore.providersError }}</span>
      <el-button link type="primary" size="small" @click="configStore.loadProviders(true)">
        重新加载服务商列表
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.generate-view__progress-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.generate-view__progress-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.generate-view__progress-icon {
  color: var(--t2v-color-primary);
}

.generate-view__progress-meta {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.generate-view__stage {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  font-size: 13px;
  flex-wrap: wrap;
}

.generate-view__stage-label {
  font-weight: 600;
  color: var(--t2v-text-primary);
}

.generate-view__steps {
  margin-top: 18px;
}

.generate-view__cancel {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.generate-view__cancel-hint {
  font-size: 12px;
}

.generate-view__notice-list {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.8;
}

.generate-view__provider-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: var(--t2v-radius-sm);
  background: var(--t2v-color-danger-soft);
  color: var(--t2v-color-danger);
  font-size: 13px;
}

@media (max-width: 1024px) {
  .generate-view__steps :deep(.el-step__title) {
    font-size: 12px;
  }
}
</style>