import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { AnalyticsPage } from './AnalyticsPage'

describe('AnalyticsPage', () => {
  it('labels telemetry as sample data and supports error drill-in', async () => {
    const user = userEvent.setup()
    render(<AnalyticsPage />)

    expect(screen.getAllByText('Sample data').length).toBeGreaterThan(0)
    expect(screen.getByRole('note')).toHaveTextContent('Azure Monitor')

    const viewButtons = screen.getAllByRole('button', { name: 'View' })
    await user.click(viewButtons[0])

    expect(screen.getByRole('dialog')).toBeVisible()
    expect(screen.getByText('Suggested follow-up')).toBeVisible()
  })
})
