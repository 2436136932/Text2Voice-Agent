<script setup lang="ts">
/**
 * Media picker: upload an audio/video file, inspect it, optionally trim a
 * window out of it, and preview the result.
 *
 * Why a dedicated component: the same widget is needed for the voice-clone
 * reference clip and (potentially) future features, and the trim UX is subtle
 * enough that inlining it would bloat the view.
 *
 * The component never uploads on its own — it emits `update:mediaId` after the
 * backend has accepted the file, so the parent owns the lifecycle.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Delete, Download, MagicStick, RefreshRight, Scissor, UploadFilled, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { deleteMedia, enhanceMedia, inspectMedia, mediaUrl, trimMedia } from '@/api'
import { ApiError } from '@/api/http'
import type { MediaInfo } from '@/api/types'
import { formatBytes, formatDuration } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    /** Currently selected media id (v-model). */
    mediaId: string | null
    /** Uploaded/trimmed media metadata, mirrored back to the parent. */
    media?: MediaInfo | null
    /** Accepted extensions, e.g. ".wav,.mp3,.mp4". */
    accept?: string
    /** Maximum upload size in MB. */
    maxSizeMb?: number
    /** Whether trimming is enabled for the current file. */
    allowTrim?: boolean
    /** Hide the video element (e.g. when only audio makes sense). */
    hint?: string
  }>(),
  {
    media: null,
    accept: '.wav,.mp3,.m4a,.mp4,.mov,.webm,.ogg,.flac,.aac',
    maxSizeMb: 50,
    allowTrim: true,
    hint: '',
  },
)

const emit = defineEmits<{
  'update:mediaId': [value: string | null]
  'update:media': [value: MediaInfo | null]
  ready: [value: MediaInfo]
  cleared: []
}>()

const uploading = ref(false)
const uploadPercent = ref(0)
const trimming = ref(false)
const enhancing = ref(false)
const dragActive = ref(false)
const info = ref<MediaInfo | null>(props.media ?? null)
const localError = ref<string | null>(null)

/** The file whose upload failed, kept so "重新上传" can resend it. */
const lastFailedFile = ref<File | null>(null)
const lastFailedName = computed<string>(() => lastFailedFile.value?.name ?? '')

/** Prompt shown inside the drop zone; switches to a retry wording after a failure. */
const retryPrompt = computed<string>(() =>
  localError.value ? '点击或拖拽，重新上传' : '点击或拖拽上传音频 / 视频',
)

/** Re-send the file that failed, without making the user find it again. */
function retryUpload(): void {
  const file = lastFailedFile.value
  if (!file) {
    openFileDialog()
    return
  }
  void handleFile(file)
}

/** The single hidden file input this component owns. */
const fileInputRef = ref<HTMLInputElement | null>(null)

/** Open the OS file picker. */
function openFileDialog(): void {
  if (uploading.value) return
  fileInputRef.value?.click()
}

/** Original upload, kept so "重置裁剪" can restore it. */
const original = ref<MediaInfo | null>(null)

/** The `<audio>` / `<video>` element, used to preview the trim selection. */
const playerRef = ref<HTMLMediaElement | null>(null)

/* ------------------------------- trimming ------------------------------- */

const trimStart = ref(0)
const trimEnd = ref(0)

/** Quick length presets — the common cloning window is 5–30 s. */
const TRIM_PRESETS = [
  { label: '5 秒', ms: 5_000 },
  { label: '10 秒', ms: 10_000 },
  { label: '20 秒', ms: 20_000 },
  { label: '30 秒', ms: 30_000 },
  { label: '1 分钟', ms: 60_000 },
] as const

/** Slider granularity: fine for short clips, coarse enough for long ones. */
const trimStep = computed<number>(() => (durationMs.value > 120_000 ? 500 : 100))

/** Tick labels so a long file is still readable on the slider. */
const trimMarks = computed<Record<number, string>>(() => {
  const total = durationMs.value
  if (total <= 0) return {}
  const marks: Record<number, string> = { 0: '0' }
  const parts = total > 300_000 ? 4 : 2
  for (let i = 1; i <= parts; i += 1) {
    const at = Math.round((total * i) / (parts + 1))
    marks[at] = formatDuration(at)
  }
  return marks
})

const durationMs = computed<number>(() => info.value?.duration_ms ?? 0)
const canTrim = computed<boolean>(
  () =>
    props.allowTrim &&
    (info.value?.trimmable ?? false) &&
    durationMs.value > 0,
)
const trimWindowMs = computed<number>(() => Math.max(0, trimEnd.value - trimStart.value))
const trimDirty = computed<boolean>(
  () => trimStart.value > 0 || (durationMs.value > 0 && trimEnd.value < durationMs.value),
)

/** Reactive proxy for `<el-slider range>`; keeps start <= end by construction. */
const trimRange = computed({
  get: (): [number, number] => [trimStart.value, trimEnd.value],
  set: (value: number[] | [number, number]): void => {
    const [start, end] = value
    trimStart.value = Math.max(0, Math.min(start, end))
    trimEnd.value = Math.min(durationMs.value, Math.max(start, end))
  },
})

/* ---- time text fields (accept "1:23.5" or "83.5s" or raw ms) ---------- */

function msToText(ms: number): string {
  const totalSeconds = ms / 1000
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds - minutes * 60
  return `${minutes}:${seconds.toFixed(1).padStart(4, '0')}`
}

/**
 * Parse a human time string into milliseconds.
 *
 * Accepts `1:23.5`, `83.5s`, `83.5`, or `83500ms`; returns null when the input
 * cannot be understood so the caller can revert instead of guessing.
 */
function parseTimeText(raw: string): number | null {
  const text = raw.trim().toLowerCase()
  if (!text) return null
  if (text.endsWith('ms')) {
    const value = Number.parseFloat(text.slice(0, -2))
    return Number.isFinite(value) ? Math.max(0, Math.round(value)) : null
  }
  const bare = text.endsWith('s') ? text.slice(0, -1) : text
  if (bare.includes(':')) {
    const [minPart, secPart] = bare.split(':')
    const minutes = Number.parseFloat(minPart)
    const seconds = Number.parseFloat(secPart)
    if (!Number.isFinite(minutes) || !Number.isFinite(seconds)) return null
    return Math.max(0, Math.round((minutes * 60 + seconds) * 1000))
  }
  const seconds = Number.parseFloat(bare)
  return Number.isFinite(seconds) ? Math.max(0, Math.round(seconds * 1000)) : null
}

const trimStartText = ref('0:00.0')
const trimEndText = ref('0:00.0')
const trimLengthText = ref('0:00.0')
const editingTime = ref(false)

/** Mirror the numeric state into the text fields (unless the user is typing). */
function syncTimeTexts(): void {
  if (editingTime.value) return
  trimStartText.value = msToText(trimStart.value)
  trimEndText.value = msToText(trimEnd.value)
  trimLengthText.value = msToText(trimWindowMs.value)
}

function commitStartText(): void {
  const parsed = parseTimeText(trimStartText.value)
  editingTime.value = false
  if (parsed === null) {
    syncTimeTexts()
    return
  }
  trimStart.value = Math.min(parsed, Math.max(0, trimEnd.value - 200))
  syncTimeTexts()
}

function commitEndText(): void {
  const parsed = parseTimeText(trimEndText.value)
  editingTime.value = false
  if (parsed === null) {
    syncTimeTexts()
    return
  }
  trimEnd.value = Math.max(Math.min(parsed, durationMs.value), trimStart.value + 200)
  syncTimeTexts()
}

/** Setting a *length* keeps the start fixed and moves the end. */
function commitLengthText(): void {
  const parsed = parseTimeText(trimLengthText.value)
  editingTime.value = false
  if (parsed === null) {
    syncTimeTexts()
    return
  }
  const wanted = Math.max(200, parsed)
  if (trimStart.value + wanted <= durationMs.value) {
    trimEnd.value = trimStart.value + wanted
  } else {
    // Not enough room after the start: anchor the window at the end of the file.
    trimEnd.value = durationMs.value
    trimStart.value = Math.max(0, durationMs.value - wanted)
  }
  syncTimeTexts()
}

const activePreset = computed<string | null>(() => {
  const match = TRIM_PRESETS.find((preset) => preset.ms === trimWindowMs.value)
  return match ? match.label : null
})

/** Apply a preset length starting at the current start position. */
function applyPreset(ms: number): void {
  if (ms >= durationMs.value) {
    trimStart.value = 0
    trimEnd.value = durationMs.value
  } else if (trimStart.value + ms <= durationMs.value) {
    trimEnd.value = trimStart.value + ms
  } else {
    trimEnd.value = durationMs.value
    trimStart.value = Math.max(0, durationMs.value - ms)
  }
  syncTimeTexts()
}

watch(durationMs, (value) => {
  trimStart.value = 0
  trimEnd.value = value
  syncTimeTexts()
})

watch([trimStart, trimEnd], syncTimeTexts)

function resetTrim(): void {
  trimStart.value = 0
  trimEnd.value = durationMs.value
  syncTimeTexts()
}

/* ---- preview the selected segment without trimming it ------------------ */

const previewing = ref(false)
let previewStopTimer: ReturnType<typeof setTimeout> | null = null

/**
 * Play only the selected window, then stop.
 *
 * Lets the user confirm the cut before committing to a trim, which matters most
 * on long files where the interesting part is hard to find by scrubbing.
 */
function previewSelection(): void {
  const el = playerRef.value
  if (!el) return
  if (previewStopTimer !== null) {
    clearTimeout(previewStopTimer)
    previewStopTimer = null
  }
  el.currentTime = trimStart.value / 1000
  void el.play().catch(() => undefined)
  previewing.value = true
  previewStopTimer = setTimeout(() => {
    el.pause()
    previewing.value = false
    previewStopTimer = null
  }, trimWindowMs.value)
}

function onPlayerTimeUpdate(): void {
  const el = playerRef.value
  if (!el || !previewing.value) return
  if (el.currentTime * 1000 >= trimEnd.value) {
    el.pause()
    previewing.value = false
    if (previewStopTimer !== null) {
      clearTimeout(previewStopTimer)
      previewStopTimer = null
    }
  }
}

onBeforeUnmount(() => {
  if (previewStopTimer !== null) clearTimeout(previewStopTimer)
})

async function applyTrim(): Promise<void> {
  if (!info.value) return
  if (trimWindowMs.value < 200) {
    ElMessage.warning('裁剪片段太短，请至少保留 0.2 秒')
    return
  }
  trimming.value = true
  try {
    const result = await trimMedia({
      media_id: info.value.media_id,
      start_ms: Math.round(trimStart.value),
      end_ms: Math.round(trimEnd.value),
      output_format: 'wav',
    })
    const updated: MediaInfo = {
      ...info.value,
      media_id: result.media_id,
      preview_url: result.preview_url,
      duration_ms: result.duration_ms,
      size_bytes: result.size_bytes,
      format: result.format,
      content_type: 'audio/wav',
      kind: 'audio',
      trimmable: true,
      sample_rate: 16000,
      channels: 1,
      filename: `trimmed_${result.media_id.slice(0, 8)}.wav`,
    }
    info.value = updated
    emit('update:mediaId', updated.media_id)
    emit('update:media', updated)
    ElMessage.success(`裁剪完成（${formatDuration(updated.duration_ms)}）`)
  } catch (error) {
    showError(error, '裁剪失败')
  } finally {
    trimming.value = false
  }
}

async function restoreOriginal(): Promise<void> {
  if (!original.value) return
  info.value = original.value
  emit('update:mediaId', original.value.media_id)
  emit('update:media', original.value)
  resetTrim()
}

/**
 * Produce a clone-friendly copy of the current clip: leading/trailing silence
 * trimmed and loudness normalized to -16 LUFS. The backend returns a NEW
 * media id, so the original stays available via 恢复原文件.
 */
async function applyEnhance(): Promise<void> {
  if (!info.value) return
  enhancing.value = true
  try {
    const result = await enhanceMedia(info.value.media_id)
    const updated: MediaInfo = {
      ...info.value,
      media_id: result.media_id,
      preview_url: result.preview_url,
      duration_ms: result.duration_ms,
      size_bytes: result.size_bytes,
      format: result.format,
      content_type: 'audio/wav',
      kind: 'audio',
      trimmable: true,
      sample_rate: 24000,
      channels: 1,
      filename: `enhanced_${result.media_id.slice(0, 8)}.wav`,
      warnings: result.warnings ?? [],
    }
    info.value = updated
    emit('update:mediaId', updated.media_id)
    emit('update:media', updated)
    // Rebind the trim selection to the shortened clip.
    trimStart.value = 0
    trimEnd.value = result.duration_ms ?? 0
    if (result.warnings && result.warnings.length > 0) {
      ElMessage.warning(result.warnings.join('；'))
    } else {
      ElMessage.success(`优化完成（${formatDuration(updated.duration_ms)}）`)
    }
  } catch (error) {
    showError(error, '优化失败')
  } finally {
    enhancing.value = false
  }
}

/* -------------------------------- upload -------------------------------- */

async function handleFile(file: File): Promise<void> {
  localError.value = null
  if (file.size > props.maxSizeMb * 1024 * 1024) {
    lastFailedFile.value = file
    localError.value =
      `文件过大：${formatBytes(file.size)}，上限 ${props.maxSizeMb} MB。` +
      '请先裁剪出较短的片段，或压缩后再上传。'
    ElMessage.error(localError.value)
    return
  }

  uploading.value = true
  uploadPercent.value = 0
  try {
    const result = await inspectMedia(file, (percent) => {
      uploadPercent.value = percent
    })
    info.value = result
    original.value = result
    lastFailedFile.value = null
    localError.value = null
    resetTrim()
    emit('update:mediaId', result.media_id)
    emit('update:media', result)
    emit('ready', result)
    if (result.warnings?.length) {
      ElMessage.warning(result.warnings.join('；'))
    } else {
      ElMessage.success(`已导入 ${result.filename}（${formatDuration(result.duration_ms)}）`)
    }
  } catch (error) {
    // Remember the file so the user can retry with one click.
    lastFailedFile.value = file
    showError(error, '导入失败')
  } finally {
    uploading.value = false
    uploadPercent.value = 0
  }
}

function onFileInput(event: Event): void {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  // Reset first so re-selecting the same file fires `change` again.
  target.value = ''
  if (!file) return
  void handleFile(file)
}

function onDrop(event: DragEvent): void {
  dragActive.value = false
  const file = event.dataTransfer?.files?.[0]
  if (!file) {
    // A drop that carries no file (e.g. dragging text) must not silently no-op.
    ElMessage.warning('没有检测到文件，请拖入音频或视频文件')
    return
  }
  void handleFile(file)
}

async function clear(): Promise<void> {
  const current = info.value
  info.value = null
  original.value = null
  emit('update:mediaId', null)
  emit('update:media', null)
  emit('cleared')
  resetTrim()
  if (current) {
    try {
      // Best-effort cleanup; the file may already be gone.
      await deleteMedia(current.media_id)
    } catch {
      /* ignore */
    }
  }
}

/* -------------------------------- errors -------------------------------- */

/**
 * Surface the backend's `detail.suggestion` when present — that is where the
 * actionable "install ffmpeg / convert to wav" guidance lives.
 */
function showError(error: unknown, fallback: string): void {
  if (error instanceof ApiError) {
    const suggestion = (error.detail as { suggestion?: string } | undefined)?.suggestion
    localError.value = suggestion ?? error.message
    ElMessage.error({ message: `${error.message}${suggestion ? `\n${suggestion}` : ''}`, duration: 6000 })
  } else {
    localError.value = fallback
    ElMessage.error(fallback)
  }
}

const previewSrc = computed<string | null>(() => (info.value ? mediaUrl(info.value.preview_url) : null))
const isVideo = computed<boolean>(() => info.value?.kind === 'video')
</script>

<template>
  <div class="media-picker">
    <!--
      A hand-rolled drop zone rather than <el-upload>.

      el-upload renders its own hidden <input type="file">, so nesting a second
      input inside it produced two competing inputs: the click could land on the
      component's input (whose select handler is not ours), and with
      `auto-upload=false` nothing forwarded the chosen file. Owning the input
      makes the click path unambiguous.
    -->
    <div
      v-if="!info"
      class="media-picker__drop"
      :class="{ 'is-dragging': dragActive, 'is-busy': uploading }"
      role="button"
      tabindex="0"
      @click="openFileDialog"
      @keydown.enter.prevent="openFileDialog"
      @keydown.space.prevent="openFileDialog"
      @dragover.prevent="dragActive = true"
      @dragenter.prevent="dragActive = true"
      @dragleave.prevent="dragActive = false"
      @drop.prevent="onDrop"
    >
      <div class="media-picker__inner">
        <el-icon class="media-picker__icon"><UploadFilled /></el-icon>
        <div class="media-picker__title">
          {{ uploading ? `上传中 ${uploadPercent}%` : retryPrompt }}
        </div>
        <div class="media-picker__sub">
          支持 {{ accept }}，单文件不超过 {{ maxSizeMb }} MB
        </div>
        <el-progress
          v-if="uploading"
          class="media-picker__progress"
          :percentage="uploadPercent"
          :stroke-width="6"
        />
      </div>
      <input
        ref="fileInputRef"
        class="media-picker__input"
        type="file"
        :accept="accept"
        @change="onFileInput"
      />
    </div>

    <!--
      A failed upload must be recoverable. The drop zone stays available, and the
      reason plus a retry action are shown explicitly — previously the error was
      only a transient toast, so after it faded the panel looked untouched and
      the user had no idea what went wrong or what to do next.
    -->
    <el-alert
      v-if="!info && localError"
      class="media-picker__failure"
      type="error"
      show-icon
      :closable="false"
      :title="lastFailedName ? `上传失败：${lastFailedName}` : '上传失败'"
    >
      <template #default>
        <div class="media-picker__failure-body">{{ localError }}</div>
        <div class="media-picker__failure-actions">
          <el-button size="small" type="primary" :icon="RefreshRight" @click="retryUpload">
            重新上传
          </el-button>
          <el-button v-if="lastFailedFile" size="small" @click="openFileDialog">
            选择其他文件
          </el-button>
        </div>
      </template>
    </el-alert>

    <!-- Preview is keyed off `info` explicitly. Using v-else here would pair it
         with the alert above and stop TypeScript narrowing `info` to non-null. -->
    <div v-if="info" class="media-picker__preview">
      <div class="media-picker__meta">
        <el-tag size="small" :type="isVideo ? 'warning' : 'success'" effect="plain">
          {{ isVideo ? '视频' : '音频' }}
        </el-tag>
        <span class="media-picker__filename">{{ info.filename }}</span>
        <span class="t2v-muted">{{ formatBytes(info.size_bytes) }}</span>
        <span class="t2v-muted">{{ formatDuration(info.duration_ms) }}</span>
        <el-button link size="small" :icon="Delete" @click="clear">移除</el-button>
      </div>

      <!-- Video preview (画面 + 声音) -->
      <video
        v-if="isVideo && previewSrc"
        ref="playerRef"
        class="media-picker__video"
        :src="previewSrc"
        controls
        preload="metadata"
        @timeupdate="onPlayerTimeUpdate"
      ></video>
      <!-- Audio preview -->
      <audio
        v-else-if="previewSrc"
        ref="playerRef"
        class="media-picker__audio"
        :src="previewSrc"
        controls
        preload="metadata"
        @timeupdate="onPlayerTimeUpdate"
      ></audio>

      <el-alert
        v-for="warning in info.warnings ?? []"
        :key="warning"
        class="media-picker__warning"
        type="warning"
        :closable="false"
        show-icon
        :title="warning"
      />

      <div v-if="canTrim" class="media-picker__trim">
        <div class="media-picker__trim-head">
          <el-icon><Scissor /></el-icon>
          <span>裁剪片段</span>
          <span class="t2v-muted">
            已选 {{ formatDuration(trimWindowMs) }} / 全长 {{ formatDuration(durationMs) }}
          </span>
        </div>

        <!-- Dual-handle range: far easier to aim on a long clip than two
             independent sliders, and it cannot produce start > end. -->
        <el-slider
          v-model="trimRange"
          range
          :min="0"
          :max="durationMs"
          :step="trimStep"
          :marks="trimMarks"
          :disabled="trimming"
        />

        <div class="media-picker__trim-values">
          <div class="media-picker__time-field">
            <span class="media-picker__time-label">起点</span>
            <el-input v-model="trimStartText" size="small" class="media-picker__time-input"
                      @change="commitStartText" @blur="commitStartText" />
            <span class="t2v-muted">{{ formatDuration(trimStart) }}</span>
          </div>
          <div class="media-picker__time-field">
            <span class="media-picker__time-label">终点</span>
            <el-input v-model="trimEndText" size="small" class="media-picker__time-input"
                      @change="commitEndText" @blur="commitEndText" />
            <span class="t2v-muted">{{ formatDuration(trimEnd) }}</span>
          </div>
          <div class="media-picker__time-field">
            <span class="media-picker__time-label">时长</span>
            <el-input v-model="trimLengthText" size="small" class="media-picker__time-input"
                      @change="commitLengthText" @blur="commitLengthText" />
            <span class="t2v-muted">从起点起算</span>
          </div>
        </div>
        <div class="media-picker__hint">
          支持 <span class="t2v-mono">1:23.5</span>（分:秒）或 <span class="t2v-mono">83.5s</span> 两种写法；
          也可以直接输入总时长来自定裁剪长度。
        </div>

        <div class="media-picker__presets">
          <span class="t2v-muted">快速选择长度：</span>
          <el-button
            v-for="preset in TRIM_PRESETS"
            :key="preset.label"
            size="small"
            :type="activePreset === preset.label ? 'primary' : 'default'"
            @click="applyPreset(preset.ms)"
          >
            {{ preset.label }}
          </el-button>
        </div>

        <div class="media-picker__trim-actions">
          <el-button size="small" :icon="Scissor" :loading="trimming" @click="applyTrim">
            应用裁剪
          </el-button>
          <el-button
            size="small"
            type="primary"
            plain
            :icon="MagicStick"
            :loading="enhancing"
            :disabled="durationMs <= 0"
            @click="applyEnhance"
          >
            优化音色
          </el-button>
          <el-button size="small" :icon="VideoPlay" @click="previewSelection">
            试听选中片段
          </el-button>
          <el-button size="small" :disabled="!trimDirty" @click="resetTrim">重置选区</el-button>
          <el-button
            v-if="original && original.media_id !== info.media_id"
            size="small"
            :icon="Download"
            @click="restoreOriginal"
          >
            恢复原文件
          </el-button>
        </div>

        <div class="media-picker__enhance-hint t2v-muted">
          优化会去掉首尾静音并把响度统一到 -16 LUFS，克隆出的音色更干净；
          返回一个新片段，原文件保留。
        </div>
      </div>

      <div v-else-if="allowTrim && durationMs > 0" class="media-picker__no-trim">
        <el-icon><VideoPlay /></el-icon>
        <span>该格式暂不支持裁剪，但可以直接用于声音克隆。</span>
      </div>

      <div v-if="hint" class="media-picker__hint">{{ hint }}</div>
    </div>
  </div>
</template>

<style scoped>
.media-picker__drop {
  position: relative;
  border: 1px dashed var(--el-border-color);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
  outline: none;
}

.media-picker__drop:hover,
.media-picker__drop:focus-visible {
  border-color: var(--t2v-color-primary, var(--el-color-primary));
  background: var(--el-fill-color-lighter);
}

.media-picker__drop.is-dragging {
  border-color: var(--t2v-color-primary, var(--el-color-primary));
  background: var(--t2v-color-primary-light, #ecf5ff);
}

.media-picker__drop.is-busy {
  cursor: progress;
}

.media-picker__failure {
  margin-top: 10px;
}

.media-picker__failure-body {
  font-size: 12px;
  line-height: 1.6;
  word-break: break-word;
}

.media-picker__failure-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.media-picker__inner {
  padding: 22px 16px;
  text-align: center;
}

.media-picker__icon {
  font-size: 34px;
  color: var(--t2v-color-primary);
}

.media-picker__title {
  margin-top: 8px;
  font-size: 14px;
  font-weight: 500;
}

.media-picker__sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.media-picker__progress {
  margin-top: 10px;
}

.media-picker__input {
  display: none;
}

.media-picker__preview {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px;
}

.media-picker__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 13px;
}

.media-picker__filename {
  font-weight: 500;
  word-break: break-all;
}

.media-picker__video,
.media-picker__audio {
  width: 100%;
  border-radius: 6px;
}

.media-picker__video {
  max-height: 260px;
  background: #000;
}

.media-picker__warning {
  margin-top: 10px;
}

.media-picker__trim {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.media-picker__trim-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 6px;
}

.media-picker__trim :deep(.el-slider) {
  margin: 0 6px;
}

.media-picker__trim-values {
  display: flex;
  align-items: center;
  gap: 16px;
  margin: 10px 0;
  flex-wrap: wrap;
}

.media-picker__time-field {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.media-picker__time-label {
  color: var(--el-text-color-regular);
  white-space: nowrap;
}

.media-picker__time-input {
  width: 92px;
}

.media-picker__presets {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 12px;
}

.media-picker__trim :deep(.el-slider__marks-text) {
  font-size: 11px;
  white-space: nowrap;
}

.media-picker__trim-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.media-picker__enhance-hint {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.5;
}

.media-picker__no-trim,
.media-picker__hint {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
