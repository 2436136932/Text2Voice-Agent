<script setup lang="ts">
/**
 * Voice-clone manager.
 *
 * Two-step flow, matching the backend contract:
 *   1. upload a reference clip (`MediaPicker` -> `POST /media/inspect`)
 *   2. create the voice (`POST /voices/clone`) — the backend calls the vendor's
 *      clone API (or registers the clip for prompt-based cloning) and stores a
 *      row locally. **The API key is used for that call only and is never
 *      persisted.**
 *
 * Cloned voices show up in the TTS voice list on this page, so they can be
 * selected like any built-in voice.
 */
import { computed, onMounted, ref } from 'vue'
import { Delete, MagicStick, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import MediaPicker from '@/components/MediaPicker.vue'
import { ApiError } from '@/api/http'
import { cloneVoice, deleteVoice, fetchVoices } from '@/api'
import type { ClonedVoice, MediaInfo } from '@/api/types'
import { useConfigStore } from '@/stores/config'
import { formatDateTime, formatDuration } from '@/utils/format'

const configStore = useConfigStore()

const mediaId = ref<string | null>(null)
const media = ref<MediaInfo | null>(null)
const voiceName = ref('')
const cloning = ref(false)
const loading = ref(false)
const voices = ref<ClonedVoice[]>([])

/** Providers that can actually clone — drives the guard below. */
const provider = computed(() => configStore.ttsProvider)
const supportsClone = computed(() => configStore.supportsVoiceClone)
const supportsReference = computed(() => configStore.supportsReferenceAudio)
const needsReferenceMode = computed(() => !supportsClone.value && supportsReference.value)

const cloneModels = computed(() => configStore.cloneModels)

const cloneModel = ref<string>('')

const blockingReason = computed<string | null>(() => {
  if (!provider.value) return '请先在上方选择「语音合成服务商」'
  if (!supportsClone.value && !supportsReference.value) {
    return `当前服务商「${provider.value.name}」不支持声音克隆，因此此处无法创建音色。请在上方切换到支持克隆的服务商（例如 ElevenLabs、MiniMax、自定义(OpenAI 兼容)），或直接使用「文字转语音」。`
  }
  if (!configStore.ttsApiKey.trim()) return '请先在上方填写该服务商的 API Key'
  if (!voiceName.value.trim()) return '请给这个音色起个名字'
  if (!mediaId.value) return '请先上传参考音频或视频'
  return null
})

const canClone = computed(() => blockingReason.value === null && !cloning.value)

async function loadVoices(): Promise<void> {
  loading.value = true
  try {
    voices.value = (await fetchVoices()).items
  } catch (error) {
    if (error instanceof ApiError) ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

async function handleClone(): Promise<void> {
  const reason = blockingReason.value
  if (reason || !mediaId.value) {
    ElMessage.warning(reason ?? '参考文件缺失')
    return
  }

  cloning.value = true
  try {
    const created = await cloneVoice({
      provider: configStore.ttsProviderId,
      api_key: configStore.ttsApiKey,
      base_url: configStore.ttsBaseUrlOrNull,
      name: voiceName.value.trim(),
      media_id: mediaId.value,
      model: cloneModel.value || configStore.ttsModel || null,
      extra: {},
    })
    ElMessage.success(`音色「${created.name}」创建成功，已在音色列表中可选`)
    voiceName.value = ''
    await loadVoices()
    // Refresh the provider catalogue so the new voice appears in the selector.
    await configStore.loadProviders(true)
  } catch (error) {
    reportError(error, '创建音色失败')
  } finally {
    cloning.value = false
  }
}

async function handleDelete(item: ClonedVoice): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除音色「${item.name}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteVoice(item.id)
    ElMessage.success('已删除')
    await loadVoices()
    await configStore.loadProviders(true)
  } catch (error) {
    reportError(error, '删除失败')
  }
}

function reportError(error: unknown, fallback: string): void {
  if (error instanceof ApiError) {
    const suggestion = (error.detail as { suggestion?: string } | undefined)?.suggestion
    ElMessage.error({
      message: `${error.message}${suggestion ? `\n${suggestion}` : ''}${error.code ? `\n[${error.code}]` : ''}`,
      duration: 8000,
      showClose: true,
    })
  } else {
    ElMessage.error(fallback)
  }
}

onMounted(loadVoices)
</script>

<template>
  <el-card class="voice-clone" shadow="never">
    <template #header>
      <div class="voice-clone__header">
        <span class="voice-clone__title">声音克隆</span>
        <el-button link size="small" :icon="Refresh" :loading="loading" @click="loadVoices">
          刷新
        </el-button>
      </div>
    </template>

    <el-alert
      v-if="blockingReason && !mediaId"
      class="voice-clone__block"
      type="info"
      show-icon
      :closable="false"
      :title="blockingReason"
    />

    <el-alert
      v-if="needsReferenceMode"
      class="voice-clone__block"
      type="warning"
      show-icon
      :closable="false"
      title="当前服务商没有独立的声音克隆接口，将把参考音频作为「音色提示」在合成时一并发送。效果取决于所选模型。"
    />

    <el-form label-position="top" @submit.prevent>
      <el-form-item label="参考音频 / 视频">
        <MediaPicker
          v-model:media-id="mediaId"
          v-model:media="media"
          :allow-trim="true"
          :hint="'建议 5–30 秒、单人清晰人声。可先裁剪出最干净的片段再克隆。'"
        />
      </el-form-item>

      <el-form-item label="音色名称">
        <el-input
          v-model="voiceName"
          clearable
          maxlength="40"
          show-word-limit
          placeholder="例如：我的声音、客服女声"
        />
      </el-form-item>

      <el-form-item v-if="cloneModels.length > 0" label="克隆使用的模型">
        <el-select v-model="cloneModel" clearable placeholder="使用服务商默认克隆模型">
          <el-option v-for="item in cloneModels" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
      </el-form-item>

      <el-button
        type="primary"
        :icon="MagicStick"
        :loading="cloning"
        :disabled="!canClone"
        @click="handleClone"
      >
        创建音色
      </el-button>
    </el-form>

    <el-divider />

    <div class="voice-clone__list-title">已创建的音色（{{ voices.length }}）</div>
    <el-table v-if="voices.length > 0" :data="voices" size="small" class="voice-clone__table">
      <el-table-column prop="name" label="名称" min-width="120" show-overflow-tooltip />
      <el-table-column label="服务商" width="120">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ row.provider }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时长" width="90">
        <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button link size="small" @click="configStore.ttsVoice = row.voice_id">选用</el-button>
          <el-button link type="danger" size="small" :icon="Delete" @click="handleDelete(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-else description="还没有创建过音色" :image-size="60" />
  </el-card>
</template>

<style scoped>
.voice-clone__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.voice-clone__title {
  font-weight: 600;
}

.voice-clone__block {
  margin-bottom: 14px;
}

.voice-clone__list-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
}

.voice-clone__table {
  width: 100%;
}
</style>
