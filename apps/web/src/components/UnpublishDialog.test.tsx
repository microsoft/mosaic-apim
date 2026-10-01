import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { UnpublishDialog, type UnpublishTarget } from './UnpublishDialog'
import { accessSnapshot } from '../test/model-access'
import type { McpAccessSnapshot, PublishPlan, PublishRun } from '../types'

const timestamp = '2026-09-30T11:20:00Z'
const publication = { id: 'pub_1', displayName: 'GPT-4o' }

const governedPlan: PublishPlan = {
  id: 'unpublish-plan-1',
  tenantId: 'tenant',
  entityType: 'publishPlan',
  publicationId: 'pub_1',
  gatewayId: 'gateway_1',
  digest: 'unpublish-digest',
  target: 'model',
  operation: 'unpublish',
  steps: [
    {
      kind: 'api',
      name: 'chat',
      action: 'delete',
      reason: 'Delete the API that fronts this model. The gateway stops serving it.',
      resourceId: '/apis/chat',
      existed: true,
    },
    {
      kind: 'subscription',
      name: 'dedicated-user-sub',
      action: 'delete',
      reason: "Delete Ada Lovelace's subscription. Its keys stop working.",
      resourceId: '/subscriptions/dedicated-user-sub',
      existed: true,
      entitlementId: 'direct_grant',
    },
    {
      kind: 'backend',
      name: 'backend',
      action: 'delete',
      reason: 'Delete the backend that points at the model endpoint.',
      resourceId: '/backends/backend',
      existed: true,
    },
  ],
  facets: [],
  policyContentSha256: null,
  warnings: [
    "Before it deletes anything, MOSAIC replaces this API's policy with one that refuses every call, and suspends every grant's subscription, so callers are cut off first.",
  ],
  accessSnapshot: {
    ...accessSnapshot,
    grants: [
      { ...accessSnapshot.grants[0], costCenterCode: 'CI-204' },
      {
        entitlementId: 'group_grant',
        subject: { kind: 'securityGroup', id: 'principal_group' },
        objectId: 'group-object-1',
        displayName: 'AI Model Users',
        subscriptionName: null,
        costCenterCode: 'general',
        enabled: true,
        enforcement: null,
        intentDigest: 'group-digest',
      },
      // Keys are created on request: this grant never asked for one, so it has only its token.
      {
        ...accessSnapshot.grants[0],
        entitlementId: 'keyless_grant',
        displayName: 'Ada Lovelace',
        subscriptionName: 'keyless-sub',
        costCenterCode: 'general',
      },
      {
        ...accessSnapshot.grants[0],
        entitlementId: 'revoked_grant',
        displayName: 'Former contractor',
        subscriptionName: 'revoked-sub',
        enabled: false,
      },
    ],
  },
  createdAt: timestamp,
  updatedAt: timestamp,
}

const legacyPlan: PublishPlan = {
  ...governedPlan,
  id: 'unpublish-plan-legacy',
  accessSnapshot: null,
  warnings: [
    'Deleting product chat also deletes every subscription to it, including any that API Management or another administrator added.',
  ],
}

const mcpSnapshot: McpAccessSnapshot = {
  version: 3,
  audience: 'runtime-audience',
  delegatedScope: 'Mcp.Invoke',
  applicationRole: 'Mcp.Invoke.Application',
  grants: [
    {
      entitlementId: 'mcp_grant',
      subject: { kind: 'application', id: 'principal_agent' },
      objectId: 'agent-object-1',
      displayName: 'Research agent',
      enabled: true,
      enforcement: null,
      intentDigest: 'mcp-digest',
    },
  ],
}

const mcpPlan: PublishPlan = {
  ...governedPlan,
  id: 'mcp-unpublish-plan',
  target: 'mcp',
  accessSnapshot: null,
  mcpAccessSnapshot: mcpSnapshot,
  steps: [
    {
      kind: 'api',
      name: 'mosaic-mcp-docs',
      action: 'delete',
      reason: 'Delete the MCP API. The gateway stops serving the server.',
      resourceId: '/apis/mosaic-mcp-docs',
      existed: true,
    },
  ],
  warnings: [],
}

function run(overrides: Partial<PublishRun> = {}): PublishRun {
  return {
    id: 'run_unpublish',
    tenantId: 'tenant',
    entityType: 'publishRun',
    publicationId: 'pub_1',
    gatewayId: 'gateway_1',
    planId: 'unpublish-plan-1',
    planDigest: 'unpublish-digest',
    status: 'succeeded',
    startedAt: timestamp,
    completedAt: timestamp,
    durationMs: 1000,
    steps: governedPlan.steps.map((step) => ({
      kind: step.kind,
      name: step.name,
      action: 'delete',
      status: 'succeeded',
      resourceId: step.resourceId,
      createdByMosaic: false,
      error: null,
    })),
    rolledBack: false,
    orphanedResources: [],
    errors: [],
    createdAt: timestamp,
    updatedAt: timestamp,
    ...overrides,
  }
}

function refusal(message: string, status: number) {
  return Object.assign(new Error(message), { status })
}

const api = {
  planUnpublishPublication: vi.fn(),
  unpublishPublication: vi.fn(),
  getPublishRun: vi.fn(),
  planUnpublishMcpPublication: vi.fn(),
  unpublishMcpPublication: vi.fn(),
  getMcpPublishRun: vi.fn(),
  listPrincipals: vi.fn(),
  getPublicationLock: vi.fn(),
  getMcpPublicationLock: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

// Opens the dialog the way the Models and MCP servers pages do, and keeps it mounted while closed.
function Parent({
  target,
  onUnpublished,
  onClosed,
}: {
  target: UnpublishTarget
  onUnpublished: (message: string) => void
  onClosed: () => void
}) {
  const [opened, setOpened] = useState<typeof publication | null>(null)
  return (
    <>
      <button type="button" onClick={() => setOpened(publication)}>
        Open unpublish
      </button>
      <UnpublishDialog
        open={opened !== null}
        target={target}
        publication={opened}
        onClose={() => {
          setOpened(null)
          onClosed()
        }}
        onUnpublished={onUnpublished}
      />
    </>
  )
}

async function open(target: UnpublishTarget = 'model') {
  const user = userEvent.setup()
  const onUnpublished = vi.fn()
  const onClosed = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Parent target={target} onUnpublished={onUnpublished} onClosed={onClosed} />
    </QueryClientProvider>,
  )
  await user.click(screen.getByRole('button', { name: 'Open unpublish' }))
  const dialog = await screen.findByRole('alertdialog', { name: 'Unpublish GPT-4o?' })
  return { user, dialog, onUnpublished, onClosed }
}

// The element that has focus, which must be inside the open dialog, and not the dialog itself.
function focused() {
  const element = document.activeElement as HTMLElement
  const dialog = screen.getByRole('alertdialog')
  expect(element).not.toBe(document.body)
  expect(element).not.toBe(dialog)
  expect(dialog).toContainElement(element)
  return element
}

describe('UnpublishDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.planUnpublishPublication.mockResolvedValue(governedPlan)
    api.unpublishPublication.mockResolvedValue(run({ status: 'running', completedAt: null }))
    api.getPublishRun.mockResolvedValue(run())
    api.planUnpublishMcpPublication.mockResolvedValue(mcpPlan)
    api.unpublishMcpPublication.mockResolvedValue(run({ planId: 'mcp-unpublish-plan', status: 'running' }))
    api.getMcpPublishRun.mockResolvedValue(run({ planId: 'mcp-unpublish-plan' }))
    api.listPrincipals.mockResolvedValue([])
  })

  it('plans the unpublish as it opens, and says who loses access and what MOSAIC deletes', async () => {
    const { dialog } = await open()

    const losing = await within(dialog).findByRole('table', { name: 'Grants that lose access' })
    expect(api.planUnpublishPublication).toHaveBeenCalledTimes(1)
    expect(api.planUnpublishPublication).toHaveBeenCalledWith('pub_1')
    expect(api.unpublishPublication).not.toHaveBeenCalled()
    expect(dialog).toHaveTextContent(
      'The gateway stops serving it, and the portal stops listing it until you publish it again.',
    )
    expect(dialog).toHaveTextContent(
      '3 grants lose access. Subscription keys for these grants stop working, and the gateway refuses their Entra tokens.',
    )
    expect(
      within(losing)
        .getAllByRole('row')
        .slice(1)
        .map((row) => row.textContent),
    ).toEqual([
      'Ada LovelacePersonCI-204Key and Entra token',
      'AI Model UsersSecurity groupgeneralEntra token, for every member',
      'Ada LovelacePersongeneralEntra token',
    ])
    expect(losing).not.toHaveTextContent('Former contractor')
    expect(dialog).toHaveTextContent('Grants stay in MOSAIC. Publish the model again, and apply its plan, to restore access.')

    const steps = within(dialog).getByRole('table', { name: 'Unpublish plan steps' })
    expect(dialog).toHaveTextContent('3 resources MOSAIC created, deleted in this order.')
    expect(within(steps).getAllByRole('row')).toHaveLength(4)
    expect(within(steps).getByText('Grant: Ada Lovelace')).toBeVisible()
    expect(within(steps).getByText("Delete Ada Lovelace's subscription. Its keys stop working.")).toBeVisible()
    expect(within(dialog).getByText(/replaces this API's policy with one that refuses every call/)).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Unpublish model' })).toBeEnabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it("says the named value goes and the key stays in Key Vault", async () => {
    api.planUnpublishPublication.mockResolvedValue({
      ...governedPlan,
      steps: [
        ...governedPlan.steps,
        {
          kind: 'namedValue',
          name: 'mosaic-fabrikam-claude-key',
          action: 'delete',
          reason:
            "Delete the named value API Management reads the model endpoint's API key through. " +
            'The key itself stays in Key Vault.',
          resourceId: '/namedValues/mosaic-fabrikam-claude-key',
          existed: true,
        },
      ],
    })
    const { dialog } = await open()

    const steps = await within(dialog).findByRole('table', { name: 'Unpublish plan steps' })
    const rows = within(steps).getAllByRole('row')
    const last = rows[rows.length - 1]
    expect(last).toHaveTextContent('Named value')
    expect(last).toHaveTextContent('mosaic-fabrikam-claude-key')
    expect(last).toHaveTextContent('The key itself stays in Key Vault.')
  })

  it('moves focus to the review once the plan arrives, so it is read', async () => {
    await open()

    const review = await screen.findByRole('region', { name: 'Unpublish review' })
    await waitFor(() => expect(focused()).toBe(review))
  })

  it("says MOSAIC can't list who calls a model without governed access", async () => {
    api.planUnpublishPublication.mockResolvedValue(legacyPlan)

    const { dialog } = await open()

    expect(
      await within(dialog).findByText(
        "This model doesn't use governed access, so MOSAIC can't list who calls it. Everyone who calls it with a key for its product loses access.",
      ),
    ).toBeVisible()
    expect(within(dialog).queryByRole('table', { name: 'Grants that lose access' })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/Deleting product chat also deletes every subscription to it/)).toBeVisible()
  })

  it('says so when no grant has access at the gateway', async () => {
    api.planUnpublishPublication.mockResolvedValue({
      ...governedPlan,
      accessSnapshot: {
        ...accessSnapshot,
        grants: accessSnapshot.grants.map((grant) => ({ ...grant, enabled: false })),
      },
    })

    const { dialog } = await open()

    expect(await within(dialog).findByText('No grant has access to this model at the gateway right now.')).toBeVisible()
  })

  it('reviews and unpublishes an MCP server whose grants use Entra tokens only', async () => {
    const { user, dialog } = await open('mcp')

    expect(await within(dialog).findByText('1 grant loses access. The gateway refuses its Entra tokens.')).toBeVisible()
    const losing = within(dialog).getByRole('table', { name: 'Grants that lose access' })
    expect(within(losing).getByText('Research agent')).toBeVisible()
    expect(within(losing).getByText('Entra token')).toBeVisible()
    expect(dialog).toHaveTextContent('Publish the MCP server again, and apply its plan, to restore access.')

    await user.click(within(dialog).getByRole('button', { name: 'Unpublish MCP server' }))

    await waitFor(() => expect(api.unpublishMcpPublication).toHaveBeenCalledWith('pub_1', 'mcp-unpublish-plan'))
    expect(api.planUnpublishPublication).not.toHaveBeenCalled()
    expect(api.unpublishPublication).not.toHaveBeenCalled()
    expect(await within(dialog).findByRole('table', { name: 'Unpublish run steps' })).toBeVisible()
    expect(api.getMcpPublishRun).toHaveBeenCalledWith('pub_1', 'run_unpublish')
  })

  it('unpublishes the plan it reviewed, then moves focus to the result, where Escape closes it', async () => {
    const { user, dialog, onUnpublished, onClosed } = await open()

    await user.click(await within(dialog).findByRole('button', { name: 'Unpublish model' }))

    await waitFor(() => expect(api.unpublishPublication).toHaveBeenCalledWith('pub_1', 'unpublish-plan-1'))
    expect(api.unpublishPublication).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(focused()).toContainElement(screen.getByRole('table', { name: 'Unpublish run steps' })))
    expect(focused()).toHaveTextContent('Local development service reported completion; live APIM changes are not verified.')
    expect(onUnpublished).toHaveBeenCalledWith(
      'Local development service reported completion. Live APIM changes are not verified.',
    )
    expect(within(dialog).queryByRole('button', { name: 'Unpublish model' })).not.toBeInTheDocument()

    await user.keyboard('{Escape}')

    expect(onClosed).toHaveBeenCalledTimes(1)
    await screen.findByRole('button', { name: 'Open unpublish' })
    expect(screen.queryByRole('alertdialog', { hidden: true })).not.toBeInTheDocument()
  })

  it('keeps focus on Unpublish while it works, without letting it be pressed again', async () => {
    let finish: (value: PublishRun) => void = () => {}
    api.unpublishPublication.mockReturnValue(new Promise<PublishRun>((resolve) => { finish = resolve }))
    const { user, dialog } = await open()

    await user.click(await within(dialog).findByRole('button', { name: 'Unpublish model' }))

    // A browser takes focus off a button that becomes disabled, so a busy button stays focusable instead.
    const busy = await within(dialog).findByRole('button', { name: 'Unpublishing…' })
    expect(busy).toHaveFocus()
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.click(busy)
    expect(api.unpublishPublication).toHaveBeenCalledTimes(1)

    await act(async () => {
      finish(run({ status: 'running', completedAt: null }))
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    await waitFor(() => expect(focused()).toContainElement(screen.getByRole('table', { name: 'Unpublish run steps' })))
  })

  it('says why MOSAIC refused the reviewed plan, above a fresh one, and unpublishes only the fresh one', async () => {
    const message =
      'This publication changed after its unpublish plan was made, so the plan no longer says what unpublishing removes or who loses access. Review a new plan before you unpublish.'
    const fresh: PublishPlan = {
      ...governedPlan,
      id: 'unpublish-plan-2',
      accessSnapshot: {
        ...governedPlan.accessSnapshot!,
        grants: [
          ...governedPlan.accessSnapshot!.grants,
          { ...accessSnapshot.grants[0], entitlementId: 'new_grant', displayName: 'Grace Hopper', subscriptionName: 'grace-sub' },
        ],
      },
    }
    api.planUnpublishPublication.mockResolvedValueOnce(governedPlan).mockResolvedValueOnce(fresh)
    api.unpublishPublication.mockRejectedValueOnce(refusal(message, 409))
    const { user, dialog } = await open()

    await user.click(await within(dialog).findByRole('button', { name: 'Unpublish model' }))

    expect(
      await within(dialog).findByText('MOSAIC has planned again. Review the fresh plan below before you unpublish.'),
    ).toBeVisible()
    await waitFor(() => {
      const element = focused()
      expect(element).toHaveTextContent("MOSAIC didn't unpublish with the plan you reviewed")
      expect(element).toHaveTextContent(message)
      expect(element).not.toContainElement(screen.getByRole('table', { name: 'Unpublish plan steps' }))
    })
    expect(within(dialog).getByText('Grace Hopper')).toBeVisible()
    expect(dialog).toHaveTextContent('4 grants lose access.')
    expect(api.planUnpublishPublication).toHaveBeenCalledTimes(2)

    await user.click(within(dialog).getByRole('button', { name: 'Unpublish model' }))

    await waitFor(() => expect(api.unpublishPublication).toHaveBeenLastCalledWith('pub_1', 'unpublish-plan-2'))
  })

  it('shows why MOSAIC refused another way, without planning again', async () => {
    api.unpublishPublication.mockRejectedValueOnce(refusal('API Management is unavailable.', 502))
    const { user, dialog } = await open()

    await user.click(await within(dialog).findByRole('button', { name: 'Unpublish model' }))

    await waitFor(() => {
      const element = focused()
      expect(element).toHaveTextContent("MOSAIC didn't unpublish GPT-4o")
      expect(element).toHaveTextContent('API Management is unavailable.')
    })
    expect(api.planUnpublishPublication).toHaveBeenCalledTimes(1)
    expect(within(dialog).getByRole('button', { name: 'Unpublish model' })).toBeEnabled()
  })

  it("says why MOSAIC can't unpublish, and offers nothing to confirm", async () => {
    api.planUnpublishPublication.mockRejectedValue(
      refusal(
        'MOSAIC did not create anything in API Management for this publication, so there is nothing to remove.',
        409,
      ),
    )

    const { dialog } = await open()

    await waitFor(() => {
      const element = focused()
      expect(element).toHaveTextContent("MOSAIC can't unpublish GPT-4o")
      expect(element).toHaveTextContent('so there is nothing to remove.')
    })
    expect(within(dialog).queryByRole('button', { name: 'Unpublish model' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeEnabled()
    expect(api.unpublishPublication).not.toHaveBeenCalled()
  })

  it('reports a failed unpublish as failed, and says what is left', async () => {
    api.getPublishRun.mockResolvedValue(
      run({
        status: 'failed',
        errors: ['backend backend: The backend is in use.'],
        orphanedResources: [
          { kind: 'backend', name: 'backend', resourceId: '/backends/backend', createdByMosaic: true, appliedAt: timestamp },
        ],
      }),
    )
    const { user, dialog, onUnpublished } = await open()

    await user.click(await within(dialog).findByRole('button', { name: 'Unpublish model' }))

    await waitFor(() => expect(focused()).toHaveTextContent('Unpublish failed'))
    expect(focused()).toHaveTextContent('Resources left behind in API Management')
    expect(focused()).toHaveTextContent('backend backend: The backend is in use.')
    expect(onUnpublished).not.toHaveBeenCalled()
  })

  it('does not announce an unpublish that finishes after the dialog closed', async () => {
    let finish: (value: PublishRun) => void = () => {}
    api.getPublishRun.mockReturnValue(new Promise<PublishRun>((resolve) => { finish = resolve }))
    const { user, dialog, onUnpublished } = await open()
    await user.click(await within(dialog).findByRole('button', { name: 'Unpublish model' }))
    await within(dialog).findByText('Unpublishing GPT-4o')

    await user.click(within(dialog).getByRole('button', { name: 'Close' }))
    await act(async () => {
      finish(run())
      await new Promise((resolve) => setTimeout(resolve, 50))
    })

    expect(onUnpublished).not.toHaveBeenCalled()
  })
})
