import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { MosaicThemeProvider } from '../theme'
import { AppearanceControl } from './AppearanceControl'

describe('AppearanceControl', () => {
  beforeEach(() => localStorage.clear())

  it('switches and persists light, dark and system from the sidebar', async () => {
    const user = userEvent.setup()
    render(<MosaicThemeProvider><AppearanceControl /></MosaicThemeProvider>)
    const group = within(screen.getByRole('group', { name: 'Appearance' }))
    await user.click(group.getByRole('button', { name: 'Dark' }))
    expect(group.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('mosaic-theme')).toBe('dark')
    await user.click(group.getByRole('button', { name: 'Light' }))
    expect(document.documentElement.dataset.theme).toBe('light')
    await user.click(group.getByRole('button', { name: 'System' }))
    expect(localStorage.getItem('mosaic-theme')).toBe('system')
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('offers the same preferences in collapsed navigation', async () => {
    const user = userEvent.setup()
    render(<MosaicThemeProvider><AppearanceControl /></MosaicThemeProvider>)
    await user.click(screen.getByRole('button', { name: 'Appearance: system' }))
    await user.click(screen.getByRole('menuitem', { name: 'Dark' }))
    expect(localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(screen.getByRole('button', { name: 'Appearance: dark' })).toBeVisible()
  })
})
