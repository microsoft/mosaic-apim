import assert from 'node:assert/strict'
import { execFile, spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
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

// The test runner's own MOSAIC_SMOKE_* variables, if any, must not reach the runs below.
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.toUpperCase().startsWith('MOSAIC_SMOKE_')))

/** Runs drive.ts against a stub live driver that records each request and answers with `reply`. */
async function driveStub(reply: unknown, env: Record<string, string | undefined>, ...args: string[]) {
  const dir = mkdtempSync(join(tmpdir(), 'mosaic-e2e-drive-stub-'))
  const received: { authorization?: string; body: unknown }[] = []
  const server = createServer((request, response) => {
    const chunks: Buffer[] = []
    request.on('data', (chunk: Buffer) => chunks.push(chunk))
    request.on('end', () => {
      received.push({ authorization: request.headers.authorization, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) })
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify(reply))
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    const { port } = server.address() as AddressInfo
    writeFileSync(join(dir, 'live.json'), JSON.stringify({ pid: process.pid, port, token: 'stub-run-token' }))
    const run = await new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
      const options = { env: { ...env, MOSAIC_E2E_STATE_DIR: dir }, encoding: 'utf8' as const }
      execFile(process.execPath, [join(e2eRoot, 'tools', 'drive.ts'), ...args], options, (error, stdout, stderr) => {
        resolve({ status: error ? ((error as { code?: number }).code ?? 1) : 0, stdout, stderr })
      })
    })
    return { ...run, received }
  } finally {
    server.close()
    rmSync(dir, { recursive: true, force: true })
  }
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

test('verify sends everything after -- to the live driver, with its people and the allowlisted variables', async () => {
  const verifierArgs = [
    '--user-entitlement', 'ent_x',
    '--revocation-timeout=120',
    '--watch-revocation', 'ent_x',
    '--check-ungranted-user',
    '--send-model-requests',
  ]
  const personas = { user: 'member', stranger: 'outsider' }
  const reply = { ok: true, result: { exitCode: 0, timedOut: false, lines: ['PASS: one', 'PASS: two'], personas } }
  const env = {
    ...cleanEnv,
    MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET: 'short-lived-secret',
    MOSAIC_SMOKE_PAYLOAD: '  ',
    MOSAIC_SMOKE_USER_CONTROL_TOKEN: 'stale-control-token',
    MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN: 'stale-admin-token',
  }
  const run = await driveStub(reply, env, 'verify', '--user', 'member', '--stranger', 'outsider', '--', ...verifierArgs)
  assert.equal(run.status, 0, run.stderr)
  assert.equal(run.stderr, '')
  assert.equal(run.stdout, 'Personas: user member, stranger outsider\nPASS: one\nPASS: two\n')
  assert.deepEqual(run.received, [
    {
      authorization: ['Bearer', 'stub-run-token'].join(' '),
      body: {
        action: 'verify',
        args: { verifierArgs, user: 'member', stranger: 'outsider', env: { MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET: 'short-lived-secret' } },
      },
    },
  ])
})

test('verify prints what the verifier printed and exits with its exit code', async () => {
  const valid = ['--user-entitlement', 'ent_x', '--send-model-requests']
  const result = (exitCode: number | null, timedOut: boolean) => ({
    ok: true,
    result: { exitCode, timedOut, lines: ['FAIL: User grant 1 connection details: unexpected HTTP 404'], personas: { user: 'member' } },
  })
  const failed = await driveStub(result(130, false), cleanEnv, 'verify', '--', ...valid)
  assert.equal(failed.status, 130)
  assert.equal(failed.stdout, 'Personas: user member\nFAIL: User grant 1 connection details: unexpected HTTP 404\n')
  assert.equal(failed.stderr, 'The verifier exited with code 130.\n')

  const timedOut = await driveStub(result(null, true), cleanEnv, 'verify', '--', ...valid)
  assert.equal(timedOut.status, 1)
  assert.equal(timedOut.stderr, 'The verifier timed out.\n')

  const refused = await driveStub({ ok: false, error: 'Unknown persona "nobody"' }, cleanEnv, 'verify', '--user', 'nobody', '--', ...valid)
  assert.equal(refused.status, 2)
  assert.equal(refused.stdout, '')
  assert.match(refused.stderr, /^Error: Unknown persona "nobody"/)
})

test('verify flags must follow --', () => {
  const { status, stderr } = drive('verify', '--user-entitlement', 'ent_x', '--send-model-requests')
  assert.equal(status, 2)
  assert.match(stderr, /Unknown option '--user-entitlement'/)
})

test('verify-mcp sends everything after -- to the live driver, with only the MCP verifier variables', async () => {
  const verifierArgs = ['--user-entitlement', '@target:mcp.grants.tools-user.id', '--check-missing-scope', '--prove-call-limit=ent_x']
  const reply = { ok: true, result: { exitCode: 0, timedOut: false, lines: ['PASS: one', 'INFO: two'], personas: { user: 'member', admin: 'boss' } } }
  const env = {
    ...cleanEnv,
    MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN: 'mcp-user-token',
    MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'model-token',
    MOSAIC_SMOKE_MCP_UNGRANTED_USER_RUNTIME_TOKEN: ' ',
    // The model verifier's own variables, and the MOSAIC API tokens, aren't passed on.
    MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN: 'agent-token',
    MOSAIC_SMOKE_PAYLOAD: '{}',
    MOSAIC_SMOKE_USER_CONTROL_TOKEN: 'stale-control-token',
  }
  const run = await driveStub(reply, env, 'verify-mcp', '--user', 'member', '--admin', 'boss', '--', ...verifierArgs)
  assert.equal(run.status, 0, run.stderr)
  assert.equal(run.stderr, '')
  assert.equal(run.stdout, 'Personas: user member, admin boss\nPASS: one\nINFO: two\n')
  assert.deepEqual(run.received, [
    {
      authorization: ['Bearer', 'stub-run-token'].join(' '),
      body: {
        action: 'verify-mcp',
        args: {
          verifierArgs,
          user: 'member',
          admin: 'boss',
          env: { MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN: 'mcp-user-token', MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'model-token' },
        },
      },
    },
  ])

  const failed = await driveStub(
    { ok: true, result: { exitCode: 1, timedOut: false, lines: ['FAIL: User grant 1 (M-tools) rejecting an anonymous call: unexpected HTTP 200'], personas: { user: 'member' } } },
    cleanEnv,
    'verify-mcp',
    '--',
    '--user-entitlement',
    'ent_x',
  )
  assert.equal(failed.status, 1)
  assert.equal(failed.stderr, 'The verifier exited with code 1.\n')
})

test('verify-mcp takes its people as options and its flags after --', () => {
  assert.match(drive('member', 'verify-mcp', '--', '--user-entitlement', 'ent_x').stderr, /"verify-mcp" takes no persona before it/)
  const flags = drive('verify-mcp', '--user-entitlement', 'ent_x')
  assert.equal(flags.status, 2)
  assert.match(flags.stderr, /Unknown option '--user-entitlement'/)
  // The people options are accepted, so the run gets as far as looking for the live driver.
  const people = drive('verify-mcp', '--user', 'member', '--admin', 'boss', '--stranger', 'nobody', '--', '--user-entitlement', 'ent_x')
  assert.equal(people.status, 2)
  assert.match(people.stderr, /live driver is not running/)
})

test('verify takes its people as options, not a persona', () => {
  assert.match(drive('member', 'verify', '--', '--send-model-requests').stderr, /"verify" takes no persona before it/)
  for (const option of ['--user', '--admin', '--stranger']) {
    const { status, stderr } = drive('admin', 'click', 'role:button:Revoke', option, 'member')
    assert.equal(status, 2)
    assert.match(stderr, /only apply to "verify"/)
  }
})
