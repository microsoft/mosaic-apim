import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ApprovalPolicy } from '../admin-types'
import { ApprovalPolicies } from './ApprovalPolicies'

const api = vi.hoisted(() => ({
  listApprovalPolicies: vi.fn(), getApprovalEvaluator: vi.fn(), getEnvironmentCatalog: vi.fn(),
  listCostCenters: vi.fn(), listAccessRequests: vi.fn(), saveApprovalPolicy: vi.fn(),
  dryRunApprovalPolicy: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => api }))

const policy: ApprovalPolicy = {
  id: 'policy-test', revision: 7, name: 'Sandbox quota', description: 'Original description',
  enabled: false, productionOptIn: false,
  scope: { environments: ['development'], resourceKinds: ['modelApi'], resourceIds: [], costCenterIds: [] },
  schedule: { timeZone: 'UTC', windows: [{ daysOfWeek: [0], startTime: '09:00', endTime: '17:00' }] },
  minimumRequestAgeSeconds: 3600,
  grantTemplate: { enforcement: { requests: {
    counterKeyExpression: '@("retained-template-counter")', calls: 30, renewalPeriodSeconds: 900,
    callQuota: 1000, callQuotaPeriod: 'Weekly',
  } } },
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<FluentProvider theme={webLightTheme}><QueryClientProvider client={client}>
    <MemoryRouter initialEntries={['/entitlements?tab=approvalPolicies&selected=policy-test&mode=edit']}>
      <ApprovalPolicies />
    </MemoryRouter>
  </QueryClientProvider></FluentProvider>)
}

beforeEach(() => {
  vi.resetAllMocks()
  api.listApprovalPolicies.mockResolvedValue([policy])
  api.getApprovalEvaluator.mockResolvedValue({ paused: true, pendingCount: 0 })
  api.getEnvironmentCatalog.mockResolvedValue({
    environments: [{ key: 'development', displayName: 'Development', production: false }],
    compatibility: [], updatedAt: null,
  })
  api.listCostCenters.mockResolvedValue([])
  api.listAccessRequests.mockResolvedValue([])
  api.saveApprovalPolicy.mockResolvedValue(policy)
})

describe('approval template quota editing', () => {
  it('hydrates a conditional template quota and preserves its full enforcement on a description-only save', async () => {
    const user = userEvent.setup()
    mount()
    expect(await screen.findByRole('spinbutton', { name: 'Call quota' })).toHaveValue(1000)
    expect(screen.getByRole('combobox', { name: 'Call quota period' })).toHaveValue('Weekly')
    expect(api.saveApprovalPolicy).not.toHaveBeenCalled()
    expect(api.dryRunApprovalPolicy).not.toHaveBeenCalled()
    await user.clear(screen.getByRole('textbox', { name: 'Description' }))
    await user.type(screen.getByRole('textbox', { name: 'Description' }), 'Revised description')
    await user.click(screen.getByRole('button', { name: 'Save policy' }))
    const { id, revision, ...input } = policy
    await waitFor(() => expect(api.saveApprovalPolicy).toHaveBeenCalledWith(id, {
      ...input,
      description: 'Revised description',
      expectedRevision: revision,
      enableConfirmed: false,
    }))
  })

  it('removes a quota without silently rewriting another saved template limit', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByRole('spinbutton', { name: 'Call quota' })
    await user.click(screen.getByRole('button', { name: 'Remove call quota' }))
    await user.click(screen.getByRole('button', { name: 'Save policy' }))
    await waitFor(() => expect(api.saveApprovalPolicy).toHaveBeenCalledOnce())
    expect(api.saveApprovalPolicy.mock.calls[0][1].grantTemplate.enforcement).toEqual({
      requests: { counterKeyExpression: '@("retained-template-counter")', calls: 30, renewalPeriodSeconds: 900 },
    })
  })
})
