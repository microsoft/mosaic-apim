import {
  Badge,
  Button,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  ProgressBar,
  Radio,
  RadioGroup,
  Switch,
  Text,
  Textarea,
} from '@fluentui/react-components'
import { type FormEvent, useEffect, useState } from 'react'
import {
  BUDGET_LEVEL_LABELS,
  addressList,
  budgetBadgeColor,
  budgetBarColor,
  formatBudgetMonth,
  formatPercent,
  monthEndLabel,
  notificationText,
  thresholdList,
  unblockReasonText,
} from '../budgets'
import { formatCost } from '../cost-format'
import type { BudgetAction, BudgetUpdate, BudgetView } from '../types'
import styles from './BudgetEditor.module.css'

function forecastText(budget: BudgetView): string {
  const { status } = budget
  if (status.forecast == null) return 'Forecast after a day of figures'
  return `Projected ${formatCost(status.forecast)} by ${monthEndLabel(status.month)}`
}

/** Spend so far against the amount, the level, and the month-end forecast. */
export function BudgetMeter({ budget, label }: { budget: BudgetView; label: string }) {
  const { status } = budget
  const used = status.used ?? 0
  return (
    <div className={styles.meter}>
      <div className={styles.meterHeader}>
        <span>
          <span className={styles.figure}>{formatCost(status.monthToDate, 'No figures yet')}</span>{' '}
          <span className={styles.muted}>of {formatCost(budget.amount)}</span>
        </span>
        <Badge appearance="tint" color={budgetBadgeColor(status.level)}>
          {BUDGET_LEVEL_LABELS[status.level]}
        </Badge>
      </div>
      <ProgressBar
        aria-label={`${label}: ${formatPercent(status.used)} of its monthly budget used`}
        value={Math.min(Math.max(used, 0), 1)}
        max={1}
        thickness="large"
        color={budgetBarColor(status.level)}
      />
      <Text size={200} className={styles.muted}>
        {formatPercent(status.used)} used · {forecastText(budget)}
      </Text>
    </div>
  )
}

/** What the last check found: forecast, freshness, the block at each gateway, and the emails. */
export function BudgetDetails({ budget }: { budget: BudgetView }) {
  const { status } = budget
  const sent = [...status.notifications].reverse()
  return (
    <>
      {status.blocked && (
        <MessageBar intent="error" layout="multiline">
          <MessageBarBody>
            Calls charged to this cost center are refused at the gateway until an administrator raises
            the budget, turns blocking off, or the month ends.
          </MessageBarBody>
        </MessageBar>
      )}
      {status.error && (
        <MessageBar intent="warning" layout="multiline">
          <MessageBarBody>{status.error}</MessageBarBody>
        </MessageBar>
      )}
      <dl className={styles.details}>
        <div>
          <dt>Month</dt>
          <dd>{formatBudgetMonth(status.month)}</dd>
        </div>
        <div>
          <dt>Forecast</dt>
          <dd>
            {status.forecast == null ? '—' : `${formatCost(status.forecast)} (${formatPercent(status.forecastUsed)})`}
          </dd>
        </div>
        <div>
          <dt>Thresholds reached</dt>
          <dd>{status.crossed.length ? status.crossed.map((item) => `${item}%`).join(', ') : 'None yet'}</dd>
        </div>
        <div>
          <dt>Figures through</dt>
          <dd>{status.through ? new Date(status.through).toLocaleString() : '—'}</dd>
        </div>
      </dl>
      {status.unpricedTokens > 0 && (
        <Text size={200} className={styles.muted}>
          {status.unpricedTokens.toLocaleString()} tokens this month have no price, so they don&apos;t count
          against the budget.
        </Text>
      )}
      {!status.blocked && status.unblockedAt && (
        <Text size={200} className={styles.muted}>
          Unblocked {new Date(status.unblockedAt).toLocaleString()} because {unblockReasonText(status.unblockReason)}.
        </Text>
      )}
      {budget.action === 'block' && status.gateways.length > 0 && (
        <ul className={styles.list} aria-label="Gateways that enforce this budget">
          {status.gateways.map((gateway) => (
            <li key={gateway.gatewayId}>
              <span>{gateway.name}</span>
              {gateway.error ? (
                <Badge appearance="outline" color="danger">Not updated</Badge>
              ) : (
                <Badge appearance="tint" color={gateway.enforcing ? 'danger' : 'success'}>
                  {gateway.enforcing ? 'Refusing calls' : 'Allowing calls'}
                </Badge>
              )}
            </li>
          ))}
        </ul>
      )}
      {sent.length > 0 && (
        <ul className={styles.list} aria-label="Budget emails this month">
          {sent.map((notice) => (
            <li key={notice.id}>
              <span>{notificationText(notice)}</span>
              <span className={styles.muted}>{new Date(notice.sentAt ?? notice.createdAt).toLocaleDateString()}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

interface BudgetFormProps {
  budget: BudgetView | null
  /** The cost center's owners, emailed when Email owners is on. Absent for the organization. */
  owners?: string[]
  organization?: boolean
  saving: boolean
  removing?: boolean
  error?: string | null
  onSave: (payload: BudgetUpdate) => void
  onRemove?: () => void
}

/** Set or change a budget: its amount, when it warns, who it emails, and what happens at 100%. */
export function BudgetForm({ budget, owners = [], organization = false, saving, removing = false, error, onSave, onRemove }: BudgetFormProps) {
  const [amount, setAmount] = useState('')
  const [thresholds, setThresholds] = useState('80, 100')
  const [recipients, setRecipients] = useState('')
  const [notifyOwners, setNotifyOwners] = useState(true)
  const [action, setAction] = useState<BudgetAction>('continue')
  const [invalid, setInvalid] = useState<string | null>(null)

  useEffect(() => {
    setAmount(budget ? String(budget.amount) : '')
    setThresholds(budget ? budget.thresholds.join(', ') : '80, 100')
    setRecipients(budget ? budget.recipients.join(', ') : '')
    setNotifyOwners(budget ? budget.notifyOwners : true)
    setAction(budget ? budget.action : 'continue')
  }, [budget])

  function submit(event: FormEvent) {
    event.preventDefault()
    const value = Number(amount)
    if (!Number.isFinite(value) || value <= 0) {
      setInvalid('Enter a monthly amount in US dollars, more than zero.')
      return
    }
    const percentages = thresholdList(thresholds)
    if (!percentages) {
      setInvalid('Warn at one to five whole percentages from 1 to 1000, such as 80, 100.')
      return
    }
    setInvalid(null)
    onSave({
      amount: value,
      thresholds: percentages,
      recipients: addressList(recipients),
      notifyOwners: organization ? false : notifyOwners,
      action: organization ? 'continue' : action,
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate aria-label={organization ? 'Organization budget' : 'Cost center budget'}>
      {(invalid || error) && (
        <MessageBar intent="error" layout="multiline">
          <MessageBarBody>{invalid ?? error}</MessageBarBody>
        </MessageBar>
      )}
      <div className={styles.formGrid}>
        <Field label="Monthly amount (USD)" required>
          <Input
            aria-label="Monthly amount (USD)"
            type="number"
            min={0}
            step="any"
            value={amount}
            onChange={(_, data) => setAmount(data.value)}
          />
        </Field>
        <Field label="Warn at (%)" hint="Up to five, such as 80, 100.">
          <Input aria-label="Warn at (%)" value={thresholds} onChange={(_, data) => setThresholds(data.value)} />
        </Field>
      </div>
      {!organization && (
        <Switch
          checked={notifyOwners}
          label={owners.length ? `Email the owners (${owners.length})` : 'Email the owners'}
          onChange={(_, data) => setNotifyOwners(data.checked)}
        />
      )}
      <Field
        label={organization ? 'Email' : 'Also email'}
        hint="Email addresses separated by commas. They don't need a MOSAIC account."
      >
        <Textarea aria-label={organization ? 'Email' : 'Also email'} value={recipients} onChange={(_, data) => setRecipients(data.value)} />
      </Field>
      {!organization && (
        <Field label="At 100% of the budget">
          <RadioGroup
            aria-label="At 100% of the budget"
            value={action}
            onChange={(_, data) => setAction(data.value as BudgetAction)}
          >
            <Radio value="continue" label="Let calls continue, and email" />
            <Radio value="block" label="Block calls at the gateway until the budget is raised or the month ends" />
          </RadioGroup>
        </Field>
      )}
      <div className={styles.actions}>
        <Button appearance="primary" type="submit" disabled={saving}>
          {budget ? 'Save budget' : 'Set budget'}
        </Button>
        {budget && onRemove && (
          <Button appearance="subtle" className={styles.dangerButton} onClick={onRemove} disabled={removing}>
            Remove budget
          </Button>
        )}
      </div>
    </form>
  )
}
