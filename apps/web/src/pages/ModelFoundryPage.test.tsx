import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { ModelFoundryPage } from './ModelFoundryPage'

function LocationProbe() {
  const location = useLocation()
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>
}

describe('ModelFoundryPage', () => {
  it('opens the local deploy dialog from the shell query and clears it when closed', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/models?deploy=1']}>
        <ModelFoundryPage />
        <LocationProbe />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(screen.getByText(/does not call Azure AI Foundry/i)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/models')
  })
})
