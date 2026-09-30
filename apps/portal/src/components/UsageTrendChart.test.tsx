import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { UsageHourPoint, UsageTimelinePoint } from '../types'
import { RecentHoursChart, UsageTrendChart } from './UsageTrendChart'

// Consecutive days from Aug 31, 2026.
function dailyPoints(days: number): UsageTimelinePoint[] {
  return Array.from({ length: days }, (_, index) => ({
    date: new Date(Date.UTC(2026, 7, 31 + index)).toISOString().slice(0, 10),
    entitlementId: 'grant-model',
    environment: 'production',
    requests: 10 + index,
    promptTokens: 100,
    completionTokens: 50,
    totalTokens: 150,
    estimatedCost: 0.01,
  }))
}

function axisLabels(container: HTMLElement) {
  return Array.from(container.querySelectorAll('.chart-axis span')).map((label) => label.textContent)
}

describe('UsageTrendChart', () => {
  it('labels every fifth day of a month, ending on the latest', () => {
    const { container } = render(<UsageTrendChart points={dailyPoints(30)} />)

    expect(axisLabels(container)).toEqual(['Sep 4', 'Sep 9', 'Sep 14', 'Sep 19', 'Sep 24', 'Sep 29'])
  })

  it('labels every day of a week', () => {
    const { container } = render(<UsageTrendChart points={dailyPoints(7)} />)

    expect(axisLabels(container)).toEqual(['Aug 31', 'Sep 1', 'Sep 2', 'Sep 3', 'Sep 4', 'Sep 5', 'Sep 6'])
  })

  it('can show tokens grouped by resource in an accessible table', async () => {
    const user = userEvent.setup()
    render(<UsageTrendChart points={dailyPoints(2)} />)

    await user.selectOptions(screen.getByLabelText('Metric'), 'tokens')
    await user.selectOptions(screen.getByLabelText('Group'), 'resource')
    await user.click(screen.getByRole('button', { name: 'Show as table' }))

    expect(screen.getByRole('table', { name: 'Daily tokens by resource' })).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'grant-model' })).toBeVisible()
  })

  it("draws no line for a grant MOSAIC can't measure instead of a false zero", async () => {
    const user = userEvent.setup()
    const unmeasured = dailyPoints(3).map((point) => ({
      ...point,
      entitlementId: 'grant-unlinked',
      environment: 'development',
      requests: null,
      promptTokens: null,
      completionTokens: null,
      totalTokens: null,
      estimatedCost: null,
    }))
    render(<UsageTrendChart points={[...dailyPoints(3), ...unmeasured]} />)

    const legend = screen.getByRole('list', { name: 'Trend legend' })
    expect(within(legend).getAllByRole('listitem').map((item) => item.textContent)).toEqual(['production'])
    await user.selectOptions(screen.getByLabelText('Group'), 'resource')
    expect(within(legend).getAllByRole('listitem').map((item) => item.textContent)).toEqual(['grant-model'])
  })

  it('renders recent hours as a chart and table', async () => {
    const user = userEvent.setup()
    const points: UsageHourPoint[] = [
      {
        hour: '2026-09-30T10:00:00Z',
        requests: 4,
        totalTokens: 500,
        throttled: 1,
        quotaRefused: 0,
        errors: 2,
        peakMinuteTokens: 250,
        peakMinuteRequests: 3,
      },
    ]
    render(<RecentHoursChart points={points} />)

    expect(screen.getByRole('img', { name: 'Hourly request chart for the last 24 hours in UTC' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Show as table' }))
    expect(screen.getByRole('table', { name: 'Hourly usage for the last 24 hours' })).toBeVisible()
    expect(screen.getByRole('cell', { name: '500' })).toBeVisible()
  })
})
