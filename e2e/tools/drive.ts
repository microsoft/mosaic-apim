import { existsSync, readFileSync } from 'node:fs'
import { request } from 'node:http'
import { parseArgs } from 'node:util'
import { liveSessionFile } from '../src/paths.ts'
import { mcpForwardedVariables } from '../src/verify-mcp.ts'
import { forwardedEnvironment, forwardedVariables } from '../src/verify.ts'

const usage = `Usage: node tools/drive.ts <persona> <action> [arguments] [options]
       node tools/drive.ts status | shutdown | dialogs <accept|dismiss>
       node tools/drive.ts verify [--user <persona>] [--admin <persona>] [--stranger <persona>] -- <verifier flags>
       node tools/drive.ts verify-mcp [--user <persona>] [--admin <persona>] [--stranger <persona>] -- <verifier flags>

Actions
  open <web|portal> [path]          Launch the persona's browser (if needed) and navigate
  signin <web|portal> [path]        Sign in, waiting for the human to finish MFA if needed
  snapshot [target]                 Redacted accessibility snapshot of the page or an element
  click|hover|clear <target>        Interact with an element
  fill <target> <value>             Type a value; "@target:<path>" reads it from the manifest
  choose <combobox> <option>        Open a Fluent combobox or dropdown and pick an option
  select <target> <value>           Pick an option in a native <select>
  check|uncheck <target>            Set a checkbox, switch, or radio
  press <key> [--target <target>]   Press a key on the page or an element
  wait <target> [--state <state>]   Wait for visible (default), hidden, attached, or detached
  wait-url <fragment>               Wait until the URL contains the fragment
  text|count <target>               Read redacted text, or count matches
  url | pages | tab <index>         Inspect or switch tabs
  reload | back | logs [--clear] | shot [name] [--full] | close

Targets: role:<role>[:<name>] | label:<text> | text:<text> | placeholder:<text> | testid:<id> | css:<selector>
         Names and text may be /regular expressions/flags.
Options: --in <target>  --row <text>  --has <text>  --nth <n|last>  --exact  --timeout <ms>  --max <chars>  --state <state>
         --nth counts from 0. For the last match use "--nth last" or "--nth=-1".

Verify
  verify -- <verifier flags>        Run scripts/verify_model_access.py against the manifest's API and gateway. The
                                    driver signs the personas in, passes their MOSAIC API tokens to it, and enters
                                    its device codes in the right browser. Everything after "--" goes to the
                                    verifier, for example: verify -- --user-entitlement <id> --send-model-requests
  --user <persona>                  Holds the user grants (default: the manifest's roles.user)
  --admin <persona>                 Hands off application keys, with --application-entitlement, confirms that
                                    --foreign-user-entitlement grants are someone else's, and reads the connection
                                    details of --agent-entitlement and --group-entitlement grants (default: roles.admin)
  --stranger <persona>              Holds no grant, for --check-ungranted-user with device-code sign-in
                                    (default: roles.outsider, then roles.noRole)
  Passes ${forwardedVariables.join(', ')}
  from this shell to the verifier when they're set.

Verify MCP access
  verify-mcp -- <verifier flags>    Run scripts/verify_mcp_access.py, Phase 11's MCP client, the same way. A grant ID
                                    can be a manifest reference, such as @target:mcp.grants.tools-user.id, for
                                    example: verify-mcp -- --user-entitlement @target:mcp.grants.tools-user.id
  --user <persona>                  Holds the user and on-behalf grants (default: roles.user)
  --admin <persona>                 Reads application grants' connection details, what a server's last apply compiled
                                    for --prove-pooled-quota, and the --on-behalf-entitlement server's model caller
                                    (default: roles.admin)
  --stranger <persona>              Holds no grant on these MCP servers, for --check-ungranted-user with device-code
                                    sign-in (default: roles.outsider, then roles.noRole)
  Passes ${mcpForwardedVariables.join(', ')}
  from this shell to the verifier when they're set.`

function fail(message: string): never {
  process.stderr.write(`${message}\n`)
  process.exit(2)
}

function parseCommandLine() {
  try {
    return parseArgs({
      allowPositionals: true,
      options: {
        in: { type: 'string' },
        row: { type: 'string' },
        has: { type: 'string' },
        nth: { type: 'string' },
        exact: { type: 'boolean' },
        timeout: { type: 'string' },
        max: { type: 'string' },
        state: { type: 'string' },
        target: { type: 'string' },
        full: { type: 'boolean' },
        clear: { type: 'boolean' },
        user: { type: 'string' },
        admin: { type: 'string' },
        stranger: { type: 'string' },
        help: { type: 'boolean', short: 'h' },
      },
    })
  } catch (error) {
    fail(`${(error as Error).message}\n\n${usage}`)
  }
}

/** Parses a whole number. Number('last') is NaN, which JSON sends as null, so reject anything else here. */
function wholeNumber(name: string, raw: string | undefined, min: number): number | undefined {
  if (raw === undefined) return undefined
  if (!/^-?\d+$/.test(raw)) fail(`${name} must be a whole number, not "${raw}"`)
  const value = Number(raw)
  if (value < min) fail(`${name} must be ${min} or more`)
  return value
}

const { values, positionals } = parseCommandLine()

if (values.help || positionals.length === 0) {
  process.stdout.write(`${usage}\n`)
  process.exit(values.help ? 0 : 2)
}

const globalActions = new Set(['status', 'shutdown', 'dialogs', 'verify', 'verify-mcp'])
const verifyActions = new Set(['verify', 'verify-mcp'])
const [first, ...rest] = positionals
const personaKey = globalActions.has(first) ? undefined : first
const [rawAction, ...params] = personaKey ? rest : [first, ...rest]
if (!rawAction) fail(usage)
if (!verifyActions.has(rawAction) && [values.user, values.admin, values.stranger].some((value) => value !== undefined)) {
  fail('--user, --admin and --stranger only apply to "verify" and "verify-mcp"')
}

const common = {
  within: values.in,
  row: values.row,
  has: values.has,
  nth: values.nth === 'last' ? -1 : wholeNumber('--nth', values.nth, -1),
  exact: values.exact === true ? true : undefined,
  timeout: wholeNumber('--timeout', values.timeout, 0),
}

function need(index: number, name: string): string {
  const value = params[index]
  if (value === undefined) fail(`"${rawAction}" needs <${name}>\n\n${usage}`)
  return value
}

let action = rawAction
let args: Record<string, unknown>
switch (rawAction) {
  case 'status':
  case 'shutdown':
  case 'url':
  case 'pages':
  case 'reload':
  case 'back':
  case 'close':
    args = {}
    break
  case 'dialogs':
    args = { mode: need(0, 'accept|dismiss') }
    break
  case 'verify':
  case 'verify-mcp':
    if (personaKey) fail(`"${rawAction}" takes no persona before it. Choose people with --user, --admin and --stranger.`)
    args = {
      verifierArgs: params,
      user: values.user,
      admin: values.admin,
      stranger: values.stranger,
      env: forwardedEnvironment(process.env, rawAction === 'verify' ? forwardedVariables : mcpForwardedVariables),
    }
    break
  case 'open':
  case 'signin':
    args = { app: need(0, 'web|portal'), path: params[1], timeout: common.timeout }
    break
  case 'snapshot':
    args = { ...common, target: params[0], max: wholeNumber('--max', values.max, 1) }
    break
  case 'click':
  case 'hover':
  case 'clear':
  case 'text':
  case 'count':
    args = { ...common, target: need(0, 'target') }
    break
  case 'fill':
    args = { ...common, target: need(0, 'target'), value: need(1, 'value') }
    break
  case 'choose':
    args = { ...common, target: need(0, 'combobox'), option: need(1, 'option') }
    break
  case 'select':
    args = { ...common, target: need(0, 'target'), value: need(1, 'value') }
    break
  case 'check':
  case 'uncheck':
    action = 'check'
    args = { ...common, target: need(0, 'target'), checked: rawAction === 'check' }
    break
  case 'press':
    args = { ...common, key: need(0, 'key'), target: values.target }
    break
  case 'wait':
    args = { ...common, target: need(0, 'target'), state: values.state }
    break
  case 'wait-url':
    action = 'waitUrl'
    args = { contains: need(0, 'fragment'), timeout: common.timeout }
    break
  case 'tab':
    args = { index: wholeNumber('<index>', need(0, 'index'), 0) }
    break
  case 'logs':
    args = { clear: values.clear === true }
    break
  case 'shot':
    args = { name: params[0], full: values.full === true }
    break
  default:
    fail(`Unknown action "${rawAction}"\n\n${usage}`)
}
if (!globalActions.has(action) && !personaKey) fail(`"${rawAction}" needs a persona`)

const sessionFile = liveSessionFile()
if (!existsSync(sessionFile)) fail('The live driver is not running. Start it with "npm run live".')
const session = JSON.parse(readFileSync(sessionFile, 'utf8')) as { port: number; token: string }

/**
 * Uses node:http rather than fetch: fetch gives up after 300 seconds without response headers,
 * and "signin" deliberately waits much longer than that for a person to finish MFA.
 */
function rpc(body: string, timeoutMs: number): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: '127.0.0.1',
        port: session.port,
        path: '/rpc',
        method: 'POST',
        headers: {
          authorization: `Bearer ${session.token}`,
          'content-type': 'application/json',
          'content-length': Buffer.byteLength(body),
        },
      },
      (res) => {
        const chunks: Buffer[] = []
        res.on('data', (chunk: Buffer) => chunks.push(chunk))
        res.on('end', () => resolve({ status: res.statusCode ?? 0, text: Buffer.concat(chunks).toString('utf8') }))
        res.on('error', reject)
      },
    )
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`no response after ${Math.round(timeoutMs / 1000)} seconds`)))
    req.on('error', reject)
    req.end(body)
  })
}

const waitMs =
  typeof args.timeout === 'number' ? args.timeout : verifyActions.has(action) ? 3 * 3_600_000 : action === 'signin' ? 600_000 : 30_000
let reply: { status: number; text: string }
try {
  reply = await rpc(JSON.stringify({ action, persona: personaKey, args }), waitMs + 60_000)
} catch (error) {
  const cause = (error as Error & { code?: string }).code
  fail(`Could not reach the live driver: ${(error as Error).message}${cause ? ` (${cause})` : ''}`)
}
let payload: { ok: boolean; result?: unknown; error?: string }
try {
  payload = JSON.parse(reply.text) as typeof payload
} catch {
  fail(`The live driver returned HTTP ${reply.status} without a JSON body`)
}
if (!payload.ok) fail(`Error: ${payload.error ?? `HTTP ${reply.status}`}`)
if (verifyActions.has(action)) {
  const run = payload.result as { exitCode: number | null; timedOut: boolean; lines: string[]; personas: Record<string, string> }
  const people = Object.entries(run.personas).map(([part, key]) => `${part} ${key}`)
  process.stdout.write(`Personas: ${people.join(', ')}\n${run.lines.join('\n')}\n`)
  if (run.exitCode !== 0) {
    process.stderr.write(run.timedOut ? 'The verifier timed out.\n' : `The verifier exited with code ${run.exitCode ?? 'none'}.\n`)
  }
  process.exitCode = run.exitCode ?? 1
} else {
  const output = typeof payload.result === 'string' ? payload.result : JSON.stringify(payload.result, null, 2)
  process.stdout.write(`${output}\n`)
}
