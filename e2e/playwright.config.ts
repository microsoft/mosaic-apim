import { defineConfig, type ReporterDescription } from '@playwright/test'
import { flags } from './src/config.ts'

// Playwright's own failure snapshot of the page isn't redacted. The persona fixture attaches a redacted one.
process.env.PLAYWRIGHT_NO_COPY_PROMPT ??= '1'

// The HTML report records step titles and step errors as they happen, before the harness can redact them.
const reporter: ReporterDescription[] = [['list']]
if (flags.htmlReport()) reporter.push(['html', { open: 'never', outputFolder: './playwright-report' }])

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
  reporter,
  use: {
    actionTimeout: 20_000,
    navigationTimeout: 45_000,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
})
