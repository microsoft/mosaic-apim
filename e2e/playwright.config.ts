import { defineConfig } from '@playwright/test'

/**
 * Live suite against a deployed MOSAIC environment. Journeys share tenant state, so they run
 * serially in one worker. Traces and videos stay off because they would capture tokens and keys.
 */
export default defineConfig({
  testDir: './specs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  outputDir: './test-results',
  reporter: [['list'], ['html', { open: 'never', outputFolder: './playwright-report' }]],
  use: {
    actionTimeout: 20_000,
    navigationTimeout: 45_000,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
})
