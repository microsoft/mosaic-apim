export const maskedSelectors = ['[data-secret]', 'input[type="password"]', '[data-sensitive]']

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

export function redact(text: string, knownSecrets: Iterable<string> = []): string {
  let result = text
  for (const secret of knownSecrets) {
    if (secret.length >= 8) result = result.split(secret).join('[redacted-secret]')
  }
  for (const rule of rules) {
    result = result.replace(rule.pattern, rule.replace)
  }
  return result
}

export function redactUrl(value: string): string {
  try {
    const url = new URL(value)
    const fragment = url.hash.length > 1 ? '#[redacted]' : ''
    return redact(`${url.origin}${url.pathname}${url.search}`) + fragment
  } catch {
    return redact(value)
  }
}

export function truncate(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text
  return `${text.slice(0, maxChars)}\n… truncated ${text.length - maxChars} characters`
}
