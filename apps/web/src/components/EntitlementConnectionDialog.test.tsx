import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useNavigate, type NavigateFunction } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { connectionInfo, directGrant } from '../test/model-access'
import type { Entitlement, KeyRevealResult } from '../types'
import { EntitlementConnectionDialog } from './EntitlementConnectionDialog'

const auth = vi.hoisted(() => ({
  accounts: [{ homeAccountId: 'actor-a', localAccountId: 'actor-a', tenantId: 'tenant' }],
}))
vi.mock('@azure/msal-react', () => ({ useMsal: () => auth }))
const api = { getEntitlementConnection: vi.fn(), getMcpConnection: vi.fn(), revealEntitlementKey: vi.fn() }
vi.mock('../api', () => ({ useMosaicApi: () => api }))

const revealed: KeyRevealResult = {
  entitlementId: directGrant.id,
  subscriptionName: 'dedicated-user-sub',
  slot: 'primary',
  key: 'test-only-primary-secret',
}

function renderDialog(entitlement = directGrant) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClose = vi.fn()
  let navigate: NavigateFunction
  function Navigation() {
    navigate = useNavigate()
    return null
  }
  function tree(open = true, grant = entitlement) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <Navigation />
          <EntitlementConnectionDialog open={open} entitlement={grant} onClose={onClose} />
        </MemoryRouter>
      </QueryClientProvider>
    )
  }
  const rendered = render(tree())
  return {
    ...rendered,
    queryClient,
    onClose,
    rerenderDialog: (open = true, grant = entitlement) => rendered.rerender(tree(open, grant)),
    navigate: () => act(() => { void navigate('/another-page') }),
  }
}

function expectNoCachedSecret(queryClient: QueryClient) {
  expect(JSON.stringify(queryClient.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(revealed.key)
  expect(queryClient.getMutationCache().getAll()).toHaveLength(0)
}

// Elements marked as holding a secret, as the portal marks its revealed key. Only the key itself is one.
function secrets() {
  return document.querySelectorAll('[data-secret]')
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

describe('EntitlementConnectionDialog', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    auth.accounts = [{ homeAccountId: 'actor-a', localAccountId: 'actor-a', tenantId: 'tenant' }]
    api.getEntitlementConnection.mockResolvedValue(connectionInfo)
    api.getMcpConnection.mockResolvedValue({
      entitlementId: 'mcp-grant',
      mcpServerId: 'mcp_1',
      publicationId: 'mcp_pub_1',
      gatewayId: 'gateway_1',
      displayName: 'Weather MCP',
      tenantId: 'tenant',
      serverUrl: 'https://gateway.example.test/mosaic/mcp/weather/mcp',
      transport: 'streamable',
      enforced: true,
      statusMessage: 'The gateway enforces this grant.',
      runtime: { publicationId: 'mcp_pub_1', status: 'applied', appliedMethods: { keysEnabled: false, entraEnabled: true } },
      entraAudience: 'runtime-client-id',
      delegatedScope: 'api://runtime-client-id/Mcp.Invoke',
      applicationScope: 'api://runtime-client-id/.default',
      requiredAppRole: 'Mcp.Invoke.Application',
      clientId: 'runtime-client-id',
      principalKind: 'user',
      resourceMetadataUrl: 'https://gateway.example.test/.well-known/oauth-protected-resource/mosaic/mcp/weather/mcp',
      limits: { requests: { counterKeyExpression: '@(context.Subscription.Id)', calls: 60, renewalPeriodSeconds: 60 } },
    })
    api.revealEntitlementKey.mockResolvedValue(revealed)
  })

  it('does not request or copy a key until explicitly asked, and never caches key material', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    const { queryClient } = renderDialog()
    expect(await screen.findByText(connectionInfo.endpoint)).toBeVisible()
    expect(api.revealEntitlementKey).not.toHaveBeenCalled()
    expect(copy).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    expect(api.revealEntitlementKey).toHaveBeenCalledWith(directGrant.id, 'primary', expect.any(AbortSignal))
    expect(await screen.findByText(revealed.key)).toBeVisible()
    expect(copy).not.toHaveBeenCalled()
    expectNoCachedSecret(queryClient)
    await user.click(screen.getByRole('button', { name: 'Copy revealed key' }))
    expect(copy).toHaveBeenCalledWith(revealed.key)
    expect(await screen.findByText(/Key copied/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expectNoCachedSecret(queryClient)
  })

  it.each([
    ['user', 'api://model-runtime/Models.Invoke'],
    ['application', 'api://model-runtime/.default'],
  ] as const)('displays the server-provided scope for a %s grant without substituting another scope', async (kind, scope) => {
    api.getEntitlementConnection.mockResolvedValue({ ...connectionInfo, entraScope: scope })
    renderDialog({ ...directGrant, subject: { ...directGrant.subject, kind } })
    expect(await screen.findByText(scope)).toBeVisible()
    expect(api.revealEntitlementKey).not.toHaveBeenCalled()
  })

  it('reveals the selected secondary key and displays clipboard failure', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('Denied'))
    api.revealEntitlementKey.mockResolvedValue({ ...revealed, slot: 'secondary' })
    renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal secondary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal secondary key' }))
    expect(api.revealEntitlementKey).toHaveBeenCalledWith(directGrant.id, 'secondary', expect.any(AbortSignal))
    await user.click(await screen.findByRole('button', { name: 'Copy revealed key' }))
    expect(await screen.findByText(/Could not copy the key/)).toBeVisible()
    expect(screen.queryByText(/Key copied/)).not.toBeInTheDocument()
  })

  it('clears a revealed key on close and does not retain it when reopened', async () => {
    const user = userEvent.setup()
    const { rerenderDialog, queryClient } = renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    expect(await screen.findByText(revealed.key)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    rerenderDialog(false)
    rerenderDialog(true)
    expect(await screen.findByText(connectionInfo.endpoint)).toBeVisible()
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expect(api.revealEntitlementKey).toHaveBeenCalledTimes(1)
    expectNoCachedSecret(queryClient)
  })

  it('aborts and ignores a late reveal after close, even if the transport ignores abort', async () => {
    const user = userEvent.setup()
    let complete!: (result: KeyRevealResult) => void
    api.revealEntitlementKey.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const { rerenderDialog, queryClient } = renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    const signal = api.revealEntitlementKey.mock.calls[0][2] as AbortSignal
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(signal.aborted).toBe(true)
    rerenderDialog(false)
    rerenderDialog(true)
    await act(async () => { complete(revealed) })
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expectNoCachedSecret(queryClient)
  })

  it.each(['identity', 'navigation', 'grant'] as const)('clears a revealed key when %s changes', async (change) => {
    const user = userEvent.setup()
    const rendered = renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    expect(await screen.findByText(revealed.key)).toBeVisible()
    if (change === 'identity') {
      auth.accounts = [{ homeAccountId: 'actor-b', localAccountId: 'actor-b', tenantId: 'tenant' }]
      rendered.rerenderDialog()
    } else if (change === 'navigation') {
      rendered.navigate()
    } else {
      rendered.rerenderDialog(true, { ...directGrant, id: 'different-grant' })
    }
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expect(api.revealEntitlementKey).toHaveBeenCalledTimes(1)
    expectNoCachedSecret(rendered.queryClient)
  })

  it('ignores a late response after identity changes', async () => {
    const user = userEvent.setup()
    let complete!: (result: KeyRevealResult) => void
    api.revealEntitlementKey.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const rendered = renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    auth.accounts = [{ homeAccountId: 'actor-b', localAccountId: 'actor-b', tenantId: 'tenant' }]
    rendered.rerenderDialog()
    await act(async () => { complete(revealed) })
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expectNoCachedSecret(rendered.queryClient)
  })

  it('clears the secret before full-page navigation can retain the document', async () => {
    const user = userEvent.setup()
    const { queryClient, onClose } = renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    expect(await screen.findByText(revealed.key)).toBeVisible()
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expect(onClose).toHaveBeenCalledOnce()
    expectNoCachedSecret(queryClient)
  })

  it.each([
    { ...directGrant, runtime: { ...directGrant.runtime!, status: 'pending' as const } },
    { ...directGrant, enabled: false, runtime: { ...directGrant.runtime!, status: 'revocationPending' as const } },
    { ...directGrant, runtime: { ...directGrant.runtime!, status: 'unknown' as const } },
    { ...directGrant, binding: { gatewayId: 'gateway_1', source: 'manual' as const } },
    { ...directGrant, runtime: { ...directGrant.runtime!, appliedMethods: { keysEnabled: false, entraEnabled: true } } },
  ] satisfies Entitlement[])('does not reveal for an ineligible grant (%#)', async (grant) => {
    renderDialog(grant)
    expect(await screen.findByText(connectionInfo.endpoint)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reveal secondary key' })).toBeDisabled()
    expect(api.revealEntitlementKey).not.toHaveBeenCalled()
  })

  it('clears the earlier value and shows a live reveal failure, with no cached fallback', async () => {
    const user = userEvent.setup()
    renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    expect(await screen.findByText(revealed.key)).toBeVisible()
    api.revealEntitlementKey.mockRejectedValue(new Error('APIM secret-read permission is missing.'))
    await user.click(screen.getByRole('button', { name: 'Reveal secondary key' }))
    expect(await screen.findByText('APIM secret-read permission is missing.')).toBeVisible()
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expect(screen.queryByRole('button', { name: 'Copy revealed key' })).not.toBeInTheDocument()
  })

  it('shows the Messages operation and Anthropic client guidance for an Anthropic model', async () => {
    api.getEntitlementConnection.mockResolvedValue({
      ...connectionInfo,
      deploymentName: 'claude-sonnet-4-5',
      apiShape: 'anthropicMessages',
      operations: [{ name: 'messages', method: 'POST', path: '/anthropic/v1/messages' }],
      publicationLimits: null,
    })
    renderDialog()
    expect(await screen.findByText('POST /anthropic/v1/messages · messages')).toBeVisible()
    expect(screen.getByText(/uses the Anthropic Messages API/)).toBeVisible()
    expect(screen.getByText(new RegExp(`${connectionInfo.endpoint}/anthropic`))).toBeVisible()
    expect(screen.getByText('Publication: Token limits are unavailable for this model on this gateway\'s tier.')).toBeVisible()
  })

  it('does not show Anthropic guidance for other models', async () => {
    renderDialog()
    expect(await screen.findByText('POST /chat/completions · chat')).toBeVisible()
    expect(screen.queryByText(/uses the Anthropic Messages API/)).not.toBeInTheDocument()
  })

  it('explains agent identity connection requirements', async () => {
    api.getEntitlementConnection.mockResolvedValue({
      ...connectionInfo,
      principalKind: 'agentIdentity',
      entraClientId: 'agent-client-id',
      entraScope: 'api://model-runtime/.default',
      requiredAppRole: 'Models.Invoke.Application',
    })
    renderDialog()

    expect(await screen.findByText(/Agent identity signs in as itself/)).toBeVisible()
    expect(screen.getByText('agent-client-id')).toBeVisible()
    expect(screen.getByText('api://model-runtime/.default')).toBeVisible()
    expect(screen.getByText('Models.Invoke.Application')).toBeVisible()
  })

  it('explains agent user delegated tokens', async () => {
    api.getEntitlementConnection.mockResolvedValue({
      ...connectionInfo,
      principalKind: 'agentUser',
      entraClientId: 'parent-agent-client',
      entraScope: 'api://model-runtime/Models.Invoke',
    })
    renderDialog()

    expect(await screen.findByText(/parent agent identity parent-agent-client requests a delegated token/)).toBeVisible()
  })

  it('hides key reveal for security-group grants', async () => {
    api.getEntitlementConnection.mockResolvedValue({
      ...connectionInfo,
      principalKind: 'securityGroup',
      entraScope: 'api://model-runtime/Models.Invoke',
      entraApplicationScope: 'api://model-runtime/.default',
      requiredAppRole: 'Models.Invoke.Application',
      keysAvailable: false,
      viaGroupId: 'sg_1',
      viaGroupName: 'Security readers',
    })
    renderDialog({ ...directGrant, subject: { kind: 'securityGroup', id: 'sg_1' } })

    expect(await screen.findByText(/Members sign in as themselves/)).toBeVisible()
    expect(screen.getByText(/Keys are not available/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Reveal primary key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reveal secondary key' })).not.toBeInTheDocument()
    expect(api.revealEntitlementKey).not.toHaveBeenCalled()
  })

  it('shows MCP connection metadata and VS Code configuration without key reveal', async () => {
    renderDialog({
      ...directGrant,
      id: 'mcp-grant',
      resource: { kind: 'mcpServer', id: 'mcp_1' },
      binding: null,
      runtime: null,
    })

    expect(await screen.findByText('MCP connection')).toBeVisible()
    expect(await screen.findByText('Weather MCP')).toBeVisible()
    expect(screen.getByText('https://gateway.example.test/mosaic/mcp/weather/mcp')).toBeVisible()
    expect(screen.getByText('https://gateway.example.test/.well-known/oauth-protected-resource/mosaic/mcp/weather/mcp')).toBeVisible()
    expect(screen.getByText('api://runtime-client-id/Mcp.Invoke')).toBeVisible()
    expect(screen.getByText('Mcp.Invoke.Application')).toBeVisible()
    expect(screen.getByText(/"type": "http"/)).toBeVisible()
    expect(screen.getByText('Limits traffic to 60 calls per minute.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Reveal primary key' })).not.toBeInTheDocument()
    expect(api.getMcpConnection).toHaveBeenCalledWith('mcp-grant')
    expect(api.revealEntitlementKey).not.toHaveBeenCalled()
  })

  it('marks the revealed key, and nothing else, as a secret', async () => {
    const user = userEvent.setup()
    api.revealEntitlementKey
      .mockResolvedValueOnce(revealed)
      .mockResolvedValueOnce({ ...revealed, slot: 'secondary', key: 'test-only-secondary-secret' })
    renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    expect(secrets()).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    const primaryKey = await screen.findByText(revealed.key)
    expect(primaryKey).toHaveAttribute('data-secret', 'true')
    expect(secrets()).toHaveLength(1)
    expect(secrets()[0]).toBe(primaryKey)

    await user.click(screen.getByRole('button', { name: 'Reveal secondary key' }))
    const secondaryKey = await screen.findByText('test-only-secondary-secret')
    expect(secondaryKey).toHaveAttribute('data-secret', 'true')
    expect(secrets()).toHaveLength(1)
    expect(secrets()[0]).toBe(secondaryKey)

    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(secrets()).toHaveLength(0)
  })

  it('keeps focus on a Reveal button while it works, without letting either be pressed again', async () => {
    const user = userEvent.setup()
    let complete!: (result: KeyRevealResult) => void
    api.revealEntitlementKey.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    renderDialog()
    const primary = screen.getByRole('button', { name: 'Reveal primary key' })
    const secondary = screen.getByRole('button', { name: 'Reveal secondary key' })
    await waitFor(() => expect(primary).toBeEnabled())

    await user.click(primary)

    // A browser takes focus off a button that becomes disabled, so a busy button stays focusable instead.
    await waitFor(() => expect(primary).toHaveAttribute('aria-disabled', 'true'))
    expect(primary).toHaveFocus()
    expect(primary).not.toBeDisabled()
    expect(secondary).toHaveAttribute('aria-disabled', 'true')
    expect(secondary).not.toBeDisabled()
    await user.click(secondary)
    await user.click(primary)
    await user.keyboard('{Enter}')
    expect(api.revealEntitlementKey).toHaveBeenCalledTimes(1)

    await act(async () => { complete(revealed) })

    // Focus stays on the button, rather than moving to the key, which a screen reader would read out.
    expect(await screen.findByText(revealed.key)).toBeVisible()
    expect(primary).not.toHaveAttribute('aria-disabled')
    expect(focused()).toBe(primary)
    expect(screen.getByText('Primary key revealed.')).toHaveAttribute('role', 'status')
  })

  it('says which key it revealed, never the key, and leaves focus where Escape closes the dialog', async () => {
    const user = userEvent.setup()
    const { onClose } = renderDialog()
    const primary = screen.getByRole('button', { name: 'Reveal primary key' })
    await waitFor(() => expect(primary).toBeEnabled())
    // The status is there before anything is revealed, so a screen reader announces what it comes to say.
    const status = screen.getByRole('status')
    expect(status).toBeEmptyDOMElement()

    await user.click(primary)

    await waitFor(() => expect(status).toHaveTextContent('Primary key revealed.'))
    expect(status).not.toHaveTextContent(revealed.key)
    expect(focused()).toBe(primary)
    expect(secrets()[0]).not.toContainElement(document.activeElement as HTMLElement)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
  })

  it('keeps focus on each Reveal button pressed, and says which key each one revealed', async () => {
    const user = userEvent.setup()
    const secondaryKey = 'test-only-secondary-secret'
    api.revealEntitlementKey
      .mockResolvedValueOnce(revealed)
      .mockResolvedValueOnce({ ...revealed, slot: 'secondary', key: secondaryKey })
    renderDialog()
    const primary = screen.getByRole('button', { name: 'Reveal primary key' })
    const secondary = screen.getByRole('button', { name: 'Reveal secondary key' })
    await waitFor(() => expect(primary).toBeEnabled())
    const status = screen.getByRole('status')

    await user.click(primary)
    await waitFor(() => expect(status).toHaveTextContent('Primary key revealed.'))
    expect(focused()).toBe(primary)
    await user.click(secondary)

    await waitFor(() => expect(status).toHaveTextContent('Secondary key revealed.'))
    expect(await screen.findByText(secondaryKey)).toBeVisible()
    expect(focused()).toBe(secondary)
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    for (const region of screen.getAllByRole('status')) {
      expect(region).not.toHaveTextContent(revealed.key)
      expect(region).not.toHaveTextContent(secondaryKey)
    }
  })

  it('moves focus to the reason each time a reveal fails, where Escape closes the dialog', async () => {
    const user = userEvent.setup()
    api.revealEntitlementKey.mockRejectedValue(new Error('APIM secret-read permission is missing.'))
    const { onClose } = renderDialog()
    const primary = screen.getByRole('button', { name: 'Reveal primary key' })
    await waitFor(() => expect(primary).toBeEnabled())

    await user.click(primary)

    await waitFor(() => expect(focused()).toHaveTextContent('APIM secret-read permission is missing.'))
    expect(primary).toBeEnabled()
    expect(screen.getByRole('status')).toBeEmptyDOMElement()
    await user.click(primary)
    await waitFor(() => expect(focused()).toHaveTextContent('APIM secret-read permission is missing.'))
    expect(api.revealEntitlementKey).toHaveBeenCalledTimes(2)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
  })

  it('leaves focus on Copy when copying fails, and alerts a screen reader to each failure', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('Denied'))
    renderDialog()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reveal primary key' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Reveal primary key' }))
    const copyButton = await screen.findByRole('button', { name: 'Copy revealed key' })

    await user.click(copyButton)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Could not copy the key/)
    expect(copyButton).toHaveFocus()
    // Trying again puts a new alert in place of the old one, so a screen reader announces it again.
    await user.click(copyButton)
    await waitFor(() => expect(screen.getByRole('alert')).not.toBe(alert))
    expect(screen.getByRole('alert')).toHaveTextContent(/Could not copy the key/)
    expect(copyButton).toHaveFocus()
  })

  it('closes on Escape while a reveal is in flight, and drops the late key', async () => {
    const user = userEvent.setup()
    let complete!: (result: KeyRevealResult) => void
    api.revealEntitlementKey.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const { onClose, queryClient } = renderDialog()
    const primary = screen.getByRole('button', { name: 'Reveal primary key' })
    await waitFor(() => expect(primary).toBeEnabled())
    await user.click(primary)
    await waitFor(() => expect(primary).toHaveAttribute('aria-disabled', 'true'))
    expect(focused()).toBe(primary)
    const signal = api.revealEntitlementKey.mock.calls[0][2] as AbortSignal

    await user.keyboard('{Escape}')

    expect(onClose).toHaveBeenCalledOnce()
    expect(signal.aborted).toBe(true)
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
    await act(async () => { complete(revealed) })
    expect(screen.queryByText(revealed.key)).not.toBeInTheDocument()
    expect(secrets()).toHaveLength(0)
    expectNoCachedSecret(queryClient)
  })
})
