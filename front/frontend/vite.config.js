import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'
import { fileURLToPath, URL } from 'node:url'

const envDir = fileURLToPath(new URL('../..', import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, envDir, 'VITE_')
  const apiBaseUrl = String(env.VITE_API_BASE_URL || '').trim()
  if (!apiBaseUrl) {
    throw new Error('VITE_API_BASE_URL is required in the root .env file')
  }
  return {
    envDir,
    plugins: [vue()],
    server: {
      proxy: {
        '/api': apiBaseUrl,
      },
    },
  }
})
