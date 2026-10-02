import { describe, expect, it } from 'vitest'
import { listOf, sharedKeyModels } from './connection-format'

describe('listOf', () => {
  it.each([
    [[], ''],
    [['Opus'], 'Opus'],
    [['Opus', 'Sonnet'], 'Opus and Sonnet'],
    [['Opus', 'Sonnet', 'Haiku'], 'Opus, Sonnet, and Haiku'],
  ])('joins %j', (names, text) => {
    expect(listOf(names)).toBe(text)
  })
})

describe('sharedKeyModels', () => {
  const sonnet = { poolModelId: 'pool_model_sonnet', displayName: 'Claude Sonnet', publicName: 'claude-sonnet-4-5' }

  it('is null when no other model shares the key', () => {
    expect(sharedKeyModels({})).toBeNull()
    expect(sharedKeyModels({ keySharedWith: [] })).toBeNull()
  })

  it('names models by display name, falling back to the name callers send', () => {
    expect(
      sharedKeyModels({
        keySharedWith: [sonnet, { poolModelId: 'pool_model_haiku', displayName: '', publicName: 'claude-haiku-4-5' }],
      }),
    ).toBe('Claude Sonnet and claude-haiku-4-5')
  })
})
