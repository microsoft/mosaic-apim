import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { TargetsError, parseTargets, resolveTargetRef } from '../src/config.ts'
import { e2eRoot } from '../src/paths.ts'

const example = () => JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')) as Record<string, any>

test('the committed example manifest is valid', () => {
  const targets = parseTargets(example())
  assert.equal(targets.roles.admin, 'admin')
  assert.equal(targets.origins.web, 'https://mosaic-dev-web-example.azurewebsites.net')
  assert.equal(targets.endpoints['foundry-partners'].kind, 'foundry')
})

test("the workload's object ID is optional, and a GUID when given", () => {
  const input = example()
  assert.equal(parseTargets(input).workload?.objectId, '22222222-2222-2222-2222-222222222222')
  delete input.workload.objectId
  assert.equal(parseTargets(input).workload?.objectId, undefined)
  input.workload.objectId = 'mosaic-dev-e2e-workload'
  assert.throws(() => parseTargets(input), /targets\.workload\.objectId must be a GUID/)
})

test('origins must be bare https origins', () => {
  for (const web of ['http://mosaic.example', 'https://mosaic.example/app', 'https://user:pw@mosaic.example', 'not a url']) {
    const input = example()
    input.origins.web = web
    assert.throws(() => parseTargets(input), TargetsError, web)
  }
  const local = example()
  local.origins.web = 'http://localhost:5173'
  assert.equal(parseTargets(local).origins.web, 'http://localhost:5173')
})

test('persona keys are safe profile directory names', () => {
  const input = example()
  input.personas['../escape'] = { upn: 'x@contoso.example', expectedRole: 'None' }
  assert.throws(() => parseTargets(input), /must match/)
})

test('journey roles must reference known personas', () => {
  const input = example()
  input.roles.user = 'nobody'
  assert.throws(() => parseTargets(input), /unknown persona "nobody"/)
})

test('endpoints must be Cognitive Services accounts', () => {
  const input = example()
  input.endpoints['aoai-east'].resourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Web/sites/app'
  assert.throws(() => parseTargets(input), /CognitiveServices/)
  const project = example()
  project.endpoints['aoai-east'].resourceId += '/projects/p'
  assert.throws(() => parseTargets(project), /CognitiveServices/)
})

test('resolves @target references and passes literals through', () => {
  const targets = parseTargets(example())
  assert.equal(
    resolveTargetRef(targets, '@target:endpoints.aoai-east.resourceId'),
    targets.endpoints['aoai-east'].resourceId,
  )
  assert.equal(resolveTargetRef(targets, '@target:personas.admin.upn'), 'admin@contoso.example')
  assert.equal(resolveTargetRef(targets, 'plain value'), 'plain value')
  assert.throws(() => resolveTargetRef(targets, '@target:endpoints.missing.resourceId'), TargetsError)
  assert.throws(() => resolveTargetRef(targets, '@target:personas.admin'), TargetsError)
  assert.throws(() => resolveTargetRef(targets, '@target:constructor.name'), TargetsError)
})

test('the suite section is optional and parses the example', () => {
  const input = example()
  delete input.suite
  assert.equal(parseTargets(input).suite, undefined)

  const suite = parseTargets(example()).suite
  assert.equal(suite?.gateway, 'contoso-apim')
  assert.deepEqual(suite?.runtime?.userGrants[0], { endpoint: 'aoai-east', deployment: 'gpt-4.1-mini' })
  assert.equal(suite?.runtime?.foreignGrant?.persona, 'guest')
  assert.equal(suite?.runtime?.chatTokenParameter, 'max_completion_tokens')
  assert.deepEqual(suite?.disposable?.publication, {
    endpoint: 'aoai-west',
    deployment: 'gpt-4.1-nano',
    requester: 'guest',
    limits: { tokensPerMinute: 1000, calls: 10, perSeconds: 60 },
  })
  assert.equal(suite?.disposable?.sharedBudget?.calls, 2)
  assert.equal(suite?.disposable?.tokenLimit?.tokensPerMinute, 100)
})

test('the disposable requester defaults to roles.user and limits to none', () => {
  const input = example()
  delete input.suite.disposable.publication.requester
  delete input.suite.disposable.publication.limits
  const publication = parseTargets(input).suite?.disposable?.publication
  assert.equal(publication?.requester, 'user-a')
  assert.deepEqual(publication?.limits, {})
})

test('suite references must name manifest endpoints and personas', () => {
  const endpoint = example()
  endpoint.suite.runtime.userGrants[0].endpoint = 'missing'
  assert.throws(() => parseTargets(endpoint), /unknown endpoint "missing"/)
  const persona = example()
  persona.suite.disposable.sharedBudget.persona = 'nobody'
  assert.throws(() => parseTargets(persona), /unknown persona "nobody"/)
  const noRole = example()
  noRole.suite.disposable.publication.requester = 'outsider'
  assert.throws(() => parseTargets(noRole), /MOSAIC role/)
  const gateway = example()
  delete gateway.suite.gateway
  assert.throws(() => parseTargets(gateway), /suite\.gateway/)
})

test('the runtime grants are on published models, listed once', () => {
  const unpublished = example()
  unpublished.suite.runtime.userGrants[0].deployment = 'gpt-4.1-nano'
  assert.throws(() => parseTargets(unpublished), /must be one of targets\.endpoints\.aoai-east\.publish/)
  const empty = example()
  empty.suite.runtime.userGrants = []
  assert.throws(() => parseTargets(empty), /non-empty array/)
  const twice = example()
  twice.suite.runtime.userGrants.push({ endpoint: 'aoai-east', deployment: 'gpt-4.1-mini' })
  assert.throws(() => parseTargets(twice), /more than once/)
  const foreign = example()
  foreign.suite.runtime.foreignGrant.persona = 'user-a'
  assert.throws(() => parseTargets(foreign), /someone other than roles\.user/)
  const workload = example()
  delete workload.workload
  assert.throws(() => parseTargets(workload), /needs targets\.workload/)
  const version = example()
  version.suite.runtime.apiVersion = '--flag'
  assert.throws(() => parseTargets(version), /API version/)
  const parameter = example()
  parameter.suite.runtime.chatTokenParameter = 'max_output_tokens'
  assert.throws(() => parseTargets(parameter), /chatTokenParameter/)
})

test('the disposable publication is never a model the manifest keeps', () => {
  const kept = example()
  kept.suite.disposable.publication.deployment = 'o4-mini'
  assert.throws(() => parseTargets(kept), /must not be in targets\.endpoints\.aoai-west\.publish/)
  const halfRate = example()
  delete halfRate.suite.disposable.publication.limits.perSeconds
  assert.throws(() => parseTargets(halfRate), /go together/)
  const fraction = example()
  fraction.suite.disposable.publication.limits.tokensPerMinute = 1.5
  assert.throws(() => parseTargets(fraction), /whole number/)
})

test('the throwaway grants match the verifier proofs and avoid the runtime models', () => {
  const budget = example()
  budget.suite.disposable.sharedBudget.calls = 3
  assert.throws(() => parseTargets(budget), /2 calls per 300 seconds/)
  const ceiling = example()
  ceiling.suite.disposable.tokenLimit.tokensPerMinute = 101
  assert.throws(() => parseTargets(ceiling), /from 1 to 100/)
  const same = example()
  same.suite.disposable.tokenLimit.endpoint = 'aoai-east'
  same.suite.disposable.tokenLimit.deployment = 'gpt-4o'
  assert.throws(() => parseTargets(same), /different models/)
  const relied = example()
  relied.suite.disposable.sharedBudget.deployment = 'gpt-4.1-mini'
  assert.throws(() => parseTargets(relied), /doesn't use/)
  const unpublished = example()
  unpublished.suite.disposable.tokenLimit.deployment = 'gpt-4.1-nano'
  unpublished.suite.disposable.tokenLimit.endpoint = 'aoai-west'
  assert.throws(() => parseTargets(unpublished), /must be one of targets\.endpoints\.aoai-west\.publish/)
})
