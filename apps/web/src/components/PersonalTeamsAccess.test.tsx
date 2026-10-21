import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CapabilityCheck } from '../admin-types'
import { PersonalTeamsAccess } from './PersonalTeamsAccess'

const api = vi.hoisted(() => ({
  getMyTeamsStatus: vi.fn(), checkMyTeamsAccess: vi.fn(), testMyTeamsConnection: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => api }))

function checks(revision: string): CapabilityCheck[] {
  return [{
    feature: 'teamsTest', scope: 'self', required: true, state: 'needsAction',
    reason: `Test evidence for ${revision}.`, evidence: [], checkedAt: '2026-10-01T12:00:00Z',
    freshUntil: null, configurationRevision: revision, lastTestAt: null,
    remediation: null,
  }]
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<FluentProvider theme={webLightTheme}><QueryClientProvider client={client}>
    <MemoryRouter><PersonalTeamsAccess /></MemoryRouter>
  </QueryClientProvider></FluentProvider>)
}

describe('administrator private Teams setup', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getMyTeamsStatus.mockResolvedValue({
      enabled: true, state: 'installed', checks: checks('current'), lastTest: null,
    })
    api.checkMyTeamsAccess.mockResolvedValue([
      ...checks('current'),
      { ...checks('current')[0], feature: 'teamsBotOAuth', reason: 'Runtime token checked without sending.' },
    ])
    api.testMyTeamsConnection.mockResolvedValue({
      outcome: 'providerAccepted', message: 'Teams accepted the synthetic test.',
      testedAt: '2026-10-01T12:00:00Z', confirmedDelivered: false,
    })
  })

  it('checks access without sending, and requires a separate explicit test confirmation', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('installed')
    expect(api.testMyTeamsConnection).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Check my Teams access' }))
    expect(await screen.findByText('Runtime token checked without sending.')).toBeVisible()
    expect(api.checkMyTeamsAccess).toHaveBeenCalledExactlyOnceWith()
    expect(api.testMyTeamsConnection).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Send a test to my private Teams' }))
    expect(screen.getByRole('dialog')).toBeVisible()
    expect(api.testMyTeamsConnection).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Send private synthetic test' }))
    expect(await screen.findByText('Teams accepted the synthetic test.')).toBeVisible()
    expect(api.testMyTeamsConnection).toHaveBeenCalledExactlyOnceWith()
    expect(screen.getByText('Provider acceptance is not proof of receipt. Check your private Teams chat.')).toBeVisible()
  })

  it('does not let an old access result replace current binding or configuration evidence', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('installed')
    api.getMyTeamsStatus.mockResolvedValue({
      enabled: true, state: 'uninstalled', checks: checks('changed'), lastTest: null,
    })
    await user.click(screen.getByRole('button', { name: 'Check my Teams access' }))
    await screen.findByText('uninstalled')
    await waitFor(() => expect(api.checkMyTeamsAccess).toHaveBeenCalledOnce())
    expect(screen.getByText('Test evidence for changed.')).toBeVisible()
    expect(screen.queryByText('Runtime token checked without sending.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send a test to my private Teams' })).toBeDisabled()
    expect(api.testMyTeamsConnection).not.toHaveBeenCalled()
  })

  it('cancels a synthetic test without sending or leaving the page blocked', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('installed')
    const trigger = screen.getByRole('button', { name: 'Send a test to my private Teams' })
    await user.click(trigger)
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(await screen.findByRole('button', { name: 'Send a test to my private Teams' })).toBeEnabled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(api.testMyTeamsConnection).not.toHaveBeenCalled()
  })

  it('shows a failed access check without substituting send success', async () => {
    const user = userEvent.setup()
    api.checkMyTeamsAccess.mockRejectedValue(new Error('The exact bot identity is not attached.'))
    mount()
    await screen.findByText('installed')
    await user.click(screen.getByRole('button', { name: 'Check my Teams access' }))
    expect(await screen.findByText('The exact bot identity is not attached.')).toBeVisible()
    expect(api.testMyTeamsConnection).not.toHaveBeenCalled()
  })
})
