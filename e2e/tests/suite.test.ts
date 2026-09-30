import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { ApiEntitlement } from '../src/mosaic-api.ts'
import {
  describeLimits,
  disposableDisplayName,
  grantSettled,
  isDisposableName,
  isSuiteGrant,
  keptModels,
  limitLines,
  limitsMatch,
  methodProblems,
  methodsLabel,
  missingSuite,
  portalCardTitle,
  runTag,
  runtimeModels,
  suiteJustification,
  suiteNote,
  throwawayGrant,
} from '../src/suite.ts'

function grant(overrides: Partial<ApiEntitlement> = {}): ApiEntitlement {
  return {
    id: 'ent_1',
    subject: { kind: 'principal', id: 'principal_1' },
    resource: { kind: 'modelApi', id: 'model_api_1' },
    enabled: true,
    enforcement: { tokens: null, requests: { calls: 2, renewalPeriodSeconds: 300 } },
    notes: suiteNote('R5'),
    ...overrides,
  }
}

const budget = { calls: 2, perSeconds: 300 }

test('the suite recognizes the grants it wrote by their note', () => {
  assert.equal(suiteNote('A12'), 'Created by the MOSAIC e2e suite (A12). Safe to revoke.')
  assert.equal(isSuiteGrant({ notes: suiteNote('R6') }), true)
  assert.equal(isSuiteGrant({ notes: 'Pilot access for the finance team' }), false)
  assert.equal(isSuiteGrant({ notes: null }), false)
  assert.equal(isSuiteGrant({}), false)
})

test('limits match only when every limit is the manifest\'s and there is no token quota', () => {
  assert.equal(limitsMatch({ requests: { calls: 2, renewalPeriodSeconds: 300 } }, budget), true)
  assert.equal(limitsMatch({ requests: { calls: 2, renewalPeriodSeconds: 60 } }, budget), false)
  assert.equal(limitsMatch({ requests: { calls: 3, renewalPeriodSeconds: 300 } }, budget), false)
  assert.equal(limitsMatch({ tokens: { tokensPerMinute: 100 }, requests: { calls: 2, renewalPeriodSeconds: 300 } }, budget), false)
  assert.equal(limitsMatch({ tokens: { tokensPerMinute: 100 } }, { tokensPerMinute: 100 }), true)
  assert.equal(limitsMatch({ tokens: { tokensPerMinute: 100, tokenQuota: 5_000 } }, { tokensPerMinute: 100 }), false)
  assert.equal(limitsMatch({ requests: { calls: 2, renewalPeriodSeconds: 300, callQuota: 1_000 } }, budget), false)
  assert.equal(limitsMatch({ tokens: { tokensPerMinute: null }, requests: null }, {}), true)
  assert.equal(limitsMatch(null, {}), true)
  assert.equal(limitsMatch(null, budget), false)
})

test('limits read as a sentence', () => {
  assert.equal(describeLimits(budget), '2 calls per 300 seconds')
  assert.equal(describeLimits({ tokensPerMinute: 1_000, calls: 10, perSeconds: 60 }), '1000 tokens per minute and 10 calls per 60 seconds')
  assert.equal(describeLimits({}), 'no limits')
})

test('a persona without a grant gets a new throwaway grant', () => {
  assert.deepEqual(throwawayGrant([], budget, 'user-a'), { action: 'add' })
})

test('the suite reuses its own grant, re-enabling it if it was revoked', () => {
  const enabled = grant()
  assert.deepEqual(throwawayGrant([enabled], budget, 'user-a'), { action: 'keep', grant: enabled })
  const revoked = grant({ enabled: false })
  assert.deepEqual(throwawayGrant([revoked], budget, 'user-a'), { action: 're-enable', grant: revoked })
})

test('the suite leaves alone grants it did not create, grants with other limits, and duplicates', () => {
  const foreign = throwawayGrant([grant({ notes: 'Production access' })], budget, 'user-a')
  assert.equal(foreign.action, 'skip')
  assert.match(foreign.action === 'skip' ? foreign.reason : '', /user-a already holds a grant on this model that the suite didn't create/)

  const changed = throwawayGrant([grant({ enforcement: { requests: { calls: 5, renewalPeriodSeconds: 300 } } })], budget, 'user-a')
  assert.equal(changed.action, 'skip')
  assert.match(changed.action === 'skip' ? changed.reason : '', /doesn't have the manifest's limits \(2 calls per 300 seconds\)/)

  const twice = throwawayGrant([grant(), grant({ id: 'ent_2' })], budget, 'user-a')
  assert.equal(twice.action, 'skip')
  assert.match(twice.action === 'skip' ? twice.reason : '', /holds 2 grants/)
})

test('on the disposable publication, a grant an approval created counts as the suite\'s', () => {
  const approved = grant({ notes: null, enabled: false })
  assert.equal(throwawayGrant([approved], budget, 'guest').action, 'skip')
  assert.deepEqual(throwawayGrant([approved], budget, 'guest', { ownedModel: true }), { action: 're-enable', grant: approved })
  const other = throwawayGrant([grant({ notes: null, enforcement: { requests: { calls: 9, renewalPeriodSeconds: 60 } } })], budget, 'guest', {
    ownedModel: true,
  })
  assert.equal(other.action, 'skip')
})

test('a grant has settled once its applied state matches its intent', () => {
  const runtime = (status: NonNullable<ApiEntitlement['runtime']>['status']) => ({ publicationId: 'pub_1', status })
  assert.equal(grantSettled({ enabled: true, runtime: runtime('applied') }), true)
  assert.equal(grantSettled({ enabled: true, runtime: runtime('pending') }), false)
  assert.equal(grantSettled({ enabled: true, runtime: null }), false)
  assert.equal(grantSettled({ enabled: false, runtime: runtime('revoked') }), true)
  assert.equal(grantSettled({ enabled: false, runtime: runtime('revocationPending') }), false)
  assert.equal(grantSettled({ enabled: false, runtime: runtime('applied') }), false)
})

test('method problems name what a runtime journey needs but the model lacks', () => {
  const applied = (keysEnabled: boolean, entraEnabled: boolean) => ({
    accessState: 'applied' as const,
    appliedAccess: { settings: { keysEnabled, entraEnabled }, grants: [] },
  })
  assert.deepEqual(methodProblems(applied(true, true), { keys: true, entra: true }), [])
  assert.deepEqual(methodProblems(applied(true, false), { keys: true, entra: false }), [])
  assert.deepEqual(methodProblems(applied(true, false), { keys: true, entra: true }), ['Microsoft Entra tokens are off'])
  assert.deepEqual(methodProblems(applied(false, true), { keys: true, entra: true }), ['subscription keys are off'])
  assert.deepEqual(methodProblems({ accessState: 'pending', appliedAccess: null }, { keys: true, entra: false }), [
    'its access reads pending, not applied',
    'subscription keys are off',
  ])
})

test("access methods read as the console's governed-access card shows them", () => {
  assert.equal(methodsLabel(null), 'Not applied')
  assert.equal(methodsLabel(undefined), 'Not applied')
  assert.equal(methodsLabel({ keysEnabled: true, entraEnabled: true }), 'Subscription key OR Entra token')
  assert.equal(methodsLabel({ keysEnabled: true, entraEnabled: false }), 'Subscription key only')
  assert.equal(methodsLabel({ keysEnabled: false, entraEnabled: true }), 'Entra token only')
  assert.equal(methodsLabel({ keysEnabled: false, entraEnabled: false }), 'Deny all — both methods disabled')
})

test('skip reasons point at the runbook', () => {
  assert.match(missingSuite('disposable.publication'), /no suite\.disposable\.publication, so this journey is skipped\. docs\/e2e\/runbook\.md/)
})

test('each run tags what it writes', () => {
  assert.equal(runTag(new Date(Date.UTC(2026, 8, 30, 9, 5, 7))), '20260930-090507')
  assert.equal(suiteJustification('P4', '20260930-090507'), 'E2E suite P4 request 20260930-090507. Safe to deny.')
})

test('the disposable publication carries a name cleanup can recognize', () => {
  const name = disposableDisplayName({ endpoint: 'aoai-west', deployment: 'gpt-4.1-nano' })
  assert.equal(name, 'E2E suite aoai-west gpt-4.1-nano')
  assert.equal(isDisposableName(name), true)
  assert.equal(isDisposableName('gpt-4.1-nano'), false)
  assert.equal(isDisposableName('Not an E2E suite model'), false)
  assert.equal(isDisposableName(null), false)
})

test('the kept models are every deployment the manifest publishes', () => {
  const endpoint = (publish: string[]) => ({ kind: 'azure-openai' as const, resourceId: '/fictional', registerVia: 'paste' as const, publish })
  const endpoints = { 'aoai-east': endpoint(['gpt-4.1-mini', 'gpt-4o']), 'aoai-west': endpoint(['o4-mini']), empty: endpoint([]) }
  assert.deepEqual(keptModels({ endpoints }), [
    { endpoint: 'aoai-east', deployment: 'gpt-4.1-mini' },
    { endpoint: 'aoai-east', deployment: 'gpt-4o' },
    { endpoint: 'aoai-west', deployment: 'o4-mini' },
  ])
})

test('the runtime models list each model the runtime journeys use once', () => {
  const east = { endpoint: 'aoai-east', deployment: 'gpt-4.1-mini' }
  const grok = { endpoint: 'foundry-partners', deployment: 'grok-4.3' }
  const west = { endpoint: 'aoai-west', deployment: 'o4-mini' }
  assert.deepEqual(runtimeModels({ userGrants: [east, grok], applicationGrant: { ...east }, foreignGrant: { persona: 'guest', ...west } }), [
    east,
    grok,
    west,
  ])
  assert.deepEqual(runtimeModels({ userGrants: [grok] }), [grok])
})

test('limit lines read like the portal lists them', () => {
  const none = ['No additional grant limits configured']
  assert.deepEqual(limitLines(null), none)
  assert.deepEqual(limitLines({ tokens: null, requests: null }), none)
  assert.deepEqual(limitLines({ tokens: { tokensPerMinute: 1000 }, requests: { calls: 10, renewalPeriodSeconds: 60 } }), [
    '1,000 tokens per minute',
    '10 calls per 60 seconds',
  ])
  assert.deepEqual(
    limitLines({
      tokens: { tokensPerMinute: 100, tokenQuota: 250000, tokenQuotaPeriod: 'Monthly' },
      requests: { calls: 2, renewalPeriodSeconds: 300, callQuota: 5000, callQuotaPeriod: 'Daily' },
    }),
    ['100 tokens per minute', '250,000 tokens per month', '2 calls per 300 seconds', '5,000 calls per day'],
  )
  // A quota without its period, or calls without their window, isn't a limit the portal lists.
  assert.deepEqual(limitLines({ tokens: { tokenQuota: 10 }, requests: { calls: 2 } }), none)
})

test('a My access card is headed by the name the portal would pick, never an ID', () => {
  const entitlement = grant()
  assert.equal(portalCardTitle({ entitlement, resourceDisplayName: '  Chat model ', resourceSummary: { displayName: 'Other' } }), 'Chat model')
  assert.equal(portalCardTitle({ entitlement, resourceDisplayName: '  ', resourceSummary: { displayName: 'Docs search' } }), 'Docs search')
  assert.equal(portalCardTitle({ entitlement, resourceDisplayName: null, resourceSummary: { displayName: null, available: false } }), 'Resource no longer available')
  assert.equal(portalCardTitle({ entitlement }), 'Model API resource')
  assert.equal(portalCardTitle({ entitlement: grant({ resource: { kind: 'mcpServer', id: 'mcp_1' } }), resourceSummary: null }), 'MCP server resource')
})
