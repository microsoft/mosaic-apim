import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import type { ModelDiscoveryRun, ModelEndpoint, ModelProposalDetail } from '../types'
import { ModelDiscoveryWorkbench } from './ModelDiscoveryWorkbench'

const api = vi.hoisted(() => ({
  listModelProposals: vi.fn(),
  getModelProposal: vi.fn(),
  discoverModelChanges: vi.fn(),
  saveModelProposalContext: vi.fn(),
  listGateways: vi.fn(),
  listCostCenters: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => api }))
vi.mock('../environments', () => ({
  useEnvironmentCatalog: () => ({ data: { environments: [{ key: 'development', displayName: 'Development' }] } }),
}))

const endpoint: ModelEndpoint = {
  id: 'endpoint_demo', tenantId: 'demo', name: 'Fictional Azure account',
  provider: 'azureOpenAi', endpoint: 'https://fictional.openai.azure.com/',
  environment: null, authMode: 'managedIdentity', managementMode: 'observe', status: 'connected',
  access: { canRead: true, evaluation: 'effectivePermissions', missingActions: [] },
  runtimeAccess: [], capabilities: { managementApiVersion: '2024-10-01', notes: [] },
  inventory: { deployments: 0, availableModels: 0, succeededDeployments: 0, deprecatedDeployments: 0 },
  createdAt: '', updatedAt: '',
}
const time = '2026-10-01T12:00:00Z'
const evidence: ModelDiscoveryRun = {
  id: 'discoveryrun_demo', status: 'complete',
  source: { provider: 'azureOpenAi', cloud: 'commercial', location: 'eastus2', accountScope: 'fictional', connectionId: null, apiVersion: '2024-10-01' },
  rulesRevision: 'model-lifecycle/1', startedAt: time, completedAt: time,
  catalogObservedAt: time, deploymentsObservedAt: time, findings: [],
}
const proposal: ModelProposalDetail = {
  id: 'modelproposal_demo', model: { modelName: 'gpt-4o', modelFormat: 'OpenAI', modelVersion: '2024-11-20' },
  listed: true, change: 'new', deploymentStatus: 'notObserved', contextRevision: 0,
  evidence, evidenceDigest: 'a'.repeat(64), evidenceCurrent: true,
  lifecycleStatus: null, deployments: [], publications: [], savedContext: null,
  contextStatus: 'notSaved', contextNotes: [], runtimeFreshness: null,
  runtimeCheckedAt: null, runtimeRulesRevision: null,
}

function openWorkbench() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={cache}><MemoryRouter>
    <ModelDiscoveryWorkbench endpoint={endpoint} onClose={vi.fn()} />
  </MemoryRouter></QueryClientProvider>)
}

beforeEach(() => {
  vi.resetAllMocks()
  api.listModelProposals.mockResolvedValue({
    supported: true, latestAttempt: evidence, lastComplete: evidence, evidenceCurrent: true, items: [proposal],
  })
  api.getModelProposal.mockResolvedValue(proposal)
  api.listGateways.mockResolvedValue([])
  api.listCostCenters.mockResolvedValue([])
  api.discoverModelChanges.mockResolvedValue(evidence)
})

it('reads saved evidence until explicitly triggered and saves only draft context', async () => {
  const user = userEvent.setup()
  openWorkbench()
  const row = await screen.findByRole('button', { name: /gpt-4o.*New catalog version/ })
  expect(api.discoverModelChanges).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Discover changes' }))
  expect(api.discoverModelChanges).toHaveBeenCalledWith(endpoint.id, expect.any(String))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Discover changes' })).toBeEnabled())
  await user.click(row)
  expect(await screen.findByText('No exact deployment observed')).toBeVisible()
  expect(screen.getByText('No matching publication recorded')).toBeVisible()
  expect(screen.getByText('No access created')).toBeVisible()

  api.saveModelProposalContext.mockResolvedValue({ ...proposal, contextRevision: 1, contextStatus: 'incomplete' })
  await user.click(screen.getByRole('button', { name: 'Review proposal' }))
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Intended environment' }), 'development')
  await user.click(screen.getByRole('button', { name: 'Save proposal' }))
  expect(api.saveModelProposalContext).toHaveBeenCalledWith(endpoint.id, proposal.id, {
    environment: 'development', gatewayId: null, costCenterId: null,
    evidenceRunId: evidence.id, evidenceDigest: proposal.evidenceDigest, expectedRevision: 0,
  })
  expect(await screen.findByText('Proposal context saved. No operational changes made.')).toBeVisible()
})

it('labels historical evidence after a partial scan and blocks review', async () => {
  api.listModelProposals.mockResolvedValue({
    supported: true, latestAttempt: { ...evidence, status: 'partial' },
    lastComplete: evidence, evidenceCurrent: false, items: [proposal],
  })
  api.getModelProposal.mockResolvedValue({ ...proposal, evidenceCurrent: false })
  openWorkbench()
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /gpt-4o.*New catalog version/ }))
  expect(await screen.findByText(/Showing prior evidence/)).toBeVisible()
  expect(await screen.findByRole('button', { name: 'Review proposal' })).toBeDisabled()
  expect(api.discoverModelChanges).not.toHaveBeenCalled()
})

it('retries an unconfirmed request but starts a fresh scan after its terminal status is observed', async () => {
  const user = userEvent.setup()
  api.discoverModelChanges.mockRejectedValueOnce(new Error('Connection lost'))
    .mockRejectedValueOnce(new Error('Connection lost'))
  openWorkbench()
  const discover = await screen.findByRole('button', { name: 'Discover changes' })
  await user.click(discover)
  await waitFor(() => expect(discover).toBeEnabled())
  const requestId = api.discoverModelChanges.mock.calls[0][1]
  const running = { ...evidence, id: 'new-run', requestId, status: 'running' }
  api.listModelProposals.mockResolvedValue({
    supported: true, latestAttempt: running, lastComplete: evidence, evidenceCurrent: false, items: [],
  })

  await user.click(discover)
  expect(api.discoverModelChanges).toHaveBeenLastCalledWith(endpoint.id, requestId)
  const check = await screen.findByRole('button', { name: 'Check discovery status' })
  const complete = { ...running, status: 'complete' }
  api.listModelProposals.mockResolvedValue({
    supported: true, latestAttempt: complete, lastComplete: complete, evidenceCurrent: true, items: [],
  })
  await user.click(check)
  expect(api.discoverModelChanges).toHaveBeenCalledTimes(2)
  await user.click(await screen.findByRole('button', { name: 'Discover changes' }))
  expect(api.discoverModelChanges).toHaveBeenCalledTimes(3)
  expect(api.discoverModelChanges.mock.calls[2][1]).not.toBe(requestId)
})
