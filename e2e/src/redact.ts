import type { Page } from '@playwright/test'

/**
 * Elements whose content is secret. Screenshots mask them, text reads refuse them, and their values are
 * redacted from snapshots and errors.
 */
export const maskedSelectors = [
  '[data-secret]',
  '[data-sensitive]',
  'input[type="password"]',
  'input[autocomplete="one-time-code"]',
  'input[name="otc"]',
  // The console's key reveal (EntitlementConnectionDialog) now has a data-secret marker. Its label also
  // matches, for deployed builds from before the marker.
  '[aria-label^="Revealed "][aria-label$=" key"]',
]

const secretHeaderNames = [
  'authorization',
  'ocp-apim-subscription-key',
  'api-key',
  'x-api-key',
  'x-ms-client-principal',
  'cookie',
  'set-cookie',
]

const secretFieldNames = [
  'access_token',
  'id_token',
  'refresh_token',
  'client_secret',
  'code_verifier',
  'client_assertion',
  'password',
  'primaryKey',
  'secondaryKey',
  'subscriptionKey',
  'apiKey',
  'key1',
  'key2',
]

interface Rule {
  pattern: RegExp
  replace: string
}

const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const rules: Rule[] = [
  { pattern: /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/g, replace: '[redacted-jwt]' },
  { pattern: /(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, replace: '$1[redacted]' },
  {
    pattern: new RegExp(`((?:${secretHeaderNames.map(escape).join('|')})["']?\\s*[:=]\\s*["']?)[^\\s"',;&}]+`, 'gi'),
    replace: '$1[redacted]',
  },
  {
    pattern: new RegExp(`((?:${secretFieldNames.map(escape).join('|')})["']?\\s*[:=]\\s*["']?)[^\\s"',;&}]+`, 'gi'),
    replace: '$1[redacted]',
  },
  { pattern: /([?&#](?:code|sig|client_info|state)=)[^&\s"'#]+/gi, replace: '$1[redacted]' },
  { pattern: /(InstrumentationKey=)[^;\s"']+/gi, replace: '$1[redacted]' },
  { pattern: /(AccountKey=|SharedAccessKey=)[^;\s"']+/gi, replace: '$1[redacted]' },
  { pattern: /\b[A-Za-z0-9_~.-]{3}\dQ~[A-Za-z0-9_~.-]{31,34}\b/g, replace: '[redacted-secret]' },
  { pattern: /(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{86}==/g, replace: '[redacted-key]' },
  { pattern: /\b[A-Za-z0-9]{84}\b/g, replace: '[redacted-key]' },
  { pattern: /\b[0-9a-f]{32}\b/gi, replace: '[redacted-key]' },
]

const substringSecretLength = 8

/**
 * Removes known secrets, then anything that looks like a credential. Secrets of 8 or more characters are
 * removed wherever they appear. Shorter ones, such as a partly typed code, are removed only as whole
 * tokens, so they don't wipe out every matching letter in the text.
 */
export function redact(text: string, knownSecrets: Iterable<string> = []): string {
  let result = text
  const secrets = [...new Set(knownSecrets)].filter((secret) => secret !== '').sort((a, b) => b.length - a.length)
  for (const secret of secrets) {
    result = secret.length >= substringSecretLength
      ? result.split(secret).join('[redacted-secret]')
      : result.replace(new RegExp(`(?<![A-Za-z0-9])${escape(secret)}(?![A-Za-z0-9])`, 'g'), '[redacted-secret]')
  }
  for (const rule of rules) {
    result = result.replace(rule.pattern, rule.replace)
  }
  return result
}

/** Values currently shown in secret elements, so they can be redacted wherever else they appear. */
export async function pageSecrets(page: Page): Promise<string[]> {
  return page
    .locator(maskedSelectors.join(', '))
    .evaluateAll((elements) =>
      elements
        .map((element) => ((element as HTMLInputElement).value || element.textContent || '').trim())
        .filter((value) => value !== ''),
    )
    .catch(() => [])
}

export interface RedactableError {
  message?: string
  stack?: string
  value?: string
  errorContext?: string
  cause?: RedactableError
}

const errorTextFields = ['message', 'stack', 'value', 'errorContext'] as const

/**
 * Redacts Playwright test errors in place. A failed matcher's message can quote the received text, and its
 * errorContext holds an accessibility snapshot, input values included. Playwright writes both to
 * error-context.md and the reports after fixtures tear down.
 */
export function redactErrors(errors: Iterable<RedactableError>, knownSecrets: Iterable<string> = []): void {
  const secrets = [...knownSecrets]
  const visit = (error: RedactableError | undefined, depth: number) => {
    if (!error || depth > 8) return
    for (const field of errorTextFields) {
      const value = error[field]
      if (typeof value === 'string') error[field] = redact(value, secrets)
    }
    visit(error.cause, depth + 1)
  }
  for (const error of errors) visit(error, 0)
}

export function redactUrl(value: string): string {
  try {
    const url = new URL(value)
    if (url.protocol === 'data:' || url.protocol === 'blob:') return `${url.protocol}[redacted]`
    const fragment = url.hash.length > 1 ? '#[redacted]' : ''
    // Pages such as about:blank and chrome-error:// have an opaque origin, which the URL API reports as "null".
    const base = url.origin === 'null' ? `${url.protocol}${url.host ? `//${url.host}` : ''}` : url.origin
    return redact(`${base}${url.pathname}${url.search}`) + fragment
  } catch {
    return redact(value)
  }
}

export function truncate(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text
  return `${text.slice(0, maxChars)}\n… truncated ${text.length - maxChars} characters`
}
