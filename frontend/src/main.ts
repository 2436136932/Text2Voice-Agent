import { createApp } from 'vue'
import { createPinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import 'element-plus/dist/index.css'
// Dark palette must load BEFORE the app styles so [data-theme] overrides win.
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/main.css'

// Applies the persisted/OS theme to <html> before first paint.
import '@/composables/theme'

import App from '@/App.vue'
import router from '@/router'
import { useConfigStore } from '@/stores/config'
import { useSettingsStore } from '@/stores/settings'

const app = createApp(App)

const pinia = createPinia()
pinia.use(piniaPluginPersistedstate)

app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: zhCn })

// Register every Element Plus icon globally so views can use `<el-icon><Microphone /></el-icon>`.
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

/**
 * Persist the provider configuration whenever it changes.
 *
 * The user's expectation is "fill it in once and it stays filled" — requiring a
 * separate save action (or a trip to the settings page) meant a key typed on
 * the generate page was silently lost on reload. This watcher closes that gap:
 * any change to a credential, provider selection OR per-generation parameter
 * is written to localStorage immediately (debounced), but only while the user
 * has opted in via 自动保存配置. `autoSaveCredentials` itself is persisted by
 * the settings store.
 */
const configStore = useConfigStore()
const settingsStore = useSettingsStore()
const PERSISTED_FIELDS = [
  'llmApiKey',
  'ttsApiKey',
  'llmBaseUrl',
  'ttsBaseUrl',
  'llmProviderId',
  'llmModel',
  'ttsProviderId',
  'ttsVoice',
  'ttsModel',
  'ttsSpeechPath',
  // Per-generation parameters: a reload resetting these was a daily annoyance.
  'temperature',
  'maxTokens',
  'systemPrompt',
  'speed',
  'format',
  'mode',
  'text',
] as const

let saveTimer: ReturnType<typeof setTimeout> | null = null

configStore.$subscribe(
  (_mutation, state) => {
    if (!settingsStore.autoSaveCredentials) return
    // The persisted-plugin mirror loads asynchronously; skip until it is known.
    if (saveTimer !== null) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      const changed = PERSISTED_FIELDS.some((field) => state[field] !== undefined)
      if (changed) configStore.persistCredentials()
    }, 400)
  },
  { detached: true },
)

app.mount('#app')