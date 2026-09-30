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
