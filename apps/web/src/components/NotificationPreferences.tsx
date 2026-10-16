import { Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import { NOTIFICATION_CATEGORIES, type NotificationPreferencesInput, type CategoryNotificationPreference } from '../admin-types'
import { ErrorState, Loading } from './AsyncState'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { CadenceFields, DigestFields, TimeWindowFields, TimeZoneField } from './GovernanceFields'
import { notificationPreferencesInput, schedulesFor, validTimeZone } from '../admin-governance'
import { CapabilityEvidence } from './NotificationSettings'
import styles from './AdminGovernance.module.css'

export function NotificationPreferences({ onEditingChange }: { onEditingChange?: (editing: boolean) => void }) {
  const api = useMosaicApi()
  const client = useQueryClient()
  const preferences = useQuery({ queryKey: ['admin-notification-preferences'], queryFn: () => api.getNotificationPreferences(), retry: false })
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<NotificationPreferencesInput | null>(null)
  const [draftRevision, setDraftRevision] = useState<number | undefined>()
  const [thresholdText, setThresholdText] = useState('')
  const [showPreview, setShowPreview] = useState(false)
  const [testTeamsOpen, setTestTeamsOpen] = useState(false)
  const effective = useQuery({ queryKey: ['admin-notification-preferences-preview'], queryFn: () => api.previewNotificationPreferences(), enabled: showPreview, retry: false })
  const testTeams = useMutation({ mutationFn: () => api.testMyTeamsConnection() })
  function closeEditor() { setEditing(false); onEditingChange?.(false) }
  const save = useMutation({
    mutationFn: (body: NotificationPreferencesInput) => api.saveNotificationPreferences(body, draftRevision),
    onSuccess: value => { client.setQueryData(['admin-notification-preferences'], value); void client.invalidateQueries({ queryKey: ['admin-notification-preferences-preview'] }); closeEditor(); setDraft(null) },
  })
  function updateCategory(category: CategoryNotificationPreference) {
    if (!draft) return
    const categories = draft.categories.map(c => c.category === category.category ? category : c)
    setDraft({ ...draft, categories, digestSchedules: schedulesFor(categories.flatMap(c => [...c.email.digestPeriods, ...c.teams.digestPeriods]), draft.digestSchedules) })
  }
  if (editing && draft) {
    const thresholds = thresholdText.split(',').map(s => s.trim() ? Number(s.trim()) : NaN)
    const invalid = !validTimeZone(draft.timeZone) || draft.categories.some(c => [c.email, c.teams].some(channel => channel.mode === 'digest' && !channel.digestPeriods.length))
      || (draft.quietHours && (!draft.quietHours.daysOfWeek.length || draft.quietHours.startTime === draft.quietHours.endTime))
      || thresholds.length > 5 || thresholds.some(t => !Number.isInteger(t) || t < 1 || t > 1000)
      || !Number.isInteger(draft.repeatCooldownSeconds) || draft.repeatCooldownSeconds < 300 || draft.repeatCooldownSeconds > 2678400
    return <WorkspaceEditorFrame title="Personal notification preferences" description="These settings apply only to your administrator persona, not your portal preferences. Already accepted provider sends cannot be recalled."
      onBack={closeEditor} onCancel={closeEditor} dirty={JSON.stringify({ ...draft, thresholds }) !== JSON.stringify(preferences.data ? notificationPreferencesInput(preferences.data) : null)} pending={save.isPending}
      error={save.error ? <ErrorState error={save.error} /> : undefined}
      primaryAction={{ label: 'Save preferences', onClick: () => save.mutate({ ...draft, thresholds }), disabled: Boolean(invalid) }}>
      <div className={styles.stack}>
        <TimeZoneField value={draft.timeZone} onChange={timeZone => setDraft({ ...draft, timeZone })} />
        {NOTIFICATION_CATEGORIES.map(({ value, label }) => {
          const category = draft.categories.find(c => c.category === value)
          if (!category) return null
          return <section className={styles.section} key={value} aria-label={label}>
            <Title3 as="h3">{label}</Title3>
            <div className={styles.form}>
              <Checkbox label="In-app notifications" checked={category.inApp} onChange={(_, d) => updateCategory({ ...category, inApp: Boolean(d.checked) })} />
              <CadenceFields label={`${label} email`} value={category.email} onChange={email => updateCategory({ ...category, email })} />
              <CadenceFields label={`${label} personal Teams`} value={category.teams} onChange={teams => updateCategory({ ...category, teams })} />
            </div>
          </section>
        })}
        <Text>Email, Teams, and recurring summaries are opt-in. Selecting a channel does not provision it. Private Teams is never redirected to a shared administrator channel.</Text>
        <DigestFields value={draft.digestSchedules} onChange={digestSchedules => setDraft({ ...draft, digestSchedules })} />
        <Checkbox label="Enable quiet hours" checked={Boolean(draft.quietHours)} onChange={(_, d) => setDraft({ ...draft, quietHours: d.checked ? { daysOfWeek: [0, 1, 2, 3, 4, 5, 6], startTime: '18:00', endTime: '09:00' } : null })} />
        {draft.quietHours && <TimeWindowFields value={draft.quietHours} onChange={quietHours => setDraft({ ...draft, quietHours })} />}
        <Field label="Usage notification thresholds (%)" hint="Comma-separated percentages. Summaries and alerts do not create new spending caps.">
          <Input value={thresholdText} onChange={(_, d) => setThresholdText(d.value)} />
        </Field>
        <Field label="Repeat cooldown (seconds)"><Input type="number" min={300} max={2678400} value={String(draft.repeatCooldownSeconds)} onChange={(_, d) => setDraft({ ...draft, repeatCooldownSeconds: Number(d.value) })} /></Field>
        {invalid && <Text role="status">Choose a valid time zone, explicit digest periods, distinct quiet-hour times, and thresholds from 1 to 1000.</Text>}
      </div>
    </WorkspaceEditorFrame>
  }
  return <section className={styles.section} aria-label="Personal notifications">
    <div className={styles.toolbar}><Title3 as="h2">Personal notifications</Title3>
      <Button disabled={!preferences.data} onClick={() => { setDraft(notificationPreferencesInput(preferences.data!)); setDraftRevision(preferences.data!.revision); setThresholdText(preferences.data!.thresholds.join(', ')); setEditing(true); onEditingChange?.(true); save.reset() }}>Edit preferences</Button>
      <Button onClick={() => { setShowPreview(true); if (showPreview) void effective.refetch() }}>Preview effective delivery</Button></div>
    <Text>Administrator and portal preferences are independent. In-app defaults on; external channels and summaries require your opt-in.</Text>
    {preferences.isPending && <Loading label="Loading personal notification preferences" />}
    {preferences.isError && <><ErrorState error={preferences.error} /><Button onClick={() => void preferences.refetch()}>Retry preferences</Button></>}
    {preferences.data && <><Text>Time zone: {preferences.data.timeZone}</Text>
      <div className={styles.scroll}><table className={styles.table}><caption>Saved administrator preferences</caption><thead><tr><th>Category</th><th>In-app</th><th>Email</th><th>Personal Teams</th></tr></thead>
        <tbody>{preferences.data.categories.map(c => <tr key={c.category}><th scope="row">{NOTIFICATION_CATEGORIES.find(n => n.value === c.category)?.label}</th>
          <td>{c.inApp ? 'On' : 'Off'}</td><td>{c.email.mode}{c.email.digestPeriods.length ? ` · ${c.email.digestPeriods.join(', ')}` : ''}</td><td>{c.teams.mode}{c.teams.digestPeriods.length ? ` · ${c.teams.digestPeriods.join(', ')}` : ''}</td></tr>)}</tbody></table></div></>}
    {showPreview && <div className={styles.stack}>
      <Title3 as="h3">Effective delivery preview</Title3>
      {effective.isPending && <Loading label="Checking personal channel availability" />}
      {effective.isError && <ErrorState error={effective.error} />}
      {effective.data && <><Text>Preview uses saved preferences. Availability is not proof of receipt.</Text>
        <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Category</th><th>Channel</th><th>Cadence</th><th>Availability</th><th>Next delivery</th></tr></thead>
          <tbody>{effective.data.channels.map(c => <tr key={`${c.category}-${c.channel}`}><th scope="row">{NOTIFICATION_CATEGORIES.find(n => n.value === c.category)?.label}</th><td>{c.channel}</td><td>{c.mode} {c.digestPeriods.join(', ')}</td><td>{c.available ? 'Available' : 'Unavailable'}{c.reasons.map((reason, i) => <div key={i}>{reason}</div>)}</td><td>{c.nextDeliveryAt ? new Date(c.nextDeliveryAt).toLocaleString() : 'Not scheduled'}</td></tr>)}</tbody></table></div>
        {effective.data.checks.map((check, i) => <CapabilityEvidence key={i} check={check} />)}
        <Button onClick={() => { testTeams.reset(); setTestTeamsOpen(true) }}>Test my personal Teams connection</Button></>}
    </div>}
    <Dialog open={testTeamsOpen} onOpenChange={(_, d) => { if (!testTeams.isPending) setTestTeamsOpen(d.open) }}><DialogSurface><DialogBody><DialogTitle>Send a private Teams test?</DialogTitle><DialogContent>
      This sends a synthetic message only to your authenticated personal Teams conversation. An unavailable private channel is never redirected to a shared administrator destination.
      {testTeams.isError && <ErrorState error={testTeams.error} />}
      {testTeams.data && <Text block>{testTeams.data.outcome}: {testTeams.data.message}. Provider acceptance is not proof of user delivery.</Text>}
    </DialogContent><DialogActions><Button disabled={testTeams.isPending} onClick={() => setTestTeamsOpen(false)}>Close</Button><Button appearance="primary" disabled={testTeams.isPending || testTeams.isSuccess} onClick={() => testTeams.mutate()}>Send private synthetic test</Button></DialogActions></DialogBody></DialogSurface></Dialog>
  </section>
}
