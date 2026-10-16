import { Checkbox, Field, Input, Select, Text } from '@fluentui/react-components'
import type { ChannelCadence, DigestPeriod, DigestSchedule, WeeklyTimeWindow } from '../admin-types'
import { QUOTA_PERIODS, type LimitForm } from '../entitlement-limits'
import type { QuotaPeriod } from '../types'
import styles from './AdminGovernance.module.css'

export function TimeZoneField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <Field label="Time zone" hint="IANA zone, such as UTC or America/New_York. Local time observes daylight saving.">
    <Input value={value} onChange={(_, data) => onChange(data.value)} required />
  </Field>
}
export function LimitFields({ value, onChange, mcp = false }: { value: LimitForm; onChange: (value: LimitForm) => void; mcp?: boolean }) {
  const change = (key: keyof LimitForm, next: string) => onChange({ ...value, [key]: next })
  return <div className={styles.form}>
    {!mcp && <>
      <Field label="Tokens per minute"><Input type="number" min={1} value={value.tokensPerMinute} onChange={(_, d) => change('tokensPerMinute', d.value)} /></Field>
      <Field label="Token quota"><Input type="number" min={1} value={value.tokenQuota} onChange={(_, d) => change('tokenQuota', d.value)} /></Field>
      <Field label="Token quota period"><Select value={value.tokenQuotaPeriod} onChange={(_, d) => onChange({ ...value, tokenQuotaPeriod: d.value as QuotaPeriod })}>{QUOTA_PERIODS.map(p => <option key={p}>{p}</option>)}</Select></Field>
    </>}
    <Field label="Call limit"><Input type="number" min={1} value={value.calls} onChange={(_, d) => change('calls', d.value)} /></Field>
    <Field label="Call renewal period (seconds)"><Input type="number" min={1} value={value.renewalPeriodSeconds} onChange={(_, d) => change('renewalPeriodSeconds', d.value)} /></Field>
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
