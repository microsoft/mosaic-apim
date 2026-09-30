import assert from 'node:assert/strict'
import { type ChildProcess, execFile, spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { request } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, before, test } from 'node:test'
import { e2eRoot } from '../src/paths.ts'

// A real daemon, on the example manifest, in an empty state directory. Every request here fails validation
// before a browser would launch, so no browser opens and nothing is signed in.
const stateDir = mkdtempSync(join(tmpdir(), 'mosaic-e2e-live-'))
const env = {
  ...process.env,
  MOSAIC_E2E_STATE_DIR: stateDir,
  MOSAIC_E2E_TARGETS: join(e2eRoot, 'targets.example.json'),
  MOSAIC_E2E_HEADLESS: '1',
}
let daemon: ChildProcess | undefined

before(async () => {
  const child = spawn(process.execPath, [join(e2eRoot, 'tools', 'live.ts')], { env, stdio: ['ignore', 'pipe', 'pipe'] })
  daemon = child
  let output = ''
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`The live driver didn't start: ${output}`)), 60_000)
    const read = (chunk: Buffer) => {
      output += chunk.toString()
      if (output.includes('MOSAIC live driver ready')) {
        clearTimeout(timer)
        resolve()
      }
    }
    child.stdout?.on('data', read)
    child.stderr?.on('data', read)
    child.on('exit', (code) => {
      clearTimeout(timer)
      reject(new Error(`The live driver exited with ${code}: ${output}`))
    })
  })
})

after(async () => {
  if (daemon && daemon.exitCode === null) {
    const exited = new Promise((resolve) => daemon?.once('exit', resolve))
    await drive('shutdown').catch(() => undefined)
    await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 15_000))])
    if (daemon.exitCode === null) daemon.kill()
  }
  rmSync(stateDir, { recursive: true, force: true })
})

function drive(...args: string[]): Promise<{ status: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    execFile(process.execPath, [join(e2eRoot, 'tools', 'drive.ts'), ...args], { env, encoding: 'utf8' }, (error, stdout, stderr) => {
      resolve({ status: error ? ((error as { code?: number }).code ?? 1) : 0, stdout, stderr })
    })
  })
}

function rpc(body: unknown): Promise<{ ok: boolean; error?: string }> {
  const session = JSON.parse(readFileSync(join(stateDir, 'live.json'), 'utf8')) as { port: number; token: string }
  const text = JSON.stringify(body)
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: '127.0.0.1',
        port: session.port,
        path: '/rpc',
        method: 'POST',
        headers: { authorization: ['Bearer', session.token].join(' '), 'content-type': 'application/json' },
      },
      (res) => {
        const chunks: Buffer[] = []
        res.on('data', (chunk: Buffer) => chunks.push(chunk))
        res.on('end', () => resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))))
      },
    )
    req.on('error', reject)
    req.end(text)
  })
}

const valid = ['--user-entitlement', 'ent_x', '--send-model-requests']

test('the daemon refuses verifier flags the harness does not allow', async () => {
  const cases: [string[], RegExp][] = [
    [['--user-entitlement', 'ent_x'], /Add --send-model-requests/],
    [['--api-base-url', 'https://evil.example', ...valid], /comes from the targets manifest/],
    [['--gateway-origin', 'https://evil.example', ...valid], /comes from the targets manifest/],
    [['--bogus', ...valid], /Unknown verifier flag "--bogus"/],
    [['--user-entitlement', 'https://evil.example/x', '--send-model-requests'], /needs a grant ID/],
  ]
  for (const [flags, message] of cases) {
    const { status, stderr } = await drive('verify', '--', ...flags)
    assert.equal(status, 2, flags.join(' '))
    assert.match(stderr, message, flags.join(' '))
  }
})

test('the daemon checks the people in a run before signing anyone in', async () => {
  const cases: [string[], RegExp][] = [
    [['--admin', 'admin', '--', ...valid], /--admin is only used with --application-entitlement/],
    [['--user', 'nobody', '--', ...valid], /Unknown persona "nobody"/],
    [['--stranger', 'outsider', '--', ...valid], /--stranger is only used with/],
    // Grants held by someone else need no --send-model-requests, so this run gets as far as its people.
    [['--stranger', 'outsider', '--', '--foreign-user-entitlement', 'ent_other'], /--stranger is only used with/],
    [['--user', 'admin', '--', '--application-entitlement', 'ent_app', '--send-model-requests'], /must be different people/],
  ]
  for (const [args, message] of cases) {
    const { status, stderr } = await drive('verify', ...args)
    assert.equal(status, 2, args.join(' '))
    assert.match(stderr, message, args.join(' '))
  }
})

test('the daemon never takes a MOSAIC API token, or any other variable, from the caller', async () => {
  for (const name of ['MOSAIC_SMOKE_USER_CONTROL_TOKEN', 'MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN', 'NODE_OPTIONS']) {
    const reply = await rpc({ action: 'verify', args: { verifierArgs: valid, env: { [name]: 'value' } } })
    assert.equal(reply.ok, false, name)
    assert.match(reply.error ?? '', /can't be passed to the verifier/, name)
  }
  const reply = await rpc({ action: 'verify', args: { verifierArgs: ['--send-model-requests', 1] } })
  assert.equal(reply.ok, false)
  assert.match(reply.error ?? '', /"verifierArgs" must be an array of strings/)
})

test('drive.ts does not forward MOSAIC API tokens from its own environment', async () => {
  const stale = { ...env, MOSAIC_SMOKE_USER_CONTROL_TOKEN: 'stale', MOSAIC_SMOKE_PAYLOAD: '{}' }
  const result = await new Promise<{ stderr: string }>((resolve) => {
    execFile(
      process.execPath,
      [join(e2eRoot, 'tools', 'drive.ts'), 'verify', '--admin', 'admin', '--', ...valid],
      { env: stale, encoding: 'utf8' },
      (_error, _stdout, stderr) => resolve({ stderr }),
    )
  })
  // The daemon checks forwarded variables first, so reaching the --admin error shows none were refused.
  assert.match(result.stderr, /--admin is only used with --application-entitlement/)
})

test('the daemon is still healthy after refusing runs', async () => {
  const { status, stdout } = await drive('status')
  assert.equal(status, 0)
  assert.match(stdout, /"environment": "mosaic-dev"/)
})
