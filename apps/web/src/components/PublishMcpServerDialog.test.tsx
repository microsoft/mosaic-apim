import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import { Profiler, useState } from 'react'
import { useRestoreFocusTarget } from '@fluentui/react-components'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PublishMcpServerDialog } from './PublishMcpServerDialog'
import type { Gateway, McpEndpoint, McpPublication, PublishPlan, PublishRun } from '../types'

function gateway(overrides: Partial<Gateway> = {}): Gateway {
  return {
    id: 'gateway_1',
    tenantId: 'tenant-test',
    name: 'Managed gateway',
    provider: 'apim',
    azureResourceId: '/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim',
    subscriptionId: 's',
    resourceGroup: 'rg',
    serviceName: 'apim',
    environment: null,
    environmentLabel: null,
    managementMode: 'manage',
    status: 'connected',
    access: { canRead: true, canWrite: true, evaluation: 'effectivePermissions', missingActions: [], remediation: null, message: 'Ready.' },
    capabilities: { managementApiVersion: '2024-05-01', aiGatewayPolicies: 'available', mcpServers: 'available', gatewayUrl: 'https://gateway.example.test', identityObserved: true, notes: [] },
    inventory: { apis: 0, aiApis: 0, mcpServers: 0, operations: 0, products: 0, subscriptions: 0, users: 0, groups: 0, backends: 0, namedValues: 0, policyDocuments: 0, policyFragments: 0, recognizedFacets: 0, unrecognizedFacets: 0, mosaicManagedFacets: 0 },
    lastSyncedAt: '2026-09-01T12:00:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

function endpoint(overrides: Partial<McpEndpoint> = {}): McpEndpoint {
  return {
    id: 'endpoint_1',
    tenantId: 'tenant-test',
    name: 'Weather tools',
    endpoint: 'https://mcp.example.test/mcp',
    environment: null,
    environmentLabel: null,
    authMode: 'none',
    credentialReferenceId: null,
    resourceAudience: null,
    status: 'connected',
    access: { canDiscover: true, evaluation: 'handshake', checkedAt: '2026-09-01T12:00:00Z', challenge: null, message: null },
    capabilities: { offeredProtocolVersion: '2025-11-25', protocolVersion: '2025-11-25', transportType: 'streamable', supportsTools: 'available', sessionManaged: true, notes: [] },
    inventory: { tools: 2, readOnlyTools: 1, unannotatedTools: 0 },
    lastSyncedAt: '2026-09-01T12:00:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

const publication: McpPublication = {
  id: 'mcp_pub_1',
  tenantId: 'tenant-test',
  entityType: 'mcpPublication',
  gatewayId: 'gateway_1',
  mcpEndpointId: 'endpoint_1',
  displayName: 'Weather tools',
  apiName: 'mosaic-mcp-weather-tools',
  apiPath: 'mosaic/mcp/weather-tools',
  backendName: 'mosaic-mcp-weather-tools',
  fragmentName: 'mosaic-mcp-weather-tools',
  metadataApiName: 'mosaic-mcp-weather-tools-prm',
  mcpServerId: 'mcp_1',
  status: 'planned',
  resources: [],
  lastPlanId: 'plan_1',
  lastPlanDigest: 'digest',
  lastRunId: null,
  lastAppliedAt: null,
  lastError: null,
  appliedAccess: null,
  accessState: 'pending',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

const plan: PublishPlan = {
  id: 'plan_1',
  tenantId: 'tenant-test',
  entityType: 'publishPlan',
  publicationId: 'mcp_pub_1',
  gatewayId: 'gateway_1',
  digest: 'digest',
  steps: [{ kind: 'api', name: 'mosaic-mcp-weather-tools', action: 'create', reason: 'Create the MCP API.', resourceId: '/apis/mosaic-mcp-weather-tools', existed: false, stage: 'prepare' }],
  facets: [{ kind: 'authorization', element: 'validate-azure-ad-token', section: 'inbound', summary: 'Validates MCP caller tokens.', details: ['Requires Mcp.Invoke.'], attributes: {}, confidence: 'recognized', managedByMosaic: true }],
  policyContentSha256: 'policy-digest',
  warnings: ['VS Code and other interactive clients need consent for api://runtime-client-id/Mcp.Invoke'],
  target: 'mcp',
  accessSnapshot: null,
  mcpAccessSnapshot: {
    version: 1,
    audience: 'runtime-client-id',
    delegatedScope: 'Mcp.Invoke',
    applicationRole: 'Mcp.Invoke.Application',
    grants: [{
      entitlementId: 'grant_1',
      subject: { kind: 'securityGroup', id: 'sg_1' },
      objectId: '00000000-0000-0000-0000-000000000001',
      displayName: 'Security readers',
      enabled: true,
      enforcement: { requests: { counterKeyExpression: '@(context.Subscription.Id)', calls: 60, renewalPeriodSeconds: 60 } },
      intentDigest: 'digest',
    }],
  },
  previousAccessVersion: null,
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

function run(overrides: Partial<PublishRun> = {}): PublishRun {
  return {
    id: 'run_1',
    tenantId: 'tenant-test',
    entityType: 'publishRun',
    publicationId: 'mcp_pub_1',
    gatewayId: 'gateway_1',
    planId: 'plan_1',
    planDigest: 'digest',
    status: 'succeeded',
    startedAt: '2026-09-01T12:00:00Z',
    completedAt: '2026-09-01T12:00:02Z',
    durationMs: 2000,
    steps: [{ kind: 'api', name: 'mosaic-mcp-weather-tools', action: 'create', status: 'succeeded', resourceId: '/apis/mosaic-mcp-weather-tools', createdByMosaic: true, error: null, stage: 'prepare' }],
    rolledBack: false,
    orphanedResources: [],
    errors: [],
    target: 'mcp',
    mcpAccessSnapshot: plan.mcpAccessSnapshot,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:02Z',
    ...overrides,
  }
}

const api = {
  getEnvironmentCatalog: vi.fn(),
  listPrincipals: vi.fn(),
  listGateways: vi.fn(),
  listMcpEndpoints: vi.fn(),
  getMcpPublishingCapability: vi.fn(),
  createMcpPublication: vi.fn(),
  updateMcpPublication: vi.fn(),
  deleteMcpPublication: vi.fn(),
  planMcpPublication: vi.fn(),
  applyMcpPublication: vi.fn(),
  getMcpPublishRun: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderDialog(onPublished = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PublishMcpServerDialog open onClose={vi.fn()} onPublished={onPublished} />
    </QueryClientProvider>,
  )
}

type Review = { publication: McpPublication; plan: PublishPlan; message?: string }
type Frame = { step?: string; text: string }

// Record committed frames before effects can replace the opening controls or closing content.
function recordFrame(frames: Frame[]) {
  const dialog = document.querySelector('[role="dialog"]')
  if (!dialog) return
  const text = dialog.textContent ?? ''
  frames.push({ step: text.match(/Step \d of 4/)?.[0], text })
}

function DialogParent({ reviews, frames, onPublished }: {
  reviews: Review[]; frames: Frame[]; onPublished: (message: string) => void
}) {
  const [opened, setOpened] = useState<{ review: Review | null } | null>(null)
  const restoreFocus = useRestoreFocusTarget()
  return (
    <>
      <button {...restoreFocus} onClick={() => setOpened({ review: null })}>Start publishing</button>
      {reviews.map((review, index) => (
        <button {...restoreFocus} key={index} onClick={() => setOpened({ review })}>Open review {index + 1}</button>
      ))}
      <Profiler id="mcp-dialog" onRender={() => recordFrame(frames)}>
        <PublishMcpServerDialog open={opened !== null} initialReview={opened?.review}
          onClose={() => setOpened(null)} onPublished={onPublished} />
      </Profiler>
    </>
  )
}

function renderDialogParent(
  reviews: Review[] = [],
  onPublished = vi.fn(),
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  const frames: Frame[] = []
  render(
    <QueryClientProvider client={queryClient}>
      <DialogParent reviews={reviews} frames={frames} onPublished={onPublished} />
    </QueryClientProvider>,
  )
  return frames
}

function focused() {
  const element = document.activeElement as HTMLElement
  const dialog = screen.getByRole('dialog')
  expect(dialog.closest('[aria-hidden="true"]')).toBeNull()
  expect(element).not.toBe(dialog)
  expect(dialog).toContainElement(element)
  return element
}

async function settle() {
  // Include Tabster's delayed aria-hidden pass, not just React's immediate render.
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)) })
}

async function configure(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
  await user.click(screen.getByRole('button', { name: 'Configure' }))
}

describe('PublishMcpServerDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [
        { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, aliases: [], acceptsEndpointsFrom: [], order: 10, builtIn: true, usage: { gateways: 1, modelEndpoints: 0, mcpEndpoints: 1 } },
        { key: 'development', displayName: 'Development', description: null, color: 'informative', production: false, aliases: [], acceptsEndpointsFrom: [], order: 20, builtIn: true, usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 1 } },
      ],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [
        { level: 'allowed', reason: 'Production can publish production servers.', gatewayEnvironment: 'production', endpointEnvironment: 'production', viaException: false },
        { level: 'blocked', reason: 'Production gateways cannot publish development MCP servers.', gatewayEnvironment: 'production', endpointEnvironment: 'development', viaException: false },
        { level: 'warning', reason: 'Review before publishing production through development.', gatewayEnvironment: 'development', endpointEnvironment: 'production', viaException: false },
      ],
      updatedAt: null,
    })
    api.listPrincipals.mockResolvedValue([])
    api.listGateways.mockResolvedValue([gateway()])
    api.listMcpEndpoints.mockResolvedValue([endpoint()])
    api.getMcpPublishingCapability.mockResolvedValue({ gatewayId: 'gateway_1', supported: true, reasons: [], warnings: [] })
    api.createMcpPublication.mockResolvedValue(publication)
    api.updateMcpPublication.mockResolvedValue(publication)
    api.deleteMcpPublication.mockResolvedValue(undefined)
    api.planMcpPublication.mockResolvedValue(plan)
    api.applyMcpPublication.mockResolvedValue(run())
    api.getMcpPublishRun.mockResolvedValue(run())
  })

  it('opens an existing review on its first frame with focus inside the accessible dialog', async () => {
    const user = userEvent.setup()
    const frames = renderDialogParent([{ publication, plan }])
    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await settle()

    expect(focused()).toBe(screen.getByRole('button', { name: 'Close' }))
    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 3 of 4']))
    expect(api.applyMcpPublication).not.toHaveBeenCalled()
  })

  it('moves focus to each new step instead of leaving it on a removed control', async () => {
    const user = userEvent.setup()
    renderDialog()
    await configure(user)
    await settle()
    expect(focused()).toBe(screen.getByText('Step 2 of 4'))
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(focused()).toBe(screen.getByText('Step 1 of 4'))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')
    await settle()
    expect(focused()).toBe(screen.getByText('Step 3 of 4'))
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(focused()).toBe(screen.getByText('Step 2 of 4'))
  })

  it.each(['Escape', 'Close'])('dismisses an existing review with %s and restores the publication opener', async (action) => {
    const user = userEvent.setup()
    const frames = renderDialogParent([{ publication, plan }])
    const opener = screen.getByRole('button', { name: 'Open review 1' })
    await user.click(opener)
    await settle()
    focused()
    frames.length = 0
    if (action === 'Escape') await user.keyboard('{Escape}')
    else await user.keyboard('{Enter}')
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
    await waitFor(() => expect(opener).toHaveFocus())
    expect(frames.length).toBeGreaterThan(0)
    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 3 of 4']))
    expect(frames.every((frame) => frame.text.includes('Review MCP access'))).toBe(true)
  })

  it('starts each reopening with the requested plan and forgets the previous apply error', async () => {
    const user = userEvent.setup()
    const second: Review = {
      publication: { ...publication, id: 'mcp_pub_2' },
      plan: { ...plan, id: 'plan_2', publicationId: 'mcp_pub_2', warnings: ['Second publication review'] },
    }
    api.applyMcpPublication.mockRejectedValueOnce(new Error('First apply refused.'))
    const frames = renderDialogParent([{ publication, plan }, second])
    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await screen.findByText('First apply refused.')
    await user.click(screen.getByRole('button', { name: 'Close' }))
    const opener = await screen.findByRole('button', { name: 'Open review 2' })
    frames.length = 0
    await user.click(opener)
    await settle()
    expect(frames.every((frame) => frame.step === 'Step 3 of 4' && frame.text.includes('Second publication review'))).toBe(true)
    expect(screen.queryByText('First apply refused.')).not.toBeInTheDocument()
    expect(focused()).toBe(screen.getByRole('button', { name: 'Close' }))
    expect(api.applyMcpPublication).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(api.applyMcpPublication).toHaveBeenLastCalledWith('mcp_pub_2', 'plan_2'))
    await screen.findByText('Step 4 of 4')
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await user.click(await screen.findByRole('button', { name: 'Open review 2' }))
    await settle()
    expect(screen.getByText('Step 3 of 4')).toBeVisible()
    expect(focused()).toBe(screen.getByRole('button', { name: 'Close' }))
  })

  it('retains the publish step during closing and starts fresh when reopened', async () => {
    const user = userEvent.setup()
    const frames = renderDialogParent()
    await user.click(screen.getByRole('button', { name: 'Start publishing' }))
    await configure(user)
    frames.length = 0
    await user.click(screen.getByRole('button', { name: 'Close' }))
    const opener = await screen.findByRole('button', { name: 'Start publishing' })
    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 2 of 4']))
    await user.click(opener)
    expect(screen.getByText('Step 1 of 4')).toBeVisible()
    expect(await screen.findByRole('checkbox', { name: 'Publish Weather tools' })).not.toBeChecked()
    focused()
  })

  it('keeps pending buttons focusable without duplicate plan or apply, then focuses the result', async () => {
    const user = userEvent.setup()
    let finishPlan!: (value: PublishPlan) => void
    let finishApply!: (value: PublishRun) => void
    api.planMcpPublication.mockReturnValueOnce(new Promise<PublishPlan>((resolve) => { finishPlan = resolve }))
    api.applyMcpPublication.mockReturnValueOnce(new Promise<PublishRun>((resolve) => { finishApply = resolve }))
    renderDialogParent()
    const opener = screen.getByRole('button', { name: 'Start publishing' })
    await user.click(opener)
    await configure(user)
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    const creating = await screen.findByRole('button', { name: 'Creating plan…' })
    expect(creating).toHaveFocus()
    expect(creating).toHaveAttribute('aria-disabled', 'true')
    expect(creating).not.toBeDisabled()
    await user.click(creating)
    await user.keyboard('{Enter} ')
    expect(api.planMcpPublication).toHaveBeenCalledTimes(1)
    await act(async () => { finishPlan(plan) })
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))
    const applying = await screen.findByRole('button', { name: 'Applying…' })
    expect(applying).toHaveFocus()
    expect(applying).toHaveAttribute('aria-disabled', 'true')
    expect(applying).not.toBeDisabled()
    await user.click(applying)
    await user.keyboard('{Enter} ')
    expect(api.applyMcpPublication).toHaveBeenCalledTimes(1)
    await act(async () => { finishApply(run()) })
    await screen.findByText('Step 4 of 4')
    await settle()
    expect(focused()).toContainElement(screen.getByRole('table', { name: 'MCP publish run steps' }))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it.each(['plan', 'apply'] as const)('refreshes caches without changing the new dialog after a delayed %s completion', async (operation) => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const keys = operation === 'apply'
      ? ['mcp-publications', 'mcp-servers', 'entitlements', 'entitlement-connection']
      : ['mcp-publications']
    keys.forEach((key) => queryClient.setQueryData([key], []))
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    let finish!: () => void
    const pending = new Promise<PublishPlan | PublishRun>((resolve) => {
      finish = () => resolve(operation === 'plan' ? plan : run())
    })
    const onPublished = vi.fn()
    renderDialogParent([{ publication, plan }], onPublished, queryClient)
    if (operation === 'plan') {
      api.planMcpPublication.mockReturnValueOnce(pending)
      await user.click(screen.getByRole('button', { name: 'Start publishing' }))
      await configure(user)
      await user.click(screen.getByRole('button', { name: 'Review plan' }))
      await screen.findByRole('button', { name: 'Creating plan…' })
    } else {
      api.applyMcpPublication.mockReturnValueOnce(pending)
      await user.click(screen.getByRole('button', { name: 'Open review 1' }))
      await user.click(screen.getByRole('button', { name: 'Apply plan' }))
      await screen.findByRole('button', { name: 'Applying…' })
    }
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await user.click(await screen.findByRole('button', { name: 'Start publishing' }))
    invalidate.mockClear()
    await act(async () => { finish() })
    await settle()
    expect(screen.getByText('Step 1 of 4')).toBeVisible()
    expect(focused()).toBe(screen.getByRole('combobox', { name: 'Gateway' }))
    expect(onPublished).not.toHaveBeenCalled()
    for (const key of keys) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: [key] })
      expect(queryClient.getQueryState([key])?.isInvalidated).toBe(true)
    }
  })

  it('refreshes caches without announcing or focusing a delayed apply while the closed session is mounted', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const keys = ['mcp-publications', 'mcp-servers', 'entitlements', 'entitlement-connection']
    keys.forEach((key) => queryClient.setQueryData([key], []))
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    let finish!: (value: PublishRun) => void
    api.applyMcpPublication.mockReturnValueOnce(new Promise<PublishRun>((resolve) => { finish = resolve }))
    const onPublished = vi.fn()
    renderDialogParent([{ publication, plan }], onPublished, queryClient)
    const opener = screen.getByRole('button', { name: 'Open review 1' })
    await user.click(opener)
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await screen.findByRole('button', { name: 'Applying…' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(opener).toHaveFocus())
    invalidate.mockClear()
    await act(async () => { finish(run()) })
    await settle()
    expect(onPublished).not.toHaveBeenCalled()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(opener).toHaveFocus()
    for (const key of keys) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: [key] })
      expect(queryClient.getQueryState([key])?.isInvalidated).toBe(true)
    }
  })

  it('focuses and refreshes caches for a polled terminal outcome after showing the running apply step', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const running = run({ status: 'running', completedAt: null })
    api.applyMcpPublication.mockResolvedValueOnce(running)
    api.getMcpPublishRun.mockResolvedValueOnce(running).mockResolvedValue(run())
    renderDialogParent([{ publication, plan }], vi.fn(), queryClient)
    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(focused()).toBe(screen.getByText('Step 4 of 4')))
    invalidate.mockClear()
    await waitFor(() => expect(focused()).toHaveTextContent('Local development service reported completion'), { timeout: 3000 })
    expect(api.getMcpPublishRun).toHaveBeenCalledTimes(2)
    expect(invalidate).toHaveBeenCalledTimes(4)
    for (const key of ['mcp-publications', 'mcp-servers', 'entitlements', 'entitlement-connection']) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: [key] })
    }
  })

  it.each(['failed', 'interrupted', 'rolledBack', 'rollbackFailed'] as const)('focuses the %s run outcome', async (status) => {
    const user = userEvent.setup()
    const result = run({ status, errors: ['The backend could not be created.'] })
    api.applyMcpPublication.mockResolvedValueOnce(result)
    api.getMcpPublishRun.mockResolvedValue(result)
    renderDialogParent([{ publication, plan }])
    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(focused()).toHaveTextContent('The backend could not be created.'))
    expect(focused()).toContainElement(screen.getByRole('table', { name: 'MCP publish run steps' }))
  })

  it.each([false, true])('focuses a stale-plan refusal and requires explicit reapply (refresh fails: %s)', async (refreshFails) => {
    const user = userEvent.setup()
    api.applyMcpPublication.mockRejectedValueOnce(Object.assign(new Error('Plan is stale.'), { status: 409 }))
    if (refreshFails) api.planMcpPublication.mockRejectedValueOnce(new Error('Cannot refresh this plan.'))
    else api.planMcpPublication.mockResolvedValueOnce({ ...plan, id: 'fresh_plan', warnings: ['Fresh plan'] })
    renderDialogParent([{ publication, plan }])
    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(focused()).toHaveTextContent('Plan is stale.'))
    expect(api.applyMcpPublication).toHaveBeenCalledTimes(1)
    if (refreshFails) {
      expect(screen.getByText('Cannot refresh this plan.')).toBeVisible()
      expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
    } else {
      expect(screen.getByText('Fresh plan')).toBeVisible()
      await user.click(screen.getByRole('button', { name: 'Apply plan' }))
      await waitFor(() => expect(api.applyMcpPublication).toHaveBeenLastCalledWith('mcp_pub_1', 'fresh_plan'))
    }
  })

  it('focuses an apply error and keeps retry focus until the next outcome', async () => {
    const user = userEvent.setup()
    let finish!: (value: PublishRun) => void
    api.applyMcpPublication.mockRejectedValueOnce(new Error('Apply refused.'))
      .mockReturnValueOnce(new Promise<PublishRun>((resolve) => { finish = resolve }))
    renderDialogParent([{ publication, plan }])
    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(focused()).toHaveTextContent('Apply refused.'))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await settle()
    expect(focused()).toBe(screen.getByRole('button', { name: 'Applying…' }))
    await act(async () => { finish(run()) })
    await waitFor(() => expect(focused()).toHaveTextContent('Local development service reported completion'))
  })

  it.each([false, true])('handles a delayed plan error without stealing an edited field (editing: %s)', async (editing) => {
    const user = userEvent.setup()
    let refuse!: (error: Error) => void
    api.planMcpPublication.mockReturnValueOnce(new Promise<PublishPlan>((_, reject) => { refuse = reject }))
    renderDialog()
    await configure(user)
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    const field = screen.getByRole('textbox', { name: 'Display name' })
    if (editing) await user.type(field, ' EU')
    await act(async () => { refuse(new Error('Plan could not be created.')) })
    await screen.findByText('Plan could not be created.')
    await settle()
    if (editing) {
      expect(focused()).toBe(field)
      expect(field).toHaveValue('Weather tools EU')
    } else expect(focused()).toHaveTextContent('Plan could not be created.')
  })

  it('blocks publishing when gateway capability returns reasons', async () => {
    api.getMcpPublishingCapability.mockResolvedValue({
      gatewayId: 'gateway_1',
      supported: false,
      reasons: ['Synchronize the gateway before publishing MCP servers.'],
      warnings: ['Diagnostics must log zero-byte response bodies for MCP streaming.'],
    })
    renderDialog()

    expect(await screen.findByText('Synchronize the gateway before publishing MCP servers.')).toBeVisible()
    expect(screen.getByText('Diagnostics must log zero-byte response bodies for MCP streaming.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Configure' })).toBeDisabled()
  })

  it('explains endpoint blockers before create', async () => {
    api.listMcpEndpoints.mockResolvedValue([
      endpoint({ id: 'blocked', authMode: 'apiKey', capabilities: { ...endpoint().capabilities, transportType: 'sse' } }),
    ])
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable MCP servers' })
    expect(within(table).getByText("MOSAIC can't publish an MCP server that needs an API key yet.")).toBeVisible()
    expect(within(table).getByText('SSE servers cannot be published. Register a Streamable HTTP endpoint instead.')).toBeVisible()
    expect(within(table).getByRole('checkbox', { name: 'Publish Weather tools' })).toBeDisabled()
  })

  it('blocks a server that API Management cannot reach through a backend URL', async () => {
    api.listMcpEndpoints.mockResolvedValue([
      endpoint({ id: 'stream', name: 'Stream tools', endpoint: 'https://mcp.example.test/api/stream' }),
      endpoint({ id: 'query', name: 'Query tools', endpoint: 'https://mcp.example.test/mcp?tenant=contoso' }),
      endpoint({ id: 'nested', name: 'Nested tools', endpoint: 'https://mcp.example.test/runtime/webhooks/mcp/' }),
    ])
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable MCP servers' })
    expect(
      within(table).getByText(
        'MOSAIC can publish an MCP server only if its URL ends in /mcp, because API Management adds /mcp when it forwards a call. Register its Streamable HTTP endpoint that ends in /mcp.',
      ),
    ).toBeVisible()
    expect(
      within(table).getByText(
        "MOSAIC can't publish an MCP server registered with a query string or fragment. Register it by a URL without one.",
      ),
    ).toBeVisible()
    expect(within(table).getByRole('checkbox', { name: 'Publish Stream tools' })).toBeDisabled()
    expect(within(table).getByRole('checkbox', { name: 'Publish Query tools' })).toBeDisabled()
    expect(within(table).getByRole('checkbox', { name: 'Publish Nested tools' })).toBeEnabled()
  })

  it('shows environments and disables blocked pairings with the reason', async () => {
    api.listGateways.mockResolvedValue([gateway({ environment: 'production' })])
    api.listMcpEndpoints.mockResolvedValue([endpoint({ environment: 'development' })])
    renderDialog()

    expect(await screen.findByText('Gateway environment:')).toBeVisible()
    expect(screen.getByText('Production')).toBeVisible()
    const table = await screen.findByRole('table', { name: 'Publishable MCP servers' })
    expect(within(table).getByText('Server environment:')).toBeVisible()
    expect(within(table).getByText('Development')).toBeVisible()
    expect(within(table).getByText('Production gateways cannot publish development MCP servers.')).toBeVisible()
    expect(within(table).getByRole('checkbox', { name: 'Publish Weather tools' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Configure' })).toBeDisabled()
  })

  it('shows warning pairings and still allows review', async () => {
    const user = userEvent.setup()
    api.listGateways.mockResolvedValue([gateway({ environment: 'development' })])
    api.listMcpEndpoints.mockResolvedValue([endpoint({ environment: 'production' })])
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable MCP servers' })
    expect(within(table).getByText('Review before publishing production through development.')).toBeVisible()
    await user.click(within(table).getByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))

    expect(await screen.findByText('Security readers')).toBeVisible()
    expect(api.planMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
  })

  it('shows an environment refusal when create or plan is blocked', async () => {
    const user = userEvent.setup()
    api.planMcpPublication.mockRejectedValueOnce(Object.assign(new Error('Blocked'), {
      status: 409,
      body: {
        details: {
          reason: 'environmentBlocked',
          verdict: { level: 'blocked', reason: 'Production gateways cannot publish development MCP servers.', gatewayEnvironment: 'production', endpointEnvironment: 'development', viaException: false },
        },
      },
    }))
    renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))

    expect(await screen.findByText('Environment rules block this publication')).toBeVisible()
    expect(screen.getByText('Production gateways cannot publish development MCP servers.')).toBeVisible()
    expect(focused()).toHaveTextContent('Environment rules block this publication')
  })

  it('creates, plans, reviews MCP grants, applies, and polls the run', async () => {
    const user = userEvent.setup()
    const onPublished = vi.fn()
    renderDialog(onPublished)

    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    expect(screen.getByRole('textbox', { name: 'API name' })).toHaveValue('mosaic-mcp-weather-tools')
    await user.click(screen.getByRole('button', { name: 'Review plan' }))

    await waitFor(() => expect(api.createMcpPublication).toHaveBeenCalledWith({
      gatewayId: 'gateway_1',
      mcpEndpointId: 'endpoint_1',
      displayName: 'Weather tools',
      apiName: 'mosaic-mcp-weather-tools',
      apiPath: 'mosaic/mcp/weather-tools',
    }))
    expect(api.planMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(await screen.findByText('Security readers')).toBeVisible()
    expect(screen.getByText('Security group')).toBeVisible()
    expect(screen.getByText('Limits traffic to 60 calls per minute.')).toBeVisible()
    expect(screen.getByText('Create the MCP API.')).toBeVisible()
    expect(screen.getByText('Validates MCP caller tokens.')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(api.applyMcpPublication).toHaveBeenCalledWith('mcp_pub_1', 'plan_1'))
    expect(await screen.findByText('Local development service reported completion; live APIM apply is not verified.')).toBeVisible()
    expect(await screen.findByRole('table', { name: 'MCP publish run steps' })).toBeVisible()
    expect(onPublished).toHaveBeenCalledWith('Local development service reported completion. Live APIM apply is not verified.')
  })

  it('shows an environment refusal on apply and does not re-plan', async () => {
    const user = userEvent.setup()
    api.applyMcpPublication.mockRejectedValueOnce(Object.assign(new Error('Blocked'), {
      status: 409,
      body: {
        details: {
          reason: 'environmentBlocked',
          verdict: { level: 'blocked', reason: 'Environment rules changed. Reclassify before applying.', gatewayEnvironment: 'production', endpointEnvironment: 'development', viaException: false },
        },
      },
    }))
    renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')
    expect(api.planMcpPublication).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText('Environment rules block this publication')).toBeVisible()
    expect(screen.getByText('Environment rules changed. Reclassify before applying.')).toBeVisible()
    expect(focused()).toHaveTextContent('Environment rules changed. Reclassify before applying.')
    expect(api.planMcpPublication).toHaveBeenCalledTimes(1)
  })

  it('uses principal kinds from the directory and falls back to subject labels', async () => {
    api.listPrincipals.mockResolvedValue([{ id: 'app_1', tenantId: 'tenant-test', objectId: 'object', kind: 'agentIdentity', label: 'Agent app', createdAt: '', updatedAt: '' }])
    const reviewPlan: PublishPlan = {
      ...plan,
      mcpAccessSnapshot: {
        ...plan.mcpAccessSnapshot!,
        grants: [
          { ...plan.mcpAccessSnapshot!.grants[0], entitlementId: 'grant_agent', subject: { kind: 'application', id: 'app_1' }, objectId: 'agent-object', displayName: 'Agent app' },
          { ...plan.mcpAccessSnapshot!.grants[0], entitlementId: 'grant_unknown', subject: { kind: 'application', id: 'missing' }, objectId: 'missing-object', displayName: 'Unknown app' },
        ],
      },
    }
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <PublishMcpServerDialog
          open
          onClose={vi.fn()}
          onPublished={vi.fn()}
          initialReview={{ publication, plan: reviewPlan }}
        />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Agent · agent-object')).toBeVisible()
    expect(screen.getByText('Application · missing-object')).toBeVisible()
  })

  it('names the application the plan calls models as, or none', async () => {
    const reviewPlan: PublishPlan = {
      ...plan,
      mcpAccessSnapshot: {
        ...plan.mcpAccessSnapshot!,
        modelCaller: {
          principalId: 'principal_docs',
          objectId: 'aaaabbbb-cccc-dddd-eeee-ffff00001111',
          displayName: 'Docs Search service',
        },
      },
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(
      <QueryClientProvider client={client}>
        <PublishMcpServerDialog
          open
          onClose={vi.fn()}
          onPublished={vi.fn()}
          initialReview={{ publication, plan: reviewPlan }}
        />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText(
        "Calls models as: Docs Search service · aaaabbbb-cccc-dddd-eeee-ffff00001111. The server receives each call's reference to pass on to its model calls.",
      ),
    ).toBeVisible()

    view.rerender(
      <QueryClientProvider client={client}>
        <PublishMcpServerDialog
          open
          onClose={vi.fn()}
          onPublished={vi.fn()}
          initialReview={{ publication, plan }}
        />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Calls models as: none.')).toBeVisible()
  })

  it('reuses its draft when a failed plan is retried, rather than creating a duplicate', async () => {
    const user = userEvent.setup()
    api.planMcpPublication.mockRejectedValueOnce(new Error('The gateway did not respond.'))
    renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    expect(await screen.findByText('The gateway did not respond.')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    expect(await screen.findByText('Security readers')).toBeVisible()
    expect(api.createMcpPublication).toHaveBeenCalledTimes(1)
    expect(api.planMcpPublication).toHaveBeenCalledTimes(2)
    expect(api.planMcpPublication).toHaveBeenLastCalledWith('mcp_pub_1')
    expect(api.updateMcpPublication).not.toHaveBeenCalled()
    expect(api.deleteMcpPublication).not.toHaveBeenCalled()
  })

  it('after going back, renames its draft in place and replaces it when an API name changes', async () => {
    const user = userEvent.setup()
    renderDialog()
    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')

    api.updateMcpPublication.mockResolvedValue({ ...publication, displayName: 'Forecasts' })
    await user.click(screen.getByRole('button', { name: 'Back' }))
    const displayName = screen.getByRole('textbox', { name: 'Display name' })
    await user.clear(displayName)
    await user.type(displayName, 'Forecasts')
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')
    expect(api.updateMcpPublication).toHaveBeenCalledWith('mcp_pub_1', { displayName: 'Forecasts' })
    expect(api.createMcpPublication).toHaveBeenCalledTimes(1)
    expect(api.deleteMcpPublication).not.toHaveBeenCalled()

    // API names shape API Management resources and can't be edited, so the unapplied draft is
    // replaced instead.
    api.createMcpPublication.mockResolvedValue({
      ...publication, displayName: 'Forecasts', apiName: 'mosaic-mcp-forecasts',
    })
    await user.click(screen.getByRole('button', { name: 'Back' }))
    const apiName = screen.getByRole('textbox', { name: 'API name' })
    await user.clear(apiName)
    await user.type(apiName, 'mosaic-mcp-forecasts')
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')
    expect(api.deleteMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(api.createMcpPublication).toHaveBeenCalledTimes(2)
    expect(api.createMcpPublication).toHaveBeenLastCalledWith({
      gatewayId: 'gateway_1',
      mcpEndpointId: 'endpoint_1',
      displayName: 'Forecasts',
      apiName: 'mosaic-mcp-forecasts',
      apiPath: 'mosaic/mcp/weather-tools',
    })
    expect(api.planMcpPublication).toHaveBeenCalledTimes(3)
  })
})
