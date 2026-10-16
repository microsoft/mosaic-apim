import { Badge, Button, Checkbox, Field, Input, MessageBar, MessageBarBody, Select, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useMosaicApi } from '../api'
import type { AiApprovalRecommendation, ApprovalPreview, BulkApprovalInput, BulkApprovalItemResult } from '../admin-types'
import type { CostCenter, Principal } from '../types'
import { buildEnforcement, describeLimits, emptyLimitForm, type LimitForm } from '../entitlement-limits'
import { ErrorState, Loading } from './AsyncState'
import { LimitFields } from './GovernanceFields'
import { limitInputError, unresolvedRequestIds } from '../admin-governance'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './AdminGovernance.module.css'

interface ItemDraft { included: boolean; explicitLimits: boolean; limits: LimitForm; costCenterId: string; note: string; confirmedMove: boolean }
function initialDraft(item: ApprovalPreview): ItemDraft {
  return { included: item.blockers.length === 0 && !item.mutationLocked, explicitLimits: false, limits: { ...emptyLimitForm }, costCenterId: item.effectiveCostCenterId ?? '', note: '', confirmedMove: false }
}
function moved(item: ApprovalPreview) { return (item.request.requestedEnvironment ?? null) !== (item.currentEnvironment ?? null) }
export function RecommendationEvidence({ value, contextRevision }: { value: AiApprovalRecommendation; contextRevision: string }) {
  const stale = value.context.contextRevision !== contextRevision || new Date(value.expiresAt).getTime() <= Date.now()
  return <section className={styles.evidenceSection} aria-label="AI advisory evidence">
    <Title3 as="h4">AI recommendation · advisory only</Title3>
    <Text>{stale ? 'Expired or changed context — request a fresh recommendation.' : value.recommendation.suggestedOutcome}</Text>
    <Text>Model: {value.modelResource.kind} · {value.modelResource.id}. Generated {new Date(value.generatedAt).toLocaleString()}. Expires {new Date(value.expiresAt).toLocaleString()}.</Text>
    <Text>AI cannot approve, deny, change limits, or apply a plan. Deterministic blockers remain authoritative.</Text>
    <ul>{value.recommendation.reasons.map((reason, i) => <li key={i} className={styles.evidence}>{reason.explanation}<br />Evidence: {reason.evidenceIds.join(', ')}</li>)}</ul>
    {value.recommendation.missingInformation.length > 0 && <><strong>Missing information</strong><ul>{value.recommendation.missingInformation.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
    {value.recommendation.riskFlags.length > 0 && <><strong>Risk flags</strong><ul>{value.recommendation.riskFlags.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
    {value.deterministicBlockers.map(b => <Text key={b.code}>{b.message} {b.nextAction}</Text>)}
  </section>
}
function RequestRecommendation({ requestId, contextRevision, generated }: { requestId: string; contextRevision: string; generated?: AiApprovalRecommendation }) {
  const api = useMosaicApi()
  const existing = useQuery({
    queryKey: ['approval-recommendations', requestId, contextRevision],
    queryFn: () => api.getApprovalRecommendations(requestId),
    enabled: !generated,
    retry: false,
  })
  const value = generated ?? existing.data?.slice().sort((a, b) => b.generatedAt.localeCompare(a.generatedAt))[0]
  if (value) return <RecommendationEvidence value={value} contextRevision={contextRevision} />
  if (existing.isError) return <div className={styles.stack}><ErrorState error={existing.error} /><Button onClick={() => void existing.refetch()}>Retry saved recommendations</Button></div>
  return null
}
export function BulkApprovalWorkspace({ requestIds, principals, costCenters, onBack, onComplete }: {
  requestIds: string[]; principals: Principal[]; costCenters: CostCenter[]; onBack: () => void; onComplete: () => void
}) {
  const api = useMosaicApi()
  const [ids, setIds] = useState(requestIds)
  const preview = useQuery({ queryKey: ['bulk-approval-preview', ids], queryFn: () => api.previewBulkApprovals(ids), retry: false, staleTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false })
  const [drafts, setDrafts] = useState<Record<string, ItemDraft>>({})
  const [shared, setShared] = useState<LimitForm>({ ...emptyLimitForm })
  const [results, setResults] = useState<BulkApprovalItemResult[] | null>(null)
  const [recommendations, setRecommendations] = useState<Record<string, AiApprovalRecommendation>>({})
  const retryPayload = useRef<BulkApprovalInput | null>(null)
  const ai = useMutation({
    mutationFn: (requestId: string) => api.recommendApprovals([requestId]),
    onSuccess: items => setRecommendations(previous => ({ ...previous, ...Object.fromEntries(items.filter(i => i.recommendation).map(i => [i.requestId, i.recommendation!])) })),
  })
  const execute = useMutation({ mutationFn: (body: BulkApprovalInput) => api.approveBulkRequests(body), onSuccess: value => { setResults(value.items); retryPayload.current = null; onComplete() } })
  const items = preview.data?.items ?? []
  const draftFor = (item: ApprovalPreview) => drafts[item.request.id] ?? initialDraft(item)
  const update = (item: ApprovalPreview, change: Partial<ItemDraft>) => {
    retryPayload.current = null
    setDrafts(previous => ({ ...previous, [item.request.id]: { ...(previous[item.request.id] ?? initialDraft(item)), ...change } }))
  }
  const selected = items.filter(item => draftFor(item).included)
  const compatible = selected.length > 0 && new Set(selected.map(item => item.request.resource.kind === 'mcpServer' ? 'mcp' : 'model')).size === 1
  const invalid = selected.some(item => {
    const draft = draftFor(item)
    return item.blockers.length > 0 || item.mutationLocked || (moved(item) && !draft.confirmedMove) || (draft.explicitLimits && (limitInputError(draft.limits) || !buildEnforcement(draft.limits, true)))
  })
  const outcomes = useMemo(() => results ? {
    approved: results.filter(i => i.status === 'approved').length, existing: results.filter(i => i.status === 'alreadyApproved').length, unresolved: unresolvedRequestIds(results),
  } : null, [results])
  function approve() {
    if (!selected.length || invalid) return
    const body = retryPayload.current ?? {
      idempotencyKey: crypto.randomUUID(),
      items: selected.map(item => {
        const draft = draftFor(item)
        return { requestId: item.request.id, expectedContext: item.revision, approval: {
          note: draft.note.trim() || null,
          ...(draft.costCenterId ? { costCenterId: draft.costCenterId } : {}),
          ...(draft.explicitLimits ? { enforcement: buildEnforcement(item.request.resource.kind === 'mcpServer' ? { ...draft.limits, tokensPerMinute: '', tokenQuota: '' } : draft.limits, true) } : {}),
          ...(moved(item) && draft.confirmedMove ? { confirmedEnvironment: item.currentEnvironment ?? 'unclassified' } : {}),
        } }
      }),
    }
    retryPayload.current = body
    execute.mutate(body)
  }
  function reviewUnresolved() {
    const unresolved = outcomes?.unresolved ?? []
    if (!unresolved.length) return
    setIds(unresolved); setResults(null); setDrafts({}); setRecommendations({}); execute.reset()
    if (JSON.stringify(unresolved) === JSON.stringify(ids)) void preview.refetch()
  }
  return <WorkspaceEditorFrame title={results ? 'Bulk approval results' : 'Review bulk approvals'} onBack={onBack}
    description="Each request is independent. Approvals create grant intent; model, pool, and MCP gateway plans still need review and apply."
    dirty={!results && Object.keys(drafts).length > 0} pending={execute.isPending}
    pendingLabel="Approving independently — successful items will not be rolled back"
    error={execute.error ? <ErrorState error={execute.error} /> : undefined}
    primaryAction={!results ? { label: `Approve ${selected.length} selected`, onClick: approve, disabled: !preview.isSuccess || !selected.length || Boolean(invalid) } : undefined}>
    <div className={styles.stack}>
      {preview.isPending && <Loading label="Checking current request context and inherited limits" />}
      {preview.isError && <><ErrorState error={preview.error} /><Button onClick={() => void preview.refetch()}>Retry preview</Button></>}
      {results && outcomes ? <>
        <MessageBar intent={outcomes.unresolved.length ? 'warning' : 'success'}><MessageBarBody>
          {outcomes.approved} approved · {outcomes.existing} already approved · {outcomes.unresolved.length} unresolved. Successful grants still require plan review/apply.
        </MessageBarBody></MessageBar>
        {results.map(result => <section key={result.requestId} className={styles.section}>
          <Title3 as="h3">{items.find(i => i.request.id === result.requestId)?.resourceSummary?.displayName ?? 'Request outcome'}</Title3>
          <Text>Request {result.requestId} · <Badge>{result.status}</Badge></Text>
          <Text>{result.message} {result.nextAction}</Text>
          {result.entitlementId && <RouterLink to={`/entitlements?tab=grants&selected=${encodeURIComponent(result.entitlementId)}`}>Review saved grant</RouterLink>}
          {result.requiresPlanApply && <Text>Gateway access is not ready until the affected plan is applied.</Text>}
        </section>)}
        {outcomes.unresolved.length > 0 && <Button appearance="primary" onClick={reviewUnresolved}>Review unresolved requests only</Button>}
      </> : <>
        {execute.isError && <Text>Outcome unknown after a network failure? Retry unchanged to reuse the same idempotency key. Do not resubmit successful items as a new batch.</Text>}
        <Text>AI evidence is advisory only. Generating a recommendation makes a governed model call charged to its dedicated grant and cost center; it never makes the approval decision.</Text>
        <section className={styles.section}>
          <Title3 as="h3">Compatible shared limits</Title3>
          <Text>Shared limits are explicit overrides for included requests only. Heterogeneous model and MCP selections cannot share token settings.</Text>
          <LimitFields value={shared} onChange={setShared} mcp={selected.every(i => i.request.resource.kind === 'mcpServer')} />
          <Button disabled={!compatible || Boolean(limitInputError(shared)) || !buildEnforcement(shared, true)} onClick={() => selected.forEach(item => update(item, { explicitLimits: true, limits: item.request.resource.kind === 'mcpServer' ? { ...shared, tokensPerMinute: '', tokenQuota: '' } : { ...shared } }))}>Apply limits to compatible selection</Button>
        </section>
        {preview.data?.unavailable.map(item => <MessageBar intent="error" key={item.requestId}><MessageBarBody>Request {item.requestId}: {item.blocker.message} {item.blocker.nextAction}</MessageBarBody></MessageBar>)}
        {items.map(item => {
          const draft = draftFor(item)
          const requester = principals.find(p => p.id === item.request.requesterPrincipalId || p.objectId === item.request.requesterObjectId)
          return <section key={item.request.id} className={styles.section} aria-label={`Review ${item.resourceSummary?.displayName ?? 'request'}`}>
            <div className={styles.toolbar}>
              <Checkbox label={`Include ${item.resourceSummary?.displayName ?? 'request'}`} checked={draft.included} disabled={item.blockers.length > 0 || item.mutationLocked} onChange={(_, d) => update(item, { included: Boolean(d.checked) })} />
              <Badge appearance="outline">{item.production ? 'Production' : item.currentEnvironment ?? 'Unclassified'}</Badge>
            </div>
            <dl className={styles.facts}>
              <div><dt>Requester</dt><dd>{requester?.label ?? 'Requester name unavailable'}</dd></div>
              <div><dt>Resource</dt><dd>{item.resourceSummary?.displayName ?? item.request.resourceSnapshot?.displayName ?? 'Resource unavailable'}</dd></div>
              <div><dt>Environment</dt><dd>Requested: {item.request.requestedEnvironment ?? 'Unclassified'} · current: {item.currentEnvironment ?? 'Unclassified'}</dd></div>
              <div><dt>Justification</dt><dd>{item.request.justification ?? 'None supplied'}</dd></div>
              <div><dt>Effective limits</dt><dd>{item.limitsSource === 'costCenter' ? 'Inherited cost-center per-person limits. ' : item.limitsSource === 'unrestricted' ? 'No grant-specific restriction. ' : 'Explicit limits. '}{describeLimits({ enforcement: item.effectiveEnforcement }).join(' ')}</dd></div>
              <div><dt>Existing grant</dt><dd>{item.existingEntitlementId ?? 'No existing grant reported'}</dd></div>
              <div><dt>Mutation lock</dt><dd>{item.mutationLocked ? 'Locked — recover or finish the current apply first' : 'Not locked at preview'}</dd></div>
            </dl>
            {item.blockers.map((b, i) => <MessageBar key={`${b.code}-${i}`} intent="error"><MessageBarBody>{b.message} {b.nextAction}</MessageBarBody></MessageBar>)}
            <div className={styles.form}>
              <Field label="Charged cost center" hint="Changing the cost center revalidates eligibility and inherited limits during execution.">
                <Select value={draft.costCenterId} onChange={(_, d) => update(item, { costCenterId: d.value })}><option value="">Requester default</option>{costCenters.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</Select>
              </Field>
              <Field label="Approval note"><Input value={draft.note} maxLength={1000} onChange={(_, d) => update(item, { note: d.value })} /></Field>
            </div>
            {moved(item) && <Checkbox label={`I confirm the move to ${item.currentEnvironment ?? 'Unclassified'} for this request`} checked={draft.confirmedMove} onChange={(_, d) => update(item, { confirmedMove: Boolean(d.checked) })} />}
            <Checkbox label="Override inherited grant limits for this request" checked={draft.explicitLimits} onChange={(_, d) => update(item, { explicitLimits: Boolean(d.checked) })} />
            {draft.explicitLimits && <LimitFields value={draft.limits} onChange={limits => update(item, { limits })} mcp={item.request.resource.kind === 'mcpServer'} />}
            {draft.explicitLimits && limitInputError(draft.limits) && <Text role="status">{limitInputError(draft.limits)}</Text>}
            {draft.explicitLimits && !buildEnforcement(draft.limits, true) && <Text role="status">Enter a positive limit, or turn off the override to retain inherited defaults.</Text>}
            <Button disabled={ai.isPending} onClick={() => ai.mutate(item.request.id)}>Generate advisory recommendation</Button>
            {ai.data?.filter(r => r.requestId === item.request.id && r.blocker).map(r => <Text key={r.requestId}>{r.blocker!.message} {r.blocker!.nextAction}</Text>)}
            <RequestRecommendation requestId={item.request.id} contextRevision={item.revision.contextRevision} generated={recommendations[item.request.id]} />
          </section>
        })}
        {ai.isError && <ErrorState error={ai.error} />}
        {preview.isSuccess && !items.length && <Text>No available requests in this selection. Return to the queue and refresh.</Text>}
      </>}
    </div>
  </WorkspaceEditorFrame>
}
