import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests', timeout: 30000, workers: 1,
  use: { baseURL: 'http://127.0.0.1:5173', viewport: { width: 1440, height: 1080 }, headless: true },
})
