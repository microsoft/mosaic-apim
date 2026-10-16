import {
  Button, Checkbox, Field, Input, Select,
  Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { usePortalApi } from '../api'
import { ErrorState, Loading } from '../components/AsyncState'
import { NotificationReadiness } from '../components/NotificationReadiness'
import { EffectiveNotificationPreferences } from '../components/EffectiveNotificationPreferences'
import { PersonalTeamsReadiness } from '../components/PersonalTeamsReadiness'
import { PageHeader } from '../components/PageHeader'
import { UnsavedNotificationChanges } from '../components/UnsavedNotificationChanges'
import {
  defaultDigestSchedule, digestPeriods, notificationCategories, preferenceInput, preferenceValidation, weekdayLabels,
} from '../notification-format'
import { useNotificationKeys } from '../notification-queries'
import type {
  CategoryNotificationPreference, ChannelCadence, DigestSchedule,
  NotificationPreferences, NotificationPreferencesInput,
} from '../notification-types'

function CadenceControl({ label, value, onChange, disabled }: {
  label: string; value: ChannelCadence; onChange: (value: ChannelCadence) => void; disabled: boolean
}) {
  return <div className="notification-cadence">
    <Field label={label}>
      <Select value={value.mode} disabled={disabled} onChange={(_, data) => {
        const mode = data.value as ChannelCadence['mode']
        onChange({ mode, digestPeriods: mode === 'digest' ? (value.digestPeriods.length ? value.digestPeriods : ['daily']) : [] })
      }}>
        <option value="off">Off</option>
        <option value="immediate">Immediate</option>
        <option value="digest">Digests</option>
      </Select>
    </Field>
    {value.mode === 'digest' && <fieldset className="notification-periods">
      <legend>{label} periods</legend>
      {digestPeriods.map((period) => <Checkbox key={period} label={period[0].toUpperCase() + period.slice(1)}
        checked={value.digestPeriods.includes(period)} disabled={disabled}
        onChange={(_, data) => onChange({
          mode: 'digest',
          digestPeriods: data.checked ? [...value.digestPeriods, period] : value.digestPeriods.filter((item) => item !== period),
        })} />)}
    </fieldset>}
  </div>
}

function PreferencesEditor({ initial }: { initial: NotificationPreferences }) {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const queryClient = useQueryClient()
  const [baseline, setBaseline] = useState(() => preferenceInput(initial))
  const [revision, setRevision] = useState(initial.revision)
  const [draft, setDraft] = useState(() => preferenceInput(initial))
  const [thresholdText, setThresholdText] = useState(() => initial.thresholds.join(', '))
  const [validation, setValidation] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline) || thresholdText !== baseline.thresholds.join(', ')
  const save = useMutation({
    mutationFn: (input: NotificationPreferencesInput) => api.saveNotificationPreferences(input, revision),
    onSuccess: (result) => {
      const input = preferenceInput(result)
      setBaseline(input)
      setRevision(result.revision)
      setDraft(input)
      setThresholdText(input.thresholds.join(', '))
      setSaved(true)
      queryClient.setQueryData(keys.preferences, result)
      void queryClient.invalidateQueries({ queryKey: [...keys.preferences, 'preview'] })
      void queryClient.invalidateQueries({ queryKey: keys.health })
    },
  })
  function update(next: NotificationPreferencesInput) {
    setDraft(next)
    setSaved(false)
    setValidation(null)
    save.reset()
  }
  function changeCategory(category: CategoryNotificationPreference) {
    const requested = new Set(draft.categories.flatMap((item) => {
      const next = item.category === category.category ? category : item
      return [...next.email.digestPeriods, ...next.teams.digestPeriods]
    }))
    update({
      ...draft,
      categories: draft.categories.map((item) => item.category === category.category ? category : item),
      digestSchedules: [...draft.digestSchedules, ...[...requested]
        .filter((period) => !draft.digestSchedules.some((schedule) => schedule.period === period)).map(defaultDigestSchedule)],
    })
  }
  function changeSchedule(schedule: DigestSchedule) {
    update({ ...draft, digestSchedules: draft.digestSchedules.map((item) => item.period === schedule.period ? schedule : item) })
  }
  const activePeriods = digestPeriods.filter((period) => draft.categories.some((item) =>
    item.email.digestPeriods.includes(period) || item.teams.digestPeriods.includes(period)))
  return <>
    <UnsavedNotificationChanges dirty={dirty || save.isPending} />
    <form className="notification-preferences" onSubmit={(event) => {
      event.preventDefault()
      const thresholds = thresholdText.split(',').map((value) => value.trim() === '' ? NaN : Number(value.trim()))
      const input = { ...draft, thresholds }
      const error = preferenceValidation(input)
      setValidation(error)
      if (!error) save.mutate(input)
    }}>
      <section className="notification-section" aria-labelledby="notification-categories-heading">
        <h2 id="notification-categories-heading">Categories & channels</h2>
        <p>These choices apply only to your portal account. In-app is on by default; email, private Teams, and digests require your opt-in.
          Saving a choice does not enable an unready channel.</p>
        <div className="notification-category-list">
          {draft.categories.map((item) => {
            const category = notificationCategories.find((category) => category.value === item.category)!
            return <fieldset key={item.category} className="notification-category" disabled={save.isPending}>
              <legend>{category.label}</legend>
              <p>{category.description}</p>
              <div className="notification-channel-controls">
                <Checkbox label={`In-app — ${category.label}`} checked={item.inApp}
                  onChange={(_, data) => changeCategory({ ...item, inApp: data.checked === true })} />
                <CadenceControl label={`Email — ${category.label}`} value={item.email} disabled={save.isPending}
                  onChange={(email) => changeCategory({ ...item, email })} />
                <CadenceControl label={`Private Teams — ${category.label}`} value={item.teams} disabled={save.isPending}
                  onChange={(teams) => changeCategory({ ...item, teams })} />
              </div>
            </fieldset>
          })}
        </div>
      </section>
      <section className="notification-section" aria-labelledby="notification-schedule-heading">
        <h2 id="notification-schedule-heading">Digest schedules</h2>
        <p>Digests summarize completed periods. Choose periods separately for each category and external channel above.</p>
        <Field label="Time zone" hint="IANA name, for example UTC, Europe/London, or America/New_York. Schedules use local wall time.">
          <Input value={draft.timeZone} disabled={save.isPending}
            onChange={(_, data) => update({ ...draft, timeZone: data.value })} />
        </Field>
        {activePeriods.length === 0 && <p className="notification-meta">No digests are enabled. Select Digests for email or private Teams to set a schedule.</p>}
        <div className="notification-schedules">
          {activePeriods.map((period) => {
            const schedule = draft.digestSchedules.find((item) => item.period === period)!
            const name = period[0].toUpperCase() + period.slice(1)
            return <fieldset key={period} className="notification-schedule" disabled={save.isPending}>
              <legend>{name} digest</legend>
              <Field label={`${name} delivery time`}>
                <Input type="time" required value={schedule.deliveryTime}
                  onChange={(_, data) => changeSchedule({ ...schedule, deliveryTime: data.value })} />
              </Field>
              {period === 'weekly' && <Field label="Weekly delivery day">
                <Select value={String(schedule.dayOfWeek)} onChange={(_, data) => changeSchedule({ ...schedule, dayOfWeek: Number(data.value) })}>
                  {weekdayLabels.map((day, index) => <option key={day} value={index}>{day}</option>)}
                </Select>
              </Field>}
              {period === 'monthly' && <Field label="Monthly delivery day" hint="Days 1–28 work in every month.">
                <Input type="number" min={1} max={28} step={1} required value={String(schedule.dayOfMonth)}
                  onChange={(_, data) => changeSchedule({ ...schedule, dayOfMonth: Number(data.value) })} />
              </Field>}
            </fieldset>
          })}
        </div>
      </section>
      <section className="notification-section" aria-labelledby="notification-threshold-heading">
        <h2 id="notification-threshold-heading">Usage & budget alerts</h2>
        <p>Thresholds notify; they do not change grant limits or budget enforcement. Missing or unpriced telemetry is not zero usage.
          Shared budget totals are not a report of other members’ activity.</p>
        <div className="notification-form-grid">
          <Field label="Threshold percentages" hint="One to five whole numbers, separated by commas (1–1000).">
            <Input value={thresholdText} disabled={save.isPending} onChange={(_, data) => {
              setThresholdText(data.value); setSaved(false); setValidation(null); save.reset()
            }} />
          </Field>
          <Field label="Repeat cooldown (seconds)" hint="300–2678400 seconds (5 minutes–31 days).">
            <Input type="number" min={300} max={2678400} step={1} required disabled={save.isPending}
              value={String(draft.repeatCooldownSeconds)}
              onChange={(_, data) => update({ ...draft, repeatCooldownSeconds: Number(data.value) })} />
          </Field>
        </div>
      </section>
      <section className="notification-section" aria-labelledby="notification-quiet-heading">
        <h2 id="notification-quiet-heading">Quiet hours</h2>
        <Checkbox label="Use quiet hours" disabled={save.isPending} checked={draft.quietHours !== null}
          onChange={(_, data) => update({ ...draft, quietHours: data.checked ?
            { daysOfWeek: [0, 1, 2, 3, 4, 5, 6], startTime: '22:00', endTime: '08:00' } : null })} />
        {draft.quietHours && <fieldset className="notification-quiet-hours" disabled={save.isPending}>
          <legend>Quiet hours in {draft.timeZone || 'your selected time zone'}</legend>
          <p>Days name the start of the quiet window, including windows that cross midnight.</p>
          <div className="notification-weekdays">{weekdayLabels.map((day, index) => <Checkbox key={day} label={day}
            checked={draft.quietHours!.daysOfWeek.includes(index)} onChange={(_, data) => update({
              ...draft, quietHours: { ...draft.quietHours!, daysOfWeek: data.checked
                ? [...draft.quietHours!.daysOfWeek, index].sort()
                : draft.quietHours!.daysOfWeek.filter((value) => value !== index) },
            })} />)}</div>
          <div className="notification-form-grid">
            <Field label="Quiet hours start"><Input type="time" required value={draft.quietHours.startTime}
              onChange={(_, data) => update({ ...draft, quietHours: { ...draft.quietHours!, startTime: data.value } })} /></Field>
            <Field label="Quiet hours end"><Input type="time" required value={draft.quietHours.endTime}
              onChange={(_, data) => update({ ...draft, quietHours: { ...draft.quietHours!, endTime: data.value } })} /></Field>
          </div>
        </fieldset>}
      </section>
      <div className="notification-save">
        <p>Changes take effect before a delivery is claimed. Messages already accepted by a provider cannot be recalled.</p>
        {validation && <p role="alert" className="form-error">{validation}</p>}
        {save.isError && <p role="alert" className="form-error">Preferences were not saved. {save.error.message} Your draft is retained; try again.</p>}
        {saved && <p role="status">Preferences saved. Check delivery readiness below; saved settings do not prove delivery.</p>}
        <div className="notification-actions">
          <Button type="submit" appearance="primary" disabled={!dirty || save.isPending}>{save.isPending ? 'Saving…' : 'Save preferences'}</Button>
          <Button type="button" disabled={!dirty || save.isPending} onClick={() => setResetOpen(true)}>Reset changes</Button>
          {dirty && <span className="notification-meta">Unsaved changes</span>}
        </div>
      </div>
    </form>
    {resetOpen && <Dialog open onOpenChange={(_, data) => setResetOpen(data.open)}>
      <DialogSurface><DialogBody>
        <DialogTitle>Reset unsaved changes?</DialogTitle>
        <DialogContent>This returns to your last saved notification preferences.</DialogContent>
        <DialogActions>
          <Button appearance="primary" onClick={() => setResetOpen(false)}>Keep editing</Button>
          <Button onClick={() => {
            setDraft(baseline); setThresholdText(baseline.thresholds.join(', ')); setValidation(null)
            setSaved(false); save.reset(); setResetOpen(false)
          }}>Reset changes</Button>
        </DialogActions>
      </DialogBody></DialogSurface>
    </Dialog>}
  </>
}

export function NotificationPreferencesPage() {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const preferences = useQuery({ queryKey: keys.preferences, queryFn: api.getNotificationPreferences })
  return <>
    <PageHeader title="Notification preferences" description="Choose what reaches you, where, and when."
      actions={<Link to="/notifications">Back to notifications</Link>} />
    {preferences.isPending && <Loading label="Loading your notification preferences" />}
    {preferences.isError && <div className="notification-section">
      <ErrorState error={preferences.error} />
      <Button onClick={() => void preferences.refetch()} disabled={preferences.isFetching}>Retry preferences</Button>
    </div>}
    {preferences.isSuccess && <PreferencesEditor key={`editor:${keys.owner}`} initial={preferences.data} />}
    <NotificationReadiness />
    <PersonalTeamsReadiness key={`teams:${keys.owner}`} />
    <EffectiveNotificationPreferences />
  </>
}
