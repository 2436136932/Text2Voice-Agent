<script setup lang="ts">
/**
 * Text input card: the textarea, a live character counter, 清空 / 示例文本
 * buttons and the primary 生成语音 button with an explanatory tooltip.
 */
import { computed, ref } from 'vue'
import { Delete, MagicStick, Microphone } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { MAX_TOTAL_TEXT_LENGTH, useConfigStore } from '@/stores/config'
import { describeTokenEstimate, estimateUsage } from '@/utils/estimate'

const props = withDefaults(
  defineProps<{
    /** Whether a generation task is currently running. */
    generating?: boolean
    /** Whether the 生成语音 button is enabled (defaults to the store check). */
    canGenerate?: boolean
    /** Extra reasons appended to the disabled tooltip. */
    extraReasons?: string[]
  }>(),
  {
    generating: false,
    canGenerate: undefined,
    extraReasons: () => [],
  },
)

const emit = defineEmits<{
  (event: 'generate'): void
  (event: 'clear'): void
}>()

const configStore = useConfigStore()

/**
 * Real per-request limit for the selected TTS provider.
 *
 * Providers declare their own `max_chars` (volcengine 1024, edge 3000, mimo
 * 4000, openai 4096 …), so the split limit is the smaller of that and the
 * pipeline limit. Reading MAX_TEXT_LENGTH directly here is what let the badge
 * promise 2 segments while the provider rejected segment 1 outright.
 */
const maxLen = computed<number>(() => configStore.segmentLimit)
/** Ceiling for the whole input, so one run cannot queue hundreds of jobs. */
const maxTotal = MAX_TOTAL_TEXT_LENGTH

/** Sample text used by the 示例文本 button. */
const SAMPLE_TEXT =
  '请用三句话介绍什么是文本转语音（Text-to-Speech），并说明它在无障碍阅读、智能客服和有声内容创作中的典型用途。'

const sampleIndex = ref(0)

const samples: string[] = [
  SAMPLE_TEXT,
  '请写一段 100 字左右的品牌开场白，语气温暖、专业，适合用在智能音箱的早间问候场景。',
  '把下面这句话改写得更口语化，适合朗读："本产品支持多语言、多音色与语速调节。"',
]

const text = computed<string>({
  get: () => configStore.text,
  set: (value: string) => {
    configStore.text = value
  },
})

/** Live character counter, e.g. `128 / 5000`. */
/**
 * Live character counter. Shows the per-request limit while the text fits in
 * one call, then switches to the total ceiling once segmentation kicks in —
 * otherwise "6000 / 5000" would look like an error rather than a split.
 */
const counter = computed<string>(() => {
  if (text.value.length <= maxLen.value) return `${text.value.length} / ${maxLen.value}`
  return `${text.value.length} / ${maxTotal}`
})

const trimmedLength = computed<number>(() => text.value.trim().length)

/**
 * Pre-submit preview: characters, how many requests this becomes, and a rough
 * token estimate for the LLM step.
 *
 * The segment count is taken from here rather than computed again, so the badge
 * and the estimate cannot drift apart — and both use the provider-derived
 * limit above, which is what the submit path sends.
 */
const estimate = computed(() =>
  estimateUsage({
    text: text.value,
    limit: maxLen.value,
    mode: configStore.mode,
    systemPrompt: configStore.systemPrompt,
    maxTokens: configStore.maxTokens,
    totalCeiling: maxTotal,
  }),
)

/** How many requests this text becomes. Shown as a badge. */
const segmentCount = computed<number>(() => estimate.value.segmentCount)

/**
 * Token line. When the mode skips the model this says so instead of showing a
 * figure — "0 tokens" would read as a measurement, and there is nothing to
 * measure.
 */
const tokenLabel = computed<string>(() => describeTokenEstimate(estimate.value))

/** Explains where the number comes from, so it is not mistaken for exact. */
const tokenHint = computed<string>(() =>
  estimate.value.usesLlm
    ? '按字符类型粗略估算：中文约 1 token/字，英文约 4 字符/token，不含精确分词'
    : '当前模式直接把输入文字合成为语音，不会调用大模型',
)

/** Whether the button is actually clickable. */
const enabled = computed<boolean>(() => {
  if (props.canGenerate !== undefined) return props.canGenerate
  return configStore.canGenerate
})

/** Combined tooltip content explaining why the button is disabled. */
const disabledTooltip = computed<string>(() => {
  if (enabled.value) return '调用大模型生成回复并合成语音'
  const reasons = [...configStore.disabledReasons, ...props.extraReasons]
  return reasons.length > 0 ? `暂不可生成：${reasons.join('；')}` : '配置不完整，无法生成'
})

function handleClear(): void {
  configStore.text = ''
  emit('clear')
}

function handleSample(): void {
  configStore.text = samples[sampleIndex.value % samples.length]
  sampleIndex.value += 1
  ElMessage.success('已填入示例文本')
}

function handleGenerate(): void {
  if (!enabled.value) {
    ElMessage.warning(disabledTooltip.value)
    return
  }
  emit('generate')
}
</script>

<template>
  <el-card class="text-input-card" shadow="never">
    <template #header>
      <div class="text-input-card__header">
        <div class="text-input-card__header-left">
          <el-icon class="text-input-card__icon"><Microphone /></el-icon>
          <span>输入文本</span>
        </div>
        <div class="text-input-card__counter t2v-mono">{{ counter }}</div>
      </div>
    </template>

    <el-input
      v-model="text"
      type="textarea"
      :rows="8"
      :maxlength="maxTotal"
      resize="vertical"
      placeholder="在这里输入要交给大模型的文本，例如：请用三句话介绍文本转语音技术。"
      class="text-input-card__textarea"
    />

    <div class="text-input-card__toolbar">
      <div class="text-input-card__toolbar-left">
        <el-button :icon="MagicStick" @click="handleSample">示例文本</el-button>
        <el-button :icon="Delete" :disabled="text.length === 0" @click="handleClear">清空</el-button>
        <span class="text-input-card__hint t2v-muted">
          当前 {{ trimmedLength }} 个有效字符
        </span>
        <el-tag v-if="segmentCount > 1" size="small" type="warning" effect="plain">
          将自动分为 {{ segmentCount }} 段生成
        </el-tag>
        <el-tooltip :content="tokenHint" placement="top">
          <el-tag size="small" type="info" effect="plain" class="text-input-card__estimate">
            {{ tokenLabel }}
          </el-tag>
        </el-tooltip>
        <el-tag
          v-if="estimate.exceedsTotalCeiling"
          size="small"
          type="danger"
          effect="plain"
        >
          超出单次上限（最多 {{ maxTotal }} 字符）
        </el-tag>
      </div>

      <div class="text-input-card__toolbar-right">
        <el-tooltip :content="disabledTooltip" placement="top" :disabled="false">
          <span class="text-input-card__generate-wrap">
            <el-button
              type="primary"
              size="large"
              :icon="Microphone"
              :loading="generating"
              :disabled="!enabled || generating"
              @click="handleGenerate"
            >
              {{ generating ? '生成中…' : '生成语音' }}
            </el-button>
          </span>
        </el-tooltip>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.text-input-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.text-input-card__header-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.text-input-card__icon {
  color: var(--t2v-color-primary);
}

.text-input-card__counter {
  color: var(--t2v-text-secondary);
}

.text-input-card__textarea :deep(.el-textarea__inner) {
  font-size: 14px;
  line-height: 1.7;
}

.text-input-card__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.text-input-card__toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.text-input-card__toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.text-input-card__hint {
  font-size: 12px;
}

/* Tabular figures so the estimate does not jitter in width while typing. */
.text-input-card__estimate {
  font-variant-numeric: tabular-nums;
}

.text-input-card__generate-wrap {
  display: inline-flex;
}

@media (max-width: 1024px) {
  .text-input-card__toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .text-input-card__toolbar-right {
    justify-content: flex-end;
  }
}
</style>