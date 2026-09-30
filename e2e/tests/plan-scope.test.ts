import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  type AccessState,
  type ScopePlan,
  type ScopePublication,
  firstPublishProblems,
  foreignAccessChanges,
  foreignMentions,
  foreignNames,
  mentions,
  planScopeProblems,
  replanProblems,
  runFinished,
  runProblems,
  splitServiceResourceId,
  unpublishPlanProblems,
  unpublishProblems,
} from '../src/plan-scope.ts'

// Fictional Azure resource IDs, built from their parts.
const zeroGuid = [8, 4, 4, 4, 12].map((length) => '0'.repeat(length)).join('-')
const serviceId = (name: string) =>
  ['', 'subscriptions', zeroGuid, 'resourceGroups', 'contoso-apim-group', 'providers', 'Microsoft.ApiManagement', 'service', name].join('/')
const service = serviceId('contoso-apim')
const otherService = serviceId('fabrikam-apim')
/** API Management's own subscriptions, which callers authenticate with, under a service. */
const apimSubscriptions = '/subscriptions'

function publication(id: string, stem: string, overrides: Partial<ScopePublication> = {}): ScopePublication {
  return {
    id,
    gatewayId: 'gw_contoso',
    displayName: `aoai-west ${stem}`,
    deploymentName: stem,
    apiName: `${stem}-api`,
    backendName: `${stem}-backend`,
    fragmentName: `${stem}-fragment`,
    productName: `${stem}-product`,
    subscriptionName: `${stem}-subscription`,
    modelApiId: `mapi_${stem}`,
    resources: [],
    ...overrides,
  }
}

const target = publication('pub_nano', 'gpt-4.1-nano')
const neighbour = publication('pub_mini', 'gpt-4.1-mini')

function step(kind: string, path: string, name: string, action = 'create', extra: Partial<ScopePlan['steps'][number]> = {}) {
  return { kind, name, action, resourceId: `${service}${path}`, ...extra }
}

function fullPlan(): ScopePlan {
  return {
    publicationId: 'pub_nano',
    steps: [
      step('backend', '/backends/gpt-4.1-nano-backend', 'gpt-4.1-nano-backend'),
      step('policyFragment', '/policyFragments/gpt-4.1-nano-fragment', 'gpt-4.1-nano-fragment'),
      step('api', '/apis/gpt-4.1-nano-api', 'gpt-4.1-nano-api'),
      step('apiOperation', '/apis/gpt-4.1-nano-api/operations/chat-completions', 'chat-completions'),
      step('apiPolicy', '/apis/gpt-4.1-nano-api/policies/policy', 'policy'),
      step('product', '/products/gpt-4.1-nano-product', 'gpt-4.1-nano-product'),
      step('productApi', '/products/gpt-4.1-nano-product/apis/gpt-4.1-nano-api', 'gpt-4.1-nano-api'),
      step('subscription', `${apimSubscriptions}/gpt-4.1-nano-subscription`, 'gpt-4.1-nano-subscription'),
      step('subscription', `${apimSubscriptions}/grant-guest-nano`, 'grant-guest-nano', 'create', { entitlementId: 'ent_guest' }),
    ],
    accessSnapshot: { grants: [{ entitlementId: 'ent_guest', subscriptionName: 'grant-guest-nano' }] },
  }
}

const context = {
  target,
  others: [neighbour],
  gatewayResourceId: service,
  targetGrantIds: new Set(['ent_guest']),
}

test('a plan that changes only the target model is in scope', () => {
  assert.deepEqual(planScopeProblems(fullPlan(), context), [])
  assert.deepEqual(firstPublishProblems(fullPlan()), [])
})

test('API Management names are compared without regard to case', () => {
  const plan = fullPlan()
  plan.steps = [step('api', '/APIS/GPT-4.1-NANO-API', 'GPT-4.1-NANO-API')]
  assert.deepEqual(planScopeProblems(plan, { ...context, gatewayResourceId: service.toUpperCase() }), [])
})

test('a plan for another publication is out of scope', () => {
  const plan = { ...fullPlan(), publicationId: 'pub_mini' }
  assert.deepEqual(planScopeProblems(plan, context), ['The plan is for another publication, not aoai-west gpt-4.1-nano.'])
})

test('steps on another model, another gateway, or an unknown subscription are out of scope', () => {
  const plan = fullPlan()
  plan.steps = [
    step('api', '/apis/gpt-4.1-mini-api', 'gpt-4.1-mini-api', 'update'),
    step('productApi', '/products/gpt-4.1-nano-product/apis/gpt-4.1-mini-api', 'gpt-4.1-mini-api'),
    step('subscription', `${apimSubscriptions}/gpt-4.1-mini-subscription`, 'gpt-4.1-mini-subscription', 'update'),
    { ...step('backend', '/backends/gpt-4.1-nano-backend', 'gpt-4.1-nano-backend'), resourceId: `${otherService}/backends/gpt-4.1-nano-backend` },
    { kind: 'backend', name: 'gpt-4.1-nano-backend', action: 'create', resourceId: 'gpt-4.1-nano-backend' },
    step('namedValue', '/namedValues/gpt-4.1-nano-key', 'gpt-4.1-nano-key'),
  ]
  assert.deepEqual(planScopeProblems(plan, context), [
    "Step api gpt-4.1-mini-api changes /apis/gpt-4.1-mini-api, which isn't part of aoai-west gpt-4.1-nano.",
    "Step productApi gpt-4.1-mini-api changes /products/gpt-4.1-nano-product/apis/gpt-4.1-mini-api, which isn't part of aoai-west gpt-4.1-nano.",
    `Step subscription gpt-4.1-mini-subscription changes ${apimSubscriptions}/gpt-4.1-mini-subscription, which isn't part of aoai-west gpt-4.1-nano.`,
    'Step backend gpt-4.1-nano-backend is on another API Management service.',
    "Step backend gpt-4.1-nano-backend doesn't name an API Management resource.",
    "Step namedValue gpt-4.1-nano-key changes /namedValues/gpt-4.1-nano-key, which isn't part of aoai-west gpt-4.1-nano.",
  ])
})

test('grants on another model are out of scope, in the steps and in the access review', () => {
  const plan = fullPlan()
  plan.steps = [step('subscription', `${apimSubscriptions}/grant-guest-nano`, 'grant-guest-nano', 'update', { entitlementId: 'ent_other' })]
  plan.accessSnapshot = { grants: [{ entitlementId: 'ent_other', subscriptionName: 'grant-guest-nano' }] }
  assert.deepEqual(planScopeProblems(plan, context), [
    'Step subscription grant-guest-nano changes a grant on another model.',
    'The access review includes a grant on another model.',
  ])
})

test('subscriptions the target already owns are in scope', () => {
  const owner = { ...target, resources: [{ kind: 'subscription', name: 'grant-old', resourceId: `${service}${apimSubscriptions}/grant-old` }] }
  const plan: ScopePlan = { publicationId: 'pub_nano', steps: [step('subscription', `${apimSubscriptions}/grant-old`, 'grant-old', 'delete')] }
  assert.deepEqual(planScopeProblems(plan, { ...context, target: owner }), [])
})

test("a key publication's own backend key named value is in scope, and another's is not", () => {
  const keyed = { ...target, backendKeyName: 'gpt-4.1-nano-backend-key' }
  const own = step('namedValue', '/namedValues/GPT-4.1-NANO-BACKEND-KEY', 'gpt-4.1-nano-backend-key')
  const foreign = step('namedValue', '/namedValues/gpt-4.1-mini-backend-key', 'gpt-4.1-mini-backend-key')
  const nested = step('namedValue', '/namedValues/gpt-4.1-nano-backend-key/listValue', 'gpt-4.1-nano-backend-key')
  const plan: ScopePlan = { publicationId: 'pub_nano', steps: [own, foreign, nested] }
  assert.deepEqual(planScopeProblems(plan, { ...context, target: keyed }), [
    "Step namedValue gpt-4.1-mini-backend-key changes /namedValues/gpt-4.1-mini-backend-key, which isn't part of aoai-west gpt-4.1-nano.",
    "Step namedValue gpt-4.1-nano-backend-key changes /namedValues/gpt-4.1-nano-backend-key/listValue, which isn't part of aoai-west gpt-4.1-nano.",
  ])
  const keyedNeighbour = { ...neighbour, backendKeyName: 'gpt-4.1-mini-backend-key' }
  assert.equal(foreignNames(keyed, [keyedNeighbour]).includes('gpt-4.1-mini-backend-key'), true)
  assert.equal(foreignNames(keyed, [keyedNeighbour]).includes('gpt-4.1-nano-backend-key'), false)
})

test('names match only as whole API Management names', () => {
  assert.equal(mentions('Create · gpt-4.1-mini-api', 'gpt-4.1-mini-api'), true)
  assert.equal(mentions('Removes GPT-4.1-MINI-API.', 'gpt-4.1-mini-api'), true)
  assert.equal(mentions('Create · gpt-4.1-mini-api.v2', 'gpt-4.1-mini-api'), false)
  assert.equal(mentions('Create · gpt-4.1-mini-api-v2', 'gpt-4.1-mini-api'), false)
  assert.equal(mentions('Create · xgpt-4.1-mini-api', 'gpt-4.1-mini-api'), false)
  assert.equal(mentions('xgpt-4.1-mini-api then (gpt-4.1-mini-api)', 'gpt-4.1-mini-api'), true)
  assert.equal(mentions('aaa', 'aa'), false)
  assert.equal(mentions('anything', ''), false)
})

test('the step list may mention only the model being changed', () => {
  const text = 'API · gpt-4.1-nano-api Create\nSubscription · grant-guest-nano Create'
  assert.deepEqual(foreignMentions(text, target, [neighbour]), [])
  assert.deepEqual(foreignMentions(`${text}\nProduct · gpt-4.1-mini-product Update`, target, [neighbour]), ['gpt-4.1-mini-product'])
})

test('names the target shares, or contains, are not foreign', () => {
  const sharing = publication('pub_shared', 'shared', { productName: 'gpt-4.1-nano-product', apiName: 'nano' })
  const names = foreignNames({ ...target, displayName: 'aoai-west nano' }, [sharing, target])
  assert.equal(names.includes('gpt-4.1-nano-product'), false)
  assert.equal(names.includes('nano'), false)
  assert.equal(names.includes('shared-backend'), true)
})

test('a first publish creates everything; anything else already exists', () => {
  const plan = fullPlan()
  plan.steps = [step('api', '/apis/gpt-4.1-nano-api', 'gpt-4.1-nano-api', 'update'), step('backend', '/backends/gpt-4.1-nano-backend', 'gpt-4.1-nano-backend', 'noChange')]
  assert.deepEqual(firstPublishProblems(plan), [
    "A first publish would update api gpt-4.1-nano-api, which shouldn't exist yet.",
    "A first publish would leave unchanged backend gpt-4.1-nano-backend, which shouldn't exist yet.",
  ])
  assert.deepEqual(firstPublishProblems({ publicationId: 'pub_nano', steps: [] }), [
    'A first publish should create resources, and this plan has no steps.',
  ])
})

test('a re-plan of an applied model neither creates nor deletes', () => {
  const plan = fullPlan()
  plan.steps = [
    step('apiPolicy', '/apis/gpt-4.1-nano-api/policies/policy', 'policy', 'update'),
    step('api', '/apis/gpt-4.1-nano-api', 'gpt-4.1-nano-api', 'noChange'),
  ]
  assert.deepEqual(replanProblems(plan), [])
  plan.steps = [...plan.steps, step('backend', '/backends/gpt-4.1-nano-backend', 'gpt-4.1-nano-backend', 'delete')]
  assert.deepEqual(replanProblems(plan), ['Re-planning would delete backend gpt-4.1-nano-backend.'])
})

test('an applied run succeeded at every step', () => {
  const ok = { kind: 'api', name: 'gpt-4.1-nano-api', action: 'create', status: 'succeeded', resourceId: `${service}/apis/gpt-4.1-nano-api` }
  const unchanged = { ...ok, action: 'noChange', status: 'skipped' }
  assert.deepEqual(runProblems({ status: 'succeeded', steps: [ok, unchanged] }), [])
  assert.deepEqual(runProblems({ status: 'rolledBack', steps: [ok, { ...ok, kind: 'backend', status: 'rolledBack' }, { ...ok, status: 'skipped' }] }), [
    'The run ended rolledBack.',
    'Step backend gpt-4.1-nano-api ended rolledBack.',
    'Step api gpt-4.1-nano-api ended skipped.',
  ])
})

test('a run has finished once it stops running, however it ended', () => {
  assert.equal(runFinished('running'), false)
  for (const status of ['succeeded', 'failed', 'rolledBack', 'rollbackFailed', 'interrupted']) assert.equal(runFinished(status), true)
})

test('unpublishing deletes only what the publication owned and creates nothing', () => {
  const owned = [{ kind: 'api', name: 'gpt-4.1-nano-api', resourceId: `${service}/apis/gpt-4.1-nano-api` }]
  const deleteOwn = { kind: 'api', name: 'gpt-4.1-nano-api', action: 'delete', status: 'succeeded', resourceId: `${service}/APIS/gpt-4.1-nano-api` }
  const suspend = {
    kind: 'subscription',
    name: 'grant-guest-nano',
    action: 'update',
    status: 'succeeded',
    resourceId: `${service}${apimSubscriptions}/grant-guest-nano`,
  }
  assert.deepEqual(unpublishProblems([suspend, deleteOwn], owned), [])
  const deleteOther = { ...deleteOwn, name: 'gpt-4.1-mini-api', resourceId: `${service}/apis/gpt-4.1-mini-api` }
  const create = { ...deleteOwn, kind: 'backend', action: 'create' }
  assert.deepEqual(unpublishProblems([deleteOther, create], owned), [
    "Unpublishing deleted api gpt-4.1-mini-api, which the publication didn't own.",
    'Unpublishing created backend gpt-4.1-nano-api.',
  ])
})

test('an unpublish plan deletes everything the publication owns, and nothing else', () => {
  const owned = [
    { kind: 'api', name: 'gpt-4.1-nano-api', resourceId: `${service}/apis/gpt-4.1-nano-api` },
    { kind: 'backend', name: 'gpt-4.1-nano-backend', resourceId: `${service}/backends/gpt-4.1-nano-backend` },
  ]
  const deletes = [
    step('api', '/APIS/gpt-4.1-nano-api', 'gpt-4.1-nano-api', 'delete'),
    step('backend', '/backends/gpt-4.1-nano-backend', 'gpt-4.1-nano-backend', 'delete'),
  ]
  assert.deepEqual(unpublishPlanProblems({ publicationId: 'pub_nano', operation: 'unpublish', steps: deletes }, owned), [])

  const suspend = step('subscription', `${apimSubscriptions}/grant-guest-nano`, 'grant-guest-nano', 'update')
  const other = step('api', '/apis/gpt-4.1-mini-api', 'gpt-4.1-mini-api', 'delete')
  assert.deepEqual(unpublishPlanProblems({ publicationId: 'pub_nano', operation: 'unpublish', steps: [deletes[0], suspend, other] }, owned), [
    'Unpublishing would update subscription grant-guest-nano.',
    "Unpublishing would delete api gpt-4.1-mini-api, which the publication doesn't own.",
    'The unpublish plan leaves backend gpt-4.1-nano-backend behind.',
  ])
})

test('an unpublish runs only a plan made for unpublishing', () => {
  const owned = [{ kind: 'api', name: 'gpt-4.1-nano-api', resourceId: `${service}/apis/gpt-4.1-nano-api` }]
  const deletes = [step('api', '/apis/gpt-4.1-nano-api', 'gpt-4.1-nano-api', 'delete')]
  assert.deepEqual(unpublishPlanProblems({ publicationId: 'pub_nano', operation: 'publish', steps: deletes }, owned), [
    "MOSAIC's plan is a publish plan, not an unpublish plan.",
  ])
  assert.deepEqual(unpublishPlanProblems({ publicationId: 'pub_nano', steps: deletes }, owned), [
    "MOSAIC's plan is a publish plan, not an unpublish plan.",
  ])
})

test('splits API Management resource IDs at the service', () => {
  assert.deepEqual(splitServiceResourceId(`${service}/apis/a/operations/b`), { service, path: '/apis/a/operations/b' })
  assert.equal(splitServiceResourceId(service), undefined)
  assert.equal(splitServiceResourceId('/apis/a'), undefined)
})

function access(overrides: Partial<AccessState> = {}): AccessState {
  return {
    settings: { keysEnabled: true, entraEnabled: true },
    audience: 'api://contoso-models',
    publicationEnforcement: { tokensPerMinute: null },
    grants: [
      { entitlementId: 'ent_theirs', enabled: true, intentDigest: 'digest-theirs' },
      { entitlementId: 'ent_revoked', enabled: false, intentDigest: 'digest-revoked' },
    ],
    ...overrides,
  }
}

const mine = new Set(['ent_suite'])

test('on a shared model, the suite may apply its own grant and nothing else', () => {
  const applied = access()
  const planned = access({ grants: [...applied.grants, { entitlementId: 'ent_suite', enabled: true, intentDigest: 'digest-suite' }] })
  assert.deepEqual(foreignAccessChanges(planned, applied, mine), [])

  // Revoking the suite's grant later is its own change too.
  const withSuite = access({ grants: planned.grants })
  const revoking = access({ grants: [...applied.grants, { entitlementId: 'ent_suite', enabled: false, intentDigest: 'digest-suite-off' }] })
  assert.deepEqual(foreignAccessChanges(revoking, withSuite, mine), [])
})

test("someone else's pending grant change stops the suite's apply", () => {
  const applied = access()
  const changedLimits = access({ grants: [{ ...applied.grants[0], intentDigest: 'digest-theirs-v2' }, applied.grants[1]] })
  const revoked = access({ grants: [{ ...applied.grants[0], enabled: false }, applied.grants[1]] })
  const added = access({ grants: [...applied.grants, { entitlementId: 'ent_new', enabled: false, intentDigest: 'digest-new' }] })
  const dropped = access({ grants: [applied.grants[0]] })
  const message = "The plan would also apply changes to 1 grant(s) the suite didn't make, saved since the model was last applied."
  for (const planned of [changedLimits, revoked, added, dropped]) assert.deepEqual(foreignAccessChanges(planned, applied, mine), [message])
})

test("the model's own pending changes stop the suite's apply unless the suite owns the model", () => {
  const applied = access()
  const keysOnly = access({ settings: { keysEnabled: true, entraEnabled: false } })
  assert.deepEqual(foreignAccessChanges(keysOnly, applied, mine), [
    "The plan would change the model's access methods from keys on and Entra tokens on to keys on and Entra tokens off.",
  ])
  assert.deepEqual(foreignAccessChanges(keysOnly, applied, mine, { methods: true }), [])
  assert.deepEqual(foreignAccessChanges(access({ audience: 'api://fabrikam-models' }), applied, mine), [
    "The plan would change the model's Microsoft Entra audience.",
  ])
  assert.deepEqual(foreignAccessChanges(access({ publicationEnforcement: { tokensPerMinute: 500 } }), applied, mine), [
    "The plan would change the model's token enforcement.",
  ])
})

test('without both snapshots, the suite cannot tell what else a plan would apply', () => {
  assert.match(foreignAccessChanges(undefined, access(), mine)[0], /no model-wide access review/)
  assert.match(foreignAccessChanges(access(), null, mine)[0], /never applied this model's access/)
})

test('a digest only one side reports is not a change', () => {
  const applied = access({ grants: [{ entitlementId: 'ent_theirs', enabled: true }] })
  assert.deepEqual(foreignAccessChanges(access({ grants: [{ entitlementId: 'ent_theirs', enabled: true, intentDigest: 'digest' }] }), applied, mine), [])
})
