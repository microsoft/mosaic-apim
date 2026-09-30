import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { UsageTimelinePoint } from '../types'
import { UsageTrendChart } from './UsageTrendChart'

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
})
