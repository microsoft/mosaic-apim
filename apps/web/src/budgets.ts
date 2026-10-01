import type { BudgetLevel, BudgetNotification, BudgetUnblockReason, BudgetView } from './types'

/** What each budget level means to an administrator, and how loudly to say it. See ADR 0023. */
export const BUDGET_LEVEL_LABELS: Record<BudgetLevel, string> = {
  ok: 'On track',
  warning: 'Near its limit',
  exceeded: 'Over budget',
  blocked: 'Blocked',
}

export function budgetBadgeColor(level: BudgetLevel): 'success' | 'warning' | 'danger' {
  if (level === 'ok') return 'success'
  if (level === 'warning') return 'warning'
  return 'danger'
}

export function budgetBarColor(level: BudgetLevel): 'success' | 'warning' | 'error' {
  if (level === 'ok') return 'success'
  if (level === 'warning') return 'warning'
  return 'error'
}

const UNBLOCK_REASONS: Record<BudgetUnblockReason, string> = {
  newMonth: 'the month ended',
  budgetRaised: 'the budget was raised',
  blockingOff: 'blocking was turned off',
  budgetRemoved: 'the budget was removed',
}

export function unblockReasonText(reason: BudgetUnblockReason | null): string {
  return reason ? UNBLOCK_REASONS[reason] : 'its budget allowed calls again'
}

export function formatPercent(share: number | null | undefined): string {
  if (share == null) return '—'
  return `${Math.round(share * 100)}%`
}

/** "2026-03" as "March 2026". */
export function formatBudgetMonth(month: string): string {
  const [year, number] = month.split('-').map(Number)
  if (!year || !number) return month
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'long', year: 'numeric' }).format(
    new Date(Date.UTC(year, number - 1, 1)),
  )
}

/** The month's last day, as "Mar 31". */
export function monthEndLabel(month: string): string {
  const [year, number] = month.split('-').map(Number)
  if (!year || !number) return 'month end'
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(
    new Date(Date.UTC(year, number, 0)),
  )
}

export function budgetName(budget: BudgetView): string {
  if (budget.scope === 'organization' || !budget.costCenter) return 'Organization'
  return budget.costCenter.name
}

export function actionText(budget: BudgetView): string {
  if (budget.scope === 'organization') return 'Warns only'
  return budget.action === 'block' ? 'Blocks calls at 100%' : 'Calls continue past 100%'
}

function noticeSubject(notice: BudgetNotification): string {
  if (notice.kind === 'threshold') return `${notice.threshold ?? 100}% email`
  if (notice.kind === 'blocked') return 'Block notice'
  return 'Unblock notice'
}

export function notificationText(notice: BudgetNotification): string {
  const subject = noticeSubject(notice)
  if (notice.status === 'sent') {
    return `${subject} sent to ${notice.recipients} ${notice.recipients === 1 ? 'person' : 'people'}`
  }
  if (notice.status === 'sending') return `${subject} sending`
  if (notice.status === 'failed') return `${subject} failed: ${notice.error ?? 'Communication Services refused it'}`
  return `${subject} not sent: ${notice.error ?? 'nothing to send'}`
}

/** Comma- or newline-separated email addresses, trimmed, without blanks. */
export function addressList(value: string): string[] {
  return value
    .split(/[,;\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

/** "80, 100" as [80, 100], or null when it isn't a list of whole percentages. */
export function thresholdList(value: string): number[] | null {
  const parts = value
    .split(/[,\s]+/)
    .map((item) => item.replace('%', '').trim())
    .filter(Boolean)
  if (parts.length === 0 || parts.length > 5) return null
  const numbers = parts.map(Number)
  if (numbers.some((item) => !Number.isInteger(item) || item < 1 || item > 1000)) return null
  return [...new Set(numbers)].sort((left, right) => left - right)
}
