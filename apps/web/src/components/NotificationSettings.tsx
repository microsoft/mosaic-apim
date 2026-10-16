import { Badge, Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Select, Tab, TabList, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { NOTIFICATION_CATEGORIES, type CapabilityCheck, type NotificationChannelSettingsInput, type NotificationRoute, type NotificationRouteInput, type PersonalTeamsSettingsInput, type TeamsDestination, type TeamsDestinationInput } from '../admin-types'
import { ErrorState, Loading } from './AsyncState'
import { CadenceFields, DigestFields, TimeZoneField } from './GovernanceFields'
import { schedulesFor, validTimeZone } from '../admin-governance'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { ResourceWorkbench } from './ResourceWorkbench'
import styles from './AdminGovernance.module.css'

const tabs = ['channels', 'routes', 'teams', 'personalTeams', 'deliveries', 'health'] as const
export function NotificationSettings() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('notificationView')
  const active = tabs.find(t => t === requested) ?? 'channels'
  return <div className={styles.stack}>
    <TabList className={styles.tabs} selectedValue={active} onTabSelect={(_, d) => setParams(current => { const next = new URLSearchParams(current); next.set('notificationView', String(d.value)); return next })}>
      <Tab value="channels">Channels</Tab><Tab value="routes">Routing</Tab><Tab value="teams">Shared Teams</Tab><Tab value="personalTeams">Personal Teams app</Tab><Tab value="deliveries">Delivery history</Tab><Tab value="health">Readiness</Tab>
    </TabList>
    {active === 'channels' && <ChannelSettings />}
    {active === 'routes' && <NotificationRoutes />}
    {active === 'teams' && <TeamsDestinations />}
    {active === 'personalTeams' && <PersonalTeamsRegistration />}
    {active === 'deliveries' && <NotificationDeliveries />}
    {active === 'health' && <NotificationReadiness />}
  </div>
}
function ChannelSettings() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const settings = useQuery({ queryKey: ['notification-settings'], queryFn: () => api.getNotificationSettings(), retry: false })
  const [draft, setDraft] = useState<NotificationChannelSettingsInput | null>(null)
  const [draftRevision, setDraftRevision] = useState<number | undefined>()
  const save = useMutation({ mutationFn: (body: NotificationChannelSettingsInput) => api.saveNotificationSettings(body, draftRevision), onSuccess: value => { client.setQueryData(['notification-settings'], value); void client.invalidateQueries({ queryKey: ['notification-health'] }); setDraft(null) } })
  if (draft) return <WorkspaceEditorFrame title="Edit notification channels" onBack={() => setDraft(null)} onCancel={() => setDraft(null)} dirty pending={save.isPending}
    description="Channel availability is not delivery evidence. External channels still require explicit recipient preferences and ready integrations."
    primaryAction={{ label: 'Save channels', onClick: () => save.mutate(draft), disabled: !Number.isInteger(draft.retentionDays) || draft.retentionDays < 7 || draft.retentionDays > 365 }}
    error={save.isError ? <ErrorState error={save.error} /> : undefined}>
    <div className={styles.stack}>
      <Checkbox label="Enable in-app notifications" checked={draft.inAppEnabled} onChange={(_, d) => setDraft({ ...draft, inAppEnabled: Boolean(d.checked) })} />
      <Checkbox label="Enable email channel" checked={draft.emailEnabled} onChange={(_, d) => setDraft({ ...draft, emailEnabled: Boolean(d.checked) })} />
      <Checkbox label="Enable shared Teams Workflows channel" checked={draft.teamsChannelEnabled} onChange={(_, d) => setDraft({ ...draft, teamsChannelEnabled: Boolean(d.checked) })} />
      <Checkbox label="Enable personal Teams channel" checked={draft.teamsPersonalEnabled} onChange={(_, d) => setDraft({ ...draft, teamsPersonalEnabled: Boolean(d.checked) })} />
      <Field label="Notification retention (days)" hint="7–365 days. Does not change spending limits."><Input type="number" min={7} max={365} value={String(draft.retentionDays)} onChange={(_, d) => setDraft({ ...draft, retentionDays: Number(d.value) })} /></Field>
      <Text>Personal Teams additionally needs the MOSAIC app/bot registration, trusted installation, and a personal conversation binding. It is never redirected to shared Teams.</Text>
    </div>
  </WorkspaceEditorFrame>
  return <section className={styles.section}><Title3 as="h2">Notification channels</Title3>
    <Text>In-app defaults on; email, Teams, and recurring summaries are opt-in. Existing budget email configuration is retained separately.</Text>
    {settings.isPending && <Loading label="Loading shared channel settings" />}
    {settings.isError && <><ErrorState error={settings.error} /><Button onClick={() => void settings.refetch()}>Retry channel settings</Button></>}
    {settings.data && <><dl className={styles.facts}>
      <div><dt>In-app</dt><dd>{settings.data.inAppEnabled ? 'Enabled' : 'Disabled'}</dd></div>
      <div><dt>Email</dt><dd>{settings.data.emailEnabled ? 'Enabled' : 'Disabled'}</dd></div>
      <div><dt>Shared Teams</dt><dd>{settings.data.teamsChannelEnabled ? 'Enabled' : 'Disabled'}</dd></div>
      <div><dt>Personal Teams</dt><dd>{settings.data.teamsPersonalEnabled ? 'Enabled' : 'Disabled'}</dd></div>
      <div><dt>Retention</dt><dd>{settings.data.retentionDays} days</dd></div>
    </dl><Button appearance="primary" onClick={() => { const s = settings.data!; setDraftRevision(s.revision); setDraft({ inAppEnabled: s.inAppEnabled, emailEnabled: s.emailEnabled, teamsChannelEnabled: s.teamsChannelEnabled, teamsPersonalEnabled: s.teamsPersonalEnabled, retentionDays: s.retentionDays }); save.reset() }}>Edit channel settings</Button></>}
    <div className={styles.toolbar}><RouterLink to="/settings?tab=email">Configure ACS budget email</RouterLink><RouterLink to="/profile">My administrator preferences</RouterLink></div>
  </section>
}
export function CapabilityEvidence({ check }: { check: CapabilityCheck }) {
  const expired = Boolean(check.freshUntil && new Date(check.freshUntil).getTime() <= Date.now())
  const verified = check.state === 'verified' && Boolean(check.checkedAt) && check.evidence.length > 0 && !expired
  return <section className={styles.evidenceSection} aria-label={`${check.feature} readiness`}>
    <div className={styles.toolbar}><Title3 as="h3">{check.feature}</Title3><Badge color={verified ? 'success' : check.state === 'failed' ? 'danger' : 'warning'}>{expired ? 'Stale evidence' : check.state === 'verified' && !verified ? 'Unverified' : check.state}</Badge></div>
    <Text>{check.reason ?? 'No reason was supplied.'}</Text>
    <Text>Scope: {check.scope} · {check.required ? 'Required' : 'Optional'} · Checked: {check.checkedAt ? new Date(check.checkedAt).toLocaleString() : 'Not checked'}</Text>
    {check.evidence.length > 0 && <ul>{check.evidence.map((e, i) => <li key={i} className={styles.evidence}>{e}</li>)}</ul>}
    {check.remediation && <div className={styles.stack}><Text>{check.remediation.action} · Owner: {check.remediation.owner}</Text>
      {check.remediation.path?.startsWith('/') && !check.remediation.path.startsWith('//') && <RouterLink to={check.remediation.path}>Open remediation</RouterLink>}
      {check.remediation.command && <code className={styles.evidence}>{check.remediation.command}</code>}</div>}
  </section>
}
function NotificationReadiness() {
  const api = useMosaicApi()
  const health = useQuery({ queryKey: ['notification-health'], queryFn: () => api.getNotificationHealth(), retry: false })
  return <div className={styles.stack}><div className={styles.toolbar}><Title3 as="h2">Notification readiness</Title3><Button disabled={health.isFetching} onClick={() => void health.refetch()}>Refresh checks</Button></div>
    <Text>Saved configuration, connectivity, provider acceptance, and confirmed delivery are different states. Refreshing checks never sends a test message.</Text>
    {health.isPending && <Loading label="Checking notification capabilities" />}
    {health.isError && <ErrorState error={health.error} />}
    {health.data?.map((check, i) => <CapabilityEvidence key={`${check.feature}-${check.scope}-${i}`} check={check} />)}
    {health.isSuccess && !health.data.length && <Text>No capability evidence has been reported. Do not assume channels are healthy.</Text>}
  </div>
}
function NotificationDeliveries() {
  const api = useMosaicApi()
  const deliveries = useQuery({ queryKey: ['notification-deliveries'], queryFn: () => api.listNotificationDeliveries(), retry: false })
  const [state, setState] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const retry = useMutation({ mutationFn: (id: string) => api.retryNotificationDelivery(id), onSuccess: () => void deliveries.refetch() })
  return <div className={styles.stack}>
    <Text>Provider acceptance is not proof of user delivery or reading. Unknown outcomes may have already reached the provider and are not retried here.</Text>
    {deliveries.isError && <><ErrorState error={deliveries.error} /><Button onClick={() => void deliveries.refetch()}>Retry delivery history</Button></>}
    {retry.isError && <ErrorState error={retry.error} />}
    <ResourceWorkbench title="Delivery history" items={(deliveries.data ?? []).filter(d => !state || d.state === state)} getId={d => d.id} selectedId={selectedId} onSelect={setSelectedId}
      listLabel="Notification deliveries" detailLabel="Selected notification delivery"
      emptyList={deliveries.isPending ? <Loading label="Loading delivery history" /> : deliveries.isError ? 'Delivery history unavailable.' : 'No delivery records match this filter.'}
      toolbar={<Field label="Delivery state"><Select value={state} onChange={(_, d) => { setState(d.value); setSelectedId(null) }}><option value="">All states</option>{['queued', 'sending', 'providerAccepted', 'confirmedDelivered', 'suppressed', 'retrying', 'permanentlyFailed', 'unknownOutcome'].map(s => <option key={s} value={s}>{s}</option>)}</Select></Field>}
      renderListItem={d => <div className={styles.stack}><strong>{d.channel}</strong><Badge appearance="outline">{d.state}</Badge><Text>{d.attempts} attempts</Text></div>}
      renderDetail={d => <div className={styles.stack}><Title3 as="h3">{d.channel} delivery</Title3><dl className={styles.facts}>
        <div><dt>State</dt><dd>{d.state}</dd></div><div><dt>Recipient reference</dt><dd>{d.recipientReference}</dd></div>
        <div><dt>Attempts</dt><dd>{d.attempts}</dd></div><div><dt>Provider accepted</dt><dd>{d.providerAcceptedAt ?? 'No acceptance evidence'}</dd></div>
        <div><dt>Confirmed delivery</dt><dd>{d.confirmedDeliveredAt ?? 'No delivery evidence'}</dd></div>
        <div><dt>Next attempt</dt><dd>{d.nextAttemptAt ?? 'Not scheduled'}</dd></div><div><dt>Suppression</dt><dd>{d.suppressionReason ?? 'None reported'}</dd></div>
        <div><dt>Last error</dt><dd>{d.lastError ?? 'None reported'}</dd></div></dl>
        {(d.state === 'permanentlyFailed' || d.state === 'retrying') && <Button disabled={retry.isPending} onClick={() => retry.mutate(d.id)}>Retry this delivery</Button>}
      </div>} />
  </div>
}
function newRoute(): NotificationRouteInput {
  return { name: '', enabled: false, audience: 'selectedAdmins', administratorObjectIds: [], budgetId: null, teamsDestinationId: null, categories: ['requestLifecycle'], eventTypes: [], channel: 'inApp', cadence: { mode: 'immediate', digestPeriods: [] }, timeZone: 'UTC', digestSchedules: [] }
}
function NotificationRoutes() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const routes = useQuery({ queryKey: ['notification-routes'], queryFn: () => api.listNotificationRoutes(), retry: false })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ id: string | null; body: NotificationRouteInput; expectedRevision?: number } | null>(null)
  const [deleting, setDeleting] = useState<NotificationRoute | null>(null)
  const save = useMutation({ mutationFn: (draft: { id: string | null; body: NotificationRouteInput; expectedRevision?: number }) => api.saveNotificationRoute(draft.id, draft.body, draft.expectedRevision), onSuccess: value => { void client.invalidateQueries({ queryKey: ['notification-routes'] }); setSelectedId(value.id); setEditing(null) } })
  const remove = useMutation({ mutationFn: (value: NotificationRoute) => api.deleteNotificationRoute(value.id, value.revision), onSuccess: () => { void client.invalidateQueries({ queryKey: ['notification-routes'] }); setDeleting(null); setSelectedId(null) } })
  const preview = useMutation({ mutationFn: (id: string) => api.previewNotificationRoute(id) })
  if (editing) return <RouteEditor initial={editing.body} onBack={() => setEditing(null)} onSave={body => save.mutate({ ...editing, body })} pending={save.isPending} error={save.error} />
  return <div className={styles.stack}>{routes.isError && <><ErrorState error={routes.error} /><Button onClick={() => void routes.refetch()}>Retry routes</Button></>}
    <ResourceWorkbench title="Administrator routing" description="Personal opt-outs still apply to personal channels. A deliberately configured shared Teams route has its own opt-in."
      items={routes.data ?? []} getId={r => r.id} selectedId={selectedId} onSelect={setSelectedId} listLabel="Notification routes" detailLabel="Selected notification route"
      actions={<Button appearance="primary" onClick={() => { save.reset(); setEditing({ id: null, body: newRoute() }) }}>New route</Button>}
      emptyList={routes.isPending ? <Loading label="Loading notification routes" /> : 'No routes configured.'}
      renderListItem={r => <div className={styles.stack}><strong>{r.name}</strong><Text>{r.channel} · {r.audience}</Text><Badge appearance="outline">{r.enabled ? 'Enabled' : 'Disabled'}</Badge></div>}
      renderDetail={r => <div className={styles.stack}><Title3 as="h3">{r.name}</Title3>
        <dl className={styles.facts}><div><dt>Audience</dt><dd>{r.audience}</dd></div><div><dt>Categories</dt><dd>{r.categories.map(c => NOTIFICATION_CATEGORIES.find(n => n.value === c)?.label).join(', ')}</dd></div><div><dt>Channel / cadence</dt><dd>{r.channel} · {r.cadence.mode} {r.cadence.digestPeriods.join(', ')}</dd></div><div><dt>Time zone</dt><dd>{r.timeZone}</dd></div></dl>
        <div className={styles.toolbar}><Button appearance="primary" onClick={() => { save.reset(); setEditing({ id: r.id, expectedRevision: r.revision, body: { name: r.name, enabled: r.enabled, audience: r.audience, administratorObjectIds: [...r.administratorObjectIds], budgetId: r.budgetId, teamsDestinationId: r.teamsDestinationId, categories: [...r.categories], eventTypes: [...r.eventTypes], channel: r.channel, cadence: structuredClone(r.cadence), timeZone: r.timeZone, digestSchedules: structuredClone(r.digestSchedules) } }) }}>Edit route</Button><Button disabled={preview.isPending} onClick={() => preview.mutate(r.id)}>Preview effective recipients</Button><Button onClick={() => setDeleting(r)}>Delete route</Button></div>
        {preview.isError && preview.variables === r.id && <ErrorState error={preview.error} />}
        {preview.data?.routeId === r.id && <><Title3 as="h4">Effective recipient preview</Title3><Text>Checked {new Date(preview.data.checkedAt).toLocaleString()}. This uses a sample event, sends nothing, and is not evidence of receipt.</Text>
          {preview.data.unavailable.map((reason, i) => <Text key={i}>{reason}</Text>)}
          {!preview.data.recipients.length && <Text>No eligible recipients resolved for this sample event.</Text>}
          {preview.data.recipients.map((recipient, i) => <div key={i}><Text>{recipient.recipientReference} · {recipient.channel} · {recipient.eligible ? 'Eligible' : 'Suppressed'}</Text>{recipient.reasons.map((reason, j) => <Text key={j} block>{reason}</Text>)}<Text>Next delivery: {recipient.nextDeliveryAt ?? 'Not scheduled'}</Text></div>)}</>}
      </div>} />
    <Dialog open={Boolean(deleting)} onOpenChange={(_, d) => { if (!d.open) setDeleting(null) }}><DialogSurface><DialogBody><DialogTitle>Delete notification route?</DialogTitle><DialogContent>Future messages will no longer use {deleting?.name}. Previously accepted provider sends cannot be recalled.{remove.isError && <ErrorState error={remove.error} />}</DialogContent><DialogActions><Button disabled={remove.isPending} onClick={() => setDeleting(null)}>Cancel</Button><Button appearance="primary" disabled={remove.isPending} onClick={() => deleting && remove.mutate(deleting)}>Delete route</Button></DialogActions></DialogBody></DialogSurface></Dialog>
  </div>
}
function RouteEditor({ initial, onBack, onSave, pending, error }: { initial: NotificationRouteInput; onBack: () => void; onSave: (body: NotificationRouteInput) => void; pending: boolean; error: unknown }) {
  const api = useMosaicApi()
  const [draft, setDraft] = useState(() => structuredClone(initial))
  const [eventTypesText, setEventTypesText] = useState(initial.eventTypes.join(', '))
  const choices = useQuery({ queryKey: ['notification-administrator-choices'], queryFn: () => api.getNotificationAdministratorChoices(), retry: false })
  const destinations = useQuery({ queryKey: ['teams-destinations'], queryFn: () => api.listTeamsDestinations(), retry: false })
  const budgets = useQuery({ queryKey: ['budgets'], queryFn: () => api.getBudgets(), enabled: draft.audience === 'budgetRecipients', retry: false })
  const invalid = !draft.name.trim() || !validTimeZone(draft.timeZone) || !draft.categories.length
    || (draft.audience === 'selectedAdmins' && (!draft.administratorObjectIds.length || (draft.enabled && draft.administratorObjectIds.some(id => !choices.data?.administrators.some(a => a.objectId === id && a.available)))))
    || (draft.enabled && draft.audience === 'allAdmins' && (!choices.data?.complete || !choices.data.freshUntil || new Date(choices.data.freshUntil).getTime() <= Date.now()))
    || (draft.audience === 'teamsDestination' && !draft.teamsDestinationId)
    || (draft.audience === 'budgetRecipients' && !draft.budgetId)
    || (draft.cadence.mode === 'digest' && !draft.cadence.digestPeriods.length)
  return <WorkspaceEditorFrame title={initial.name ? `Edit ${initial.name}` : 'New notification route'} onBack={onBack} onCancel={onBack} dirty={JSON.stringify(initial) !== JSON.stringify(draft) || eventTypesText !== initial.eventTypes.join(', ')} pending={pending}
    error={error ? <ErrorState error={error} /> : undefined}
    primaryAction={{ label: 'Save route', onClick: () => onSave({ ...draft, eventTypes: eventTypesText.split(',').map(s => s.trim()).filter(Boolean) }), disabled: invalid }}>
    <div className={styles.stack}>
      <Field label="Route name"><Input value={draft.name} maxLength={120} onChange={(_, d) => setDraft({ ...draft, name: d.value })} /></Field>
      <Field label="Audience"><Select value={draft.audience} onChange={(_, d) => setDraft({ ...draft, audience: d.value as NotificationRouteInput['audience'], administratorObjectIds: [], teamsDestinationId: null, budgetId: null, channel: d.value === 'teamsDestination' ? 'teamsChannel' : d.value === 'budgetRecipients' ? 'email' : 'inApp', cadence: { mode: 'immediate', digestPeriods: [] }, digestSchedules: [] })}>
        <option value="selectedAdmins">Selected MOSAIC administrators</option><option value="allAdmins" disabled={!choices.data?.complete}>All actual MOSAIC administrators</option><option value="teamsDestination">Shared Teams destination</option>{initial.audience === 'budgetRecipients' && <option value="budgetRecipients">Existing budget recipients</option>}
      </Select></Field>
      {choices.isPending && <Loading label="Resolving actual MOSAIC administrators" />}{choices.isError && <><ErrorState error={choices.error} /><Button onClick={() => void choices.refetch()}>Retry administrator discovery</Button></>}
      {choices.data && !choices.data.complete && <Text>Administrator discovery is incomplete: {choices.data.reason ?? 'Required app-role or directory permissions are unavailable.'} All-admin routing is unavailable. You may explicitly choose from verified administrators below.</Text>}
      {draft.audience === 'selectedAdmins' && <div className={styles.stack}>{choices.data?.administrators.map(a => <Checkbox key={a.objectId} label={`${a.displayName}${a.available ? '' : ` · ${a.reason ?? 'Unavailable'}`}`} checked={draft.administratorObjectIds.includes(a.objectId)} disabled={!a.available} onChange={(_, d) => setDraft({ ...draft, administratorObjectIds: d.checked ? [...draft.administratorObjectIds, a.objectId] : draft.administratorObjectIds.filter(id => id !== a.objectId) })} />)}</div>}
      {draft.audience === 'teamsDestination' && <><Text>Shared Teams is a deliberate team route, never a fallback for private user notifications.</Text>{destinations.isError && <ErrorState error={destinations.error} />}
        <Field label="Shared Teams destination"><Select value={draft.teamsDestinationId ?? ''} onChange={(_, d) => setDraft({ ...draft, teamsDestinationId: d.value || null })}><option value="">Select destination</option>{destinations.data?.map(t => <option key={t.id} value={t.id} disabled={!t.enabled}>{t.name}{t.enabled ? '' : ' (disabled)'}</option>)}</Select></Field></>}
      {draft.audience === 'budgetRecipients' && <><Text>Only the budget's existing explicit addresses and owner routing are used. No new forwarding address is accepted.</Text>{budgets.isError && <ErrorState error={budgets.error} />}<Field label="Existing budget"><Select value={draft.budgetId ?? ''} onChange={(_, d) => setDraft({ ...draft, budgetId: d.value || null })}><option value="">Select budget</option>{[...(budgets.data?.organization ? [budgets.data.organization] : []), ...(budgets.data?.costCenters ?? [])].map(b => <option key={b.id} value={b.id}>{b.costCenter?.name ?? 'Organization'}</option>)}</Select></Field></>}
      <div className={styles.days}>{NOTIFICATION_CATEGORIES.map(c => <Checkbox key={c.value} label={c.label} checked={draft.categories.includes(c.value)} onChange={(_, d) => { setEventTypesText(''); setDraft({ ...draft, categories: d.checked ? [...draft.categories, c.value] : draft.categories.filter(v => v !== c.value), eventTypes: [] }) }} />)}</div>
      <Field label="Event filters" hint="Comma-separated catalog event types. Empty means all events in the selected categories; the server validates category membership."><Input value={eventTypesText} onChange={(_, d) => setEventTypesText(d.value)} /></Field>
      <Field label="Delivery channel"><Select value={draft.channel} disabled={draft.audience === 'teamsDestination' || draft.audience === 'budgetRecipients'} onChange={(_, d) => setDraft({ ...draft, channel: d.value as NotificationRouteInput['channel'], cadence: { mode: 'immediate', digestPeriods: [] }, digestSchedules: [] })}><option value="inApp">In-app</option><option value="email">Email</option>{draft.audience === 'teamsDestination' && <option value="teamsChannel">Shared Teams</option>}</Select></Field>
      <CadenceFields label="Route cadence" value={draft.cadence} inApp={draft.channel === 'inApp'} onChange={cadence => setDraft({ ...draft, cadence, digestSchedules: schedulesFor(cadence.digestPeriods, draft.digestSchedules) })} />
      <TimeZoneField value={draft.timeZone} onChange={timeZone => setDraft({ ...draft, timeZone })} />
      <DigestFields value={draft.digestSchedules} onChange={digestSchedules => setDraft({ ...draft, digestSchedules })} />
      <Checkbox label="Enable this notification route" checked={draft.enabled} onChange={(_, d) => setDraft({ ...draft, enabled: Boolean(d.checked) })} />
      {invalid && <Text role="status">Choose a name, available authorized audience, categories, valid time zone, and explicit digest periods before saving.</Text>}
    </div>
  </WorkspaceEditorFrame>
}
function TeamsDestinations() {
  const api = useMosaicApi()
  const destinations = useQuery({ queryKey: ['teams-destinations'], queryFn: () => api.listTeamsDestinations(), retry: false })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ id: string | null; body: TeamsDestinationInput } | null>(null)
  const [deleting, setDeleting] = useState<TeamsDestination | null>(null)
  const [testing, setTesting] = useState<TeamsDestination | null>(null)
  const test = useMutation({ mutationFn: (id: string) => api.testTeamsDestination(id) })
  const save = useMutation({ mutationFn: (value: { id: string | null; body: TeamsDestinationInput }) => api.saveTeamsDestination(value.id, value.body), onSuccess: value => { setEditing(null); setSelectedId(value.id); void destinations.refetch() } })
  const remove = useMutation({ mutationFn: (id: string) => api.deleteTeamsDestination(id), onSuccess: () => { setDeleting(null); setSelectedId(null); void destinations.refetch() } })
  if (editing) return <TeamsDestinationEditor initial={editing.body} existing={Boolean(editing.id)} onBack={() => setEditing(null)} onSave={body => save.mutate({ id: editing.id, body })} pending={save.isPending} error={save.error} />
  return <div className={styles.stack}>
    <Text>Use Teams Workflows HTTPS webhook destinations for explicitly configured shared-channel notifications. Webhook credentials are write-only and stored through Key Vault.</Text>
    {destinations.isError && <><ErrorState error={destinations.error} /><Button onClick={() => void destinations.refetch()}>Retry Teams destinations</Button></>}
    <ResourceWorkbench title="Shared Teams destinations" items={destinations.data ?? []} getId={d => d.id} selectedId={selectedId} onSelect={setSelectedId} listLabel="Teams destinations" detailLabel="Selected Teams destination"
      actions={<Button appearance="primary" onClick={() => { save.reset(); setEditing({ id: null, body: { name: '', enabled: false, allowedCategories: ['operationalIssues'] } }) }}>New Teams destination</Button>}
      emptyList={destinations.isPending ? <Loading label="Loading Teams destinations" /> : 'No shared Teams destinations.'}
      renderListItem={d => <div className={styles.stack}><strong>{d.name}</strong><Badge appearance="outline">{d.enabled ? 'Enabled' : 'Disabled'}</Badge></div>}
      renderDetail={d => <div className={styles.stack}><Title3 as="h3">{d.name}</Title3><Text>{d.allowedCategories.map(c => NOTIFICATION_CATEGORIES.find(n => n.value === c)?.label).join(', ')}</Text><Text>Credential is not returned. Saved configuration is not verified delivery.</Text>
        <div className={styles.toolbar}><Button appearance="primary" onClick={() => { save.reset(); setEditing({ id: d.id, body: { name: d.name, enabled: d.enabled, allowedCategories: [...d.allowedCategories] } }) }}>Edit destination</Button><Button onClick={() => { test.reset(); setTesting(d) }}>Test destination</Button><Button onClick={() => setDeleting(d)}>Delete destination</Button></div></div>} />
    <Dialog open={Boolean(testing)} onOpenChange={(_, d) => { if (!d.open && !test.isPending) setTesting(null) }}><DialogSurface><DialogBody><DialogTitle>Send a shared Teams test?</DialogTitle><DialogContent>
      This sends a synthetic MOSAIC message to {testing?.name}. Provider acceptance is not confirmed user delivery.
      {test.isError && <ErrorState error={test.error} />}
      {test.data && <Text block>{test.data.outcome}: {test.data.message} · {new Date(test.data.testedAt).toLocaleString()}. {test.data.confirmedDelivered ? 'Confirmed delivery evidence reported.' : 'No confirmed delivery evidence.'}</Text>}
    </DialogContent><DialogActions><Button disabled={test.isPending} onClick={() => setTesting(null)}>Close</Button><Button appearance="primary" disabled={test.isPending || test.isSuccess} onClick={() => testing && test.mutate(testing.id)}>Send synthetic Teams test</Button></DialogActions></DialogBody></DialogSurface></Dialog>
    <Dialog open={Boolean(deleting)} onOpenChange={(_, d) => { if (!d.open) setDeleting(null) }}><DialogSurface><DialogBody><DialogTitle>Delete Teams destination?</DialogTitle><DialogContent>Routes using {deleting?.name} will no longer be able to deliver there. Existing provider sends cannot be recalled.{remove.isError && <ErrorState error={remove.error} />}</DialogContent><DialogActions><Button disabled={remove.isPending} onClick={() => setDeleting(null)}>Cancel</Button><Button appearance="primary" disabled={remove.isPending} onClick={() => deleting && remove.mutate(deleting.id)}>Delete destination</Button></DialogActions></DialogBody></DialogSurface></Dialog>
  </div>
}
function PersonalTeamsRegistration() {
  const api = useMosaicApi()
  const settings = useQuery({ queryKey: ['personal-teams-settings'], queryFn: () => api.getPersonalTeamsSettings(), retry: false })
  const [draft, setDraft] = useState<PersonalTeamsSettingsInput | null>(null)
  const save = useMutation({ mutationFn: (body: PersonalTeamsSettingsInput) => api.savePersonalTeamsSettings(body), onSuccess: () => { setDraft(null); void settings.refetch() } })
  if (draft) return <WorkspaceEditorFrame title="Configure the personal Teams app" description="These are application registration IDs, not a place to paste tokens or client secrets. Publishing/installing the Teams app and consent remain explicit external administrator tasks."
    onBack={() => setDraft(null)} onCancel={() => setDraft(null)} dirty pending={save.isPending} error={save.isError ? <ErrorState error={save.error} /> : undefined}
    primaryAction={{ label: 'Save personal Teams configuration', onClick: () => save.mutate(draft), disabled: draft.cloud !== 'public' || (draft.enabled && (!draft.appId?.trim() || !draft.botAppId?.trim())) }}>
    <div className={styles.stack}>
      <Field label="Teams app ID"><Input value={draft.appId ?? ''} onChange={(_, d) => setDraft({ ...draft, appId: d.value || null })} /></Field>
      <Field label="Bot application ID"><Input value={draft.botAppId ?? ''} onChange={(_, d) => setDraft({ ...draft, botAppId: d.value || null })} /></Field>
      <Field label="Teams cloud"><Select value={draft.cloud} onChange={(_, d) => setDraft({ ...draft, cloud: d.value as PersonalTeamsSettingsInput['cloud'] })}><option value="public">Public cloud</option><option value="usGovernment" disabled>US Government (not supported)</option></Select></Field>
      <Checkbox label="Enable the optional personal Teams integration" checked={draft.enabled} onChange={(_, d) => setDraft({ ...draft, enabled: Boolean(d.checked) })} />
    </div>
  </WorkspaceEditorFrame>
  return <section className={styles.section}><Title3 as="h2">Personal Teams app</Title3><Text>Private notifications use an authenticated personal-scope MOSAIC bot conversation, never a shared-channel destination. A user must install the app before a personal binding exists.</Text>
    {settings.isPending && <Loading label="Loading personal Teams app configuration" />}
    {settings.isError && <><ErrorState error={settings.error} /><Button onClick={() => void settings.refetch()}>Retry personal Teams configuration</Button></>}
    {settings.data && <><dl className={styles.facts}><div><dt>Saved state</dt><dd>{settings.data.enabled ? 'Enabled' : 'Disabled'}</dd></div><div><dt>Teams app</dt><dd>{settings.data.appId ?? 'Not configured'}</dd></div><div><dt>Bot application</dt><dd>{settings.data.botAppId ?? 'Not configured'}</dd></div><div><dt>Cloud</dt><dd>{settings.data.cloud}</dd></div></dl>
      <Button appearance="primary" onClick={() => { const s = settings.data!; setDraft({ enabled: s.enabled, appId: s.appId, botAppId: s.botAppId, cloud: s.cloud }); save.reset() }}>Edit personal Teams app</Button></>}
    <RouterLink to="/profile">Review my personal channel readiness</RouterLink>
  </section>
}
function TeamsDestinationEditor({ initial, existing, onBack, onSave, pending, error }: { initial: TeamsDestinationInput; existing: boolean; onBack: () => void; onSave: (body: TeamsDestinationInput) => void; pending: boolean; error: unknown }) {
  const [draft, setDraft] = useState(() => structuredClone(initial))
  const [url, setUrl] = useState('')
  const invalid = !draft.name.trim() || !draft.allowedCategories.length || (!existing && !url.trim()) || (Boolean(url) && !/^https:\/\//i.test(url))
  return <WorkspaceEditorFrame title={existing ? 'Edit Teams destination' : 'New Teams destination'} onBack={onBack} onCancel={onBack} dirty={Boolean(url) || JSON.stringify(initial) !== JSON.stringify(draft)} pending={pending}
    description="Only supported Teams Workflows URLs are accepted. The secret never enters browser storage, URLs, or read responses."
    error={error ? <ErrorState error={error} /> : undefined}
    primaryAction={{ label: 'Save Teams destination', onClick: () => onSave({ ...draft, ...(url.trim() ? { webhookUrl: url.trim() } : {}) }), disabled: invalid }}>
    <div className={styles.stack}>
      <Field label="Destination name"><Input value={draft.name} maxLength={120} onChange={(_, d) => setDraft({ ...draft, name: d.value })} /></Field>
      <Field label={existing ? 'Replace Workflows webhook URL' : 'Workflows webhook URL'} hint={existing ? 'Leave blank to retain the stored credential.' : 'Create a supported Teams Workflow first; paste its HTTPS URL here.'}>
        <Input type="password" autoComplete="off" value={url} onChange={(_, d) => setUrl(d.value)} />
      </Field>
      <div className={styles.days}>{NOTIFICATION_CATEGORIES.map(c => <Checkbox key={c.value} label={c.label} checked={draft.allowedCategories.includes(c.value)} onChange={(_, d) => setDraft({ ...draft, allowedCategories: d.checked ? [...draft.allowedCategories, c.value] : draft.allowedCategories.filter(v => v !== c.value) })} />)}</div>
      <Checkbox label="Enable this shared Teams destination" checked={draft.enabled} onChange={(_, d) => setDraft({ ...draft, enabled: Boolean(d.checked) })} />
    </div>
  </WorkspaceEditorFrame>
}
