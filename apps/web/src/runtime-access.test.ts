import { describe, expect, it } from 'vitest'
import {
  CAN_INVOKE,
  CANNOT_INVOKE,
  NOT_CONFIRMED,
  describeScope,
  findingSummary,
  runtimeVerdict,
  type RuntimeVerdict,
} from './runtime-access'
import type {
  GatewayRuntimeAccess,
  RuntimeAccessEvaluation,
  RuntimeAccessReason,
  RuntimeRoleFinding,
} from './types'

const SUBSCRIPTION = '/subscriptions/00000000-0000-0000-0000-000000000000'
const ACCOUNT =
  `${SUBSCRIPTION}/resourceGroups/rg-contoso-ai` +
  '/providers/Microsoft.CognitiveServices/accounts/contoso-aoai'

function access(
  reason: RuntimeAccessReason | null,
  evaluation: RuntimeAccessEvaluation,
  canInvoke = false,
): GatewayRuntimeAccess {
  return {
    gatewayId: 'gateway_1',
    gatewayName: 'Development gateway',
    canInvoke,
    evaluation,
    reason,
    inherited: false,
  }
}

function finding(overrides: Partial<RuntimeRoleFinding>): RuntimeRoleFinding {
  return {
    kind: 'sufficient',
    roleName: 'Foundry User',
    roleDefinitionId: '53ca6127-db72-4b80-b1b0-d745d6d5456d',
    scope: ACCOUNT,
    inherited: false,
    missingDataActions: [],
    ...overrides,
  }
}

describe('runtimeVerdict', () => {
  it.each<[RuntimeAccessReason, RuntimeAccessEvaluation, boolean, RuntimeVerdict]>([
    ['granted', 'roleAssignments', true, CAN_INVOKE],
    ['missingRole', 'roleAssignments', false, CANNOT_INVOKE],
    ['narrowerScope', 'roleAssignments', false, CANNOT_INVOKE],
    ['noGatewayIdentity', 'noGatewayIdentity', false, CANNOT_INVOKE],
    ['networkUnreachable', 'roleAssignments', false, CANNOT_INVOKE],
    ['denyAssignment', 'roleAssignments', false, CANNOT_INVOKE],
    // Whatever MOSAIC cannot read or evaluate is "not confirmed", never a denial.
    ['denyAssignment', 'notEvaluated', false, NOT_CONFIRMED],
    ['conditional', 'notEvaluated', false, NOT_CONFIRMED],
    ['roleUnreadable', 'notEvaluated', false, NOT_CONFIRMED],
    ['assignmentsUnreadable', 'notEvaluated', false, NOT_CONFIRMED],
    ['identityNotObserved', 'notEvaluated', false, NOT_CONFIRMED],
    ['networkUnverified', 'notEvaluated', false, NOT_CONFIRMED],
  ])('%s (%s) reads as the expected verdict', (reason, evaluation, canInvoke, expected) => {
    expect(runtimeVerdict(access(reason, evaluation, canInvoke))).toBe(expected)
  })

  it('falls back to evaluation and canInvoke for results recorded before reasons existed', () => {
    expect(runtimeVerdict(access(null, 'roleAssignments', true))).toBe(CAN_INVOKE)
    expect(runtimeVerdict(access(null, 'roleAssignments', false))).toBe(CANNOT_INVOKE)
    expect(runtimeVerdict(access(null, 'notEvaluated', false))).toBe(NOT_CONFIRMED)
  })
})

describe('describeScope', () => {
  it.each([
    [ACCOUNT, 'contoso-aoai'],
    [`${ACCOUNT}/projects/team-a`, 'project team-a'],
    [`${SUBSCRIPTION}/resourceGroups/rg-contoso-ai`, 'resource group rg-contoso-ai'],
    [SUBSCRIPTION, 'subscription 00000000-0000-0000-0000-000000000000'],
    ['/providers/Microsoft.Management/managementGroups/contoso', 'management group contoso'],
    ['/', 'the root scope'],
  ])('names %s as %s', (scope, expected) => {
    expect(describeScope(scope)).toBe(expected)
  })
})

describe('findingSummary', () => {
  it('names the first data action a role lacks', () => {
    expect(
      findingSummary(
        finding({
          kind: 'insufficient',
          roleName: 'Reader',
          missingDataActions: [
            'Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action',
            'Microsoft.CognitiveServices/accounts/OpenAI/responses/write',
          ],
        }),
      ),
    ).toBe(
      'Reader at contoso-aoai does not grant ' +
        'Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action and 1 more.',
    )
  })

  it('explains a grant below the resource the published API calls', () => {
    expect(
      findingSummary(finding({ kind: 'narrowerScope', scope: `${ACCOUNT}/projects/team-a` })),
    ).toBe(
      'Foundry User at project team-a is assigned below the resource the published API calls, ' +
        'so it does not apply there.',
    )
  })

  it('does not count a conditional grant as access', () => {
    expect(findingSummary(finding({ kind: 'conditional' }))).toMatch(
      /under an ABAC condition MOSAIC cannot prove holds for these calls/,
    )
  })

  it('does not present an unreadable role as a denial', () => {
    expect(
      findingSummary(finding({ kind: 'unreadable', roleName: null, roleDefinitionId: 'abc' })),
    ).toBe('MOSAIC cannot read what the role abc at contoso-aoai grants. This is not a denial.')
  })
})
