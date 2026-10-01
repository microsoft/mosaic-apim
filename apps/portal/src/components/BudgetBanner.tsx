import { MessageBar, MessageBarBody, MessageBarTitle, Text } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { usePortalApi } from '../api'
import type { PortalBudgetAlert } from '../types'

// Rounded down, so a cost center at 99.6% never reads as having used all of its budget.
function percent(used: number): string {
  return `${Math.floor(used * 100)}%`
}

function alertCopy(alert: PortalBudgetAlert): {
  intent: 'error' | 'warning'
  title: string
  body: string
} {
  const { name, code } = alert.costCenter
  const share = `It has used ${percent(alert.used)} of this month’s budget`
  const refused = `calls charged to ${code} are refused until the budget is raised or a new month starts (UTC)`
  // Only what this budget does: another budget, or the gateway, may still refuse a call.
  const warnsOnlySentence = `This budget only warns, so it doesn’t stop calls charged to ${code}.`
  const warnsOnlyContinuation = `this budget only warns, so it doesn’t stop calls charged to ${code}.`
  if (alert.level === 'blocked') {
    return {
      intent: 'error',
      title: `${name} has used its monthly budget`,
      body: `${share}, so ${refused}.`,
    }
  }
  if (alert.level === 'exceeded') {
    return {
      intent: 'warning',
      title: `${name} is over its monthly budget`,
      body:
        alert.action === 'block'
          ? `${share}. Once MOSAIC’s next check blocks it, ${refused}.`
          : `${share}. ${warnsOnlySentence}`,
    }
  }
  return {
    intent: 'warning',
    title: `${name} is near its monthly budget`,
    body:
      alert.action === 'block'
        ? `${share}. At 100%, ${refused}.`
        : `${share}. At 100%, ${warnsOnlyContinuation}`,
  }
}

/**
 * Your cost centers whose monthly budgets are near, at, or past their limits. Each is a total for
 * the cost center, from everyone who charges it: never anyone's own share. Shows nothing otherwise,
 * or when the budgets can't be read.
 */
export function BudgetBanner() {
  const api = usePortalApi()
  const alerts = useQuery({
    queryKey: ['portal', 'budgets'],
    queryFn: () => api.getMyBudgets(),
    staleTime: 60_000,
  })
  if (!alerts.data || alerts.data.length === 0) return null
  return (
    <section className="budget-banners" aria-label="Cost center budgets">
      {alerts.data.map((alert) => {
        const copy = alertCopy(alert)
        return (
          <MessageBar key={alert.costCenter.id} intent={copy.intent} layout="multiline">
            <MessageBarBody>
              <MessageBarTitle>{copy.title}</MessageBarTitle>
              {copy.body}
            </MessageBarBody>
          </MessageBar>
        )
      })}
      <Text size={200} className="budget-banner-note">
        Budgets cover each cost center’s total, from everyone who charges it. A MOSAIC
        administrator can raise one.
      </Text>
    </section>
  )
}
