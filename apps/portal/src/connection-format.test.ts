import { describe, expect, it } from 'vitest'
import { buildSamples, buildTokenSample, governmentReadiness, keyRevealProblem, listOf, sharedKeyModels } from './connection-format'
import { connection } from './test/connection'

describe('Government connections', () => {
  const government = { ...connection, azureCloud: 'government' as const,
    appliedMethods: { keysEnabled: true, entraEnabled: false }, keyExists: false }

  it('requires applied gateway access AND a confirmed key before declaring readiness', () => {
    expect(governmentReadiness({ ...government, runtime: { ...connection.runtime!, status: 'pending' } }).applied).toBe(false)
    expect(governmentReadiness(government).label).toBe('Preparing key')
    expect(governmentReadiness({ ...government, keyExists: true }).label).toBe('Ready')
    expect(governmentReadiness({ ...government, runtime: { ...connection.runtime!, keyState: 'deleted' } }).label).toBe('Key deleted')
    expect(governmentReadiness({ ...government, runtime: { ...connection.runtime!, keyState: 'failed' } }).label).toBe('Key setup failed')
  })

  describe('standalone native Messages connections', () => {
    it('keeps the existing gateway-only caller contract for the Bedrock tuple', () => {
      const samples = buildSamples({
        ...connection,
        endpoint: 'https://gateway.example.test/models/sonnet',
        deploymentName: 'anthropic.claude-sonnet-5',
        operations: [{ name: 'messages', method: 'POST', path: '/anthropic/v1/messages' }],
      })
      expect(samples?.kind).toBe('messages')
      expect(samples?.curl).toContain('https://gateway.example.test/models/sonnet/anthropic/v1/messages')
      expect(samples?.curl).toContain('Ocp-Apim-Subscription-Key: $MOSAIC_API_KEY')
      expect(samples?.python).toContain('"model": "anthropic.claude-sonnet-5"')
      expect(JSON.stringify(samples)).not.toMatch(/api-version|amazonaws\.com|api\.aws|x-api-key|SigV4/)
    })
  })

  it('does not manufacture OAuth samples or fall back to a commercial token', () => {
    expect(buildTokenSample(government)).toBeNull()
    expect(buildSamples(government, { credential: 'token' })).toBeNull()
    expect(buildSamples({ ...government, keysAvailable: false })).toBeNull()
    const samples = buildSamples(government)
    expect(JSON.stringify(samples)).toContain('Ocp-Apim-Subscription-Key')
    expect(JSON.stringify(samples)).not.toContain('Authorization: Bearer')
  })

  it('does not recommend commercial tokens when Government cost-center keys are off', () => {
    const problem = keyRevealProblem(Object.assign(new Error('Keys off'), {
      status: 409, body: { code: 'conflict', details: { reason: 'costCenterKeysOff' } },
    }), 'government')
    expect(problem.message).toContain('key-enabled cost center')
    expect(problem.message).not.toContain('token')
  })
})

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
