import type { AccountInfo } from '@azure/msal-browser'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminProfilePage } from './AdminProfilePage'

const mocks = vi.hoisted(() => ({
  accounts: [] as AccountInfo[],
  logoutRedirect: vi.fn(),
  config: { authMode: 'local' },
}))
vi.mock('../runtime-config', () => ({ runtimeConfig: mocks.config }))
vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({
    accounts: mocks.accounts,
    instance: { logoutRedirect: mocks.logoutRedirect },
  }),
}))

describe('AdminProfilePage', () => {
  beforeEach(() => {
    mocks.accounts = []
    mocks.config.authMode = 'local'
  })

  it('discloses local authentication without inventing account metadata', () => {
    render(<AdminProfilePage />)

    expect(screen.getAllByText('Local administrator').length).toBeGreaterThan(0)
    expect(screen.getByText(/Local development authentication is enabled/)).toBeVisible()
    expect(screen.getAllByText('Not available without a signed-in account')).toHaveLength(3)
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Role assignments' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Generate credential' })).not.toBeInTheDocument()
  })

  it('shows only signed-in account facts in Entra mode', () => {
    mocks.config.authMode = 'entra'
    mocks.accounts = [{
      name: 'Signed-in administrator',
      username: 'administrator@example.test',
      tenantId: 'actual-tenant',
      homeAccountId: 'actual-home-account',
      localAccountId: 'actual-object',
      environment: 'login.microsoftonline.com',
    }]
    render(<AdminProfilePage />)

    expect(screen.getByText('administrator@example.test')).toBeVisible()
    expect(screen.getByText('actual-tenant')).toBeVisible()
    expect(screen.getByText('actual-home-account')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeVisible()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    expect(screen.queryByText('Local administrator')).not.toBeInTheDocument()
  })
})
