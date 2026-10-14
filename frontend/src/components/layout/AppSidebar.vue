<script setup lang="ts">
/**
 * Fixed left navigation rail (220px, dark) with router links.
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { Clock, Microphone, Setting } from '@element-plus/icons-vue'

interface NavItem {
  path: string
  title: string
  icon: typeof Microphone
}

const route = useRoute()

const items: NavItem[] = [
  { path: '/generate', title: '语音生成', icon: Microphone },
  { path: '/history', title: '历史记录', icon: Clock },
  { path: '/settings', title: '系统设置', icon: Setting },
]

/** Currently active menu entry. */
const activeIndex = computed<string>(() => {
  const match = items.find((item) => route.path === item.path || route.path.startsWith(`${item.path}/`))
  return match ? match.path : route.path
})

const appTitle = import.meta.env.VITE_APP_TITLE || 'Text2Voice Agent'
</script>

<template>
  <aside class="app-sidebar">
    <div class="app-sidebar__brand">
      <div class="app-sidebar__logo" aria-hidden="true">
        <el-icon :size="18"><Microphone /></el-icon>
      </div>
      <div class="app-sidebar__brand-text">
        <span class="app-sidebar__brand-name">{{ appTitle }}</span>
        <span class="app-sidebar__brand-tag">LLM → TTS</span>
      </div>
    </div>

    <el-menu
      class="app-sidebar__menu"
      :default-active="activeIndex"
      router
      background-color="transparent"
      text-color="var(--t2v-text-secondary)"
      active-text-color="var(--t2v-color-primary)"
    >
      <el-menu-item v-for="item in items" :key="item.path" :index="item.path">
        <el-icon><component :is="item.icon" /></el-icon>
        <span>{{ item.title }}</span>
      </el-menu-item>
    </el-menu>

    <div class="app-sidebar__footer">
      <span class="app-sidebar__footer-text">API Key 仅保存在浏览器内存中</span>
    </div>
  </aside>
</template>

<style scoped>
.app-sidebar {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: var(--t2v-sidebar-width);
  display: flex;
  flex-direction: column;
  /* macOS window chrome: translucent, blurred, hairline separator */
  background: var(--t2v-bg-sidebar);
  backdrop-filter: var(--t2v-backdrop-blur);
  -webkit-backdrop-filter: var(--t2v-backdrop-blur);
  border-right: 1px solid var(--t2v-border-color);
  color: var(--t2v-text-primary);
  z-index: 20;
  overflow: hidden;
}

.app-sidebar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--t2v-header-height);
  padding: 0 16px;
  flex: 0 0 auto;
  border-bottom: 1px solid var(--t2v-border-color);
}

.app-sidebar__logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  /* iOS app-icon squircle */
  border-radius: 9px;
  background: linear-gradient(180deg, var(--t2v-color-primary-hover), var(--t2v-color-primary));
  color: #fff;
  flex: 0 0 auto;
  box-shadow: var(--t2v-shadow-sm);
}

.app-sidebar__brand-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.app-sidebar__brand-name {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--t2v-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.app-sidebar__brand-tag {
  font-size: 11px;
  color: var(--t2v-text-muted);
  letter-spacing: 0.04em;
}

.app-sidebar__menu {
  flex: 1 1 auto;
  border-right: none;
  padding: 10px 10px;
  overflow-y: auto;
}

.app-sidebar__menu :deep(.el-menu-item) {
  height: 40px;
  line-height: 40px;
  border-radius: 8px;
  margin-bottom: 2px;
  font-size: 14px;
  font-weight: 500;
}

.app-sidebar__menu :deep(.el-menu-item:hover) {
  background-color: var(--t2v-bg-sidebar-hover) !important;
}

.app-sidebar__menu :deep(.el-menu-item.is-active) {
  background-color: var(--t2v-bg-sidebar-active) !important;
  font-weight: 600;
}

.app-sidebar__footer {
  flex: 0 0 auto;
  padding: 12px 16px;
  border-top: 1px solid var(--t2v-border-color);
}

.app-sidebar__footer-text {
  font-size: 11px;
  line-height: 1.5;
  color: var(--t2v-text-muted);
}

@media (max-width: 1024px) {
  .app-sidebar__brand-name {
    font-size: 13px;
  }
}
</style>