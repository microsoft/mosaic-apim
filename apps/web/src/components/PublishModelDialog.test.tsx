import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Profiler, useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PublishModelDialog } from './PublishModelDialog'
import type { Gateway, Publication, PublishableModel, PublishPlan, PublishRun } from '../types'
import { accessPlan, accessSnapshot, modelPublication } from '../test/model-access'

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
    environmentLabel: null,
    managementMode: 'manage',
    status: 'connected',
    access: { canRead: true, canWrite: true, evaluation: 'effectivePermissions', missingActions: [], remediation: null, message: 'Ready.' },
    capabilities: { managementApiVersion: '2024-05-01', aiGatewayPolicies: 'available', mcpServers: 'available', identityObserved: true, notes: [] },
    inventory: { apis: 0, aiApis: 0, mcpServers: 0, operations: 0, products: 0, subscriptions: 0, users: 0, groups: 0, backends: 0, namedValues: 0, policyDocuments: 0, policyFragments: 0, recognizedFacets: 0, unrecognizedFacets: 0, mosaicManagedFacets: 0 },
    lastSyncedAt: '2026-09-01T12:00:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

const publishableModel: PublishableModel = {
  modelEndpointId: 'endpoint_1',
  endpointName: 'Contoso models',
  provider: 'azureOpenAi',
  deploymentName: 'gpt-4o-prod',
  modelName: 'gpt-4o',
  modelVersion: '2024-11-20',
  publicationId: null,
  publicationStatus: null,
  suggestedApiName: 'gpt-4o-api',
  suggestedApiPath: 'models/gpt-4o',
  runtimeAccess: { gatewayId: 'gateway_1', gatewayName: 'Managed gateway', apimPrincipalId: 'principal', canInvoke: true, evaluation: 'roleAssignments', checkedAt: '2026-09-01T12:00:00Z', requiredRoleName: 'Cognitive Services OpenAI User', requiredRoleDefinitionId: 'role', assignmentScope: null, inherited: false, remediation: null, message: 'Gateway can invoke this endpoint.' },
}

const publication: Publication = {
  accessState: 'pending',
  id: 'pub_1', tenantId: 'tenant-test', entityType: 'publication', gatewayId: 'gateway_1', modelEndpointId: 'endpoint_1', deploymentName: 'gpt-4o-prod', provider: 'azureOpenAi', displayName: 'GPT-4o', apiName: 'gpt-4o-api', apiPath: 'models/gpt-4o', backendName: 'backend', fragmentName: 'fragment', productName: 'product', subscriptionName: 'subscription', subscriptionRequired: true, enforcement: { counterKeyExpression: '@(context.Subscription.Id)', tokensPerMinute: 12000, estimatePromptTokens: true }, shapeVersion: '1', status: 'planned', resources: [], lastPlanId: 'plan_1', lastPlanDigest: 'digest', lastRunId: null, lastAppliedAt: null, lastError: null, createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
}

const plan: PublishPlan = {
  id: 'plan_1', tenantId: 'tenant-test', entityType: 'publishPlan', publicationId: 'pub_1', gatewayId: 'gateway_1', digest: 'digest', policyContentSha256: null, warnings: ['The API path already exists and will be updated.'], createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
  steps: [{ kind: 'api', name: 'gpt-4o-api', action: 'create', reason: 'Create the API for this deployment.', resourceId: '/apis/gpt-4o-api', existed: false }],
  facets: [{ kind: 'tokenLimit', element: 'azure-openai-token-limit', section: 'inbound', summary: 'Limits tokens per caller.', details: ['12000 tokens per minute.'], attributes: {}, confidence: 'recognized', managedByMosaic: true }],
}

function run(overrides: Partial<PublishRun> = {}): PublishRun {
  return {
    id: 'run_1', tenantId: 'tenant-test', entityType: 'publishRun', publicationId: 'pub_1', gatewayId: 'gateway_1', planId: 'plan_1', planDigest: 'digest', status: 'succeeded', startedAt: '2026-09-01T12:00:00Z', completedAt: '2026-09-01T12:00:02Z', durationMs: 2000, rolledBack: false, orphanedResources: [], errors: [], createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:02Z',
    steps: [{ kind: 'api', name: 'gpt-4o-api', action: 'create', status: 'succeeded', resourceId: '/apis/gpt-4o-api', createdByMosaic: true, error: null }],
    ...overrides,
  }
}

const api = {
  listGateways: vi.fn(),
  listPublishableModels: vi.fn(),
  createPublication: vi.fn(),
  createPublishPlan: vi.fn(),
  applyPublishPlan: vi.fn(),
  getPublishRun: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderDialog(initialReview?: { publication: Publication; plan: PublishPlan }, onPublished = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PublishModelDialog open initialReview={initialReview} onClose={vi.fn()} onPublished={onPublished} />
    </QueryClientProvider>,
  )
}

async function advanceToReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('checkbox', { name: 'Publish gpt-4o-prod' }))
  await user.click(screen.getByRole('button', { name: 'Configure' }))
  await user.click(screen.getByRole('button', { name: 'Review plan' }))
}

// The element that has focus, which has to be inside the open dialog: Fluent closes a dialog on Escape
// only when the key is pressed inside it. The dialog itself contains everything, so it doesn't count.
function focused() {
  const element = document.activeElement as HTMLElement
  const dialog = screen.getByRole('dialog')
  expect(element).not.toBe(document.body)
  expect(element).not.toBe(dialog)
  expect(dialog).toContainElement(element)
  return element
}

type Review = { publication: Publication; plan: PublishPlan; message?: string }

// What the dialog showed when React committed a render.
type Frame = { title?: string; step?: string; choosing: boolean; text: string }

// Profiler calls onRender as React commits, after it updates the DOM but before useEffect callbacks run,
// so this records even a frame that an effect replaces straight away, which no query after the render
// can see.
function recordFrame(frames: Frame[]) {
  const dialog = document.querySelector('[role="dialog"]')
  if (!dialog) return
  const text = dialog.textContent ?? ''
  frames.push({
    title: dialog.querySelector('h2')?.textContent ?? undefined,
    step: text.match(/Step \d of 4/)?.[0],
    choosing: dialog.querySelector('select[aria-label="Gateway"], [aria-label="Publishable models"]') !== null,
    text,
  })
}

// Opens the dialog the way its parents do. The dialog stays mounted while closed. It opens when the parent
// sets a review, as EntitlementsPage and ModelsPage do, or to publish a model, as the Models page's publish
// button does. Closing clears it.
function DialogParent({
  reviews,
  frames,
  onPublished,
}: {
  reviews: Review[]
  frames: Frame[]
  onPublished: (message: string) => void
}) {
  const [opened, setOpened] = useState<{ review: Review | null } | null>(null)
  return (
    <>
      <button type="button" onClick={() => setOpened({ review: null })}>Start publishing</button>
      {reviews.map((review, index) => (
        <button key={index} type="button" onClick={() => setOpened({ review })}>
          Open review {index + 1}
        </button>
      ))}
      <Profiler id="publish-model-dialog" onRender={() => recordFrame(frames)}>
        <PublishModelDialog
          open={opened !== null}
          initialReview={opened?.review}
          onClose={() => setOpened(null)}
          onPublished={onPublished}
        />
      </Profiler>
    </>
  )
}

function renderDialogParent(reviews: Review[] = [], onPublished = vi.fn()) {
  const frames: Frame[] = []
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <DialogParent reviews={reviews} frames={frames} onPublished={onPublished} />
    </QueryClientProvider>,
  )
  return frames
}

const secondReview: Review = {
  publication: { ...modelPublication, id: 'pub_2', displayName: 'Second chat' },
  plan: {
    ...accessPlan,
    id: 'access-plan-2',
    publicationId: 'pub_2',
    accessSnapshot: {
      ...accessSnapshot,
      grants: [{ ...accessSnapshot.grants[0], entitlementId: 'second_grant', displayName: 'Grace Hopper' }],
    },
  },
  message: 'The earlier plan was rejected. Review the refreshed plan before applying it.',
}

describe('PublishModelDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([gateway(), gateway({ id: 'gateway_2', name: 'Observe gateway', managementMode: 'observe' })])
    api.listPublishableModels.mockResolvedValue([publishableModel])
    api.createPublication.mockResolvedValue(publication)
    api.createPublishPlan.mockResolvedValue(plan)
    api.applyPublishPlan.mockResolvedValue(run())
    api.getPublishRun.mockResolvedValue(run())
  })

  it('renders publishable models', async () => {
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable models' })
    expect(within(table).getByText('gpt-4o-prod')).toBeVisible()
    expect(within(table).getByText('gpt-4o')).toBeVisible()
    expect(within(table).getByText('Runtime permissions observed')).toBeVisible()
  })

  it('does not present a conditional role assignment as either access or a denial', async () => {
    api.listPublishableModels.mockResolvedValue([
      {
        ...publishableModel,
        runtimeAccess: {
          ...publishableModel.runtimeAccess!,
          canInvoke: false,
          evaluation: 'notEvaluated',
          reason: 'conditional',
          message: 'The role is assigned under an ABAC condition MOSAIC cannot prove holds.',
        },
      },
    ])

    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable models' })
    expect(within(table).getByText('Runtime access not confirmed')).toBeVisible()
    expect(within(table).getByText(/ABAC condition MOSAIC cannot prove holds/)).toBeVisible()
    expect(within(table).queryByText('Gateway may not be able to call this model')).not.toBeInTheDocument()
  })

  it('warns when the gateway has no network path to the endpoint', async () => {
    api.listPublishableModels.mockResolvedValue([
      {
        ...publishableModel,
        runtimeAccess: {
          ...publishableModel.runtimeAccess!,
          canInvoke: false,
          reason: 'networkUnreachable',
          networkReachability: 'unreachable',
          message: 'Public network access to this resource is disabled.',
        },
      },
    ])

    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable models' })
    expect(within(table).getByText('Gateway may not be able to call this model')).toBeVisible()
    expect(within(table).getByText(/Public network access to this resource is disabled/)).toBeVisible()
  })

  it('does not allow choosing a gateway outside manage mode', async () => {
    renderDialog()

    const option = await screen.findByRole('option', { name: /Observe gateway/ })
    expect(option).toBeDisabled()
    expect(option).toHaveTextContent(/switch to managed mode/i)
  })

  it('shows warnings, steps, and policy facets in the plan review', async () => {
    const user = userEvent.setup()
    renderDialog()

    await advanceToReview(user)

    await waitFor(() => expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1'))
    expect(await screen.findByText('The API path already exists and will be updated.')).toBeVisible()
    expect(screen.getByText('Create the API for this deployment.')).toBeVisible()
    expect(screen.getByText('Limits tokens per caller.')).toBeVisible()
  })

  it('reports a rolled-back run as rolled back', async () => {
    const user = userEvent.setup()
    const rolledBack = run({ status: 'rolledBack', rolledBack: true, steps: [{ ...run().steps[0], status: 'rolledBack' }] })
    api.applyPublishPlan.mockResolvedValue(rolledBack)
    api.getPublishRun.mockResolvedValue(rolledBack)
    renderDialog()

    await advanceToReview(user)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText(/undid what it created/i)).toBeVisible()
    // The banner title and the step's own status both read "Rolled back".
    expect(screen.getAllByText('Rolled back').length).toBeGreaterThan(1)
  })

  it('explains that a replaced resource was left in place rather than reverted', async () => {
    const user = userEvent.setup()
    const rolledBack = run({
      status: 'rolledBack',
      rolledBack: true,
      steps: [{ ...run().steps[0], kind: 'backend', status: 'skipped', createdByMosaic: false }],
    })
    api.applyPublishPlan.mockResolvedValue(rolledBack)
    api.getPublishRun.mockResolvedValue(rolledBack)
    renderDialog()

    await advanceToReview(user)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText(/left in place/i)).toBeVisible()
  })

  it('lists orphaned resources left behind in API Management', async () => {
    const user = userEvent.setup()
    const failed = run({
      status: 'rollbackFailed',
      orphanedResources: [{ kind: 'api', name: 'orphan-api', resourceId: '/apis/orphan-api', createdByMosaic: true, appliedAt: '2026-09-01T12:00:01Z' }],
    })
    api.applyPublishPlan.mockResolvedValue(failed)
    api.getPublishRun.mockResolvedValue(failed)
    renderDialog()

    await advanceToReview(user)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText(/Resources left behind in API Management/)).toBeVisible()
    expect(screen.getByText(/orphan-api/)).toBeVisible()
  })

  it('reviews every target grant, both method switches, inherited limits and bootstrap retirement', async () => {
    const snapshot = {
      ...accessSnapshot,
      settings: { keysEnabled: false, entraEnabled: false },
      grants: [...accessSnapshot.grants, {
        ...accessSnapshot.grants[0],
        entitlementId: 'application-grant', objectId: 'service-principal-object-id',
        displayName: 'Batch application', subject: { kind: 'application' as const, id: 'app_1' },
        enabled: false, subscriptionName: 'app-subscription',
      }],
    }
    renderDialog({ publication: modelPublication, plan: { ...accessPlan, accessSnapshot: snapshot } })
    const table = await screen.findByRole('table', { name: 'All target model grants' })
    expect(within(table).getByText('Ada Lovelace')).toBeVisible()
    expect(within(table).getByText('Batch application')).toBeVisible()
    expect(within(table).getByText('Disabled — revoke both methods')).toBeVisible()
    expect(screen.getByText('Target methods: Deny all — both methods disabled')).toBeVisible()
    expect(screen.getByText('Keys: disabled · Entra: disabled')).toBeVisible()
    expect(screen.getByText('Limits usage to 12,000 tokens per minute.')).toBeVisible()
    expect(screen.getByText(/retires this model's generic bootstrap subscription/)).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(api.listPublishableModels).not.toHaveBeenCalled()
  })

  it('renders repeated subscription steps with their distinct stages in the plan and result', async () => {
    const user = userEvent.setup()
    const stagedPlan: PublishPlan = {
      ...accessPlan,
      steps: [
        { ...plan.steps[0], kind: 'subscription', name: 'dedicated-sub', stage: 'prepare', subscriptionState: 'suspended', entitlementId: 'direct_grant' },
        { ...plan.steps[0], kind: 'subscription', name: 'dedicated-sub', stage: 'activate', subscriptionState: 'active', entitlementId: 'direct_grant' },
      ],
    }
    const stagedRun = run({
      steps: stagedPlan.steps.map((step) => ({ ...step, status: 'succeeded', createdByMosaic: true, error: null })),
    })
    api.applyPublishPlan.mockResolvedValue(stagedRun)
    api.getPublishRun.mockResolvedValue(stagedRun)
    renderDialog({ publication: modelPublication, plan: stagedPlan })
    const reviewTable = await screen.findByRole('table', { name: 'Publish plan steps' })
    expect(within(reviewTable).getAllByRole('row')).toHaveLength(3)
    expect(within(reviewTable).getByText('Stage: prepare')).toBeVisible()
    expect(within(reviewTable).getByText('Stage: activate')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    const resultTable = await screen.findByRole('table', { name: 'Publish run steps' })
    expect(within(resultTable).getAllByRole('row')).toHaveLength(3)
    expect(within(resultTable).getByText('Stage: prepare')).toBeVisible()
    expect(within(resultTable).getByText('Stage: activate')).toBeVisible()
  })

  it('reports interrupted apply as unknown rather than successful and stops polling', async () => {
    const user = userEvent.setup()
    const interrupted = run({ status: 'interrupted', errors: ['Worker stopped after policy install.'] })
    api.applyPublishPlan.mockResolvedValue(interrupted)
    api.getPublishRun.mockResolvedValue(interrupted)
    const onPublished = vi.fn()
    renderDialog({ publication: modelPublication, plan: accessPlan }, onPublished)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))
    expect(await screen.findByText('Apply interrupted — runtime state unknown')).toBeVisible()
    expect(screen.getByText('Worker stopped after policy install.')).toBeVisible()
    await waitFor(() => expect(api.getPublishRun).toHaveBeenCalledTimes(1))
    await new Promise((resolve) => setTimeout(resolve, 1100))
    expect(api.getPublishRun).toHaveBeenCalledTimes(1)
    expect(onPublished).not.toHaveBeenCalled()
  })

  it('does not present local development completion as verified APIM apply', async () => {
    const user = userEvent.setup()
    const onPublished = vi.fn()
    renderDialog({ publication: modelPublication, plan: accessPlan }, onPublished)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))
    expect(await screen.findByText('Local development service reported completion; live APIM apply is not verified.')).toBeVisible()
    await waitFor(() => expect(onPublished).toHaveBeenCalledWith('Local development service reported completion. Live APIM apply is not verified.'))
  })

  it('refuses a governed apply when the service omits the access snapshot', async () => {
    renderDialog({ publication: modelPublication, plan: { ...accessPlan, accessSnapshot: null } })
    expect(await screen.findByText(/The governed-access snapshot is missing/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('says plainly when API Management already matches the publication and offers nothing to apply', async () => {
    const unchanged: PublishPlan = {
      ...plan,
      steps: [{ ...plan.steps[0], action: 'noChange', reason: 'The API already matches this publication.', existed: true }],
    }
    renderDialog({ publication, plan: unchanged })

    expect(
      await screen.findByText('API Management already matches this publication. Nothing to apply.'),
    ).toBeVisible()
    expect(within(screen.getByRole('table', { name: 'Publish plan steps' })).getByText('No change')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
  })

  it('offers Apply plan when any step would change API Management', async () => {
    const mixed: PublishPlan = {
      ...plan,
      steps: [
        { ...plan.steps[0], action: 'noChange', reason: 'The API already matches this publication.', existed: true },
        { ...plan.steps[0], kind: 'product', name: 'product', action: 'update', reason: 'Replace the product that carries this API.', existed: true },
      ],
    }
    renderDialog({ publication, plan: mixed })

    expect(await screen.findByRole('button', { name: 'Apply plan' })).toBeEnabled()
    expect(screen.queryByText(/Nothing to apply/)).not.toBeInTheDocument()
  })

  it('says MOSAIC refused the reviewed plan, and why, above the fresh plan it made instead', async () => {
    const user = userEvent.setup()
    const refusal =
      'This plan does not create resources in the order MOSAIC now uses, so API Management could reject a step that ' +
      'names a resource not created yet. Re-plan this publication and review the new order before applying.'
    api.applyPublishPlan.mockRejectedValueOnce(Object.assign(new Error(refusal), { status: 409 }))
    api.createPublishPlan.mockResolvedValueOnce({ ...plan, id: 'plan_2' })
    renderDialog({ publication, plan })

    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(
      await screen.findByText('MOSAIC has already re-planned. Review the fresh plan below before you apply it.'),
    ).toBeVisible()
    expect(screen.getByText("MOSAIC didn't apply the plan you reviewed")).toBeVisible()
    expect(screen.getByText(refusal)).toBeVisible()
    expect(screen.queryByText(/earlier plan was rejected/)).not.toBeInTheDocument()
    expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1')

    await user.click(screen.getByRole('button', { name: 'Apply plan' }))

    await waitFor(() => expect(api.applyPublishPlan).toHaveBeenLastCalledWith('pub_1', 'plan_2'))
  })

  it('lists a deployment MOSAIC cannot publish with its reason and does not let it be chosen', async () => {
    const reason = 'Realtime models use WebSocket sessions, which MOSAIC can\'t publish yet.'
    api.listPublishableModels.mockResolvedValue([
      publishableModel,
      {
        ...publishableModel,
        deploymentName: 'gpt-realtime',
        modelName: 'gpt-realtime',
        modelFormat: 'OpenAI',
        capability: 'realtime',
        apiShape: null,
        publishable: false,
        unpublishableReason: reason,
      },
    ])
    const user = userEvent.setup()
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable models' })
    expect(within(table).getByText('gpt-realtime')).toBeVisible()
    expect(within(table).getByText('Not publishable')).toBeVisible()
    expect(within(table).getByText(reason)).toBeVisible()
    const blocked = within(table).getByRole('checkbox', { name: 'Publish gpt-realtime' })
    expect(blocked).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Configure' })).toBeDisabled()
    await user.click(within(table).getByRole('checkbox', { name: 'Publish gpt-4o-prod' }))
    expect(screen.getByRole('button', { name: 'Configure' })).toBeEnabled()
  })

  it('explains that a classic tier cannot token-limit Anthropic models and publishes without limits', async () => {
    const note = 'API Management applies token limits and token metrics to the Anthropic Messages API only on v2 tiers (Basic v2, Standard v2 and Premium v2). This gateway uses the Developer tier, a classic tier, so this publication applies no token limits or token metrics. Per-grant call limits are still available through governed access.'
    api.listPublishableModels.mockResolvedValue([
      {
        ...publishableModel,
        provider: 'azureAiFoundry',
        deploymentName: 'claude-sonnet-4-5',
        modelName: 'claude-sonnet-4-5',
        modelFormat: 'Anthropic',
        modelPublisher: 'Anthropic',
        capability: 'chat',
        apiShape: 'anthropicMessages',
        publishable: true,
        tokenLimitsSupported: false,
        tokenLimitsNote: note,
      },
    ])
    const user = userEvent.setup()
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable models' })
    expect(within(table).getByText('Anthropic Messages API')).toBeVisible()
    expect(within(table).getByText(/· Anthropic/)).toBeVisible()
    await user.click(within(table).getByRole('checkbox', { name: 'Publish claude-sonnet-4-5' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))

    expect(screen.getByText('Token limits unavailable')).toBeVisible()
    expect(screen.getByText(note)).toBeVisible()
    expect(screen.queryByLabelText('Counter key expression')).toBeNull()
    expect(screen.queryByLabelText('Tokens per minute')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Review plan' }))

    await waitFor(() => expect(api.createPublication).toHaveBeenCalledTimes(1))
    expect(api.createPublication).toHaveBeenCalledWith(
      expect.objectContaining({ deploymentName: 'claude-sonnet-4-5', enforcement: null }),
    )
  })

  it('keeps token limits for Anthropic models when the gateway tier supports them', async () => {
    api.listPublishableModels.mockResolvedValue([
      {
        ...publishableModel,
        provider: 'azureAiFoundry',
        deploymentName: 'claude-haiku-4-5',
        modelName: 'claude-haiku-4-5',
        modelFormat: 'Anthropic',
        apiShape: 'anthropicMessages',
        tokenLimitsSupported: true,
        tokenLimitsNote: null,
      },
    ])
    const user = userEvent.setup()
    renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Publish claude-haiku-4-5' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))

    expect(screen.queryByText('Token limits unavailable')).toBeNull()
    expect(screen.getByLabelText('Tokens per minute')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Review plan' }))

    await waitFor(() => expect(api.createPublication).toHaveBeenCalledTimes(1))
    expect(api.createPublication.mock.calls[0][0].enforcement).toMatchObject({
      counterKeyExpression: '@(context.Subscription.Id)',
      tokensPerMinute: 12000,
    })
  })

  it('shows a stale-plan refresh failure and does not allow reapplying the rejected plan', async () => {
    const user = userEvent.setup()
    api.applyPublishPlan.mockRejectedValue(Object.assign(new Error('Plan is stale.'), { status: 409 }))
    api.createPublishPlan.mockRejectedValue(new Error('Cannot refresh the model plan.'))
    renderDialog({ publication: modelPublication, plan: accessPlan })
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))
    expect(await screen.findByText('Cannot refresh the model plan.')).toBeVisible()
    expect(screen.queryByText(/already re-planned/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
  })

  it('opens a review on its review step, without first rendering the gateway and model chooser', async () => {
    const user = userEvent.setup()
    const frames = renderDialogParent([{ publication: modelPublication, plan: accessPlan }])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))

    // Every frame React committed while opening, not just the one left once effects have run.
    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 3 of 4']))
    expect(frames.filter((frame) => frame.choosing)).toEqual([])
    expect(frames[0].text).toContain('Model-wide access changes')
    expect(screen.getByText('Step 3 of 4')).toBeVisible()
    expect(screen.getByRole('table', { name: 'All target model grants' })).toBeVisible()
    expect(screen.queryByRole('combobox', { name: 'Gateway' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
    expect(api.listGateways).not.toHaveBeenCalled()
  })

  it('moves focus into the review as it opens', async () => {
    const user = userEvent.setup()
    renderDialogParent([{ publication: modelPublication, plan: accessPlan }])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))

    expect(document.activeElement).not.toBe(document.body)
    expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement)
  })

  it('leaves focus where Fluent puts it as a review opens', async () => {
    const user = userEvent.setup()
    renderDialogParent([{ publication: modelPublication, plan: accessPlan }])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))

    // Fluent focuses the first control Tab reaches, which in this review is Close.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus())
    expect(screen.getByText('Step 3 of 4')).not.toHaveFocus()
  })

  it('shows the next review from its first frame after closing and reopening', async () => {
    const user = userEvent.setup()
    const frames = renderDialogParent([{ publication: modelPublication, plan: accessPlan }, secondReview])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    expect(within(screen.getByRole('table', { name: 'All target model grants' })).getByText('Ada Lovelace')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Close' }))

    // The page stays aria-hidden for a moment after a modal closes.
    const openSecond = await screen.findByRole('button', { name: 'Open review 2' })
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
    frames.length = 0
    await user.click(openSecond)

    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 3 of 4']))
    expect(frames[0].text).toContain('Grace Hopper')
    expect(frames[0].text).toContain(secondReview.message)
    expect(frames.filter((frame) => frame.text.includes('Ada Lovelace'))).toEqual([])
    expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement)

    // Closing forgets the review, so reopening the very same one starts on it again.
    await user.click(screen.getByRole('button', { name: 'Close' }))
    const reopenSecond = await screen.findByRole('button', { name: 'Open review 2' })
    frames.length = 0
    await user.click(reopenSecond)

    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 3 of 4']))
    expect(frames[0].text).toContain('Grace Hopper')
    expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement)
  })

  it('keeps showing the review, as it was, while it closes', async () => {
    const user = userEvent.setup()
    api.applyPublishPlan.mockRejectedValue(new Error('API Management refused the change.'))
    const frames = renderDialogParent([{ publication: modelPublication, plan: accessPlan }])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    expect(await screen.findByText('API Management refused the change.')).toBeVisible()
    frames.length = 0
    await user.click(screen.getByRole('button', { name: 'Close' }))

    await screen.findByRole('button', { name: 'Open review 1' })
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
    // Every frame React committed while Fluent animated the dialog out.
    expect(frames.length).toBeGreaterThan(0)
    expect(new Set(frames.map((frame) => `${frame.title} · ${frame.step}`))).toEqual(
      new Set(['Review model access · Step 3 of 4']),
    )
    expect(frames.filter((frame) => frame.choosing)).toEqual([])
    expect(frames.filter((frame) => !frame.text.includes('API Management refused the change.'))).toEqual([])
  })

  it('keeps showing the publish step it was on while it closes, and starts over when opened again', async () => {
    const user = userEvent.setup()
    const frames = renderDialogParent()

    await user.click(screen.getByRole('button', { name: 'Start publishing' }))
    await advanceToReview(user)
    expect(await screen.findByText('Step 3 of 4')).toBeVisible()
    frames.length = 0
    await user.click(screen.getByRole('button', { name: 'Close' }))

    const startAgain = await screen.findByRole('button', { name: 'Start publishing' })
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
    expect(frames.length).toBeGreaterThan(0)
    expect(new Set(frames.map((frame) => `${frame.title} · ${frame.step}`))).toEqual(
      new Set(['Publish a model · Step 3 of 4']),
    )
    expect(frames.filter((frame) => frame.choosing)).toEqual([])

    frames.length = 0
    await user.click(startAgain)

    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 1 of 4']))
    expect(await screen.findByRole('checkbox', { name: 'Publish gpt-4o-prod' })).not.toBeChecked()
    expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement)
  })

  it('does not announce an apply that finishes after closing, or carry it into the next opening', async () => {
    const user = userEvent.setup()
    let finishApply: (value: PublishRun) => void = () => {}
    api.applyPublishPlan.mockReturnValue(new Promise<PublishRun>((resolve) => { finishApply = resolve }))
    const onPublished = vi.fn()
    const frames = renderDialogParent([{ publication: modelPublication, plan: accessPlan }], onPublished)

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    expect(await screen.findByRole('button', { name: 'Applying…' })).toHaveAttribute('aria-disabled', 'true')
    await user.click(screen.getByRole('button', { name: 'Close' }))
    const startPublishing = await screen.findByRole('button', { name: 'Start publishing' })

    // TanStack Query reports the result on a timer, so wait inside act for everything it sets off.
    await act(async () => {
      finishApply(run())
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(onPublished).not.toHaveBeenCalled()

    frames.length = 0
    await user.click(startPublishing)

    expect(new Set(frames.map((frame) => frame.step))).toEqual(new Set(['Step 1 of 4']))
    expect(screen.getByRole('combobox', { name: 'Gateway' })).toBeVisible()
    expect(onPublished).not.toHaveBeenCalled()
  })

  it('moves focus to the top of each step it moves to, forwards and back', async () => {
    const user = userEvent.setup()
    renderDialog()

    // Opening leaves focus where Fluent puts it, on the first control.
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Gateway' })).toHaveFocus())
    await user.click(await screen.findByRole('checkbox', { name: 'Publish gpt-4o-prod' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await waitFor(() => expect(focused()).toBe(screen.getByText('Step 2 of 4')))

    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(focused()).toBe(screen.getByText('Step 1 of 4')))

    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await waitFor(() => expect(focused()).toBe(screen.getByText('Step 3 of 4')))

    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(focused()).toBe(screen.getByText('Step 2 of 4')))
  })

  it('keeps focus on Review plan and Apply plan while they work, without letting them be pressed again', async () => {
    const user = userEvent.setup()
    let finishPlan: (value: PublishPlan) => void = () => {}
    api.createPublishPlan.mockReturnValue(new Promise<PublishPlan>((resolve) => { finishPlan = resolve }))
    let finishApply: (value: PublishRun) => void = () => {}
    api.applyPublishPlan.mockReturnValue(new Promise<PublishRun>((resolve) => { finishApply = resolve }))
    renderDialog()

    await advanceToReview(user)

    // A browser takes focus off a button that becomes disabled, so a busy button stays focusable instead.
    const creating = await screen.findByRole('button', { name: 'Creating plan…' })
    expect(creating).toHaveFocus()
    expect(creating).toHaveAttribute('aria-disabled', 'true')
    expect(creating).not.toBeDisabled()
    await user.click(creating)
    expect(api.createPublication).toHaveBeenCalledTimes(1)

    await act(async () => {
      finishPlan(plan)
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    const applying = await screen.findByRole('button', { name: 'Applying…' })
    expect(applying).toHaveFocus()
    expect(applying).toHaveAttribute('aria-disabled', 'true')
    expect(applying).not.toBeDisabled()
    await user.click(applying)
    expect(api.applyPublishPlan).toHaveBeenCalledTimes(1)

    await act(async () => {
      finishApply(run())
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    await waitFor(() => expect(focused()).toContainElement(screen.getByRole('table', { name: 'Publish run steps' })))
  })

  it('moves focus to the result once the apply finishes, where Escape closes the dialog', async () => {
    const user = userEvent.setup()
    renderDialogParent([{ publication: modelPublication, plan: accessPlan }])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText('Step 4 of 4')).toBeVisible()
    await waitFor(() => expect(focused()).toContainElement(screen.getByRole('table', { name: 'Publish run steps' })))
    expect(focused()).toHaveTextContent('Local development service reported completion; live APIM apply is not verified.')

    await user.keyboard('{Escape}')

    // The page stays aria-hidden for a moment after a modal closes.
    await screen.findByRole('button', { name: 'Open review 1' })
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
  })

  it('moves focus from the apply step to the result when a running apply finishes', async () => {
    const user = userEvent.setup()
    const running = run({ status: 'running', completedAt: null, durationMs: null })
    api.applyPublishPlan.mockResolvedValue(running)
    api.getPublishRun.mockResolvedValueOnce(running).mockResolvedValue(run())
    renderDialog({ publication, plan })

    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    await waitFor(() => expect(focused()).toBe(screen.getByText('Step 4 of 4')))
    // The dialog asks the service for the run every second until it finishes.
    await waitFor(
      () => expect(focused()).toContainElement(screen.getByRole('table', { name: 'Publish run steps' })),
      { timeout: 3000 },
    )
    expect(focused()).toHaveTextContent('Local development service reported completion; live APIM apply is not verified.')
    expect(api.getPublishRun).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['failed', 'Apply failed. Do not assume the target access or revocation is active.'],
    ['rolledBack', 'MOSAIC undid what it created during this publish run.'],
    ['interrupted', 'Apply interrupted — runtime state unknown'],
  ] as const)('moves focus to the result of an apply that ends %s', async (status, message) => {
    const user = userEvent.setup()
    const finished = run({ status, rolledBack: status === 'rolledBack', errors: ['The backend could not be created.'] })
    api.applyPublishPlan.mockResolvedValue(finished)
    api.getPublishRun.mockResolvedValue(finished)
    renderDialog({ publication, plan })

    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    await waitFor(() => expect(focused()).toHaveTextContent(message))
    expect(focused()).toHaveTextContent('The backend could not be created.')
    expect(focused()).toContainElement(screen.getByRole('table', { name: 'Publish run steps' }))
  })

  it('moves focus to the refusal, above the fresh plan MOSAIC made instead, where Escape closes the dialog', async () => {
    const user = userEvent.setup()
    const refusal =
      'This publication changed after the plan was produced. Re-plan it and review the new changes before applying.'
    api.applyPublishPlan.mockRejectedValueOnce(Object.assign(new Error(refusal), { status: 409 }))
    api.createPublishPlan.mockResolvedValueOnce({ ...plan, id: 'plan_2' })
    renderDialogParent([{ publication, plan }])

    await user.click(screen.getByRole('button', { name: 'Open review 1' }))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))

    await waitFor(() =>
      expect(focused()).toHaveTextContent('MOSAIC has already re-planned. Review the fresh plan below before you apply it.'),
    )
    expect(focused()).toHaveTextContent("MOSAIC didn't apply the plan you reviewed")
    expect(focused()).toHaveTextContent(refusal)
    expect(focused()).not.toContainElement(screen.getByRole('table', { name: 'Publish plan steps' }))
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeEnabled()

    await user.keyboard('{Escape}')

    await screen.findByRole('button', { name: 'Open review 1' })
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
  })

  it('moves focus to the refusal when MOSAIC cannot re-plan after refusing the reviewed plan', async () => {
    const user = userEvent.setup()
    api.applyPublishPlan.mockRejectedValue(Object.assign(new Error('Plan is stale.'), { status: 409 }))
    api.createPublishPlan.mockRejectedValue(new Error('Cannot refresh the model plan.'))
    renderDialog({ publication: modelPublication, plan: accessPlan })

    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText('Cannot refresh the model plan.')).toBeVisible()
    await waitFor(() => expect(focused()).toHaveTextContent("MOSAIC didn't apply the plan you reviewed"))
    expect(focused()).toHaveTextContent('Plan is stale.')
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
  })

  it('moves focus to the reason an apply failed', async () => {
    const user = userEvent.setup()
    api.applyPublishPlan.mockRejectedValue(new Error('API Management refused the change.'))
    renderDialog({ publication, plan })

    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    await waitFor(() => expect(focused()).toHaveTextContent('API Management refused the change.'))
    expect(focused()).not.toContainElement(screen.getByRole('table', { name: 'Publish plan steps' }))
    expect(screen.getByText('Step 3 of 4')).toBeVisible()
  })

  it('keeps focus on Apply plan while it tries again after failing, then moves it to the result', async () => {
    const user = userEvent.setup()
    let finishApply: (value: PublishRun) => void = () => {}
    api.applyPublishPlan
      .mockRejectedValueOnce(new Error('API Management refused the change.'))
      .mockReturnValueOnce(new Promise<PublishRun>((resolve) => { finishApply = resolve }))
    renderDialog({ publication, plan })

    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(focused()).toHaveTextContent('API Management refused the change.'))
    await user.click(screen.getByRole('button', { name: 'Apply plan' }))

    const applying = await screen.findByRole('button', { name: 'Applying…' })
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(applying).toHaveFocus()
    expect(screen.queryByText('API Management refused the change.')).not.toBeInTheDocument()

    await act(async () => {
      finishApply(run())
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    await waitFor(() => expect(focused()).toContainElement(screen.getByRole('table', { name: 'Publish run steps' })))
  })

  it('moves focus to the reason MOSAIC could not create the plan', async () => {
    const user = userEvent.setup()
    api.createPublication.mockRejectedValue(new Error('The API path models/gpt-4o is already in use.'))
    renderDialog()

    await advanceToReview(user)

    await waitFor(() => expect(focused()).toHaveTextContent('The API path models/gpt-4o is already in use.'))
    expect(focused()).not.toContainElement(screen.getByRole('textbox', { name: 'Display name' }))
    expect(screen.getByText('Step 2 of 4')).toBeVisible()
  })

  it('leaves focus in a field the administrator is typing in when the plan cannot be created', async () => {
    const user = userEvent.setup()
    let refuse: (error: Error) => void = () => {}
    api.createPublication.mockReturnValue(new Promise<Publication>((_, reject) => { refuse = reject }))
    renderDialog()

    await advanceToReview(user)
    const displayName = screen.getByRole('textbox', { name: 'Display name' })
    await user.click(displayName)
    await user.keyboard(' EU')
    await act(async () => {
      refuse(new Error('The API path models/gpt-4o is already in use.'))
      await new Promise((resolve) => setTimeout(resolve, 50))
    })

    expect(screen.getByText('The API path models/gpt-4o is already in use.')).toBeVisible()
    expect(displayName).toHaveFocus()
    expect(displayName).toHaveValue('Contoso models gpt-4o-prod EU')
  })
})
