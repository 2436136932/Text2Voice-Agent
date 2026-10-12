<script setup lang="ts">
/**
 * Renders a backend error envelope: the human readable `message` is always
 * visible, while `code` / `request_id` / `detail` live in a collapsible block.
 */
import { computed, ref } from 'vue'
import { CopyDocument, Refresh, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { ApiError } from '@/api/http'
import { copyText } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    /** The failure to display; `null` renders nothing. */
    error: ApiError | null
    /** Optional headline overriding the default one. */
    title?: string
    /** Show the "重试" button. */
    retryable?: boolean
    /** Show the "复制诊断信息" button. */
    copyable?: boolean
  }>(),
  {
    title: '生成失败',
    retryable: true,
    copyable: true,
  },
)

const emit = defineEmits<{
  (event: 'retry'): void
  (event: 'dismiss'): void
}>()

const detailOpen = ref<string[]>([])

/** Backend error code, e.g. `TTS_AUTH_FAILED`. */
const code = computed<string>(() => props.error?.code ?? 'UNKNOWN_ERROR')

/** Correlation id for support tickets. */
const requestId = computed<string>(() => props.error?.requestId ?? '—')

/** Pretty-printed `detail` payload. */
const detailText = computed<string>(() => {
  const detail = props.error?.detail
  if (!detail) return ''
  try {
    return JSON.stringify(detail, null, 2)
  } catch {
    return String(detail)
  }
})

/** One-line diagnostic blob used by the copy button. */
const diagnostic = computed<string>(() =>
  JSON.stringify(
    {
      code: code.value,
      message: props.error?.message ?? '',
      status: props.error?.status ?? null,
      request_id: props.error?.requestId ?? null,
      detail: props.error?.detail ?? null,
    },
    null,
    2,
  ),
)

/** Element Plus alert type derived from the failure kind. */
const alertType = computed<'error' | 'warning'>(() => {
  if (!props.error) return 'error'
  if (props.error.isNetworkError || props.error.isTimeout) return 'warning'
  return 'error'
})

async function handleCopy(): Promise<void> {
  const ok = await copyText(diagnostic.value)
  if (ok) ElMessage.success('诊断信息已复制到剪贴板')
  else ElMessage.warning('复制失败，请手动选择文本')
}
</script>

<template>
  <el-alert
    v-if="error"
    class="error-alert"
    :type="alertType"
    :closable="true"
    show-icon
    @close="emit('dismiss')"
  >
    <template #title>
      <span class="error-alert__title">
        <el-icon><WarningFilled /></el-icon>
        {{ title }}
      </span>
    </template>

    <div class="error-alert__body">
      <p class="error-alert__message">{{ error.message }}</p>

      <div class="error-alert__meta">
        <el-tag type="danger" size="small" effect="plain">code: {{ code }}</el-tag>
        <el-tag type="info" size="small" effect="plain">
          request_id: {{ requestId }}
        </el-tag>
        <el-tag v-if="error.status !== null" type="info" size="small" effect="plain">
          HTTP {{ error.status }}
        </el-tag>
      </div>

      <div class="error-alert__actions">
        <el-button
          v-if="retryable"
          type="primary"
          size="small"
          :icon="Refresh"
          @click="emit('retry')"
        >
          重试
        </el-button>
        <el-button
          v-if="copyable"
          size="small"
          :icon="CopyDocument"
          @click="handleCopy"
        >
          复制诊断信息
        </el-button>
      </div>

      <el-collapse v-model="detailOpen" class="t2v-error-detail">
        <el-collapse-item name="detail">
          <template #title>
            <span class="error-alert__detail-title">错误详情（code / request_id / detail）</span>
          </template>
          <div class="t2v-error-detail__body">
            <div>
              <strong>code：</strong><span class="t2v-mono">{{ code }}</span>
            </div>
            <div>
              <strong>request_id：</strong><span class="t2v-mono">{{ requestId }}</span>
            </div>
            <div v-if="detailText">
              <strong>detail：</strong>
              <pre class="error-alert__pre t2v-selectable">{{ detailText }}</pre>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-alert>
</template>

<style scoped>
.error-alert {
  align-items: flex-start;
}

.error-alert :deep(.el-alert__content) {
  width: 100%;
}

.error-alert__title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}

.error-alert__body {
  margin-top: 6px;
}

.error-alert__message {
  color: var(--t2v-text-primary);
  word-break: break-word;
  line-height: 1.6;
}

.error-alert__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.error-alert__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.error-alert__detail-title {
  font-size: 12px;
  color: var(--t2v-text-secondary);
}

.error-alert__pre {
  margin: 4px 0 0;
  padding: 8px 10px;
  background: var(--t2v-bg-muted);
  border: 1px solid var(--t2v-border-color);
  border-radius: var(--t2v-radius-sm);
  font-size: 12px;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>