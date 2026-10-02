import { describe, expect, it } from 'vitest'
import {
  apiNameProblem,
  apiPathProblem,
  apimSlug,
  capacitySummary,
  checkDrafts,
  defaultPoolApiName,
  defaultPoolApiPath,
  deploymentLookup,
  describeRetries,
  describeSafeguard,
  draftFromCandidate,
  draftsFromPool,
  formatRunDuration,
  memberPosition,
  memberShares,
  newestRunsFirst,
  planProblems,
  poolPublishBlocker,
  poolRequestExample,
  poolRunOperation,
  poolWriteBlocker,
  publicNameProblem,
  readinessSummary,
  safeguardFields,
  safeguardFromFields,
  safeguardTierNote,
  sameFamily,
  specsWithMemberDrained,
  suggestedWeights,
  type DraftPoolModel,
} from './pools'
import {
  anthropicPool,
  anthropicPoolDetail,
  observedGateway,
  opusEastUs2,
  opusNorthCentral,
  opusWestUs3,
  poolCandidates,
  poolGateway,
  poolRun,
} from './test/pool-fixtures'
import type { CapacityType } from './types'

function capacity(capacityType: CapacityType, skuCapacity: number | null) {
  return { capacityType, skuCapacity }
}

describe('pool names', () => {
  it('derives the same default API name and path the API does', () => {
    expect(apimSlug('Anthropic Claude (all regions)')).toBe('anthropic-claude-all-regions')
    expect(defaultPoolApiName('Anthropic Claude')).toBe('mosaic-pool-anthropic-claude')
    expect(defaultPoolApiPath('Anthropic Claude')).toBe('mosaic/pool-anthropic-claude')
    expect(defaultPoolApiName('!!!')).toBe('mosaic-pool-models')
    expect(apimSlug(`${'a'.repeat(39)} b`)).toBe('a'.repeat(39))
  })

  it('checks API names and paths the way the API does', () => {
    expect(apiNameProblem('mosaic-pool-anthropic')).toBeNull()
    expect(apiNameProblem('  ')).toBe('Enter a name.')
    expect(apiNameProblem('a'.repeat(81))).toBe('Use at most 80 characters.')
    expect(apiNameProblem('-pool')).toMatch(/Start with a letter or digit/)
    expect(apiNameProblem('pool/anthropic')).toMatch(/only letters, digits, and hyphens/)
    expect(apiPathProblem('/mosaic/pool-anthropic/')).toBeNull()
    expect(apiPathProblem('//')).toBe('Enter a path.')
    expect(apiPathProblem(`${'a'.repeat(201)}`)).toBe('Use at most 200 characters.')
    expect(apiPathProblem('mosaic/pool anthropic')).toMatch(/forward slashes/)
  })

  it('explains a model name callers could not send, or one another model uses', () => {
    expect(publicNameProblem('', [])).toMatch(/Enter the model name/)
    expect(publicNameProblem('-opus', [])).toMatch(/Start with a letter or digit/)
    expect(publicNameProblem('claude opus', [])).toMatch(/letters, digits, periods/)
    expect(publicNameProblem('Claude-Opus', ['claude-opus'])).toMatch(/already uses this name/)
    expect(publicNameProblem('claude-opus-4.5', ['claude-sonnet'])).toBeNull()
  })
})

describe('suggested weights', () => {
  it('weights pay-as-you-go members by their share of capacity', () => {
    expect(suggestedWeights([capacity('payAsYouGo', 100), capacity('payAsYouGo', 300)], 'breaker')).toEqual([1, 3])
    expect(suggestedWeights([capacity('payAsYouGo', 1000), capacity('payAsYouGo', 50)], 'breaker')).toEqual([20, 1])
  })

  it('scales weights into 1 to 100 when the capacities share no useful divisor', () => {
    expect(suggestedWeights([capacity('payAsYouGo', 450), capacity('payAsYouGo', 7)], 'breaker')).toEqual([100, 2])
  })

  it('weights equally when capacity is unknown or measured in different units', () => {
    expect(suggestedWeights([capacity('payAsYouGo', 100), capacity('unknown', null)], 'breaker')).toEqual([1, 1])
    expect(suggestedWeights([capacity('provisioned', 100), capacity('payAsYouGo', 300)], 'breaker')).toEqual([1, 1])
  })

  it('weights provisioned and pay-as-you-go members separately in a preferential pool', () => {
    expect(
      suggestedWeights(
        [capacity('provisioned', 100), capacity('provisioned', 300), capacity('payAsYouGo', 50), capacity('payAsYouGo', 150)],
        'preferential',
      ),
    ).toEqual([1, 3, 1, 3])
  })

  it('leaves every weight at 1 in a linear pool, which goes by order', () => {
    expect(suggestedWeights([capacity('payAsYouGo', 100), capacity('payAsYouGo', 300)], 'linear')).toEqual([1, 1])
  })
})

describe('pool model drafts', () => {
  const opus = poolCandidates.models[0]

  it('starts a model with every eligible deployment, named for their shared deployment name', () => {
    const draft = draftFromCandidate(opus, 'breaker')

    expect(draft.publicName).toBe('claude-opus-4-5')
    expect(draft.members.map((member) => member.modelEndpointId)).toEqual([
      opusEastUs2.modelEndpointId,
      opusNorthCentral.modelEndpointId,
      opusWestUs3.modelEndpointId,
    ])
    // Provisioned and pay-as-you-go capacity aren't comparable, so a breaker pool weights them equally.
    expect(draft.members.map((member) => member.weight)).toEqual([1, 1, 1])
  })

  it('keeps a pool to one vendor and one API', () => {
    const family = { vendor: 'anthropic', apiShape: 'anthropicMessages' as const }
    expect(sameFamily(opus, family)).toBe(true)
    expect(sameFamily(poolCandidates.models[2], family)).toBe(false)
  })

  it('drains one member and keeps every other setting, including display names', () => {
    const specs = specsWithMemberDrained(anthropicPool, 'poolmodel_opus', opusEastUs2.modelEndpointId, 'Claude-Opus-4-5', true)

    expect(specs).toHaveLength(1)
    expect(specs[0].displayName).toBe('Claude Opus 4.5')
    expect(specs[0].members.map((member) => member.drained)).toEqual([true, false, true])
    expect(specs[0].members.map((member) => member.weight)).toEqual([2, 1, 2])
  })
})

describe('checking pool models', () => {
  const lookup = deploymentLookup(poolCandidates.models)

  function opusDraft(overrides: Partial<DraftPoolModel> = {}): DraftPoolModel {
    return { ...draftsFromPool(anthropicPool)[0], apiShape: 'anthropicMessages', ...overrides }
  }

  it('finds nothing wrong with a sound model', () => {
    expect(checkDrafts([opusDraft()], 'breaker', lookup)).toEqual({ problems: [], cautions: [] })
  })

  it('refuses to save a model whose deployments serve different versions, unless allowed', () => {
    const mixed = deploymentLookup([
      {
        ...poolCandidates.models[0],
        deployments: [opusEastUs2, { ...opusNorthCentral, modelVersion: '2' }, opusWestUs3],
      },
    ])

    expect(checkDrafts([opusDraft()], 'breaker', mixed).problems).toEqual([
      'Claude Opus 4.5: the deployments serve different versions (1, 2). Choose deployments of one version, or allow mixed versions.',
    ])
    expect(checkDrafts([opusDraft({ allowMixedVersions: true })], 'breaker', mixed).problems).toEqual([])
  })

  it('warns before publishing when a body-routed breaker model has differently named deployments', () => {
    const draft = opusDraft({
      members: [
        { modelEndpointId: 'endpoint_eastus2', deploymentName: 'opus-a', weight: 1, drained: false },
        { modelEndpointId: 'endpoint_westus3', deploymentName: 'opus-b', weight: 1, drained: false },
      ],
    })

    expect(checkDrafts([draft], 'breaker').cautions[0]).toMatch(/must share one deployment name/)
    expect(checkDrafts([draft], 'linear').cautions).toEqual([])
  })

  it('warns when every deployment is drained, and refuses a model with none', () => {
    const drained = opusDraft({
      members: opusDraft().members.map((member) => ({ ...member, drained: true })),
    })

    expect(checkDrafts([drained], 'breaker', lookup).cautions[0]).toMatch(/every deployment is drained/)
    expect(checkDrafts([opusDraft({ members: [] })], 'breaker', lookup).problems[0]).toMatch(/choose at least one deployment/)
  })

  it('refuses two models callers would name the same way', () => {
    const check = checkDrafts([opusDraft(), opusDraft({ displayName: 'Opus copy', publicName: 'CLAUDE-OPUS-4-5' })], 'breaker', lookup)

    expect(check.problems).toEqual([
      'Claude Opus 4.5: Another model in this pool already uses this name.',
      'Opus copy: Another model in this pool already uses this name.',
    ])
  })

  it('refuses a linear model with more active deployments than a request can try', () => {
    const members = Array.from({ length: 11 }, (_, index) => ({
      modelEndpointId: `endpoint_${index}`,
      deploymentName: 'claude-opus-4-5',
      weight: 1,
      drained: false,
    }))

    expect(checkDrafts([opusDraft({ members })], 'linear').problems[0]).toMatch(/at most 10 deployments per request/)
  })

  it('refuses a weight the gateway would not accept', () => {
    const members = opusDraft().members.map((member, index) => ({ ...member, weight: index === 0 ? 0 : member.weight }))

    expect(checkDrafts([opusDraft({ members })], 'breaker', lookup).problems).toEqual([
      'Claude Opus 4.5: give claude-opus-4-5 a whole-number weight from 1 to 100.',
    ])
  })
})

describe('pool summaries', () => {
  it('reports the worst readiness first', () => {
    expect(readinessSummary({ ready: 2, cannotInvoke: 1 })).toEqual({ label: '1 cannot invoke', tone: 'danger' })
    expect(readinessSummary({ ready: 2, notConfirmed: 1 })).toEqual({ label: '1 not confirmed', tone: 'warning' })
    expect(readinessSummary({ ready: 2 })).toEqual({ label: 'All ready', tone: 'success' })
    expect(readinessSummary({})).toEqual({ label: 'No active members', tone: 'muted' })
  })

  it('counts active members by the capacity they buy, and leaves an empty pool to the readiness column', () => {
    expect(capacitySummary({ provisioned: 1, payAsYouGo: 2 })).toBe('1 provisioned · 2 pay-as-you-go')
    expect(capacitySummary({ unknown: 1 })).toBe('1 unknown')
    expect(capacitySummary({})).toBe('—')
  })

  it('reads the problems a refused plan lists', () => {
    const refused = { status: 409, body: { details: { problems: ['Add at least one model to the pool.'] } } }

    expect(planProblems(refused)).toEqual(['Add at least one model to the pool.'])
    expect(planProblems({ status: 500, body: { details: { problems: ['x'] } } })).toEqual([])
    expect(planProblems(new Error('boom'))).toEqual([])
  })
})

describe('safeguard tier', () => {
  it('needs a v2 tier only for the Anthropic Messages API', () => {
    expect(safeguardTierNote('anthropicMessages', 'StandardV2')).toBeNull()
    expect(safeguardTierNote('anthropicMessages', ' premiumv2 ')).toBeNull()
    expect(safeguardTierNote('azureOpenAi', 'Developer')).toBeNull()
    expect(safeguardTierNote(null, 'Consumption')).toBeNull()
  })

  it('says why each other tier cannot apply it', () => {
    expect(safeguardTierNote('anthropicMessages', 'Premium')).toMatch(/uses the Premium tier, a classic tier/)
    expect(safeguardTierNote('anthropicMessages', 'Consumption')).toMatch(/uses the Consumption tier/)
    expect(safeguardTierNote('anthropicMessages', null)).toMatch(/hasn't read this gateway's tier/)
    expect(safeguardTierNote('anthropicMessages', 'SomethingNew')).toMatch(/hasn't read this gateway's tier/)
  })
})

describe('safeguard fields', () => {
  it('round-trips a saved safeguard and treats empty fields as no limit', () => {
    const fields = safeguardFields({ tokensPerMinute: 200000, tokenQuota: 1000000, tokenQuotaPeriod: 'Monthly' })
    expect(fields).toEqual({ tokensPerMinute: '200000', tokenQuota: '1000000', tokenQuotaPeriod: 'Monthly' })
    expect(safeguardFromFields(fields)).toEqual({
      safeguard: { tokensPerMinute: 200000, tokenQuota: 1000000, tokenQuotaPeriod: 'Monthly' },
      problem: null,
    })
    expect(safeguardFromFields(safeguardFields(null))).toEqual({ safeguard: null, problem: null })
    expect(safeguardFromFields({ tokensPerMinute: ' 500 ', tokenQuota: '', tokenQuotaPeriod: '' })).toEqual({
      safeguard: { tokensPerMinute: 500, tokenQuota: null, tokenQuotaPeriod: null },
      problem: null,
    })
  })

  it('refuses values the API would refuse', () => {
    const empty = { tokensPerMinute: '', tokenQuota: '', tokenQuotaPeriod: '' as const }
    expect(safeguardFromFields({ ...empty, tokensPerMinute: '0' }).problem).toMatch(/tokens per minute as a whole number/)
    expect(safeguardFromFields({ ...empty, tokensPerMinute: '1.5' }).problem).toMatch(/whole number/)
    expect(safeguardFromFields({ ...empty, tokenQuota: 'lots' }).problem).toMatch(/token quota as a whole number/)
    expect(safeguardFromFields({ ...empty, tokenQuota: '1000' }).problem).toMatch(/Choose the period/)
    expect(safeguardFromFields({ ...empty, tokenQuotaPeriod: 'Daily' }).problem).toMatch(/Enter a token quota/)
  })

  it('describes a safeguard in words', () => {
    expect(describeSafeguard(anthropicPool.safeguard)).toBe('200,000 tokens per minute, for each model')
    expect(describeSafeguard({ tokensPerMinute: 1000, tokenQuota: 50000, tokenQuotaPeriod: 'Daily' })).toBe(
      '1,000 tokens per minute and 50,000 tokens per day, for each model',
    )
    expect(describeSafeguard(null)).toBe('None')
  })
})

describe('pool requests', () => {
  const base = 'https://gateway.example.test/mosaic/pool-claude/'

  it('puts the model in the route for Azure OpenAI', () => {
    const example = poolRequestExample('azureOpenAi', base, 'gpt-4o')

    expect(example?.url).toBe(
      'https://gateway.example.test/mosaic/pool-claude/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21',
    )
    expect(example?.body).toBeNull()
  })

  it('puts the model in the body for the other APIs', () => {
    expect(poolRequestExample('foundryModels', base, 'mistral-large')?.body).toContain('"model": "mistral-large"')
    const anthropic = poolRequestExample('anthropicMessages', base, 'claude-opus-4-5')

    expect(anthropic?.url).toBe('https://gateway.example.test/mosaic/pool-claude/anthropic/v1/messages')
    expect(anthropic?.body).toContain('"model": "claude-opus-4-5"')
    expect(anthropic?.note).toContain('https://gateway.example.test/mosaic/pool-claude/anthropic as the base URL')
  })

  it('has no example before the pool serves an API', () => {
    expect(poolRequestExample(null, base, 'x')).toBeNull()
  })
})

describe('pool gateways', () => {
  it('publishes and unpublishes only on a gateway MOSAIC manages and can write to', () => {
    expect(poolPublishBlocker(poolGateway)).toBeNull()
    expect(poolWriteBlocker(poolGateway)).toBeNull()
    expect(poolPublishBlocker(undefined)).toBeNull()
    expect(poolWriteBlocker(null)).toBeNull()

    expect(poolPublishBlocker(observedGateway)).toMatch(/only observes this gateway/)
    expect(poolWriteBlocker(observedGateway)).toMatch(/can’t publish or unpublish the pool/)

    const readOnly = { ...poolGateway, access: { ...poolGateway.access, canWrite: false } }
    expect(poolPublishBlocker(readOnly)).toMatch(/Save the pool as a draft/)
    expect(poolWriteBlocker(readOnly)).toMatch(/Grant write access/)
  })
})

describe('pool members', () => {
  const members = anthropicPoolDetail.models[0].members

  it('splits a priority group’s requests by weight, and gives drained members none', () => {
    expect(memberShares('breaker', members)).toEqual([67, 33, null])
  })

  it('gives each overflow group its own split', () => {
    const groups = [
      { weight: 3, drained: false, priority: 1 },
      { weight: 1, drained: false, priority: 2 },
      { weight: 1, drained: false, priority: 2 },
    ]

    expect(memberShares('preferential', groups)).toEqual([100, 50, 50])
  })

  it('has no split in a linear pool, which tries members in order', () => {
    expect(memberShares('linear', members)).toEqual([null, null, null])
  })

  it('shows the order a linear pool tries a member in, or its priority group', () => {
    expect(memberPosition('linear', { order: 2, priority: 1 })).toBe('2')
    expect(memberPosition('preferential', { order: 2, priority: 1 })).toBe('1')
    expect(memberPosition('linear', { order: null, priority: 1 })).toBe('—')
    expect(memberPosition('breaker', {})).toBe('—')
  })

  it('describes retries in words', () => {
    expect(describeRetries('breaker', 0)).toBe('None. Each request makes one attempt.')
    expect(describeRetries('breaker', 1)).toBe('Up to 1 retry, on another deployment')
    expect(describeRetries('preferential', 3)).toBe('Up to 3 retries, each on another deployment')
    expect(describeRetries('linear', 3)).toBe('Every active deployment, in order, until one answers')
  })
})

describe('pool runs', () => {
  it('formats how long a run took', () => {
    expect(formatRunDuration(850)).toBe('850 ms')
    expect(formatRunDuration(999.4)).toBe('999 ms')
    expect(formatRunDuration(999.5)).toBe('1.0 s')
    expect(formatRunDuration(20_000)).toBe('20.0 s')
    expect(formatRunDuration(59_949)).toBe('59.9 s')
    expect(formatRunDuration(59_950)).toBe('1 min')
    expect(formatRunDuration(125_000)).toBe('2 min 5 s')
    expect(formatRunDuration(null)).toBe('—')
    expect(formatRunDuration(-1)).toBe('—')
  })

  it('tells a run that unpublished the pool from one that published it', () => {
    const deletes = poolRun.steps.map((step) => ({ ...step, action: 'delete' as const }))

    expect(poolRunOperation(poolRun)).toBe('publish')
    expect(poolRunOperation({ steps: deletes })).toBe('unpublish')
    expect(poolRunOperation({ steps: [] })).toBe('publish')
  })

  it('lists the newest run first without reordering the original', () => {
    const older = { ...poolRun, id: 'older', createdAt: '2026-09-01T10:00:00Z' }
    const newer = { ...poolRun, id: 'newer', createdAt: '2026-09-04T10:00:00Z' }
    const runs = [older, poolRun, newer]

    expect(newestRunsFirst(runs).map((run) => run.id)).toEqual(['newer', poolRun.id, 'older'])
    expect(runs.map((run) => run.id)).toEqual(['older', poolRun.id, 'newer'])
  })
})
