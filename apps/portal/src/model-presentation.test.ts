import { describe, expect, it } from 'vitest'
import { sameResource } from './entitlement-format'
import { groupResources, modelGroupKey, modelTitle, resourceKey } from './model-presentation'
import type { EntitlementResource, PortalModel } from './types'

const resource: EntitlementResource = { kind: 'modelApi', id: 'one', scopeId: null }
const model: PortalModel = { name: 'gpt-4o', format: 'OpenAI', version: '2024-11-20' }

describe('model presentation', () => {
  it('leads with an authoritative model name, with legacy safe fallbacks', () => {
    expect(modelTitle(resource, 'Endpoint / deployment', null, model)).toBe('gpt-4o')
    expect(modelTitle(resource, 'Legacy model')).toBe('Legacy model')
    expect(modelTitle(resource, null)).toBe('Model API resource')
  })

  it('groups models, not endpoint names, while retaining every item and version', () => {
    const items = [
      { resource, model },
      { resource: { ...resource, id: 'two' }, model: { ...model, version: '2024-08-06' } },
    ]
    const groups = groupResources(items, (item) => ({
      key: modelGroupKey(item.resource, item.model), title: item.model.name,
    }))
    expect(groups).toHaveLength(1)
    expect(groups[0].items).toEqual(items)
  })

  it('never merges different formats, near-matching names, or unknown identities', () => {
    expect(modelGroupKey(resource, model)).not.toBe(modelGroupKey(resource, { ...model, format: 'Other' }))
    expect(modelGroupKey(resource, model)).not.toBe(modelGroupKey(resource, { ...model, name: 'gpt-4o-mini' }))
    expect(modelGroupKey(resource, model)).not.toBe(modelGroupKey(resource, { ...model, name: 'GPT-4o' }))
    expect(modelGroupKey(resource)).not.toBe(modelGroupKey({ ...resource, id: 'two' }))
    expect(modelGroupKey(resource, { ...model, format: null })).toBe(resourceKey(resource))
    expect(modelGroupKey({ ...resource, kind: 'mcpServer' }, model)).not.toBe(modelGroupKey(resource, model))
  })

  it('keeps scoped models distinct for selection and request matching', () => {
    const first = { ...resource, kind: 'poolModel' as const, scopeId: 'pool-one' }
    const second = { ...first, scopeId: 'pool-two' }
    expect(resourceKey(first)).not.toBe(resourceKey(second))
    expect(sameResource(first, second)).toBe(false)
    expect(sameResource(resource, { ...resource, scopeId: 'legacy-gateway' })).toBe(true)
  })
})
