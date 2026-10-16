import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'

// `vitest/config` re-exports defineConfig with the `test` key typed.
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    // Fail fast on a busy port instead of silently moving to 5174, which would
    // break the URL the launcher prints and the browser opens.
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // Swagger UI lives on the backend; forwarding it means "/docs" works
      // from the frontend origin too (the header link defaults to the
      // backend origin, this covers anything else that links relatively).
      '/docs': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/openapi.json': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks: {
          'element-plus': ['element-plus', '@element-plus/icons-vue'],
          vendor: ['vue', 'vue-router', 'pinia', 'pinia-plugin-persistedstate', 'axios'],
        },
      },
    },
  },
  test: {
    // `node` stays the default because most specs are pure logic and jsdom
    // costs startup time for nothing. Component specs opt in per file with a
    // `// @vitest-environment jsdom` docblock, so only those pay for it.
    environment: 'node',
    include: ['src/**/*.spec.ts'],
  },
})