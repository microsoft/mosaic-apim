import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, test } from 'node:test'
import { e2eRoot } from '../src/paths.ts'

// An empty state directory has no live.json, so these runs can never reach a running live driver.
const stateDir = mkdtempSync(join(tmpdir(), 'mosaic-e2e-drive-'))
after(() => rmSync(stateDir, { recursive: true, force: true }))

function drive(...args: string[]) {
  const result = spawnSync(process.execPath, [join(e2eRoot, 'tools', 'drive.ts'), ...args], {
    encoding: 'utf8',
    env: { ...process.env, MOSAIC_E2E_STATE_DIR: stateDir },
  })
  return { status: result.status, stderr: result.stderr }
}

test('rejects --nth values that are not whole numbers', () => {
  const { status, stderr } = drive('admin', 'click', 'role:button:Revoke', '--nth', 'first')
  assert.equal(status, 2)
  assert.match(stderr, /--nth must be a whole number, not "first"/)
})

test('explains how to pass a negative --nth without a stack trace', () => {
  const { status, stderr } = drive('admin', 'click', 'role:button:Revoke', '--nth', '-1')
  assert.equal(status, 2)
  assert.match(stderr, /--nth=-XYZ/)
  assert.doesNotMatch(stderr, /\n\s+at /)
})

test('accepts --nth last and --nth=-1', () => {
  for (const nth of [['--nth', 'last'], ['--nth=-1']]) {
    const { status, stderr } = drive('admin', 'click', 'role:button:Revoke', ...nth)
    assert.equal(status, 2)
    assert.match(stderr, /live driver is not running/)
  }
})

test('rejects non-numeric tab indexes, timeouts and limits', () => {
  assert.match(drive('admin', 'tab', 'abc').stderr, /<index> must be a whole number/)
  assert.match(drive('admin', 'wait', 'role:dialog', '--timeout', '5s').stderr, /--timeout must be a whole number/)
  assert.match(drive('admin', 'snapshot', '--max', '0').stderr, /--max must be 1 or more/)
})
