<script setup lang="ts">
/**
 * Generation result: reply text with copy, audio player, download / regenerate
 * actions, degradation warning and a latency/token breakdown.
 */
import { computed, ref } from 'vue'
import { CopyDocument, Download, Microphone, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import AudioPlayer from '@/components/AudioPlayer.vue'
import { audioUrl, fetchAudioBlob } from '@/api'
import type { GenerateResponse } from '@/api/types'
import { buildAudioFilename, canPickSaveLocation, saveBlobAs } from '@/utils/download'
import { copyText, formatBytes, formatDateTime, formatSeconds } from '@/utils/format'

const props = defineProps<{
  /** Result payload from `POST /generate` or `GET /tasks/{id}/result`. */
  result: GenerateResponse
}>()

const emit = defineEmits<{
  (event: 'regenerate'): void
  (event: 'regenerate-tts'): void
  (event: 'play'): void
}>()

const copying = ref(false)
const showFullReply = ref(false)

/** Reply text, always shown selectable. */
const replyText = computed<string>(() => props.result.reply_text ?? '')

/** Download link with `?download=1` as required by the backend. */
const downloadUrl = computed<string>(() => {
  if (props.result.audio_url) {
    return props.result.audio_url.includes('?')
      ? `${props.result.audio_url}&download=1`
      : `${props.result.audio_url}?download=1`
  }
  if (props.result.record_id !== null) return audioUrl(props.result.record_id, true)
  return ''
})

/** Suggested file name for the download. */
const downloadName = computed<string>(() =>
  buildAudioFilename(props.result.record_id ?? 'result', props.result.audio_format || 'mp3'),
)

/** Whether the browser can prompt for a save location. */
const supportsSaveAs = computed<boolean>(() => canPickSaveLocation())

const saving = ref(false)

/**
 * Save the audio, preferring a user-chosen destination.
 *
 * Chromium lets us open a real "另存为" dialog via the File System Access API;
 * elsewhere we fall back to the browser's download folder.
 */
async function handleSaveAs(): Promise<void> {
  if (!props.result.audio_url && props.result.record_id === null) {
    ElMessage.warning('没有可导出的音频')
    return
  }
  saving.value = true
  try {
    let blob: Blob
    if (props.result.audio_base64) {
      const binary = atob(props.result.audio_base64)
      const bytes = new Uint8Array(binary.length)
      for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
      blob = new Blob([bytes], { type: 'audio/mpeg' })
    } else if (props.result.record_id !== null) {
      blob = await fetchAudioBlob(props.result.record_id)
    } else {
      const response = await fetch(props.result.audio_url)
      blob = await response.blob()
    }

    const mime = blob.type || 'audio/mpeg'
    const outcome = await saveBlobAs(blob, downloadName.value, mime)
    if (outcome.cancelled) return
    ElMessage.success(
      outcome.mode === 'picker' ? '已保存到你选择的文件夹' : '已保存到浏览器下载目录',
    )
  } catch (error) {
    ElMessage.error(error instanceof Error ? `保存失败：${error.message}` : '保存失败')
  } finally {
    saving.value = false
  }
}

/** Playable audio URL. */
const audioSrc = computed<string>(() => {
  if (props.result.audio_url) return props.result.audio_url
  if (props.result.record_id !== null) return audioUrl(props.result.record_id)
  return ''
})

/** Whether a downloadable audio file exists. */
const hasAudio = computed<boolean>(() => audioSrc.value.length > 0)

/** Token usage summary. */
const usage = computed(() => props.result.llm?.usage ?? null)

/** Total latency in seconds with two decimals. */
const totalLatency = computed<string>(() => formatSeconds(props.result.total_latency_ms))

async function handleCopy(): Promise<void> {
  copying.value = true
  const ok = await copyText(replyText.value)
  copying.value = false
  if (ok) ElMessage.success('回复文本已复制')
  else ElMessage.warning('复制失败，请手动选择文本')
}

/** Plain-text export of the whole result, used by the "复制全部" action. */
async function handleCopyAll(): Promise<void> {
  const payload = [
    `【输入】${props.result.input_text}`,
    '',
    `【回复】${replyText.value}`,
    '',
    `【服务商】LLM: ${props.result.llm?.provider ?? '—'} / ${props.result.llm?.model ?? '—'}`,
    `【语音】TTS: ${props.result.tts?.provider ?? '—'} / ${props.result.tts?.voice ?? '—'}`,
    `【耗时】总计 ${totalLatency.value}`,
    `【生成时间】${formatDateTime(props.result.created_at)}`,
  ].join('\n')
  const ok = await copyText(payload)
  if (ok) ElMessage.success('完整结果已复制')
  else ElMessage.warning('复制失败，请手动选择文本')
}
</script>

<template>
  <el-card class="result-card" shadow="never">
    <template #header>
      <div class="result-card__header">
        <div class="result-card__header-left">
          <el-icon class="result-card__icon"><Microphone /></el-icon>
          <span>生成结果</span>
          <el-tag v-if="result.record_id !== null" size="small" effect="plain" type="info">
            #{{ result.record_id }}
          </el-tag>
        </div>
        <div class="result-card__header-right">
          <el-button size="small" :icon="CopyDocument" :loading="copying" @click="handleCopy">
            复制回复
          </el-button>
          <el-button size="small" @click="handleCopyAll">复制全部</el-button>
        </div>
      </div>
    </template>

    <el-alert
      v-if="result.degraded"
      class="result-card__degraded"
      type="warning"
      show-icon
      :closable="false"
      title="本次生成已降级"
      :description="result.degraded_reason || '语音合成未成功，仅返回文本结果。'"
    />

    <section class="result-card__section">
      <div class="result-card__section-head">
        <h3 class="result-card__section-title">大模型回复</h3>
        <el-button link size="small" @click="showFullReply = !showFullReply">
          {{ showFullReply ? '收起' : '展开全文' }}
        </el-button>
      </div>
      <el-scrollbar :max-height="showFullReply ? 600 : 240" class="result-card__scroll">
        <p class="t2v-scroll-text t2v-selectable">{{ replyText || '（无回复内容）' }}</p>
      </el-scrollbar>
    </section>

    <section class="result-card__section">
      <h3 class="result-card__section-title">语音结果</h3>
      <AudioPlayer
        v-if="hasAudio"
        :src="audioSrc"
        :download-url="downloadUrl"
        :download-name="downloadName"
        :duration-ms="result.duration_ms"
        :format="result.audio_format"
        title="合成的语音"
        @play="emit('play')"
      />
      <el-empty v-else description="本次未生成音频（可能已降级）" :image-size="60" />
    </section>

    <section class="result-card__section">
      <h3 class="result-card__section-title">性能与用量</h3>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="LLM 服务商">
          {{ result.llm?.provider ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="模型">
          {{ result.llm?.model ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="TTS 服务商">
          {{ result.tts?.provider ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="发音人">
          <template v-if="result.tts?.used_reference_audio">
            <el-tag type="warning" size="small" effect="plain">参考音频克隆</el-tag>
          </template>
          <template v-else>{{ result.tts?.voice ?? '—' }}</template>
        </el-descriptions-item>
        <el-descriptions-item label="LLM 耗时">
          {{ formatSeconds(result.llm?.latency_ms) }}
        </el-descriptions-item>
        <el-descriptions-item label="TTS 耗时">
          {{ formatSeconds(result.tts?.latency_ms) }}
        </el-descriptions-item>
        <el-descriptions-item label="总耗时">
          <strong>{{ totalLatency }}</strong>
        </el-descriptions-item>
        <el-descriptions-item label="音频时长">
          {{ formatSeconds(result.duration_ms) }}
        </el-descriptions-item>
        <el-descriptions-item label="音频大小">
          {{ formatBytes(result.audio_bytes) }}
        </el-descriptions-item>
        <el-descriptions-item label="音频格式">
          {{ result.audio_format || '—' }}
        </el-descriptions-item>
        <el-descriptions-item v-if="usage" label="Token 用量">
          <span class="t2v-mono">
            输入 {{ usage.prompt_tokens }} / 输出 {{ usage.completion_tokens }} / 合计
            {{ usage.total_tokens }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item v-else label="Token 用量">—</el-descriptions-item>
        <el-descriptions-item label="生成时间">
          {{ formatDateTime(result.created_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="是否降级">
          <el-tag :type="result.degraded ? 'warning' : 'success'" size="small" effect="plain">
            {{ result.degraded ? '是' : '否' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
    </section>

    <div class="result-card__actions">
      <el-tooltip
        :content="
          supportsSaveAs
            ? '选择保存位置后导出音频'
            : '浏览器不支持选择位置，将保存到默认下载目录'
        "
        placement="top"
      >
        <span>
          <el-button
            type="primary"
            :icon="Download"
            :loading="saving"
            :disabled="!hasAudio"
            @click="handleSaveAs"
          >
            {{ supportsSaveAs ? '另存为…' : '下载音频' }}
          </el-button>
        </span>
      </el-tooltip>
      <el-button :icon="RefreshRight" :disabled="!hasAudio" @click="emit('regenerate-tts')">
        仅重新合成语音
      </el-button>
      <el-button :icon="RefreshRight" @click="emit('regenerate')">重新生成</el-button>
    </div>
  </el-card>
</template>

<style scoped>
.result-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.result-card__header-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.result-card__header-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.result-card__icon {
  color: var(--t2v-color-primary);
}

.result-card__degraded {
  margin-bottom: 16px;
}

.result-card__section {
  margin-bottom: 18px;
}

.result-card__section:last-of-type {
  margin-bottom: 0;
}

.result-card__section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.result-card__section-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--t2v-text-primary);
}

.result-card__scroll {
  padding: 12px 14px;
  border: 1px solid var(--t2v-border-color);
  border-radius: var(--t2v-radius-sm);
  background: var(--t2v-bg-muted);
}

.result-card__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--t2v-border-color);
}
</style>