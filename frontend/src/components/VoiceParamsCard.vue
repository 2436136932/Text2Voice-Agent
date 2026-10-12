<script setup lang="ts">
/**
 * Voice & model parameters: voice, speed, audio format, temperature and the
 * optional system prompt. Bounds and options come from the provider metadata.
 */
import { computed } from 'vue'
import { Setting } from '@element-plus/icons-vue'
import { useConfigStore } from '@/stores/config'
import { genderLabel } from '@/utils/format'

const configStore = useConfigStore()

/* ---------------------------------- voice --------------------------------- */

const voice = computed<string>({
  get: () => configStore.ttsVoice,
  set: (value: string) => {
    configStore.ttsVoice = value
  },
})

const voiceOptions = computed(() => configStore.ttsVoices)

/* ---------------------------------- speed --------------------------------- */

const speed = computed<number>({
  get: () => configStore.speed,
  set: (value: number) => {
    configStore.speed = value
  },
})

const speedMin = computed<number>(() => configStore.speedMin)
const speedMax = computed<number>(() => configStore.speedMax)
const supportsSpeed = computed<boolean>(() => configStore.supportsSpeed)

/** Slider label rendered as `1.00x`. */
const speedLabel = computed<string>(() => `${speed.value.toFixed(2)}x`)

/** Marks shown under the speed slider. */
const speedMarks = computed<Record<number, string>>(() => ({
  [speedMin.value]: `${speedMin.value.toFixed(2)}x`,
  [speedMax.value]: `${speedMax.value.toFixed(2)}x`,
}))

/* --------------------------------- format --------------------------------- */

const format = computed<string>({
  get: () => configStore.format,
  set: (value: string) => {
    configStore.format = value
  },
})

const formats = computed<string[]>(() => configStore.ttsFormats)

/* ------------------------------- temperature ------------------------------ */

const temperature = computed<number>({
  get: () => configStore.temperature,
  set: (value: number) => {
    configStore.temperature = value
  },
})

const temperatureLabel = computed<string>(() => temperature.value.toFixed(2))

/* ------------------------------ max tokens -------------------------------- */

const maxTokens = computed<number>({
  get: () => configStore.maxTokens,
  set: (value: number) => {
    configStore.maxTokens = value
  },
})

/* ----------------------------- system prompt ------------------------------ */

const systemPrompt = computed<string>({
  get: () => configStore.systemPrompt,
  set: (value: string) => {
    configStore.systemPrompt = value
  },
})

/* -------------------------------- options --------------------------------- */

const autoSave = computed<boolean>({
  get: () => configStore.autoSave,
  set: (value: boolean) => {
    configStore.autoSave = value
  },
})

const fallbackEnabled = computed<boolean>({
  get: () => configStore.fallbackEnabled,
  set: (value: boolean) => {
    configStore.fallbackEnabled = value
  },
})

/** Reset only the tunable parameters back to their defaults. */
function resetParams(): void {
  configStore.resetParams()
}
</script>

<template>
  <el-card class="voice-params-card" shadow="never">
    <template #header>
      <div class="voice-params-card__header">
        <div class="voice-params-card__header-left">
          <el-icon class="voice-params-card__icon"><Setting /></el-icon>
          <span>语音与模型参数</span>
        </div>
        <el-button link size="small" @click="resetParams">恢复默认</el-button>
      </div>
    </template>

    <el-form label-position="top" @submit.prevent>
      <el-form-item label="发音人">
        <el-select
          v-model="voice"
          class="voice-params-card__control"
          placeholder="请选择发音人"
          filterable
          :disabled="voiceOptions.length === 0"
        >
          <el-option
            v-for="item in voiceOptions"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          >
            <span class="voice-params-card__option">
              <span>{{ item.name }}</span>
              <span class="voice-params-card__option-tags">
                <el-tag size="small" effect="plain" type="info">{{ genderLabel(item.gender) }}</el-tag>
                <el-tag size="small" effect="plain">{{ item.language }}</el-tag>
              </span>
            </span>
          </el-option>
        </el-select>
      </el-form-item>

      <el-form-item>
        <template #label>
          <span class="voice-params-card__label">
            语速
            <el-tag v-if="!supportsSpeed" size="small" type="info" effect="plain">
              当前服务商不支持
            </el-tag>
          </span>
        </template>
        <div class="voice-params-card__slider-row">
          <el-slider
            v-model="speed"
            class="voice-params-card__slider"
            :min="speedMin"
            :max="speedMax"
            :step="0.05"
            :marks="speedMarks"
            :disabled="!supportsSpeed"
            show-stops
          />
          <span class="voice-params-card__value t2v-mono">{{ speedLabel }}</span>
        </div>
      </el-form-item>

      <el-form-item label="音频格式">
        <el-radio-group v-model="format" :disabled="formats.length === 0">
          <el-radio-button v-for="item in formats" :key="item" :value="item">
            {{ item.toUpperCase() }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item>
        <template #label>
          <span class="voice-params-card__label">
            温度 (temperature)
            <el-tooltip
              content="值越高回复越随机，值越低越确定。推荐 0.2 – 1.0。"
              placement="top"
            >
              <span class="voice-params-card__hint-icon">?</span>
            </el-tooltip>
          </span>
        </template>
        <div class="voice-params-card__slider-row">
          <el-slider
            v-model="temperature"
            class="voice-params-card__slider"
            :min="0"
            :max="2"
            :step="0.05"
            :marks="{ 0: '0.00', 1: '1.00', 2: '2.00' }"
          />
          <span class="voice-params-card__value t2v-mono">{{ temperatureLabel }}</span>
        </div>
      </el-form-item>

      <el-form-item label="最大生成 Token 数">
        <el-input-number
          v-model="maxTokens"
          class="voice-params-card__number"
          :min="1"
          :max="32768"
          :step="64"
          controls-position="right"
        />
      </el-form-item>

      <el-form-item label="系统提示词（可选）">
        <el-input
          v-model="systemPrompt"
          type="textarea"
          :rows="3"
          maxlength="2000"
          show-word-limit
          resize="vertical"
          placeholder="例如：你是一个乐于助人的助手。"
        />
      </el-form-item>

      <el-form-item label="生成选项">
        <div class="voice-params-card__switches">
          <el-switch v-model="autoSave" active-text="自动保存到历史记录" />
          <el-switch v-model="fallbackEnabled" active-text="失败时降级（仅返回文本）" />
        </div>
      </el-form-item>
    </el-form>
  </el-card>
</template>

<style scoped>
.voice-params-card {
  height: 100%;
}

.voice-params-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.voice-params-card__header-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.voice-params-card__icon {
  color: var(--t2v-color-primary);
}

.voice-params-card__control {
  width: 100%;
}

.voice-params-card__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.voice-params-card__option-tags {
  display: inline-flex;
  gap: 4px;
}

.voice-params-card__label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.voice-params-card__hint-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  border: 1px solid var(--t2v-border-color-strong);
  color: var(--t2v-text-muted);
  font-size: 11px;
  cursor: help;
}

.voice-params-card__slider-row {
  display: flex;
  align-items: center;
  gap: 16px;
  width: 100%;
}

.voice-params-card__slider {
  flex: 1 1 auto;
  min-width: 0;
  margin-bottom: 18px;
}

.voice-params-card__value {
  flex: 0 0 auto;
  min-width: 52px;
  text-align: right;
  color: var(--t2v-text-secondary);
  font-weight: 600;
}

.voice-params-card__number {
  width: 180px;
}

.voice-params-card__switches {
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-start;
}

.voice-params-card__switches :deep(.el-switch__label) {
  color: var(--t2v-text-secondary);
  font-size: 13px;
}
</style>