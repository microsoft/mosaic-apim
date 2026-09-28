import assert from 'node:assert/strict'
import { test } from 'node:test'
import { redact, redactErrors, redactUrl, truncate } from '../src/redact.ts'

const jwt = 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.c2lnbmF0dXJlLXZhbHVlLWhlcmU'
const apimKey = '0123456789abcdef0123456789abcdef'

test('redacts JSON web tokens anywhere in text', () => {
  assert.equal(redact(`token ${jwt} end`), 'token [redacted-jwt] end')
})

test('redacts bearer credentials and secret headers', () => {
  assert.equal(redact('Authorization: Bearer abc.def-ghi_jkl'), 'Authorization: [redacted] [redacted]')
  assert.equal(redact(`Ocp-Apim-Subscription-Key: ${apimKey}`), 'Ocp-Apim-Subscription-Key: [redacted]')
  assert.equal(redact('api-key=supersecretvalue'), 'api-key=[redacted]')
})

test('redacts secret JSON fields and bare APIM keys', () => {
  assert.equal(redact(`{"primaryKey":"${apimKey}"}`), '{"primaryKey":"[redacted]"}')
  assert.equal(redact(`key ${apimKey.toUpperCase()} shown`), 'key [redacted-key] shown')
})

test('redacts Entra client secrets and 84-character Azure AI keys', () => {
  assert.equal(redact('secret abc8Q~abcdefghijklmnopqrstuvwxyz0123456 end'), 'secret [redacted-secret] end')
  assert.equal(redact(`k ${'A1'.repeat(42)} end`), 'k [redacted-key] end')
})

test('redacts connection string and SAS secrets', () => {
  assert.equal(
    redact('InstrumentationKey=4f5a9c2e-7b1d-4e8a-9c3f-2d6b8e0a1f47;IngestionEndpoint=https://x'),
    'InstrumentationKey=[redacted];IngestionEndpoint=https://x',
  )
  assert.equal(redact('https://acct.blob.core.windows.net/c?sv=1&sig=abc%2Fdef'), 'https://acct.blob.core.windows.net/c?sv=1&sig=[redacted]')
})

test('keeps GUIDs and resource IDs readable', () => {
  const id = '/subscriptions/3c8e1f0a-5b7d-4a29-8e6c-1f4b9d2a7e53/resourceGroups/RG-AI/providers/Microsoft.CognitiveServices/accounts/Contoso-AOAI-East'
  assert.equal(redact(id), id)
  assert.equal(redact('object 8b2d6f4a-1c3e-4d5f-a7b9-0e2c4a6d8f13'), 'object 8b2d6f4a-1c3e-4d5f-a7b9-0e2c4a6d8f13')
})

test('replaces known secrets captured from the page', () => {
  assert.equal(redact('value is s3cr3t-value!', ['s3cr3t-value!']), 'value is [redacted-secret]')
  assert.equal(
    redact('- textbox "Enter the password for a@contoso.example": Hunter2-Summer!2026', ['Hunter2-Summer!2026']),
    '- textbox "Enter the password for a@contoso.example": [redacted-secret]',
  )
})

test('replaces short known secrets only as whole tokens', () => {
  assert.equal(redact('- textbox "Code": "493817"', ['493817']), '- textbox "Code": "[redacted-secret]"')
  assert.equal(redact('short abc and abcdef', ['abc']), 'short [redacted-secret] and abcdef')
})

test('replaces the longest known secret first', () => {
  assert.equal(redact('key 1234567890-abcdef', ['1234567890', '1234567890-abcdef']), 'key [redacted-secret]')
})

test('redacts test errors in place, including matcher snapshots and causes', () => {
  const errors = [
    {
      message: `Expected "ok"\nReceived: "${apimKey}"`,
      stack: `Error: token ${jwt}`,
      errorContext: '- textbox "Code": "493817"',
      cause: { message: 'value s3cr3t-value!' },
    },
  ]
  redactErrors(errors, ['493817', 's3cr3t-value!'])
  assert.deepEqual(errors, [
    {
      message: 'Expected "ok"\nReceived: "[redacted-key]"',
      stack: 'Error: token [redacted-jwt]',
      errorContext: '- textbox "Code": "[redacted-secret]"',
      cause: { message: 'value [redacted-secret]' },
    },
  ])
})

test('redacts authorization codes and URL fragments', () => {
  assert.equal(
    redactUrl('https://web.example/#code=0.AAAA&client_info=eyJ1aWQiOiJ4In0&state=abc'),
    'https://web.example/#[redacted]',
  )
  assert.equal(redactUrl('https://login.example/authorize?client_id=x&code=abc123'), 'https://login.example/authorize?client_id=x&code=[redacted]')
})

test('keeps browser page URLs readable and hides inline content', () => {
  assert.equal(redactUrl('about:blank'), 'about:blank')
  assert.equal(redactUrl('chrome-error://chromewebdata/'), 'chrome-error://chromewebdata/')
  assert.equal(redactUrl('data:text/html,<p>key 0123456789abcdef</p>'), 'data:[redacted]')
  assert.equal(redactUrl('blob:https://web.example/5f0c1d2e'), 'blob:[redacted]')
})

test('truncates long output with a marker', () => {
  assert.equal(truncate('abcdef', 10), 'abcdef')
  assert.equal(truncate('abcdefghij', 4), 'abcd\n… truncated 6 characters')
})
