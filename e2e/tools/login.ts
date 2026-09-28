import { chromium } from '@playwright/test'
import { existsSync, readFileSync } from 'node:fs'
import { parseArgs } from 'node:util'
import { type AppName, loadTargets, persona } from '../src/config.ts'
import { liveSessionFile } from '../src/paths.ts'
import { ensureSignedIn, launchPersona } from '../src/personas.ts'

/**
 * Opens each persona's persistent browser profile so a human can complete Microsoft Entra sign-in
 * (password, MFA, "Stay signed in?"). Later suite runs reuse the profile without prompting.
 */

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    app: { type: 'string', default: 'both' },
    timeout: { type: 'string', default: '600000' },
  },
})

if (positionals.length === 0) {
  process.stderr.write('Usage: node tools/login.ts <persona...> [--app web|portal|both] [--timeout ms]\n')
  process.exit(2)
}

if (existsSync(liveSessionFile())) {
  const { pid } = JSON.parse(readFileSync(liveSessionFile(), 'utf8')) as { pid?: number }
  let alive = false
  try {
    if (pid) {
      process.kill(pid, 0)
      alive = true
    }
  } catch {
    alive = false
  }
  if (alive) {
    process.stderr.write('The live driver owns the browser profiles. Use "npm run drive -- <persona> signin <app>" instead.\n')
    process.exit(1)
  }
}

const targets = loadTargets()
const apps: AppName[] = values.app === 'both' ? ['web', 'portal'] : [values.app as AppName]
if (apps.some((app) => app !== 'web' && app !== 'portal')) {
  process.stderr.write('--app must be web, portal, or both\n')
  process.exit(2)
}

let failures = 0
for (const personaKey of positionals) {
  const { upn } = persona(targets, personaKey)
  const context = await launchPersona(chromium, targets, personaKey, { headless: false })
  try {
    const page = context.pages()[0] ?? (await context.newPage())
    for (const app of apps) {
      process.stdout.write(`${personaKey}: signing in to ${app} as ${upn}…\n`)
      await ensureSignedIn(page, targets, personaKey, app, { interactive: true, timeoutMs: Number(values.timeout) })
      process.stdout.write(`${personaKey}: ${app} ready\n`)
    }
  } catch (error) {
    failures += 1
    process.stderr.write(`${personaKey}: ${(error as Error).message}\n`)
  } finally {
    await context.close()
  }
}
process.exit(failures === 0 ? 0 : 1)
