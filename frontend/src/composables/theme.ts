/**
 * Light/dark theme state, persisted to localStorage and applied to <html>
 * as `data-theme`. Element Plus's dark CSS is imported in main.ts, so the
 * EP variables follow the same attribute.
 *
 * Defaults to the OS preference on first visit (`prefers-color-scheme`),
 * then sticks to whatever the user picked.
 */
import { ref, watch } from 'vue'

const STORAGE_KEY = 't2v-theme'

export type Theme = 'light' | 'dark'

function initialTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

const theme = ref<Theme>(initialTheme())

function apply(value: Theme): void {
  // data-theme drives our token overrides; the `dark` class drives Element
  // Plus's own dark palette (element-plus/theme-chalk/dark/css-vars.css keys
  // off html.dark). Both must be set or EP controls keep light colors —
  // white-on-white segmented buttons in dark mode.
  document.documentElement.dataset.theme = value
  document.documentElement.classList.toggle('dark', value === 'dark')
}

// Apply before any component renders so there is no flash of the wrong theme.
apply(theme.value)

// Follow the OS if the user has not made an explicit choice.
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (event) => {
  if (localStorage.getItem(STORAGE_KEY) === null) {
    theme.value = event.matches ? 'dark' : 'light'
  }
})

watch(theme, (value) => {
  // The fade class makes the palette swap feel like macOS appearance switching
  // instead of a hard cut. One frame is enough — remove on the next tick.
  document.documentElement.classList.add('theme-switching')
  apply(value)
  window.setTimeout(() => document.documentElement.classList.remove('theme-switching'), 400)
})

export function useTheme() {
  function toggle(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    localStorage.setItem(STORAGE_KEY, theme.value)
  }

  function set(value: Theme): void {
    theme.value = value
    localStorage.setItem(STORAGE_KEY, value)
  }

  return { theme, toggle, set }
}
