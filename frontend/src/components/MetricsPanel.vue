<script setup lang="ts">
/**
 * Operations dashboard for the history page.
 *
 * Surfaces `GET /meta/metrics`, which previously had no frontend caller at all:
 * uptime, request/generation counters, degraded generations, the live queue
 * depth (`active_tasks`) and per-provider call counts.
 *
 * Collapsed by default so the history table stays the focus, and refreshed
 * manually — polling a counters endpoint in the background would add load for
 * data nobody is looking at.
 */
import { computed, ref, watch } from 'vue'
import { DataLine, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fetchMetrics } from '@/api'
import type { MetricsResponse } from '@/api/types'
import { formatDateTime } from '@/utils/format'
import {
  formatPercent,
  formatSuccessRate,
  formatUptime,
  parseProviderCalls,
  providerCallKeys,
  successRate,
} from '@/utils/metrics'

const expanded = ref(false)
const loading = ref(false)
/** True once a request has settled — distinguishes "" from "failed". */
const loaded = ref(false)
const metrics = ref<MetricsResponse | null>(null)
const error = ref<string | null>(null)
/** Local time of the last successful load, for the 上次更新 label. */
const lastUpdated = ref<string | null>(null)

/**
 * `uptime_seconds` is sampled per request, so the displayed uptime is simply
 * whatever the last snapshot reported — it is not a live ticking clock.
 */
const uptimeSeconds = computed<number | null>(() => metrics.value?.uptime_seconds ?? null)

const uptimeText = computed<string>(() => formatUptime(uptimeSeconds.value))

/** Generation success rate, `—` when nothing has finished yet. */
const generationRate = computed<string>(() =>
  formatSuccessRate(metrics.value?.generations_succeeded, metrics.value?.generations_total),
)

/** Divisions-by-zero guard regardless of what the backend sends. */
const generationRateValue = computed<number | null>(() =>
  successRate(metrics.value?.generations_succeeded, metrics.value?.generations_total),
)

/** Rows of the per-provider call table, busiest first. */
const providerRows = computed(() => parseProviderCalls(metrics.value?.provider_calls))

/** Success rate of one provider row; `—` when it was never called. */
function rowRate(row: { ok: number; total: number }): string {
  return formatPercent(successRate(row.ok, row.total))
}

/** 降级 is a warning: the platform silently used a fallback. */
const degradedCount = computed<number>(() => metrics.value?.generations_degraded ?? 0)

const activeTasks = computed<number>(() => metrics.value?.active_tasks ?? 0)

async function loadMetrics(): Promise<void> {
  loading.value = true
  try {
    metrics.value = await fetchMetrics()
    error.value = null
    lastUpdated.value = formatDateTime(new Date().toISOString())
  } catch (caught) {
    // Informational panel: report inline instead of replacing the page.
    metrics.value = null
    error.value = caught instanceof Error ? caught.message : '运行指标加载失败'
  } finally {
    loaded.value = true
    loading.value = false
  }
}

function refresh(): void {
  void loadMetrics()
  ElMessage.success('运行指标已刷新')
}

// Load lazily: nothing is fetched until the panel is opened for the first time.
watch(expanded, (open) => {
  if (open && !loaded.value) void loadMetrics()
})

/** Lets the parent refresh the figures after an action. */
defineExpose({ refresh: loadMetrics })
</script>

<template>
  <el-card class="metrics-panel" shadow="never">
    <template #header>
      <div class="metrics-panel__head">
        <div class="metrics-panel__head-left" @click="expanded = !expanded">
          <el-icon><DataLine /></el-icon>
          <span>运行指标</span>
          <el-tag v-if="metrics" size="small" type="info" effect="plain">
            运行 {{ uptimeText }}
          </el-tag>
          <el-tag v-if="metrics" size="small" effect="plain" type="primary">
            请求 {{ metrics.requests_total }}
          </el-tag>
          <el-tag v-if="degradedCount > 0" size="small" effect="plain" type="warning">
            降级 {{ degradedCount }}
          </el-tag>
        </div>
        <div class="metrics-panel__head-right">
          <span v-if="lastUpdated" class="metrics-panel__updated t2v-muted">
            上次更新 {{ lastUpdated }}
          </span>
          <el-button
            size="small"
            :icon="Refresh"
            :loading="loading"
            :disabled="!expanded"
            aria-label="刷新运行指标"
            @click="refresh"
          />
          <el-button size="small" link @click="expanded = !expanded">
            {{ expanded ? '收起' : '展开' }}
          </el-button>
        </div>
      </div>
    </template>

    <div v-if="expanded" v-loading="loading" class="metrics-panel__body">
      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        show-icon
        :title="error"
        class="metrics-panel__error"
      >
        <template #default>
          <span>运行指标接口不可用，不影响历史记录浏览。</span>
          <el-button size="small" link type="primary" @click="refresh">重试</el-button>
        </template>
      </el-alert>

      <template v-if="metrics">
        <!-- ---------------- overview ---------------- -->
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="运行时长">{{ uptimeText }}</el-descriptions-item>
          <el-descriptions-item label="请求总数">{{ metrics.requests_total }}</el-descriptions-item>
          <el-descriptions-item label="生成总数">{{ metrics.generations_total }}</el-descriptions-item>
          <el-descriptions-item label="成功">
            <span class="metrics-panel__ok">{{ metrics.generations_succeeded }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="失败">
            <span :class="metrics.generations_failed > 0 ? 'metrics-panel__failed' : ''">
              {{ metrics.generations_failed }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="降级">
            <span :class="degradedCount > 0 ? 'metrics-panel__degraded' : ''">
              {{ degradedCount }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="成功率">
            <span class="metrics-panel__rate">{{ generationRate }}</span>
            <span v-if="generationRateValue !== null" class="t2v-muted">
              （{{ metrics.generations_succeeded }}/{{ metrics.generations_total }}）
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="活跃任务">
            <el-tag v-if="activeTasks > 0" size="small" type="warning" effect="plain">
              {{ activeTasks }}
            </el-tag>
            <span v-else>{{ activeTasks }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="指标起始">
            <span class="t2v-muted">进程启动累计（重启后清零）</span>
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="degradedCount > 0" class="metrics-panel__hint metrics-panel__hint--warning">
          <el-icon><DataLine /></el-icon>
          <span>
            有 {{ degradedCount }} 次生成走了降级路径（备用服务商/音色），结果可用但并非首选配置。
          </span>
        </div>

        <!-- ---------------- provider calls ---------------- -->
        <section class="metrics-panel__section">
          <h4>服务商调用</h4>

          <el-table
            v-if="providerRows.length > 0"
            :data="providerRows"
            size="small"
            border
            class="metrics-panel__table"
          >
            <el-table-column label="类型" width="110">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  effect="plain"
                  :type="row.kind === 'llm' ? 'primary' : 'success'"
                >
                  {{ row.kindLabel }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="服务商" min-width="160">
              <template #default="{ row }">
                <el-tooltip :content="providerCallKeys(row)" placement="top" :show-after="300">
                  <span class="t2v-mono">{{ row.providerLabel }}</span>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column prop="ok" label="成功" width="90" align="right" />
            <el-table-column label="失败" width="90" align="right">
              <template #default="{ row }">
                <span :class="row.error > 0 ? 'metrics-panel__failed' : ''">{{ row.error }}</span>
              </template>
            </el-table-column>
            <el-table-column label="成功率" width="110" align="right">
              <template #default="{ row }">
                <span :class="row.total === 0 ? 't2v-muted' : ''">{{ rowRate(row) }}</span>
              </template>
            </el-table-column>
          </el-table>

          <el-empty v-else description="暂无服务商调用记录" :image-size="60" />
        </section>

        <div class="metrics-panel__totals t2v-muted">
          计数器为进程级累计值，服务重启后归零；活跃任务为实时队列深度。
        </div>
      </template>

      <el-empty
        v-else-if="!loading && !error"
        description="暂无运行指标"
        :image-size="60"
      />
    </div>

    <div v-else class="metrics-panel__collapsed t2v-muted">
      展开查看运行时长、生成成功率、降级次数与各服务商调用情况。
    </div>
  </el-card>
</template>

<style scoped>
.metrics-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.metrics-panel__head-left {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-weight: 600;
  flex-wrap: wrap;
}

.metrics-panel__head-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.metrics-panel__updated {
  font-size: 12px;
  white-space: nowrap;
}

.metrics-panel__body {
  min-height: 120px;
}

.metrics-panel__error {
  margin-bottom: 12px;
}

.metrics-panel__section {
  margin-top: 16px;
}

.metrics-panel__section h4 {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--t2v-text-secondary);
}

.metrics-panel__table {
  width: 100%;
}

.metrics-panel__ok {
  color: var(--t2v-color-success, #67c23a);
  font-weight: 600;
}

.metrics-panel__failed {
  color: var(--el-color-danger);
  font-weight: 600;
}

/* 降级 means a silent fallback: it must stand out from a plain zero. */
.metrics-panel__degraded {
  color: var(--el-color-warning);
  font-weight: 600;
}

.metrics-panel__rate {
  font-weight: 600;
}

.metrics-panel__hint {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  padding: 8px 12px;
  font-size: 12px;
  border-radius: var(--t2v-radius-sm);
}

.metrics-panel__hint--warning {
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9, rgba(230, 162, 60, 0.1));
  border: 1px solid var(--el-color-warning-light-5, rgba(230, 162, 60, 0.3));
}

.metrics-panel__totals {
  margin-top: 10px;
  font-size: 12px;
}

.metrics-panel__collapsed {
  font-size: 12px;
}
</style>
