import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { test } from 'node:test'
import type { Response } from '@playwright/test'
import { ResponseRecorder, apiPath, describeResponses, unexpectedSuccesses } from '../src/denials.ts'

const api = 'https://mosaic-api.contoso.example'

test('only MOSAIC API calls are recorded, by path alone', () => {
  assert.equal(apiPath(`${api}/api/v1/portal/me?x=1`, api), '/api/v1/portal/me')
  assert.equal(apiPath(`${api}/healthz`, api), undefined)
  assert.equal(apiPath('https://login.microsoftonline.com/api/v1/portal/me', api), undefined)
  assert.equal(apiPath('not a url', api), undefined)
})

test('any 2xx on a denial check is unexpected, except CORS preflights and allowed paths', () => {
  const responses = [
    { method: 'OPTIONS', path: '/api/v1/portal/me', status: 204 },
    { method: 'GET', path: '/api/v1/portal/me', status: 403 },
    { method: 'GET', path: '/api/v1/portal/catalog', status: 200 },
    { method: 'GET', path: '/api/v1/portal/environments', status: 204 },
    { method: 'GET', path: '/api/v1/portal/entitlements', status: 401 },
  ]
  assert.deepEqual(unexpectedSuccesses(responses), [responses[2], responses[3]])
  assert.deepEqual(unexpectedSuccesses(responses, [/\/environments$/]), [responses[2]])
  assert.equal(describeResponses(unexpectedSuccesses(responses).slice(0, 1)), 'GET /api/v1/portal/catalog returned 200')
})

test('the recorder listens until stopped, and keeps no bodies or headers', () => {
  const source = new EventEmitter()
  const recorder = new ResponseRecorder(source as never, api)
  const response = (url: string, status: number, method = 'GET') =>
    ({ url: () => url, status: () => status, request: () => ({ method: () => method }) }) as unknown as Response
  source.emit('response', response(`${api}/api/v1/portal/me`, 403))
  source.emit('response', response('https://portal.contoso.example/config.json', 200))
  source.emit('response', response(`${api}/api/v1/portal/catalog?q=secret`, 200))
  assert.deepEqual(recorder.responses, [
    { method: 'GET', path: '/api/v1/portal/me', status: 403 },
    { method: 'GET', path: '/api/v1/portal/catalog', status: 200 },
  ])
  assert.deepEqual(recorder.refusals, [{ method: 'GET', path: '/api/v1/portal/me', status: 403 }])
  assert.equal(recorder.unexpectedSuccesses().length, 1)
  recorder.stop()
  source.emit('response', response(`${api}/api/v1/portal/entitlements`, 200))
  assert.equal(recorder.responses.length, 2)
  assert.equal(source.listenerCount('response'), 0)
})
