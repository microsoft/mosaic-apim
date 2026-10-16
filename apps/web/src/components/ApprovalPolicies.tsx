import { Badge, Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, MessageBar, MessageBarBody, Select, Text, Textarea, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import type { ApprovalPolicy, ApprovalPolicyInput } from '../admin-types'
import type { EntitlementResourceKind } from '../types'
import { useEnvironmentCatalog } from '../environments'
import { buildEnforcement, emptyLimitForm, limitFormFrom, describeLimits, type LimitForm } from '../entitlement-limits'
import { ErrorState, Loading } from './AsyncState'
import { LimitFields, TimeWindowFields, TimeZoneField } from './GovernanceFields'
import { limitInputError, validTimeZone } from '../admin-governance'
import { ResourceWorkbench } from './ResourceWorkbench'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { useWorkbenchLocation } from './useWorkbenchLocation'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import styles from './AdminGovernance.module.css'

function newPolicy(): ApprovalPolicyInput {
  return { name: '', description: '', enabled: false, scope: { environments: [], resourceKinds: ['modelApi'], resourceIds: [], costCenterIds: [] }, productionOptIn: false, schedule: { timeZone: 'UTC', windows: [{ daysOfWeek: [0, 1, 2, 3, 4], startTime: '09:00', endTime: '17:00' }] }, minimumRequestAgeSeconds: 3600, grantTemplate: { enforcement: {} } }
}
export function ApprovalPolicies() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const workbench = useWorkbenchLocation({ section: 'approvalPolicies' })
  const policies = useQuery({ queryKey: ['approval-policies'], queryFn: () => api.listApprovalPolicies(), retry: false })
  const evaluator = useQuery({ queryKey: ['approval-evaluator'], queryFn: () => api.getApprovalEvaluator(), retry: false })
  const [deleting, setDeleting] = useState<ApprovalPolicy | null>(null)
  const [confirmResume, setConfirmResume] = useState(false)
  const remove = useMutation({ mutationFn: (policy: ApprovalPolicy) => api.deleteApprovalPolicy(policy.id, policy.revision), onSuccess: () => { setDeleting(null); workbench.select(null); void client.invalidateQueries({ queryKey: ['approval-policies'] }) } })
  const pause = useMutation({ mutationFn: (paused: boolean) => api.setApprovalEvaluator(paused), onSuccess: value => { client.setQueryData(['approval-evaluator'], value); setConfirmResume(false) } })
  const selected = policies.data?.find(p => p.id === workbench.selectedId)
  if (workbench.mode === 'new' || (workbench.mode === 'edit' && selected)) return <PolicyEditor key={selected?.id ?? 'new'} value={workbench.mode === 'edit' ? selected : undefined} onBack={() => workbench.close()} onSaved={id => { void client.invalidateQueries({ queryKey: ['approval-policies'] }); completeWorkspaceNavigation(() => workbench.close(id)) }} />
  return <div className={styles.stack}>
    <section className={styles.section}>
      <div className={styles.toolbar}><Title3 as="h2">Automatic approval evaluator</Title3>
        {evaluator.data && <Badge>{evaluator.data.paused ? 'Paused' : 'Enabled'}</Badge>}
        <Button disabled={!evaluator.data || pause.isPending} onClick={() => evaluator.data?.paused ? setConfirmResume(true) : pause.mutate(true)}>{evaluator.data?.paused ? 'Resume evaluator' : 'Pause evaluator'}</Button>
      </div>
      <Text>Only explicit, enabled policies can create grant intent. Policy scheduling never applies a gateway plan.</Text>
      {evaluator.isPending && <Loading label="Loading evaluator state" />}
      {evaluator.isError && <><ErrorState error={evaluator.error} /><Button onClick={() => void evaluator.refetch()}>Retry evaluator</Button></>}
      {evaluator.data && <Text>Last successful run: {evaluator.data.lastSucceededAt ?? 'No successful run recorded'} · Pending: {evaluator.data.pendingCount}. {evaluator.data.lastError}</Text>}
      {pause.isError && <ErrorState error={pause.error} />}
    </section>
    {policies.isError && <><ErrorState error={policies.error} /><Button onClick={() => void policies.refetch()}>Retry policies</Button></>}
    <ResourceWorkbench title="Approval policies" description="Disabled by default. Every condition must match current, classified resources; production requires explicit opt-in."
      items={policies.data ?? []} getId={p => p.id} selectedId={workbench.selectedId} onSelect={workbench.select}
      listLabel="Approval policies" detailLabel="Selected approval policy"
      actions={<Button appearance="primary" onClick={() => workbench.open('new', null)}>New approval policy</Button>}
      emptyList={policies.isPending ? <Loading label="Loading approval policies" /> : 'No approval policies. Requests remain subject to manual review.'}
      renderListItem={p => <div className={styles.stack}><strong>{p.name}</strong><Text>{p.scope.environments.join(', ')}</Text><Badge appearance="outline">{p.enabled ? 'Enabled' : 'Disabled'}</Badge></div>}
      renderDetail={p => <div className={styles.stack}>
        <Title3 as="h3">{p.name}</Title3><Text>{p.description}</Text>
        <dl className={styles.facts}>
          <div><dt>Environments</dt><dd>{p.scope.environments.join(', ')}</dd></div>
          <div><dt>Production opt-in</dt><dd>{p.productionOptIn ? 'Explicitly allowed' : 'Not allowed'}</dd></div>
          <div><dt>Minimum request age</dt><dd>{p.minimumRequestAgeSeconds} seconds</dd></div>
          <div><dt>Schedule</dt><dd>{p.schedule.timeZone}{p.schedule.windows.map((w, i) => <div key={i}>{w.daysOfWeek.map(d => ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d]).join(', ')} · {w.startTime}–{w.endTime}{w.endTime < w.startTime ? ' (next day)' : ''}</div>)}</dd></div>
          <div><dt>Grant limits</dt><dd>{describeLimits({ enforcement: p.grantTemplate.enforcement }).join(' ')}</dd></div>
          <div><dt>Revision</dt><dd>{p.revision}</dd></div>
        </dl>
        <div className={styles.toolbar}><Button appearance="primary" onClick={() => workbench.open('edit', p.id)}>Edit policy and dry-run</Button><Button onClick={() => setDeleting(p)}>Delete policy</Button></div>
      </div>} />
    <Dialog open={Boolean(deleting)} onOpenChange={(_, d) => { if (!d.open && !remove.isPending) setDeleting(null) }}><DialogSurface><DialogBody><DialogTitle>Delete approval policy?</DialogTitle><DialogContent>
      Future evaluations will no longer use {deleting?.name}. Existing grants are unchanged.
      {remove.isError && <ErrorState error={remove.error} />}
    </DialogContent><DialogActions><Button disabled={remove.isPending} onClick={() => setDeleting(null)}>Cancel</Button><Button disabled={remove.isPending} appearance="primary" onClick={() => deleting && remove.mutate(deleting)}>Delete policy</Button></DialogActions></DialogBody></DialogSurface></Dialog>
    <Dialog open={confirmResume} onOpenChange={(_, d) => setConfirmResume(d.open)}><DialogSurface><DialogBody><DialogTitle>Resume automatic approvals?</DialogTitle><DialogContent>Enabled policies may approve pending requests during their configured windows. Grants still need plan review/apply. {pause.isError && <ErrorState error={pause.error} />}</DialogContent><DialogActions><Button onClick={() => setConfirmResume(false)}>Cancel</Button><Button appearance="primary" disabled={pause.isPending} onClick={() => pause.mutate(false)}>Resume evaluator</Button></DialogActions></DialogBody></DialogSurface></Dialog>
  </div>
}

function PolicyEditor({ value, onBack, onSaved }: { value?: ApprovalPolicy; onBack: () => void; onSaved: (id: string) => void }) {
  const api = useMosaicApi()
  const catalog = useEnvironmentCatalog()
  const centers = useQuery({ queryKey: ['cost-centers'], queryFn: () => api.listCostCenters() })
  const pending = useQuery({ queryKey: ['access-requests', 'pending'], queryFn: () => api.listAccessRequests('pending') })
  const [draft, setDraft] = useState<ApprovalPolicyInput>(() => value ? {
    name: value.name, description: value.description, enabled: value.enabled, scope: structuredClone(value.scope),
    productionOptIn: value.productionOptIn, schedule: structuredClone(value.schedule),
    minimumRequestAgeSeconds: value.minimumRequestAgeSeconds, grantTemplate: structuredClone(value.grantTemplate),
  } : newPolicy())
  const [limits, setLimits] = useState<LimitForm>(() => value ? { ...limitFormFrom(value.grantTemplate.enforcement.tokens), calls: String(value.grantTemplate.enforcement.requests?.calls ?? ''), renewalPeriodSeconds: String(value.grantTemplate.enforcement.requests?.renewalPeriodSeconds ?? 60) } : { ...emptyLimitForm })
  const [confirmed, setConfirmed] = useState(false)
  const [expectedRevision] = useState(value?.revision)
  const [resourceIdsText, setResourceIdsText] = useState(draft.scope.resourceIds.join(', '))
  const [dryIds, setDryIds] = useState<string[]>([])
  const body = (): ApprovalPolicyInput => ({ ...draft, scope: { ...draft.scope, resourceIds: resourceIdsText.split(',').map(s => s.trim()).filter(Boolean) }, grantTemplate: { ...draft.grantTemplate, enforcement: buildEnforcement(limits, true) ?? {} } })
  const save = useMutation({ mutationFn: () => api.saveApprovalPolicy(value?.id ?? null, { ...body(), expectedRevision, enableConfirmed: draft.enabled && confirmed }), onSuccess: p => onSaved(p.id) })
  const dryRun = useMutation({ mutationFn: () => api.dryRunApprovalPolicy(dryIds, body()) })
  function update(change: Partial<ApprovalPolicyInput>) { setDraft(d => ({ ...d, ...change })); setConfirmed(false); dryRun.reset() }
  const productionSelected = catalog.data?.environments.some(e => e.production && draft.scope.environments.includes(e.key))
  const invalid = !draft.name.trim() || !draft.scope.environments.length || !draft.scope.resourceKinds.length || !validTimeZone(draft.schedule.timeZone)
    || !Number.isInteger(draft.minimumRequestAgeSeconds) || draft.minimumRequestAgeSeconds < 0 || draft.minimumRequestAgeSeconds > 31536000
    || draft.schedule.windows.some(w => !w.daysOfWeek.length || !w.startTime || !w.endTime || w.startTime === w.endTime)
    || Boolean(limitInputError(limits)) || !buildEnforcement(limits, true)
    || (draft.scope.resourceKinds.includes('mcpServer') && Boolean(limits.tokensPerMinute || limits.tokenQuota))
    || Boolean(productionSelected && !draft.productionOptIn)
    || (draft.enabled && !confirmed)
  return <WorkspaceEditorFrame title={value ? `Edit ${value.name}` : 'New approval policy'} onBack={onBack} onCancel={onBack} dirty pending={save.isPending}
    description="All conditions must match. Unclassified or moved resources require manual review. Saving a policy is not an immediate batch approval."
    primaryAction={{ label: 'Save policy', onClick: () => save.mutate(), disabled: invalid }} error={save.isError ? <ErrorState error={save.error} /> : undefined}>
    <div className={styles.stack}>
      <div className={styles.form}>
        <Field label="Policy name" required><Input value={draft.name} maxLength={120} onChange={(_, d) => update({ name: d.value })} /></Field>
        <Field label="Description"><Textarea value={draft.description ?? ''} maxLength={1000} onChange={(_, d) => update({ description: d.value })} /></Field>
        <Field label="Minimum request age (seconds)" hint="Measured from the original UTC request timestamp."><Input type="number" min={0} max={31536000} value={String(draft.minimumRequestAgeSeconds)} onChange={(_, d) => update({ minimumRequestAgeSeconds: Number(d.value) })} /></Field>
      </div>
      <section className={styles.section}><Title3 as="h3">Scope</Title3>
        {catalog.isError && <ErrorState error={catalog.error} />}
        <div className={styles.days}>{catalog.data?.environments.filter(e => e.key !== 'unclassified').map(e => <Checkbox key={e.key} label={`${e.displayName}${e.production ? ' (production)' : ''}`} checked={draft.scope.environments.includes(e.key)} onChange={(_, d) => update({ scope: { ...draft.scope, environments: d.checked ? [...draft.scope.environments, e.key] : draft.scope.environments.filter(k => k !== e.key) } })} />)}</div>
        <div className={styles.days}>{(['modelApi', 'poolModel', 'mcpServer', 'modelDeployment', 'product'] as EntitlementResourceKind[]).map(kind => <Checkbox key={kind} label={kind} checked={draft.scope.resourceKinds.includes(kind)} onChange={(_, d) => {
          if (kind === 'mcpServer' && d.checked) setLimits(current => ({ ...current, tokensPerMinute: '', tokenQuota: '' }))
          update({ scope: { ...draft.scope, resourceKinds: d.checked ? [...draft.scope.resourceKinds, kind] : draft.scope.resourceKinds.filter(k => k !== kind) } })
        }} />)}</div>
        <Field label="Resource IDs" hint="Comma-separated exact IDs; empty means any resource of the selected kinds."><Input value={resourceIdsText} onChange={(_, d) => { setResourceIdsText(d.value); setConfirmed(false); dryRun.reset() }} /></Field>
        <Field label="Eligible cost centers" hint="No selection means any eligible cost center; existing membership checks still apply." />
        {centers.isError && <ErrorState error={centers.error} />}
        <div className={styles.days}>{centers.data?.map(c => <Checkbox key={c.id} label={c.name} checked={draft.scope.costCenterIds.includes(c.id)} onChange={(_, d) => update({ scope: { ...draft.scope, costCenterIds: d.checked ? [...draft.scope.costCenterIds, c.id] : draft.scope.costCenterIds.filter(id => id !== c.id) } })} />)}</div>
        <Checkbox label="Explicitly allow production resources in this policy" checked={draft.productionOptIn} onChange={(_, d) => update({ productionOptIn: Boolean(d.checked) })} />
      </section>
      <section className={styles.section}><Title3 as="h3">Approval windows</Title3>
        <TimeZoneField value={draft.schedule.timeZone} onChange={timeZone => update({ schedule: { ...draft.schedule, timeZone } })} />
        {draft.schedule.windows.map((window, index) => <div className={styles.stack} key={index}><TimeWindowFields value={window} onChange={next => update({ schedule: { ...draft.schedule, windows: draft.schedule.windows.map((w, i) => i === index ? next : w) } })} />
          <Button disabled={draft.schedule.windows.length === 1} onClick={() => update({ schedule: { ...draft.schedule, windows: draft.schedule.windows.filter((_, i) => i !== index) } })}>Remove window {index + 1}</Button></div>)}
        <Button disabled={draft.schedule.windows.length >= 28} onClick={() => update({ schedule: { ...draft.schedule, windows: [...draft.schedule.windows, { daysOfWeek: [0], startTime: '09:00', endTime: '17:00' }] } })}>Add window</Button>
      </section>
      <section className={styles.section}><Title3 as="h3">Bounded grant template</Title3>
        <Text>At least one positive grant limit is required. MCP policies support call limits only.</Text>
        <LimitFields value={limits} onChange={next => { setLimits(next); setConfirmed(false); dryRun.reset() }} mcp={draft.scope.resourceKinds.includes('mcpServer')} />
        <Field label="Charged cost center"><Select value={draft.grantTemplate.costCenterId ?? ''} onChange={(_, d) => update({ grantTemplate: { ...draft.grantTemplate, costCenterId: d.value || null } })}><option value="">Use request cost center</option>{centers.data?.map(c => <option value={c.id} key={c.id}>{c.name}</option>)}</Select></Field>
        <Field label="Grant note"><Input value={draft.grantTemplate.note ?? ''} maxLength={1000} onChange={(_, d) => update({ grantTemplate: { ...draft.grantTemplate, note: d.value } })} /></Field>
      </section>
      <section className={styles.section}><Title3 as="h3">Dry-run pending requests</Title3><Text>Preview never approves a request. Select up to 100 exact requests.</Text>
        {pending.isPending && <Loading label="Loading pending requests" />}{pending.isError && <ErrorState error={pending.error} />}
        {pending.data?.slice(0, 100).map(r => <Checkbox key={r.id} label={`${r.resourceSummary?.displayName ?? r.resourceSnapshot?.displayName ?? 'Resource unavailable'} · ${new Date(r.createdAt).toLocaleString()}`} checked={dryIds.includes(r.id)} onChange={(_, d) => { setDryIds(ids => d.checked ? [...ids, r.id] : ids.filter(id => id !== r.id)); dryRun.reset() }} />)}
        <Button disabled={!dryIds.length || dryRun.isPending} onClick={() => dryRun.mutate()}>Run policy preview</Button>
        {dryRun.isError && <ErrorState error={dryRun.error} />}
        {dryRun.data?.map(e => <div key={e.id}><Badge>{e.outcome}</Badge><Text>{e.explanation}</Text>{e.nextEligibleAt && <Text>Next eligible: {e.nextEligibleAt}</Text>}{e.blockers.map((b, i) => <Text key={i}>{b.message} {b.nextAction}</Text>)}</div>)}
      </section>
      <Checkbox label="Enable this approval policy" checked={draft.enabled} onChange={(_, d) => update({ enabled: Boolean(d.checked) })} />
      {draft.enabled && <MessageBar intent="warning"><MessageBarBody><Checkbox label="I confirm the environment and resource scope, production choice, bounded limits, time zone, approval windows, and minimum request age" checked={confirmed} onChange={(_, d) => setConfirmed(Boolean(d.checked))} /></MessageBarBody></MessageBar>}
      {invalid && <Text role="status">Complete the scope, valid schedule and waiting period, bounded grant limits, and any production/enabling confirmation before saving.</Text>}
    </div>
  </WorkspaceEditorFrame>
}
