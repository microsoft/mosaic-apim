import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const e2eRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export function stateDir(): string {
  const override = process.env.MOSAIC_E2E_STATE_DIR
  if (override) return resolve(override)
  const base = process.env.LOCALAPPDATA ?? join(homedir(), '.local', 'state')
  return join(base, 'mosaic-e2e')
}

export function profilesDir(): string {
  return join(stateDir(), 'profiles')
}

export function liveSessionFile(): string {
  return join(stateDir(), 'live.json')
}

export function artifactsDir(): string {
  const override = process.env.MOSAIC_E2E_ARTIFACTS_DIR
  return override ? resolve(override) : join(stateDir(), 'artifacts')
}

export function targetsFile(): string {
  const override = process.env.MOSAIC_E2E_TARGETS
  return override ? resolve(override) : join(e2eRoot, 'targets.local.json')
}

export function ensureDir(path: string): string {
  mkdirSync(path, { recursive: true })
  return path
}
