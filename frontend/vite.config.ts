import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const target = loadEnv(mode, '.', 'KNOWFLOW_').KNOWFLOW_API_PROXY || 'http://127.0.0.1:8000'
  return { plugins: [vue()], server: { proxy: { '/api': target, '/health': target } } }
})
