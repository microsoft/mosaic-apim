import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AdminProfilePage } from './AdminProfilePage'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({
    accounts: [],
    instance: { logoutRedirect: vi.fn() },
  }),
}))

describe('AdminProfilePage', () => {
  it('distinguishes local identity from sample access metadata', () => {
    render(<AdminProfilePage />)

    expect(screen.getAllByText('Local administrator').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Sample data')).toHaveLength(4)
    expect(screen.getByRole('button', { name: 'Generate credential' })).toBeDisabled()
  })
})
