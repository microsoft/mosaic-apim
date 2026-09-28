import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Page } from '@playwright/test'
import { LocatorSpecError, describeLocator, locate, parseSpec } from '../src/locators.ts'

test('parses role locators with and without names', () => {
  assert.deepEqual(parseSpec('role:button'), { kind: 'role', role: 'button' })
  assert.deepEqual(parseSpec('role:button:Add model endpoint'), { kind: 'role', role: 'button', name: 'Add model endpoint' })
  assert.deepEqual(parseSpec('role:link:Docs: setup'), { kind: 'role', role: 'link', name: 'Docs: setup' })
})

test('parses regular expression names and text', () => {
  const parsed = parseSpec('role:button:/^approve/i')
  assert.equal(parsed.kind, 'role')
  assert.ok(parsed.kind === 'role' && parsed.name instanceof RegExp)
  assert.equal(String((parsed as { name: RegExp }).name), '/^approve/i')
  const text = parseSpec('text:/Models on .+/')
  assert.ok(text.kind === 'text' && text.value instanceof RegExp)
})

test('keeps css and test id values literal', () => {
  assert.deepEqual(parseSpec('css:[data-secret]'), { kind: 'css', value: '[data-secret]' })
  assert.deepEqual(parseSpec('testid:/not-a-regex/'), { kind: 'testid', value: '/not-a-regex/' })
})

test('rejects unknown kinds, roles, and empty values', () => {
  assert.throws(() => parseSpec('Add model endpoint'), LocatorSpecError)
  assert.throws(() => parseSpec('role:buton:Save'), LocatorSpecError)
  assert.throws(() => parseSpec('xpath://div'), LocatorSpecError)
  assert.throws(() => parseSpec('label:'), LocatorSpecError)
  assert.throws(() => parseSpec('text:/(/'), LocatorSpecError)
})

type Call = [string, ...unknown[]]

function fakeRoot(calls: Call[]): Page {
  const node: Record<string, unknown> = {}
  for (const method of ['getByRole', 'getByLabel', 'getByText', 'getByPlaceholder', 'getByTitle', 'getByAltText', 'getByTestId', 'locator', 'filter', 'nth']) {
    node[method] = (...args: unknown[]) => {
      calls.push([method, ...args])
      return node
    }
  }
  for (const method of ['first', 'last']) {
    node[method] = () => {
      calls.push([method])
      return node
    }
  }
  return node as unknown as Page
}

test('resolves scoped locators in order', () => {
  const calls: Call[] = []
  locate(fakeRoot(calls), 'role:button:Publish', { within: 'role:dialog:Publish model', row: 'gpt-4o', nth: -1, exact: true })
  assert.deepEqual(calls, [
    ['getByRole', 'dialog', { name: 'Publish model', exact: true }],
    ['first'],
    ['getByRole', 'row'],
    ['filter', { hasText: 'gpt-4o' }],
    ['first'],
    ['getByRole', 'button', { name: 'Publish', exact: true }],
    ['last'],
  ])
})

test('describes locators for logs', () => {
  assert.equal(
    describeLocator('role:button:Save', { within: 'role:dialog', row: 'x', nth: 1, exact: true }),
    'role:button:Save within role:dialog in row "x" #1 (exact)',
  )
})
