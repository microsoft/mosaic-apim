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
  if (alert.level === 'blocked') {
    return {
      intent: 'error',
      title: `${name} has used its monthly budget`,
      body: `Calls charged to ${code} are refused until its budget is raised or a new month starts (UTC). Calls charged to your other cost centers still work.`,
    }
  }
  if (alert.level === 'exceeded') {
    return {
      intent: 'warning',
      title: `${name} is over its monthly budget`,
      body: `It has used ${percent(alert.used)} of this month’s budget. Calls charged to ${code} still work.`,
    }
  }
  return {
    intent: 'warning',
    title: `${name} is near its monthly budget`,
    body:
      alert.action === 'block'
        ? `It has used ${percent(alert.used)} of this month’s budget. At 100%, calls charged to ${code} are refused until the budget is raised or a new month starts (UTC).`
        : `It has used ${percent(alert.used)} of this month’s budget.`,
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
