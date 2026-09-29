import { runtimeStatusLabel } from './entitlement-format'
import type {
  ConnectionOperation,
  EntitlementRuntime,
  KeyRevealResult,
  KeySlot,
  ModelConnection,
} from './types'

/** How long a revealed key stays on screen before it is masked again. */
export const KEY_VISIBLE_MS = 60_000

export interface Problem {
  title: string
  message: string
}

export class UnexpectedCredentialError extends Error {
  constructor() {
    super('The service returned unexpected credential metadata. No key was displayed.')
    this.name = 'UnexpectedCredentialError'
  }
}

export function operationUrl(endpoint: string, path: string) {
  return `${endpoint.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`
}

const runtimeExplanations: Record<EntitlementRuntime['status'], string> = {
  applied: 'APIM enforces this grant. Changes can take a few minutes to reach the gateway.',
  pending:
    'Changes to this grant are saved but not applied to APIM yet. Keys and tokens may not work until an administrator applies them.',
  applying: 'MOSAIC is applying this grant to APIM. Check again in a few minutes.',
  revocationPending: 'This grant is being removed from APIM. Calls can stop working at any time.',
  revoked: 'APIM no longer accepts credentials for this grant.',
  failed: 'The last attempt to apply this grant to APIM failed. Ask your administrator to retry it.',
  unknown:
    'MOSAIC cannot confirm what APIM enforces for this grant. Ask your administrator to reconcile it.',
}

export function describeConnectionRuntime(runtime: EntitlementRuntime | null | undefined) {
  if (!runtime) {
    return {
      label: 'Not set up in APIM',
      explanation:
        'An administrator has not applied governed access for this model yet, so APIM does not accept credentials for this grant.',
      applied: false,
    }
  }
  return {
    label: runtimeStatusLabel(runtime.status),
    explanation: runtimeExplanations[runtime.status],
    applied: runtime.status === 'applied',
  }
}

export type KeyAvailability = { available: true } | { available: false; reason: string }

export const GROUP_GRANT_ENTRA_ONLY =
  "Access granted to a group uses Microsoft Entra sign-in only, so there's no key. Sign in with your own account to get a token."

export function keyAvailability(connection: ModelConnection): KeyAvailability {
  if (connection.keysAvailable === false) {
    return {
      available: false,
      reason: GROUP_GRANT_ENTRA_ONLY,
    }
  }
  const runtime = connection.runtime
  const methods = connection.appliedMethods
  if (!runtime || !methods) {
    return {
      available: false,
      reason: 'Keys become available after an administrator applies governed access for this model.',
    }
  }
  if (runtime.status !== 'applied') {
    return {
      available: false,
      reason: `Keys are available only while this grant is applied to APIM. Current status: ${runtimeStatusLabel(runtime.status)}.`,
    }
  }
  if (!methods.keysEnabled || runtime.appliedMethods?.keysEnabled === false) {
    return {
      available: false,
      reason: methods.entraEnabled
        ? 'Subscription keys are turned off for this model. Use a Microsoft Entra ID token instead.'
        : 'Subscription keys are turned off for this model, and APIM currently denies every call.',
    }
  }
  return { available: true }
}

/** Rejects a response that does not describe the key that was asked for. */
export function isExpectedKey(
  result: KeyRevealResult,
  connection: ModelConnection,
  slot: KeySlot,
) {
  const expectedSubscription = connection.runtime?.subscriptionName
  return (
    result.entitlementId === connection.entitlementId &&
    result.slot === slot &&
    typeof result.key === 'string' &&
    result.key.length > 0 &&
    (!expectedSubscription || result.subscriptionName === expectedSubscription)
  )
}

function errorStatus(error: unknown) {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const { status } = error as { status?: unknown }
    return typeof status === 'number' ? status : undefined
  }
  return undefined
}

function errorCode(error: unknown) {
  if (typeof error === 'object' && error !== null && 'body' in error) {
    const { body } = error as { body?: unknown }
    if (typeof body === 'object' && body !== null && 'code' in body) {
      const { code } = body as { code?: unknown }
      return typeof code === 'string' ? code : undefined
    }
  }
  return undefined
}

function serverMessage(error: unknown) {
  if (!(error instanceof Error) || !error.message) return undefined
  if (/^Request failed with status \d+$/.test(error.message)) return undefined
  return error.message
}

function sentence(text: string) {
  return /[.!?]$/.test(text) ? text : `${text}.`
}

export function isClientError(error: unknown) {
  const status = errorStatus(error)
  return status !== undefined && status >= 400 && status < 500
}

export function isConflict(error: unknown) {
  return errorStatus(error) === 409
}

const signInAgain: Problem = {
  title: 'Sign in again',
  message: 'Your sign-in has expired. Refresh the page to sign in again.',
}

export function connectionProblem(error: unknown): Problem {
  const status = errorStatus(error)
  const detail = serverMessage(error)
  if (status === 401) return signInAgain
  if (status === 403) {
    return {
      title: 'Connection details are not available to you',
      message:
        'Your account is not allowed to view connection details. Ask an administrator to confirm that you have the MOSAIC User role.',
    }
  }
  if (status === 404) {
    return {
      title: 'This grant is not available for your account',
      message:
        'MOSAIC could not find this grant for your account. It may have been removed. Refresh My access, and contact your administrator if it is still listed.',
    }
  }
  if (status === 409) {
    return {
      title: 'This model is not ready to connect',
      message: `${sentence(detail ?? 'The model is not fully set up in MOSAIC')} Ask your administrator to finish setting it up.`,
    }
  }
  if (status === undefined) {
    return {
      title: 'Unable to load connection details',
      message: 'MOSAIC could not be reached. Check your network connection and try again.',
    }
  }
  return {
    title: 'Unable to load connection details',
    message: sentence(detail ?? 'Something went wrong while loading connection details. Try again'),
  }
}

export function keyRevealProblem(error: unknown): Problem {
  if (error instanceof UnexpectedCredentialError) {
    return { title: 'Could not show the key', message: error.message }
  }
  const status = errorStatus(error)
  const code = errorCode(error)
  const detail = serverMessage(error)
  if (code === 'gateway_forbidden') {
    return {
      title: 'MOSAIC cannot read this key',
      message:
        "MOSAIC is not permitted to read subscription keys from API Management. Ask your administrator to check MOSAIC's API Management permissions.",
    }
  }
  if (code === 'gateway_not_found') {
    return {
      title: 'Key not found in API Management',
      message:
        'API Management could not find the subscription for this grant. Ask your administrator to apply model access again.',
    }
  }
  if (status === 401) return signInAgain
  if (status === 403) {
    return {
      title: 'You cannot reveal this key',
      message: 'Your account is not allowed to reveal keys for this grant.',
    }
  }
  if (status === 404) {
    return {
      title: 'This grant is not available for your account',
      message: 'MOSAIC could not find this grant for your account. Refresh My access.',
    }
  }
  if (status === 409) {
    return {
      title: 'The key is not available right now',
      message: sentence(detail ?? 'This grant has changes that are not applied to API Management yet'),
    }
  }
  if (status === undefined) {
    return {
      title: 'Could not show the key',
      message: 'MOSAIC could not be reached. Check your network connection and try again.',
    }
  }
  return {
    title: 'Could not retrieve the key',
    message: 'API Management did not return the key. Try again in a moment.',
  }
}

export type SampleOperationKind = 'chat' | 'responses' | 'messages'

function sampleKind(operation: ConnectionOperation): SampleOperationKind | null {
  if (operation.method.toUpperCase() !== 'POST') return null
  const path = operation.path.replace(/\/+$/, '').toLowerCase()
  if (path.endsWith('/chat/completions')) return 'chat'
  if (path.endsWith('/responses')) return 'responses'
  if (path.endsWith('/anthropic/v1/messages')) return 'messages'
  return null
}

export interface ConnectionSamples {
  credential: 'key' | 'token'
  /** `messages` is the Anthropic Messages API, which takes no `api-version`. */
  kind: SampleOperationKind
  operation: ConnectionOperation
  curl: string
  python: string
}

/** Placeholders only: this deliberately has no way to receive a revealed key. */
export type SampleInput = Pick<
  ModelConnection,
  'endpoint' | 'deploymentName' | 'subscriptionHeader' | 'operations' | 'appliedMethods' | 'keysAvailable'
>

function shellDoubleQuoted(text: string) {
  return text.replace(/[\\"$`]/g, (character) => `\\${character}`)
}

function shellSingleQuoted(text: string) {
  return `'${text.replaceAll("'", `'\\''`)}'`
}

/** The request body's fields in order, written once for both the curl and Python samples. */
function sampleFields(kind: SampleOperationKind, model: string) {
  const messages = '"messages": [{"role": "user", "content": "Hello"}]'
  if (kind === 'responses') return [`"model": ${model}`, '"input": "Hello"']
  if (kind === 'messages') return [`"model": ${model}`, '"max_tokens": 256', messages]
  return [`"model": ${model}`, messages]
}

export function buildSamples(connection: SampleInput): ConnectionSamples | null {
  const methods = connection.appliedMethods
  const credential =
    connection.keysAvailable === false
      ? methods?.entraEnabled
        ? 'token'
        : null
      : methods?.keysEnabled
        ? 'key'
        : methods?.entraEnabled
          ? 'token'
          : null
  if (!credential) return null
  const candidates = connection.operations
    .map((operation) => ({ operation, kind: sampleKind(operation) }))
    .filter((candidate): candidate is { operation: ConnectionOperation; kind: SampleOperationKind } =>
      candidate.kind !== null,
    )
  const chosen = candidates.find((candidate) => candidate.kind === 'chat') ?? candidates[0]
  if (!chosen) return null

  const url = operationUrl(connection.endpoint, chosen.operation.path)
  // The Anthropic Messages API takes no api-version; the gateway adds anthropic-version.
  const needsApiVersion = chosen.kind !== 'messages'
  const query = needsApiVersion ? `${url.includes('?') ? '&' : '?'}api-version=$MOSAIC_API_VERSION` : ''
  const fields = sampleFields(chosen.kind, JSON.stringify(connection.deploymentName))
  const jsonBody = `{${fields.join(', ')}}`
  const pythonBody = fields.map((field) => `        ${field},`)
  const header = connection.subscriptionHeader
  const curlCredential =
    credential === 'key'
      ? `${shellDoubleQuoted(header)}: $MOSAIC_API_KEY`
      : 'Authorization: Bearer $MOSAIC_ACCESS_TOKEN'
  const pythonCredential =
    credential === 'key'
      ? `{${JSON.stringify(header)}: os.environ["MOSAIC_API_KEY"]}`
      : '{"Authorization": "Bearer " + os.environ["MOSAIC_ACCESS_TOKEN"]}'

  const curl = [
    `curl "${shellDoubleQuoted(url)}${query}" \\`,
    `  -H "${curlCredential}" \\`,
    '  -H "Content-Type: application/json" \\',
    `  -d ${shellSingleQuoted(jsonBody)}`,
  ].join('\n')

  const python = [
    'import os',
    '',
    'import requests',
    '',
    'response = requests.post(',
    `    ${JSON.stringify(url)},`,
    ...(needsApiVersion ? ['    params={"api-version": os.environ["MOSAIC_API_VERSION"]},'] : []),
    `    headers=${pythonCredential},`,
    '    json={',
    ...pythonBody,
    '    },',
    '    timeout=60,',
    ')',
    'response.raise_for_status()',
    'print(response.json())',
  ].join('\n')

  return { credential, kind: chosen.kind, operation: chosen.operation, curl, python }
}

/** Non-secret sign-in identifiers only; a token sample never sees a key either. */
export type TokenSampleInput = Pick<
  ModelConnection,
  'tenantId' | 'entraClientId' | 'entraScope' | 'appliedMethods'
>

/**
 * Device code sign-in with the MOSAIC model client. The prompt goes to stderr and only the
 * token to stdout, so the output can be captured into MOSAIC_ACCESS_TOKEN.
 */
export function buildTokenSample(connection: TokenSampleInput): string | null {
  const { tenantId, entraClientId, entraScope } = connection
  if (!connection.appliedMethods?.entraEnabled || !tenantId || !entraClientId || !entraScope) return null
  const authority = `https://login.microsoftonline.com/${tenantId}`
  return [
    'import sys',
    '',
    'import msal',
    '',
    'app = msal.PublicClientApplication(',
    `    ${JSON.stringify(entraClientId)},`,
    `    authority=${JSON.stringify(authority)},`,
    ')',
    `flow = app.initiate_device_flow(scopes=[${JSON.stringify(entraScope)}])`,
    'if "user_code" not in flow:',
    '    sys.exit(flow.get("error_description", "Device code sign-in could not start"))',
    'print(flow["message"], file=sys.stderr)',
    'result = app.acquire_token_by_device_flow(flow)',
    'if "access_token" not in result:',
    `    sys.exit(f"{result.get('error')}: {result.get('error_description')}")`,
    'print(result["access_token"])',
  ].join('\n')
}
