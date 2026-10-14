<script setup lang="ts">
/**
 * Top header: current page title, backend health indicator (polled every 30s)
 * and a link to the Swagger docs.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Link, Moon, Refresh, Sunny } from '@element-plus/icons-vue'
import { fetchHealth } from '@/api'
import { ApiError } from '@/api/http'
import type { HealthResponse } from '@/api/types'
import { useTheme } from '@/composables/theme'

const route = useRoute()
const { theme, toggle } = useTheme()
const isDark = computed(() => theme.value === 'dark')

// Swagger lives on the BACKEND (port 8000). A relative "/docs" would hit the
// frontend origin, where no such route exists (the Vite proxy only forwards
// /api) and the user would land on the 404 page. Default to the backend
// origin in dev; nginx proxies /docs in production so the relative value
// still works there if VITE_DOCS_URL is set to it.
const backendOrigin = window.location.port === '5173'
  ? `${window.location.protocol}//${window.location.hostname}:8000`
  : ''
const docsUrl = import.meta.env.VITE_DOCS_URL || `${backendOrigin}/docs`
const pollIntervalMs = Number(import.meta.env.VITE_HEALTH_POLL_INTERVAL_MS) || 30_000

type HealthState = 'ok' | 'down' | 'checking'

const health = ref<HealthState>('checking')
const healthInfo = ref<HealthResponse | null>(null)
const healthMessage = ref<string>('正在检测后端服务…')
const lastCheckedAt = ref<Date | null>(null)

let timer: ReturnType<typeof setInterval> | null = null

/** Page title from the matched route meta. */
const pageTitle = computed<string>(() => {
  const title = route.meta.title
  return typeof title === 'string' && title.length > 0 ? title : 'Text2Voice Agent'
})

const tagType = computed<'success' | 'danger' | 'info'>(() => {
  if (health.value === 'ok') return 'success'
  if (health.value === 'down') return 'danger'
  return 'info'
})

const tagText = computed<string>(() => {
  if (health.value === 'ok') return '后端正常'
  if (health.value === 'down') return '后端不可用'
  return '检测中…'
})

/** Tooltip describing the latest probe. */
const tagTooltip = computed<string>(() => {
  const parts: string[] = [healthMessage.value]
  if (healthInfo.value) parts.push(`版本 ${healthInfo.value.version}`)
  if (healthInfo.value) parts.push(`运行 ${Math.round(healthInfo.value.uptime_seconds)}s`)
  if (lastCheckedAt.value) {
    parts.push(`检测于 ${lastCheckedAt.value.toLocaleTimeString('zh-CN', { hour12: false })}`)
  }
  return parts.join(' · ')
})

async function checkHealth(): Promise<void> {
  health.value = 'checking'
  try {
    const info = await fetchHealth()
    healthInfo.value = info
    if (info.status === 'ok') {
      health.value = 'ok'
      healthMessage.value = '后端服务连接正常'
    } else {
      health.value = 'down'
      healthMessage.value = `后端返回状态：${info.status}`
    }
  } catch (error) {
    health.value = 'down'
    healthInfo.value = null
    healthMessage.value =
      error instanceof ApiError ? error.message : '无法连接到后端服务，请确认服务已启动。'
  } finally {
    lastCheckedAt.value = new Date()
  }
}

onMounted(() => {
  void checkHealth()
  timer = setInterval(() => {
    void checkHealth()
  }, pollIntervalMs)
})

onBeforeUnmount(() => {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
})
</script>

<template>
  <header class="app-header">
    <div class="app-header__title">
      <h1>{{ pageTitle }}</h1>
    </div>

    <div class="app-header__actions">
      <el-tooltip :content="tagTooltip" placement="bottom">
        <el-tag :type="tagType" effect="light" round size="large" class="app-header__health">
          <span class="app-header__dot" :class="`app-header__dot--${health}`" />
          {{ tagText }}
        </el-tag>
      </el-tooltip>

      <el-tooltip content="重新检测后端健康状态" placement="bottom">
        <el-button
          :icon="Refresh"
          circle
          size="small"
          :loading="health === 'checking'"
          aria-label="重新检测"
          @click="checkHealth"
        />
      </el-tooltip>

      <el-tooltip :content="isDark ? '切换到浅色模式' : '切换到深色模式'" placement="bottom">
        <el-button
          :icon="isDark ? Sunny : Moon"
          circle
          size="small"
          aria-label="切换深浅色模式"
          @click="toggle"
        />
      </el-tooltip>

      <el-divider direction="vertical" />

      <el-button
        tag="a"
        :href="docsUrl"
        target="_blank"
        rel="noopener noreferrer"
        link
        type="primary"
        :icon="Link"
      >
        接口文档
      </el-button>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  position: sticky;
  top: 0;
  z-index: 15;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: var(--t2v-header-height);
  padding: 0 var(--t2v-content-padding);
  /* macOS-style translucent toolbar */
  background: var(--t2v-bg-sidebar);
  backdrop-filter: var(--t2v-backdrop-blur);
  -webkit-backdrop-filter: var(--t2v-backdrop-blur);
  border-bottom: 1px solid var(--t2v-border-color);
}

.app-header__title h1 {
  font-size: 16px;
  font-weight: 600;
}

.app-header__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.app-header__health {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
}

.app-header__dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--t2v-text-muted);
}

.app-header__dot--ok {
  background: var(--t2v-color-success);
  box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.16);
}

.app-header__dot--down {
  background: var(--t2v-color-danger);
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.16);
}

.app-header__dot--checking {
  background: var(--t2v-color-warning);
}

@media (max-width: 1024px) {
  .app-header {
    padding: 0 14px;
  }

  .app-header__title h1 {
    font-size: 15px;
  }
}
</style>