<script setup lang="ts">
/**
 * Application shell: fixed sidebar + sticky header + scrolling content area.
 *
 * On first load it fetches the provider catalogue and the server settings, then
 * applies the documented defaults to the generation config store.
 */
import { onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import AppHeader from '@/components/layout/AppHeader.vue'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'

const configStore = useConfigStore()
const settingsStore = useSettingsStore()

onMounted(async () => {
  // Restore the saved configuration FIRST, from localStorage alone. Doing this
  // before the network round-trips means the user's keys and selections come
  // back even if the backend is slow or temporarily unreachable — and it is
  // exactly what `autoSaveCredentials` was meant to provide.
  if (settingsStore.autoSaveCredentials) configStore.restoreCredentials()

  // Server settings: they seed the generation defaults and the timeout.
  const settings = await settingsStore.load()
  if (settings) {
    configStore.applyDefaults(settings)
    // The server value is authoritative once known; re-restore if the user has
    // saving enabled but localStorage had not told us yet.
    if (settingsStore.autoSaveCredentials) configStore.restoreCredentials()
  }

  const providers = await configStore.loadProviders()
  if (!providers) {
    ElMessage.error('无法加载服务商列表，请确认后端服务已启动。')
    return
  }
  // Re-apply defaults now that the catalogue exists (guards against stale ids).
  if (settings) configStore.applyDefaults(settings)
  configStore.ensureSelections()
})
</script>

<template>
  <div class="app-shell">
    <AppSidebar />
    <div class="app-shell__main">
      <AppHeader />
      <main class="app-shell__content">
        <router-view v-slot="{ Component }">
          <transition name="t2v-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  background: var(--t2v-bg-page);
}

.app-shell__main {
  margin-left: var(--t2v-sidebar-width);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.app-shell__content {
  flex: 1 1 auto;
  overflow-y: auto;
  min-height: 0;
}

.t2v-fade-enter-active,
.t2v-fade-leave-active {
  transition: opacity 0.18s ease;
}

.t2v-fade-enter-from,
.t2v-fade-leave-to {
  opacity: 0;
}
</style>