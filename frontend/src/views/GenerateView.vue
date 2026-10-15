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
import { cancelTask, fetchCapabilities, fetchTask, fetchTaskResult, submitTask } from '@/api'
import type {
  CapabilitiesResponse,
  GenerateOptions,
  GenerateRequest,
  GenerateResponse,
  MediaInfo,
  TaskStatusResponse,
} from '@/api/types'
import { MAX_TEXT_LENGTH, useConfigStore, type GenerationMode } from '@/stores/config'
import { stageLabel } from '@/utils/format'
import {
  canRunSegment,
  cancelRun,
  isTerminalStatus,
  segmentCancelSummary,
  startRun,
  taskErrorFields,
  type SegmentRunToken,
} from '@/utils/taskStatus'
import { segmentText } from '@/utils/segment'

const configStore = useConfigStore()
const router = useRouter()

/* ------------------------------- polling ------------------------------- */

const POLL_INTERVAL_MS = 900
const POLL_TIMEOUT_MS = 5 * 60 * 1000
/** Upper bound on polls per segment in the long-text path (~4 minutes each). */
const POLL_MAX_ATTEMPTS = 260

const generating = ref(false)
const task = ref<TaskStatusResponse | null>(null)
const result = ref<GenerateResponse | null>(null)
const error = ref<ApiError | null>(null)
const elapsedSeconds = ref(0)
const capabilities = ref<CapabilitiesResponse | null>(null)
const referenceMedia = ref<MediaInfo | null>(null)
const canceling = ref(false)

/* Long-text segmentation state (see runSegmented). */
const segmented = ref(false)
const segmentResults = ref<GenerateResponse[]>([])
const segmentIndex = ref(0)
const segmentTotal = ref(0)

/**
 * Cancellation signal for the segmented run.
 *
 * `segmentRunToken` is the token the *live* run owns; `segmentCanceled` is the
 * explicit 停止生成 flag. Both are needed: the token stops a stale in-flight
 * `await` from resuming after a newer run started, the flag stops the loop the
 * user actually pressed cancel on. See SegmentRunToken in utils/taskStatus.
 */
const segmentRunToken = ref(0)
const segmentCanceled = ref(false)

/**
 * The task id of the segment that is genuinely in flight right now.
 *
 * The segmented loop used to keep its task id in a local variable, so
 * `handleCancel()` read the *stale* `task.value` from an earlier single
 * generation (or from nothing at all) and asked the backend to cancel a task
 * that was already finished — while every remaining segment kept being
 * submitted. This ref is the id cancel must target.
 */
const currentTaskId = ref<string | null>(null)

/** Snapshot of the pure cancellation signal, for the helper functions. */
function runSignal(): SegmentRunToken {
  return { current: segmentRunToken.value, canceled: segmentCanceled.value }
}

/** Apply a helper's verdict back onto the reactive refs. */
function applySignal(next: SegmentRunToken): void {
  segmentRunToken.value = next.current
  segmentCanceled.value = next.canceled
}

let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null
let pollStartedAt = 0

const stageText = computed(() => {
  if (segmented.value) return `第 ${segmentIndex.value} / ${segmentTotal.value} 段`
  return stageLabel(task.value?.stage ?? 'queued')
})
const progress = computed(() => {
  if (segmented.value && segmentTotal.value > 0) {
    return Math.round((segmentIndex.value / segmentTotal.value) * 100)
  }
  return Math.min(100, Math.max(0, task.value?.progress ?? 0))
})
const stageMessage = computed(() => {
  if (segmented.value) {
    // Only promise auto-continue while the run is actually live: after 停止生成
    // the old text ("每段完成后自动继续") directly contradicted what the loop
    // was doing, which is what made the original bug so confusing.
    if (segmentCanceled.value) return '正在停止：后续分段不再提交'
    return '长文本分段生成中，每段完成后自动继续，可随时停止'
  }
  return task.value?.message ?? ''
})
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

/**
 * Providers that accept a reference clip at synthesis time — the only ones
 * this page's clone mode can drive. Providers that clone by registering a
 * voice are listed separately by the settings page.
 */
const cloneReferenceCapableNames = computed<string[]>(() => {
  const reference = capabilities.value?.voice_clone.reference_audio_providers ?? []
  return reference
    .map((id) => configStore.ttsProviders.find((item) => item.id === id)?.name ?? id)
    .filter(Boolean)
})

const cloneBlockReason = computed<string | null>(() => {
  if (mode.value !== 'clone') return null
  if (!configStore.ttsProviderId) return '请先在「系统设置」中选择语音合成服务商'
  // Two clone shapes exist. This page can only attach a clip to the synthesis
  // call, which requires supports_reference_audio (MiMo, custom). Providers
  // that clone by *registering* a voice (ElevenLabs, MiniMax) must be used
  // through the settings page first — telling the user that here is far
  // clearer than letting them press generate and get a backend error.
  if (!configStore.supportsReferenceAudio) {
    const providerName = configStore.ttsProvider?.name ?? configStore.ttsProviderId
    if (configStore.supportsVoiceClone) {
      return `「${providerName}」需要先注册音色再使用：请到「系统设置 → 语音合成服务商」上传参考音频创建音色，然后在音色列表里选中它。它不支持在生成时直接携带参考音频。`
    }
    const names =
      cloneReferenceCapableNames.value.length > 0
        ? cloneReferenceCapableNames.value.join('、')
        : '小米 MiMo TTS、自定义 (OpenAI 兼容)'
    return `当前服务商「${providerName}」不支持声音克隆。请在「系统设置」中切换到 ${names}，或改用「文字转语音」模式。`
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
const needsLlm = computed<boolean>(() => configStore.mode === 'tts')

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
  // Every run ends here, so this is the one place that guarantees the next run
  // starts from a clean signal (fix 6).
  currentTaskId.value = null
  segmentCanceled.value = false
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

    // Every non-pending/running status is terminal. `interrupted` (the server
    // restarted mid-task) must be routed here *before* the timeout check
    // below, otherwise the dead task polls for five minutes and then reports
    // a misleading "轮询超过 5 分钟" instead of the real, actionable reason.
    if (isTerminalStatus(status.status)) {
      finish()
      const fields = taskErrorFields(status)
      error.value = new ApiError({
        code: fields.code,
        message: fields.message,
        detail: fields.detail,
        requestId: fields.requestId,
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
  // Starting a run invalidates any in-flight segment loop from an earlier run:
  // its token no longer matches, so it cannot resume submitting (fix 1).
  applySignal(startRun(runSignal()))
  currentTaskId.value = null

  let payload
  try {
    payload = configStore.buildRequest(overrides)
  } catch (caught) {
    ElMessage.warning(caught instanceof Error ? caught.message : '配置不完整')
    return
  }

  // Long text: the backend caps a single request, so split at natural
  // boundaries and queue each piece. Chunks run through the same task queue
  // (sequentially, to stay inside the rate limit) and land in history as
  // separate records, so one failed segment can be regenerated alone.
  const { segments, single } = segmentText(configStore.text, { limit: MAX_TEXT_LENGTH })
  if (!single && segments.length > 1) {
    await runSegmented(payload, segments)
    return
  }

  generating.value = true
  try {
    const submitted = await submitTask(payload)
    currentTaskId.value = submitted.task_id
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

/**
 * Generate a long text piece by piece.
 *
 * Each segment is submitted, awaited, and its result collected before the next
 * one starts: sequential submission keeps us inside the expensive-endpoint rate
 * limit and gives the user a meaningful progress count ("第 3 / 12 段").
 * Results accumulate in `segments` for the result panel's playlist.
 *
 * Cancellation (the 停止生成 button) must stop *submission*, not just the one
 * in-flight segment: every vendor call costs money, so the loop re-checks its
 * run token before each submit and again after each await. The token, not a
 * boolean, is what makes the check safe against a stale await waking up after
 * a newer run has already started.
 */
async function runSegmented(
  payload: GenerateRequest,
  pieces: string[],
): Promise<void> {
  generating.value = true
  segmented.value = true
  segmentResults.value = []
  segmentTotal.value = pieces.length
  segmentIndex.value = 0
  error.value = null
  pollStartedAt = Date.now()

  // This run owns the newest token; a later runGeneration() bumps it again.
  applySignal(startRun(runSignal()))
  const token = segmentRunToken.value

  const startedAt = Date.now()
  tickTimer = setInterval(() => {
    elapsedSeconds.value = Math.round((Date.now() - startedAt) / 1000)
  }, 1000)

  let canceled = false
  try {
    for (let index = 0; index < pieces.length; index += 1) {
      // Fix 2, first checkpoint: an explicit cancel (or a newer run that stole
      // the token) must stop us *before* we spend money on another segment.
      if (!canRunSegment(runSignal(), token)) {
        canceled = true
        break
      }

      const piece = pieces[index]
      if (piece === undefined) continue
      segmentIndex.value = index + 1

      const submitted = await submitTask(segmentPayload(payload, piece))
      // Fix 3: point the page at the segment that is genuinely in flight, so
      // handleCancel() cancels the right task id.
      currentTaskId.value = submitted.task_id
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

      // Fix 2, second checkpoint: cancel may land while this segment runs; the
      // await above resolved, so re-read the signal before interpreting the
      // status (a canceled task is an expected outcome, not a failure).
      const stoppedMidSegment = !canRunSegment(runSignal(), token)

      const finished = await waitForTask(submitted.task_id)
      const stillLive = canRunSegment(runSignal(), token)

      if (!stillLive || stoppedMidSegment) {
        // Fix 5: cancellation is a clean, non-error exit. If the segment did
        // finish successfully before the cancel was observed, keep its result —
        // we paid for it, so it must not be discarded.
        if (finished.status === 'succeeded' && stoppedMidSegment) {
          try {
            const result = await fetchTaskResult(submitted.task_id)
            segmentResults.value = [...segmentResults.value, result]
          } catch {
            // Best effort; the record is in history regardless.
          }
        }
        canceled = true
        break
      }

      if (finished.status === 'canceled') {
        // Backend-side cancellation (same click, different arrival order).
        canceled = true
        break
      }

      if (finished.status !== 'succeeded') {
        // Reuse the shared normaliser so an interrupted segment explains the
        // restart (keys are never stored, so it must be resubmitted) instead
        // of the generic "未知错误".
        const fields = taskErrorFields(finished)
        throw new ApiError({
          code: fields.code,
          message: `第 ${index + 1} 段生成失败：${fields.message}。已完成的前几段已保存在历史记录中。`,
          detail: fields.detail,
          requestId: fields.requestId,
        })
      }
      const result = await fetchTaskResult(submitted.task_id)
      segmentResults.value = [...segmentResults.value, result]
    }

    const completed = segmentResults.value.length
    if (canceled) {
      // Fix 4: never the "all segments generated" toast on a canceled run.
      const message = segmentCancelSummary(completed, pieces.length)
      segmentIndex.value = completed
      ElMessage.info(message)
    } else {
      ElMessage.success(`${pieces.length} 段全部生成完成`)
    }
    // Focus the first segment in the result card; on a canceled run this keeps
    // whatever the user already has instead of blanking the panel.
    result.value = segmentResults.value[0] ?? null
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : null
    if (!error.value) ElMessage.error('分段生成失败')
  } finally {
    finish()
    segmented.value = false
  }
}

/**
 * Rewrite a built request for one segment.
 *
 * `text` alone is not enough: in clone and 直接配音 modes the store sets
 * `options.reuse_reply_text` to the full original text (that is what the
 * backend actually speaks, skipping the LLM). Sending the whole document as
 * the reply for *every* segment would read the text N times, so the segment
 * must replace both fields together.
 */
function segmentPayload(payload: GenerateRequest, piece: string): GenerateRequest {
  const options = payload.options.reuse_reply_text
    ? { ...payload.options, reuse_reply_text: piece }
    : payload.options
  return { ...payload, text: piece, options }
}

/**
 * Poll a task until it reaches a terminal state.
 *
 * Kept separate from `poll()` because the segmented path needs to await each
 * piece without the single-generation UI state machine (timers, cancel button).
 */
async function waitForTask(taskId: string): Promise<TaskStatusResponse> {
  for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt += 1) {
    const status = await fetchTask(taskId)
    // Same closed rule as poll(): `interrupted` (among others) ends the wait.
    // Without it a restarted server would make every remaining segment poll
    // to the attempt cap, which looks like a hang rather than a lost task.
    if (isTerminalStatus(status.status)) {
      return status
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
  }
  throw new ApiError({
    code: 'REQUEST_TIMEOUT',
    message: '分段生成超时，请稍后在历史记录中查看已完成的部分。',
  })
}

function handleRetry(): void {
  void runGeneration()
}

/**
 * Cancel the in-flight task: stop the local loop *first*, then tell the
 * backend to abort.
 *
 * Ordering matters. The cancel flag is set before the `await cancelTask(...)`
 * round-trip so the segmented loop cannot slip another paid segment in while
 * the HTTP request is in flight — the loop re-reads the signal at each
 * checkpoint, and a single microtask earlier is a whole vendor call cheaper.
 */
async function handleCancel(): Promise<void> {
  // Fix 1/2: stop the segmented loop from submitting anything further. Scoped
  // to the live token so this cannot be mistaken for a newer run.
  if (segmented.value) {
    applySignal(cancelRun(runSignal(), segmentRunToken.value))
  }

  // Fix 3: the genuinely in-flight segment id, not a stale `task.value` from an
  // earlier run. Falls back to task.value for the single-generation path.
  const taskId = currentTaskId.value ?? task.value?.task_id
  if (!taskId) {
    // Nothing is in flight (e.g. cancel pressed between segments): the loop
    // check above already stopped the run, so there is nothing to abort.
    if (segmented.value) {
      ElMessage.info('已取消：后续分段不再提交。')
      finish()
      segmented.value = false
    }
    return
  }

  canceling.value = true
  try {
    await cancelTask(taskId)
    ElMessage.info('已请求取消，正在停止…')
  } catch {
    // The task may have finished between the click and the call — poll will
    // pick up the terminal state on its next tick, so a missed cancel is
    // harmless. Only network-level failures surface here.
    if (!segmented.value) ElMessage.warning('取消请求失败，任务可能已完成')
  } finally {
    canceling.value = false
  }
  // Single generation: stop the local UI immediately (the poll handler turns
  // the eventual 'canceled' status into the standard error display if it
  // lands). The segmented path must NOT finish() here: its loop is still
  // awaiting this segment and owns the honest summary, so it stops itself at
  // the next checkpoint and reports N/M completed.
  if (!segmented.value) finish()
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
        <TextInputCard
          :generating="busy"
          :can-generate="canSubmit"
          :extra-reasons="cloneBlockReason ? [cloneBlockReason] : []"
          @generate="runGeneration()"
        />

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

        <!-- ---------------- 提交状态 ----------------
             没有提交按钮：提交入口只有一个，就在文本卡片里紧挨输入框的
             「生成语音」。这里只显示阻塞原因和进度，避免两个功能相同的按钮。 -->
        <el-card v-if="blockingReasons.length > 0 || busy" class="submit-card" shadow="never">
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
              <el-button
                link
                type="danger"
                size="small"
                :loading="canceling"
                @click="handleCancel"
              >
                {{ segmented ? '停止生成（不再提交后续分段）' : '停止生成' }}
              </el-button>
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
              <p class="t2v-muted">填写文本后点击「生成语音」</p>
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
