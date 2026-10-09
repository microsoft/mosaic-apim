import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { test } from 'node:test'
import {
  type IndependentModelEvidence, type RuntimeEvidence, type NotificationDeliveryEvidence,
  governmentEmailEvidence, notificationDeliveryEvidence, propagationEvidence, selectionEvidence,
} from '../src/model-evidence.ts'
import { modelScope, observation } from './model-fixtures.ts'

function selection() {
  const scope = modelScope()
  const success = [
    ['entra-default-explicit', scope.selection.default],
    ['entra-other-case-insensitive', scope.selection.other],
    ['entra-default-implicit', scope.selection.default],
    ['key-own-case-insensitive', scope.selection.default],
    ['key-implicit', scope.selection.default],
  ] as const
  const denied = [['entra-unknown', 'cost-center'], ['entra-malformed', 'cost-center'], ['key-other', 'cost-center-mismatch']] as const
  const runtime: RuntimeEvidence = { journey: 'R10', scopeSha256: 'offline-only',
    observations: [...success.map(([label, g]) => observation(g, label, 200)), ...denied.map(([label]) => observation(scope.selection.default, label, 403))] }
  const evidence: IndependentModelEvidence = { scopeSha256: 'offline-only',
    attribution: success.map(([label, g]) => ({ source: 'logAnalytics', requestId: `request-${label}`, grantId: g.id, costCenterId: g.costCenterId })),
    backend: success.map(([label]) => ({ source: 'backend-capture', requestId: `request-${label}`, headerNames: ['Content-Type', 'api-key'] })),
    denials: denied.map(([label, reason]) => ({ source: 'gateway-trace', requestId: `request-${label}`, message: `mosaic-deny v=1 r=${reason}` })) }
  return { scope, runtime, evidence }
}

test('R10 independently correlated Analytics, stripping and denial evidence passes; wrong grant, leaked header, wrong denial and uncorrelated data fail', () => {
  const good = selection()
  selectionEvidence(good.scope, good.runtime, good.evidence)
  const badGrant = selection()
  badGrant.evidence.attribution![0].grantId = badGrant.scope.selection.other.id
  assert.throws(() => selectionEvidence(badGrant.scope, badGrant.runtime, badGrant.evidence), /exact selected grant/)
  const leaked = selection()
  leaked.evidence.backend![0].headerNames.push('X-MOSAIC-COST-CENTER')
  assert.throws(() => selectionEvidence(leaked.scope, leaked.runtime, leaked.evidence), /on-wire/)
  const wrongDenial = selection()
  wrongDenial.evidence.denials![0].message = 'mosaic-deny v=1 r=no-grant'
  assert.throws(() => selectionEvidence(wrongDenial.scope, wrongDenial.runtime, wrongDenial.evidence), /denial trace/)
  const noCorrelation = selection()
  noCorrelation.runtime.observations[0].requestId = undefined
  assert.throws(() => selectionEvidence(noCorrelation.scope, noCorrelation.runtime, noCorrelation.evidence), /correlated/)
})

function propagation() {
  const scope = modelScope()
  const g = scope.budget!.grant
  const first403 = { ...observation(g, 'budget-block-probe-1', 403), at: '2026-10-01T12:00:05Z' }
  const firstSuccess = { ...observation(g, 'budget-raise-probe-1', 200), at: '2026-10-01T12:01:05Z' }
  const runtime: RuntimeEvidence = { journey: 'R12', scopeSha256: 'offline-only',
    observations: [first403, firstSuccess, observation(scope.selection.other, 'budget-other-cost-center', 200)],
    timing: { blockSave: { startedAt: '2026-10-01T12:00:00Z', savedAt: '2026-10-01T12:00:04Z' },
      raiseSave: { startedAt: '2026-10-01T12:01:00Z', savedAt: '2026-10-01T12:01:04Z' },
      first403At: first403.at, firstSuccessAt: firstSuccess.at } }
  const key = createHash('sha256').update(g.costCenterId).digest('hex').slice(0, 12)
  const evidence: IndependentModelEvidence = { scopeSha256: 'offline-only',
    denials: [{ source: 'gateway-trace', requestId: first403.requestId!, message: 'mosaic-deny v=1 r=budget' }],
    namedValues: scope.budget!.managedGateways.flatMap((gateway) => [
      { source: 'arm-get', phase: 'before', gatewayId: gateway.id, secret: false, value: 'aaaaaaaaaaaa', observedAt: '2026-10-01T11:59:00Z' },
      { source: 'arm-get', phase: 'block', gatewayId: gateway.id, secret: false, value: `aaaaaaaaaaaa,${key}`, observedAt: '2026-10-01T12:00:04Z', writeAuditAt: '2026-10-01T12:00:02Z', auditAction: 'gateway.blockedCostCentersUpdated' },
      { source: 'arm-get', phase: 'raise', gatewayId: gateway.id, secret: false, value: 'aaaaaaaaaaaa', observedAt: '2026-10-01T12:01:04Z', writeAuditAt: '2026-10-01T12:01:02Z', auditAction: 'gateway.blockedCostCentersUpdated' },
    ]) }
  return { scope, runtime, evidence }
}

test('R12 measures the called classic gateway only, requires ARM and write audit on all gateways, and preserves other blocked keys', () => {
  const good = propagation()
  assert.deepEqual(propagationEvidence(good.scope, good.runtime, good.evidence), {
    timings: [{ gatewayId: good.scope.gatewayId, tier: 'classic', writeToFirst403Ms: 3000, writeToFirstSuccessMs: 3000 }], unavailableV2: true,
  })
  const missing = propagation()
  missing.evidence.namedValues!.pop()
  assert.throws(() => propagationEvidence(missing.scope, missing.runtime, missing.evidence), /ALL managed gateways/)
  const broad = propagation()
  broad.evidence.namedValues![2].value = '-'
  assert.throws(() => propagationEvidence(broad.scope, broad.runtime, broad.evidence), /preserve other/)
  const cached = propagation()
  cached.evidence.namedValues![1].writeAuditAt = undefined
  assert.throws(() => propagationEvidence(cached.scope, cached.runtime, cached.evidence), /timestamp/)
})

function government(): IndependentModelEvidence {
  return { scopeSha256: 'offline-only', government: {
    cloud: 'government', resourceDeployed: true, domainDeployed: true, managedIdentity: true,
    tokenScope: 'https://communication.azure.us/.default', apiVersion: '2023-03-31',
    attempts: [{ operationId: 'offline-operation', status: 202, at: '2026-10-01T12:00:00Z' }, { operationId: 'offline-operation', status: 202, at: '2026-10-01T12:01:00Z' }],
    receipts: [{ operationId: 'offline-operation', messageId: 'offline-message', receivedAt: '2026-10-01T12:02:00Z' }],
  } }
}
test('R13 rejects Commercial/source-only, ACS acceptance without delivery, different Operation-Id and duplicate mailbox receipts', () => {
  governmentEmailEvidence(government()) // Validates fixture semantics, never a claim that mail was sent.
  const commercial = government()
  commercial.government!.cloud = 'commercial'
  assert.throws(() => governmentEmailEvidence(commercial), /actual Azure Government/)
  const wrongScope = government()
  wrongScope.government!.tokenScope = 'https://communication.azure.com/.default'
  assert.throws(() => governmentEmailEvidence(wrongScope))
  const noDelivery = government()
  noDelivery.government!.receipts = []
  assert.throws(() => governmentEmailEvidence(noDelivery), /not delivery/)
  const duplicate = government()
  duplicate.government!.receipts.push({ ...duplicate.government!.receipts[0], messageId: 'duplicate-message' })
  assert.throws(() => governmentEmailEvidence(duplicate))
  const different = government()
  different.government!.attempts[1].operationId = 'different-operation'
  assert.throws(() => governmentEmailEvidence(different), /same Operation-Id/)
})

function notices(oneCent: boolean): NotificationDeliveryEvidence {
  const thresholds = oneCent ? [100] : [80, 100]
  const before: NotificationDeliveryEvidence['noticesBeforeRecheck'] = [
    ...thresholds.map((threshold) => ({ id: `notice-${threshold}`, kind: 'threshold' as const, threshold, status: 'sent', operationId: `op-${threshold}` })),
    { id: 'block', kind: 'blocked', status: 'sent', operationId: 'op-block' },
    { id: 'raise', kind: 'unblocked', status: 'sent', operationId: 'op-raise' },
  ]
  return { amount: oneCent ? 0.01 : 1,
    checks: [{ spend: oneCent ? 0.01 : 0.8, through: '2026-10-01T12:00:00Z' }, { spend: oneCent ? 0.01 : 1, through: '2026-10-01T12:01:00Z' }],
    noticesBeforeRecheck: before, noticesAfterRecheck: before.map((n) => n.id),
    receipts: before.map((n) => ({ operationId: n.operationId, messageId: `message-${n.id}`, receivedAt: '2026-10-01T12:02:00Z' })) }
}
test('R14 accepts staged 80/100 delivery or one-cent highest-only, rejects fabricated 80%, skipped sends, duplicate receipts and repeat notifications', () => {
  notificationDeliveryEvidence(notices(false))
  notificationDeliveryEvidence(notices(true))
  const false80 = notices(true)
  false80.noticesBeforeRecheck.push({ id: 'notice-80', kind: 'threshold', threshold: 80, status: 'sent', operationId: 'op-80' })
  assert.throws(() => notificationDeliveryEvidence(false80), /highest threshold/)
  const skipped = notices(false)
  skipped.noticesBeforeRecheck[0].status = 'skipped'
  assert.throws(() => notificationDeliveryEvidence(skipped), /skipped is not delivery/)
  const duplicate = notices(false)
  duplicate.receipts.push({ ...duplicate.receipts[0], messageId: 'duplicate' })
  assert.throws(() => notificationDeliveryEvidence(duplicate), /exactly one/)
  const recheck = notices(false)
  recheck.noticesAfterRecheck.push('duplicate-notice')
  assert.throws(() => notificationDeliveryEvidence(recheck), /dedupe/)
})
