import { Button, Checkbox, Field, Input, Radio, RadioGroup, Select, Text, Title3 } from '@fluentui/react-components'
import { useEffect, useRef, useState } from 'react'
import type { ChannelCadence, DigestPeriod, DigestSchedule, WeeklyTimeWindow } from '../admin-types'
import {
  QUOTA_PERIODS, buildEnforcement, callOnlyLimitForm, copyLimitForm, describeLimits, describePersonLimits,
  emptyLimitForm, hasLimitValues, limitFieldErrors, limitFormFromEnforcement,
  limitFormFromPerson, limitFormsEqual, type LimitForm, type LimitNumberField,
} from '../entitlement-limits'
import type { EntitlementEnforcement, PersonLimits, QuotaPeriod } from '../types'
import styles from './AdminGovernance.module.css'

export type LimitTargetContext = 'managed' | 'desiredOnly' | 'conditional'

export function TimeZoneField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <Field label="Time zone" hint="IANA zone, such as UTC or America/New_York. Local time observes daylight saving.">
    <Input value={value} onChange={(_, data) => onChange(data.value)} required />
  </Field>
}
export function LimitFields({
  value, onChange, mcp = false, baseline, reviewLimits, reviewLabel, reviewDescription, inherited,
  inheritedLabel = 'Cost-center per-person defaults', showSourceChoice = true, requireLimit = false,
  targetContext = 'conditional',
}: {
  value: LimitForm
  onChange: (value: LimitForm) => void
  mcp?: boolean
  baseline?: EntitlementEnforcement | null
  reviewLimits?: EntitlementEnforcement | null
  reviewLabel?: string
  reviewDescription?: string
  inherited?: PersonLimits | null
  inheritedLabel?: string
  showSourceChoice?: boolean
  requireLimit?: boolean
  targetContext?: LimitTargetContext
}) {
  const [editing, setEditing] = useState(hasLimitValues(value))
  const explicitDraft = useRef<LimitForm | undefined>(hasLimitValues(value) ? copyLimitForm(value) : undefined)
  const numberInputs = useRef(new Map<LimitNumberField, HTMLInputElement>())
  const previousValue = useRef(copyLimitForm(value))
  useEffect(() => {
    if (!limitFormsEqual(value, previousValue.current)) {
      setEditing(hasLimitValues(value))
      if (hasLimitValues(value)) explicitDraft.current = copyLimitForm(value)
    }
    previousValue.current = copyLimitForm(value)
  }, [value])
  const original = baseline ? limitFormFromEnforcement(baseline) : undefined
  const errors = limitFieldErrors(value, original)
  const configured = hasLimitValues(value)
  const defaultText = inherited
    ? describePersonLimits(inherited)
    : inherited === null ? 'No per-person defaults are configured here.'
      : 'The service resolves the cost-center defaults. No explicit grant limit is configured.'
  const previous = reviewLimits === undefined ? baseline : reviewLimits
  const before = reviewDescription ? [reviewDescription] : previous ? describeLimits({ enforcement: previous }) : [defaultText]
  const after = Object.keys(errors).length
    ? ['Correct the highlighted limits before saving.']
    : configured ? describeLimits({ enforcement: buildEnforcement(value, true, baseline) })
      : requireLimit ? ['Enter at least one positive grant limit.'] : [defaultText]
  function update(next: LimitForm) {
    const limits = mcp ? callOnlyLimitForm(next) : copyLimitForm(next)
    if (hasLimitValues(limits)) explicitDraft.current = limits
    setEditing(hasLimitValues(limits))
    onChange(limits)
  }
  function chooseSource(source: string) {
    if (source === 'inherited') {
      if (configured) explicitDraft.current = copyLimitForm(value)
      for (const [key, input] of numberInputs.current) input.value = emptyLimitForm[key]
      setEditing(false)
      onChange({ ...emptyLimitForm })
    } else {
      setEditing(true)
      if (explicitDraft.current) update(explicitDraft.current)
      else if (inherited) update(limitFormFromPerson(inherited))
    }
  }
  function numberChanged(key: LimitNumberField, input: HTMLInputElement) {
    update({
      ...value, [key]: input.value,
      invalidNumbers: { ...value.invalidNumbers, [key]: input.validity.badInput },
    })
  }
  function clearNumber(key: LimitNumberField) {
    const input = numberInputs.current.get(key)
    if (input) input.value = ''
    update({ ...value, [key]: '', invalidNumbers: { ...value.invalidNumbers, [key]: false } })
  }
  const numberField = (key: LimitNumberField, label: string, hint?: string) =>
    <Field label={label} hint={hint} validationState={errors[key] ? 'error' : undefined} validationMessage={errors[key]}>
      <Input type="number" min={1} step={1} value={value[key]}
        onChange={event => numberChanged(key, event.target)}
        input={{
          ref: input => { if (input) numberInputs.current.set(key, input); else numberInputs.current.delete(key) },
          onInput: event => numberChanged(key, event.currentTarget),
        }} />
    </Field>
  const quotaPeriod = (key: 'tokenQuotaPeriod' | 'callQuotaPeriod', label: string, amount: 'tokenQuota' | 'callQuota') =>
    <Field label={label} validationState={errors[key] ? 'error' : undefined} validationMessage={errors[key]}>
      <Select value={value[key]} disabled={!value[amount].trim()} onChange={(_, d) => update({ ...value, [key]: d.value as QuotaPeriod })}>
        {QUOTA_PERIODS.map(period => <option key={period}>{period}</option>)}
      </Select>
    </Field>
  return <div className={styles.limitEditor}>
    {showSourceChoice && <div className={styles.stack}>
      <RadioGroup aria-label="Grant limit source" value={configured || editing ? 'explicit' : 'inherited'} onChange={(_, d) => chooseSource(d.value)}>
        <Radio value="inherited" label="Use cost-center per-person defaults" />
        <Radio value="explicit" label="Set explicit grant limits" />
      </RadioGroup>
      <Text size={200}>Any explicit limit replaces the complete per-person set. Leave every amount empty to use defaults; shared publication, pool and cost-center safeguards still apply.</Text>
    </div>}
    {!showSourceChoice && <Text size={200}>These explicit grant limits replace the complete per-person set; shared publication, pool and cost-center safeguards still apply.</Text>}
    <div className={styles.limitLayout}>
      <div className={styles.limitInputs}>
        {!mcp && <section className={styles.limitGroup} aria-label="Token limits">
          <Title3 as="h3">Tokens</Title3>
          <div className={styles.limitPair}>
            {numberField('tokensPerMinute', 'Tokens per minute')}
            {numberField('tokenQuota', 'Token quota')}
            {quotaPeriod('tokenQuotaPeriod', 'Token quota period', 'tokenQuota')}
          </div>
        </section>}
        <section className={styles.limitGroup} aria-label="Call limits">
          <Title3 as="h3">Calls</Title3>
          <div className={styles.limitPair}>
            {numberField('calls', 'Calls', 'Call rate, independent of the calendar quota.')}
            {numberField('renewalPeriodSeconds', 'Per how many seconds', 'New or changed call rates use 1-300 seconds.')}
          </div>
          <div className={styles.limitPair}>
            {numberField('callQuota', 'Call quota', 'An absolute calendar allowance. Clear the amount to remove only this quota.')}
            {quotaPeriod('callQuotaPeriod', 'Call quota period', 'callQuota')}
          </div>
          {value.callQuota && <Text size={200}>{value.callQuotaPeriod === 'Weekly'
            ? 'UTC weeks start Monday at 00:00.'
            : 'The call quota uses UTC calendar boundaries.'}</Text>}
          {(value.callQuota || value.invalidNumbers?.callQuota) && <Button type="button" onClick={() => clearNumber('callQuota')}>Remove call quota</Button>}
        </section>
      </div>
      <aside className={styles.limitSummary} aria-label="Decision summary">
        <Title3 as="h3">Decision summary</Title3>
        <Text weight="semibold">Before: {reviewLabel ?? (previous ? 'Saved explicit limits' : inheritedLabel)}</Text>
        {before.map(text => <Text key={text} size={200}>{text}</Text>)}
        <div className={styles.limitAfter}>
          <Text weight="semibold">After: {configured ? 'Explicit grant limits' : requireLimit ? 'No bounded limit yet' : inheritedLabel}</Text>
          {after.map(text => <Text key={text} size={200}>{text}</Text>)}
        </div>
        <Text size={200}>{targetContext === 'desiredOnly'
          ? 'Saving records desired state only. No managed gateway plan is generated for this resource.'
          : targetContext === 'managed'
            ? 'Shared safeguards are unchanged. Saving or approving records intent; the affected gateway plan still needs separate review and apply.'
            : 'Saving records intent, not gateway activation. Where a managed target exists, its plan needs separate review and apply; otherwise access remains desired state only.'}</Text>
      </aside>
    </div>
  </div>
}
export function TimeWindowFields({ value, onChange }: { value: WeeklyTimeWindow; onChange: (value: WeeklyTimeWindow) => void }) {
  return <div className={styles.stack}>
    <div className={styles.days}>{['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, index) =>
      <Checkbox key={day} label={day} checked={value.daysOfWeek.includes(index)} onChange={(_, d) => onChange({ ...value, daysOfWeek: d.checked ? [...value.daysOfWeek, index].sort() : value.daysOfWeek.filter(i => i !== index) })} />)}</div>
    <div className={styles.form}>
      <Field label="Start time"><Input type="time" value={value.startTime} onChange={(_, d) => onChange({ ...value, startTime: d.value })} /></Field>
      <Field label="End time"><Input type="time" value={value.endTime} onChange={(_, d) => onChange({ ...value, endTime: d.value })} /></Field>
    </div>
    {value.endTime < value.startTime && <Text>Ends the following day. Weekdays refer to the window's starting day.</Text>}
  </div>
}
export function CadenceFields({ label, value, onChange, inApp = false }: { label: string; value: ChannelCadence; onChange: (value: ChannelCadence) => void; inApp?: boolean }) {
  return <div className={styles.stack}>
    <Field label={label}><Select value={value.mode} onChange={(_, d) => onChange({ mode: d.value as ChannelCadence['mode'], digestPeriods: d.value === 'digest' ? ['daily'] : [] })}>
      <option value="off">Off</option><option value="immediate">Immediate</option>{!inApp && <option value="digest">Digest</option>}
    </Select></Field>
    {value.mode === 'digest' && <div className={styles.days}>{(['daily', 'weekly', 'monthly'] as DigestPeriod[]).map(period =>
      <Checkbox key={period} label={period} checked={value.digestPeriods.includes(period)} onChange={(_, d) => onChange({ ...value, digestPeriods: d.checked ? [...value.digestPeriods, period] : value.digestPeriods.filter(p => p !== period) })} />)}</div>}
  </div>
}
export function DigestFields({ value, onChange }: { value: DigestSchedule[]; onChange: (value: DigestSchedule[]) => void }) {
  return <div className={styles.stack}>{value.map((schedule, index) => {
    const update = (change: Partial<DigestSchedule>) => onChange(value.map((s, i) => i === index ? { ...s, ...change } : s))
    return <div key={schedule.period} className={styles.form}>
      <Field label={`${schedule.period} delivery time`}><Input type="time" value={schedule.deliveryTime} onChange={(_, d) => update({ deliveryTime: d.value })} /></Field>
      {schedule.period === 'weekly' && <Field label="Weekly delivery day"><Select value={String(schedule.dayOfWeek)} onChange={(_, d) => update({ dayOfWeek: Number(d.value) })}>{['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, i) => <option key={day} value={i}>{day}</option>)}</Select></Field>}
      {schedule.period === 'monthly' && <Field label="Monthly delivery day (1–28)"><Input type="number" min={1} max={28} value={String(schedule.dayOfMonth)} onChange={(_, d) => update({ dayOfMonth: Number(d.value) })} /></Field>}
    </div>
  })}</div>
}
