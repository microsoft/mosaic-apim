import { Badge, Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Select, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useMosaicApi } from '../api'
import type { AiApprovalReadiness, AiApprovalSettingsInput } from '../admin-types'
import { ErrorState, Loading } from './AsyncState'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './AdminGovernance.module.css'

export function AiReadiness({ value }: { value: AiApprovalReadiness }) {
  return <section className={styles.section} aria-label="AI approval readiness">
    <div className={styles.toolbar}><Title3 as="h3">AI approval readiness</Title3><Badge color={value.ready ? 'success' : 'warning'}>{value.ready ? 'Ready' : 'Not ready'}</Badge></div>
    <dl className={styles.facts}>
      <div><dt>Configuration</dt><dd>{value.configured ? 'Configured' : 'Incomplete'}</dd></div>
      <div><dt>Governed connection</dt><dd>{value.connectionVerified ? 'Verified' : 'Not verified'}</dd></div>
      <div><dt>Response contract</dt><dd>{value.responseContractVerified ? 'Verified' : 'Not verified'}</dd></div>
      <div><dt>Checked</dt><dd>{new Date(value.checkedAt).toLocaleString()}</dd></div>
      <div><dt>Last successful call</dt><dd>{value.lastSuccessfulCallAt ?? 'No successful call recorded'}</dd></div>
      <div><dt>Last call error</dt><dd>{value.lastErrorCode ? `${value.lastErrorCode} · ${value.lastErrorAt ?? 'Time unavailable'}` : 'No error recorded'}</dd></div>
    </dl>
    {value.blockers.map((b, i) => <Text key={`${b.code}-${i}`}>{b.message} {b.nextAction}</Text>)}
    <div className={styles.toolbar}>{value.remediationLinks.filter(path => path.startsWith('/') && !path.startsWith('//')).map(path => <RouterLink key={path} to={path === '/identities' ? '/identity' : path}>Open {path.replace(/^\//, '')}</RouterLink>)}</div>
  </section>
}
export function ApprovalAiSettings() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const settings = useQuery({ queryKey: ['approval-ai-settings'], queryFn: () => api.getApprovalAiSettings(), retry: false })
  const readiness = useQuery({ queryKey: ['approval-ai-readiness'], queryFn: () => api.getApprovalAiReadiness(), retry: false })
  const [draft, setDraft] = useState<AiApprovalSettingsInput | null>(null)
  const [draftRevision, setDraftRevision] = useState<number | undefined>()
  const [confirmTest, setConfirmTest] = useState(false)
  const save = useMutation({
    mutationFn: (body: AiApprovalSettingsInput) => api.saveApprovalAiSettings({ ...body, expectedRevision: draftRevision }),
    onSuccess: value => { client.setQueryData(['approval-ai-settings'], value); void client.invalidateQueries({ queryKey: ['approval-ai-readiness'] }); setDraft(null) },
  })
  const test = useMutation({ mutationFn: () => api.testApprovalAi(), onSuccess: value => { client.setQueryData(['approval-ai-readiness'], value); setConfirmTest(false); void settings.refetch() } })
  function edit() {
    const v = settings.data
    if (!v) return
    setDraftRevision(v.revision)
    setDraft({ enabled: v.enabled, resource: v.resource, callerPrincipalId: v.callerPrincipalId, entitlementId: v.entitlementId, costCenterId: v.costCenterId, operation: v.operation, maxInputCharacters: v.maxInputCharacters, maxOutputTokens: v.maxOutputTokens, timeoutSeconds: v.timeoutSeconds, concurrency: v.concurrency, maxBatchSize: v.maxBatchSize })
    save.reset()
  }
  if (draft) return <AiSettingsEditor value={draft} onBack={() => setDraft(null)} onSave={body => save.mutate(body)} pending={save.isPending} error={save.error} />
  return <div className={styles.stack}>
    <section className={styles.section}>
      <Title3 as="h2">AI-assisted approval review</Title3>
      <Text>Recommendations only: AI cannot approve, deny, change limits, or apply gateway plans. Calls use an existing MOSAIC-governed model and are charged to its dedicated grant and cost center.</Text>
      <Text>Save a disabled configuration, explicitly test the governed connection and response contract, then enable the same verified configuration. Manual review remains available if AI is unavailable.</Text>
      {settings.isPending && <Loading label="Loading AI settings" />}{settings.isError && <><ErrorState error={settings.error} /><Button onClick={() => void settings.refetch()}>Retry AI settings</Button></>}
      {settings.data && <dl className={styles.facts}><div><dt>Saved state</dt><dd>{settings.data.enabled ? 'Enabled' : 'Disabled'}</dd></div>
        <div><dt>Model resource</dt><dd>{settings.data.resource ? `${settings.data.resource.kind} · ${settings.data.resource.id}` : 'Not configured'}</dd></div>
        <div><dt>Operation</dt><dd>{settings.data.operation}</dd></div><div><dt>Configuration revision</dt><dd>{settings.data.revision}</dd></div>
        <div><dt>Last verification</dt><dd>{settings.data.verifiedAt ?? 'No verification recorded'}</dd></div></dl>}
      <div className={styles.toolbar}>
        <Button appearance="primary" disabled={!settings.data} onClick={edit}>Edit AI settings</Button>
        <Button disabled={!settings.data || test.isPending} onClick={() => setConfirmTest(true)}>Test saved configuration</Button>
        <Button disabled={readiness.isFetching} onClick={() => void readiness.refetch()}>Refresh readiness</Button>
      </div>
    </section>
    {readiness.isPending && <Loading label="Checking AI readiness" />}
    {readiness.isError && <ErrorState error={readiness.error} />}
    {readiness.data && <AiReadiness value={readiness.data} />}
    <Dialog open={confirmTest} onOpenChange={(_, d) => { if (!test.isPending) setConfirmTest(d.open) }}><DialogSurface><DialogBody><DialogTitle>Test the governed model?</DialogTitle><DialogContent>
      This sends a bounded synthetic request through the saved APIM model connection. It can consume tokens and cost against the dedicated grant and cost center. It does not approve any request.
      {test.isError && <ErrorState error={test.error} />}
    </DialogContent><DialogActions><Button disabled={test.isPending} onClick={() => setConfirmTest(false)}>Cancel</Button><Button appearance="primary" disabled={test.isPending} onClick={() => test.mutate()}>Send synthetic model test</Button></DialogActions></DialogBody></DialogSurface></Dialog>
  </div>
}
function AiSettingsEditor({ value, onBack, onSave, pending, error }: { value: AiApprovalSettingsInput; onBack: () => void; onSave: (value: AiApprovalSettingsInput) => void; pending: boolean; error: unknown }) {
  const api = useMosaicApi()
  const [draft, setDraft] = useState(() => structuredClone(value))
  const modelOfferings = useQuery({ queryKey: ['approval-ai-offerings'], queryFn: () => api.listApprovalAiOfferings(), retry: false })
  const principals = useQuery({ queryKey: ['principals'], queryFn: () => api.listPrincipals() })
  const grants = useQuery({ queryKey: ['entitlements'], queryFn: () => api.listEntitlements() })
  const centers = useQuery({ queryKey: ['cost-centers'], queryFn: () => api.listCostCenters() })
  const offerings = [
    ...(modelOfferings.data ?? []).map(o => ({
      key: o.resource.kind === 'poolModel' ? `poolModel:${o.resource.scopeId}:${o.resource.id}` : `modelApi:${o.resource.id}`,
      resource: o.resource, label: o.summary?.displayName ?? 'Model name unavailable',
      compatible: o.compatible, supportedOperations: o.supportedOperations, blockers: o.blockers,
    })),
  ]
  const resourceKey = draft.resource ? draft.resource.kind === 'poolModel' ? `poolModel:${draft.resource.scopeId}:${draft.resource.id}` : `modelApi:${draft.resource.id}` : ''
  const numericInvalid = !Number.isInteger(draft.maxInputCharacters) || draft.maxInputCharacters < 1000 || draft.maxInputCharacters > 32000
    || !Number.isInteger(draft.maxOutputTokens) || draft.maxOutputTokens < 100 || draft.maxOutputTokens > 4000
    || !Number.isInteger(draft.timeoutSeconds) || draft.timeoutSeconds < 1 || draft.timeoutSeconds > 120
    || !Number.isInteger(draft.concurrency) || draft.concurrency < 1 || draft.concurrency > 4
    || !Number.isInteger(draft.maxBatchSize) || draft.maxBatchSize < 1 || draft.maxBatchSize > 20
  const incomplete = !draft.resource || !draft.callerPrincipalId || !draft.entitlementId || !draft.costCenterId
  const configChanged = JSON.stringify({ ...value, enabled: false }) !== JSON.stringify({ ...draft, enabled: false })
  return <WorkspaceEditorFrame title="Edit AI approval assistance" onBack={onBack} onCancel={onBack} dirty={JSON.stringify(value) !== JSON.stringify(draft)} pending={pending}
    description="No model keys or backend endpoint are accepted. Select an existing governed offering and dedicated application or managed identity."
    error={error ? <ErrorState error={error} /> : undefined}
    primaryAction={{ label: 'Save AI settings', onClick: () => onSave(draft), disabled: numericInvalid || (draft.enabled && (incomplete || configChanged)) }}>
    <div className={styles.stack}>
      {[modelOfferings, principals, grants, centers].filter(q => q.isError).map((q, i) => <ErrorState key={i} error={q.error} />)}
      <div className={styles.form}>
        <Field label="Governed model offering" required><Select value={resourceKey} onChange={(_, d) => setDraft({ ...draft, resource: offerings.find(o => o.key === d.value)?.resource ?? null, entitlementId: null, enabled: false })}><option value="">Select a model offering</option>{!offerings.some(o => o.key === resourceKey) && resourceKey && <option value={resourceKey}>Saved offering unavailable — select again</option>}{offerings.map(o => <option key={o.key} value={o.key} disabled={!o.compatible}>{o.label}{o.compatible ? '' : ' (incompatible)'}</option>)}</Select></Field>
        <Field label="Dedicated caller identity" required><Select value={draft.callerPrincipalId ?? ''} onChange={(_, d) => setDraft({ ...draft, callerPrincipalId: d.value || null, entitlementId: null, enabled: false })}><option value="">Select an application or managed identity</option>{principals.data?.filter(p => p.kind === 'servicePrincipal' || p.kind === 'managedIdentity').map(p => <option key={p.id} value={p.id}>{p.label ?? 'Identity name unavailable'}</option>)}</Select></Field>
        <Field label="Dedicated grant" required><Select value={draft.entitlementId ?? ''} onChange={(_, d) => setDraft({ ...draft, entitlementId: d.value || null, enabled: false })}><option value="">Select the caller's model grant</option>{grants.data?.filter(g => g.enabled && g.subject.id === draft.callerPrincipalId && g.resource.kind === draft.resource?.kind && g.resource.id === draft.resource?.id && (g.resource.scopeId ?? '') === (draft.resource?.scopeId ?? '')).map(g => <option key={g.id} value={g.id}>{g.costCenter?.name ?? g.costCenterId ?? 'No cost center'} · {g.runtime?.status ?? 'Applied state not verified'} · {g.id}</option>)}</Select></Field>
        <Field label="Charged cost center" required><Select value={draft.costCenterId ?? ''} onChange={(_, d) => setDraft({ ...draft, costCenterId: d.value || null, enabled: false })}><option value="">Select a cost center</option>{centers.data?.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</Select></Field>
        <Field label="Model operation"><Select value={draft.operation} onChange={(_, d) => setDraft({ ...draft, operation: d.value as AiApprovalSettingsInput['operation'], enabled: false })}><option value="chatCompletions">Chat completions</option><option value="responses">Responses</option></Select></Field>
        {([
          ['maxInputCharacters', 'Maximum input characters', 1000, 32000], ['maxOutputTokens', 'Maximum output tokens', 100, 4000],
          ['timeoutSeconds', 'Timeout (seconds)', 1, 120], ['concurrency', 'Concurrent model calls', 1, 4], ['maxBatchSize', 'Maximum AI batch size', 1, 20],
        ] as const).map(([key, label, min, max]) => <Field label={label} key={key}><Input type="number" min={min} max={max} value={String(draft[key])} onChange={(_, d) => setDraft({ ...draft, [key]: Number(d.value), enabled: false })} /></Field>)}
      </div>
      {offerings.filter(o => !o.compatible).map(o => <div key={o.key}><Text weight="semibold">{o.label} · unavailable for AI assistance</Text>{o.blockers.map((b, i) => <Text key={i} block>{b.message} {b.nextAction}</Text>)}</div>)}
      <Checkbox label="Enable advisory AI assistance" checked={draft.enabled} disabled={configChanged || Boolean(incomplete)} onChange={(_, d) => setDraft({ ...draft, enabled: Boolean(d.checked) })} />
      {configChanged && <Text>Save changed configuration disabled, run the explicit synthetic test, then return here to enable it. Saved settings alone do not prove readiness.</Text>}
      {incomplete && <Text>Select a model, dedicated caller, eligible grant, and cost center before enabling assistance.</Text>}
    </div>
  </WorkspaceEditorFrame>
}
