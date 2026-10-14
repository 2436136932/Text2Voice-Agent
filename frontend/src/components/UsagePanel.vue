<script setup lang="ts">
/**
 * Usage dashboard for the history page.
 *
 * Surfaces three backend endpoints that previously had no UI:
 *   GET /history/stats  — totals, success rate, disk usage
 *   GET /meta/usage     — token/audio aggregation by dimension
 *   GET /history/export — CSV download link
 *
 * Collapsed by default so the table stays the focus; expands on demand so the
 * aggregate queries only run when the user actually looks at them.
 */
import { computed, ref, watch } from 'vue'
import { Download, Histogram, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fetchHistoryStats, fetchUsage, historyExportUrl } from '@/api'
import type { HistoryStats, UsageGroupBy, UsageResponse } from '@/api/types'
import { formatBytes, formatSeconds } from '@/utils/format'

const expanded = ref(false)
const activeTab = ref<'overview' | 'usage'>('overview')
const loading = ref(false)
const stats = ref<HistoryStats | null>(null)
const usage = ref<UsageResponse | null>(null)

/** Window + dimension for the usage table. */
const days = ref(30)
const groupBy = ref<UsageGroupBy>('llm_provider')

const GROUP_OPTIONS: { value: UsageGroupBy; label: string }[] = [
  { value: 'llm_provider', label: '按大模型服务商' },
  { value: 'llm_model', label: '按大模型' },
  { value: 'tts_provider', label: '按语音服务商' },
  { value: 'tts_voice', label: '按音色' },
  { value: 'day', label: '按日期' },
]

const successRate = computed<string>(() => {
  if (!stats.value || stats.value.total === 0) return '—'
  return `${Math.round((stats.value.succeeded / stats.value.total) * 100)}%`
})

/** group label formatted for display (dates get a friendlier shape). */
function groupLabel(item: { group: string }): string {
  return item.group || '（未记录）'
}

async function loadStats(): Promise<void> {
  loading.value = true
  try {
    stats.value = await fetchHistoryStats()
  } catch {
    // Non-fatal: the dashboard is informational.
    stats.value = null
  } finally {
    loading.value = false
  }
}

async function loadUsage(): Promise<void> {
  loading.value = true
  try {
    usage.value = await fetchUsage({ days: days.value, groupBy: groupBy.value })
  } catch {
    usage.value = null
    ElMessage.warning('用量统计加载失败')
  } finally {
    loading.value = false
  }
}

function refresh(): void {
  void loadStats()
  if (activeTab.value === 'usage') void loadUsage()
}

// Load lazily: nothing is fetched until the panel is opened.
watch(expanded, (open) => {
  if (!open) return
  if (stats.value === null) void loadStats()
  if (activeTab.value === 'usage' && usage.value === null) void loadUsage()
})

watch(activeTab, (tab) => {
  if (tab === 'usage' && usage.value === null) void loadUsage()
})

// Re-aggregate when the window or dimension changes.
watch([days, groupBy], () => {
  if (expanded.value && activeTab.value === 'usage') void loadUsage()
})

/** Lets the parent refresh the figures after a delete. */
defineExpose({ refresh })
</script>

<template>
  <el-card class="usage-panel" shadow="never">
    <template #header>
      <div class="usage-panel__head">
        <div class="usage-panel__head-left" @click="expanded = !expanded">
          <el-icon><Histogram /></el-icon>
          <span>用量统计</span>
          <el-tag v-if="stats" size="small" type="info" effect="plain">
            共 {{ stats.total }} 条
          </el-tag>
        </div>
        <div class="usage-panel__head-right">
          <el-button
            size="small"
            :icon="Download"
            tag="a"
            :href="historyExportUrl()"
            @click="() => {}"
          >
            导出 CSV
          </el-button>
          <el-button
            size="small"
            :icon="Refresh"
            :loading="loading"
            :disabled="!expanded"
            @click="refresh"
          />
          <el-button size="small" link @click="expanded = !expanded">
            {{ expanded ? '收起' : '展开' }}
          </el-button>
        </div>
      </div>
    </template>

    <div v-if="expanded" v-loading="loading" class="usage-panel__body">
      <el-tabs v-model="activeTab">
        <!-- ---------------- overview ---------------- -->
        <el-tab-pane label="概览" name="overview">
          <el-descriptions v-if="stats" :column="3" border size="small">
            <el-descriptions-item label="总记录数">{{ stats.total }}</el-descriptions-item>
            <el-descriptions-item label="成功">{{ stats.succeeded }}</el-descriptions-item>
            <el-descriptions-item label="失败">{{ stats.failed }}</el-descriptions-item>
            <el-descriptions-item label="成功率">{{ successRate }}</el-descriptions-item>
            <el-descriptions-item label="音频总时长">
              {{ formatSeconds(stats.total_audio_ms) }}
            </el-descriptions-item>
            <el-descriptions-item label="磁盘占用">
              {{ formatBytes(stats.audio_bytes) }}
              <span class="t2v-muted">（{{ stats.audio_files }} 个文件）</span>
            </el-descriptions-item>
          </el-descriptions>
          <el-empty v-else description="暂无统计数据" :image-size="60" />
        </el-tab-pane>

        <!-- ---------------- usage ---------------- -->
        <el-tab-pane label="Token / 用量" name="usage">
          <div class="usage-panel__filters">
            <el-select v-model="days" size="small" class="usage-panel__days">
              <el-option :value="7" label="最近 7 天" />
              <el-option :value="30" label="最近 30 天" />
              <el-option :value="90" label="最近 90 天" />
              <el-option :value="365" label="最近 365 天" />
            </el-select>
            <el-select v-model="groupBy" size="small" class="usage-panel__group">
              <el-option
                v-for="option in GROUP_OPTIONS"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </div>

          <el-table v-if="usage && usage.items.length > 0" :data="usage.items" size="small" border>
            <el-table-column label="维度" min-width="160">
              <template #default="{ row }">{{ groupLabel(row) }}</template>
            </el-table-column>
            <el-table-column prop="generations" label="生成次数" width="100" align="right" />
            <el-table-column prop="total_tokens" label="Token" width="110" align="right" />
            <el-table-column label="音频时长" width="110" align="right">
              <template #default="{ row }">{{ formatSeconds(row.audio_ms) }}</template>
            </el-table-column>
            <el-table-column label="LLM / TTS 耗时" min-width="150" align="right">
              <template #default="{ row }">
                {{ formatSeconds(row.llm_ms) }} / {{ formatSeconds(row.tts_ms) }}
              </template>
            </el-table-column>
          </el-table>

          <div v-if="usage && usage.items.length > 0" class="usage-panel__totals">
            合计：{{ usage.totals.generations }} 次生成 ·
            {{ usage.totals.total_tokens }} tokens ·
            {{ formatBytes(usage.totals.audio_bytes) }} 音频
          </div>

          <el-empty
            v-else
            description="该时间范围内暂无成功记录"
            :image-size="60"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

    <div v-else class="usage-panel__collapsed t2v-muted">
      展开查看成功率、Token 用量与磁盘占用，或直接导出 CSV。
    </div>
  </el-card>
</template>

<style scoped>
.usage-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.usage-panel__head-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-weight: 600;
}

.usage-panel__head-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.usage-panel__body {
  min-height: 120px;
}

.usage-panel__filters {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.usage-panel__days {
  width: 140px;
}

.usage-panel__group {
  width: 180px;
}

.usage-panel__totals {
  margin-top: 10px;
  font-size: 12px;
  color: var(--t2v-text-secondary);
}

.usage-panel__collapsed {
  font-size: 12px;
}
</style>
