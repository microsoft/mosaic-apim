import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MosaicThemeProvider } from '../theme'
import { SettingsPage } from './SettingsPage'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({ accounts: [] }),
}))

describe('SettingsPage', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('applies and persists appearance preferences', async () => {
    const user = userEvent.setup()
    render(
      <MosaicThemeProvider>
        <SettingsPage />
      </MosaicThemeProvider>,
    )

    await user.click(screen.getByRole('radio', { name: 'Dark' }))

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(screen.getByText(/Resolved theme: dark/i)).toBeVisible()
  })
})
