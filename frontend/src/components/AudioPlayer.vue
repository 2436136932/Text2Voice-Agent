<script setup lang="ts">
/**
 * Custom audio player.
 *
 * Exposes `play()` / `pause()` / `toggle()` through `defineExpose` so a parent
 * (e.g. the history page) can guarantee only one track plays at a time.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Download, Headset, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { formatSeconds } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    /** Audio source URL, e.g. `/api/v1/audio/12`. */
    src: string | null
    /** Title shown above the controls. */
    title?: string
    /** Download URL (defaults to `src` + `?download=1`). */
    downloadUrl?: string | null
    /** File name suggested to the browser. */
    downloadName?: string | null
    /** Known duration in milliseconds (used before metadata loads). */
    durationMs?: number | null
    /** Audio container format label, e.g. `mp3`. */
    format?: string | null
    /** Compact layout for table rows / sticky bars. */
    compact?: boolean
    /** Autoplay after the source changes. */
    autoplay?: boolean
  }>(),
  {
    title: '语音结果',
    downloadUrl: null,
    downloadName: null,
    durationMs: null,
    format: null,
    compact: false,
    autoplay: false,
  },
)

const emit = defineEmits<{
  (event: 'play'): void
  (event: 'pause'): void
  (event: 'ended'): void
  (event: 'error', message: string): void
}>()

const audioRef = ref<HTMLAudioElement | null>(null)
const playing = ref(false)
const currentTime = ref(0)
const duration = ref(0)
const volume = ref(1)
const muted = ref(false)
const loadError = ref<string | null>(null)

/** Effective download link. */
const effectiveDownloadUrl = computed<string | null>(() => {
  if (props.downloadUrl) return props.downloadUrl
  if (!props.src) return null
  return props.src.includes('?') ? `${props.src}&download=1` : `${props.src}?download=1`
})

/** Duration in seconds, preferring real metadata over the reported value. */
const durationSeconds = computed<number>(() => {
  if (duration.value > 0) return duration.value
  if (props.durationMs && props.durationMs > 0) return props.durationMs / 1000
  return 0
})

const displayCurrent = computed<string>(() => formatSeconds(currentTime.value * 1000, ''))
const displayDuration = computed<string>(() =>
  durationSeconds.value > 0 ? formatSeconds(durationSeconds.value * 1000, '') : '--:--',
)

/** Slider position as a percentage so `el-slider` can drive seeking. */
const progressPercent = computed<number>({
  get: () => {
    if (duration.value <= 0) return 0
    return Math.min(100, Math.max(0, (currentTime.value / duration.value) * 100))
  },
  set: (value: number) => {
    const audio = audioRef.value
    if (!audio || duration.value <= 0) return
    const target = (value / 100) * duration.value
    audio.currentTime = target
    currentTime.value = target
  },
})

/* ----------------------------- controls ----------------------------- */

function play(): void {
  const audio = audioRef.value
  if (!audio || !props.src) return
  loadError.value = null
  const result = audio.play()
  if (result && typeof result.catch === 'function') {
    void result.catch((error: unknown) => {
      const message = error instanceof Error ? error.message : '音频播放失败'
      loadError.value = message
      emit('error', message)
    })
  }
}

function pause(): void {
  audioRef.value?.pause()
}

function toggle(): void {
  if (playing.value) pause()
  else play()
}

function stop(): void {
  const audio = audioRef.value
  if (!audio) return
  audio.pause()
  audio.currentTime = 0
  currentTime.value = 0
}

/** Seek relative to the current position (seconds, may be negative). */
function seekBy(delta: number): void {
  const audio = audioRef.value
  if (!audio) return
  const target = Math.min(Math.max(audio.currentTime + delta, 0), duration.value || Number.MAX_VALUE)
  audio.currentTime = target
  currentTime.value = target
}

function toggleMute(): void {
  const audio = audioRef.value
  if (!audio) return
  audio.muted = !audio.muted
  muted.value = audio.muted
}

function handleVolume(value: number): void {
  const audio = audioRef.value
  volume.value = value
  if (audio) {
    audio.volume = value
    if (value > 0 && audio.muted) {
      audio.muted = false
      muted.value = false
    }
  }
}

function handleLoadedMetadata(): void {
  const audio = audioRef.value
  if (!audio) return
  duration.value = Number.isFinite(audio.duration) ? audio.duration : 0
  loadError.value = null
}

function handleTimeUpdate(): void {
  const audio = audioRef.value
  if (!audio) return
  currentTime.value = audio.currentTime
}

function handlePlay(): void {
  playing.value = true
  emit('play')
}

function handlePause(): void {
  playing.value = false
  emit('pause')
}

function handleEnded(): void {
  playing.value = false
  currentTime.value = 0
  emit('ended')
}

function handleError(): void {
  playing.value = false
  loadError.value = '音频加载失败，请确认后端音频接口可用。'
  emit('error', loadError.value)
}

function notifyDownload(): void {
  ElMessage.success('已开始下载音频文件')
}

/* ------------------------------ wiring ------------------------------ */

watch(
  () => props.src,
  (next, previous) => {
    if (next === previous) return
    playing.value = false
    currentTime.value = 0
    duration.value = 0
    loadError.value = null
    const audio = audioRef.value
    if (!audio) return
    audio.load()
    if (next && props.autoplay) play()
  },
)

onBeforeUnmount(() => {
  const audio = audioRef.value
  if (audio) {
    audio.pause()
    audio.removeAttribute('src')
  }
})

defineExpose({ play, pause, toggle, stop, seekBy })
</script>

<template>
  <div class="audio-player" :class="{ 'audio-player--compact': compact }">
    <audio
      ref="audioRef"
      class="audio-player__element"
      :src="src ?? undefined"
      preload="metadata"
      @loadedmetadata="handleLoadedMetadata"
      @timeupdate="handleTimeUpdate"
      @play="handlePlay"
      @pause="handlePause"
      @ended="handleEnded"
      @error="handleError"
    />

    <div v-if="!compact" class="audio-player__head">
      <div class="audio-player__title">
        <el-icon><Headset /></el-icon>
        <span>{{ title }}</span>
      </div>
      <div class="audio-player__meta">
        <el-tag v-if="format" size="small" effect="plain" type="info">{{ format.toUpperCase() }}</el-tag>
        <span v-if="durationSeconds > 0" class="t2v-muted">{{ formatSeconds(durationSeconds * 1000) }}</span>
      </div>
    </div>

    <div class="audio-player__controls">
      <el-button
        class="audio-player__toggle"
        type="primary"
        :icon="playing ? VideoPause : VideoPlay"
        circle
        :disabled="!src"
        :aria-label="playing ? '暂停' : '播放'"
        @click="toggle"
      />

      <div class="audio-player__timeline">
        <span class="audio-player__time t2v-mono">{{ displayCurrent }}</span>
        <el-slider
          v-model="progressPercent"
          class="audio-player__slider"
          :min="0"
          :max="100"
          :step="0.1"
          :show-tooltip="false"
          :disabled="!src || duration <= 0"
        />
        <span class="audio-player__time t2v-mono">{{ displayDuration }}</span>
      </div>

      <div v-if="!compact" class="audio-player__volume">
        <el-button
          link
          size="small"
          :aria-label="muted ? '取消静音' : '静音'"
          @click="toggleMute"
        >
          {{ muted || volume === 0 ? '🔇' : '🔊' }}
        </el-button>
        <el-slider
          class="audio-player__volume-slider"
          :model-value="muted ? 0 : volume"
          :min="0"
          :max="1"
          :step="0.05"
          :show-tooltip="false"
          @update:model-value="handleVolume"
        />
      </div>

      <el-button
        v-if="!compact"
        size="small"
        :icon="Download"
        :disabled="!effectiveDownloadUrl"
        tag="a"
        :href="effectiveDownloadUrl ?? undefined"
        :download="downloadName ?? undefined"
        @click="notifyDownload"
      >
        下载
      </el-button>
    </div>

    <el-alert
      v-if="loadError"
      class="audio-player__error"
      type="error"
      :closable="false"
      show-icon
      :title="loadError"
    />
  </div>
</template>

<style scoped>
.audio-player {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--t2v-border-color);
  border-radius: var(--t2v-radius-md);
  background: var(--t2v-bg-muted);
}

.audio-player--compact {
  padding: 8px 10px;
  background: transparent;
  border: none;
}

.audio-player__element {
  display: none;
}

.audio-player__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.audio-player__title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--t2v-text-primary);
}

.audio-player__meta {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.audio-player__controls {
  display: flex;
  align-items: center;
  gap: 12px;
}

.audio-player__toggle {
  flex: 0 0 auto;
}

.audio-player__timeline {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1 1 auto;
  min-width: 0;
}

.audio-player__slider {
  flex: 1 1 auto;
  min-width: 80px;
}

.audio-player__time {
  flex: 0 0 auto;
  min-width: 44px;
  text-align: center;
  color: var(--t2v-text-secondary);
}

.audio-player__volume {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.audio-player__volume-slider {
  width: 80px;
}

.audio-player__error {
  margin-top: 2px;
}

@media (max-width: 1024px) {
  .audio-player__volume {
    display: none;
  }
}
</style>