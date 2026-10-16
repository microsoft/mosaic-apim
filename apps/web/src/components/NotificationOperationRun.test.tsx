import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { notificationTarget } from '../admin-governance'
import type { NotificationSubject } from '../admin-types'
import { GatewayNotificationRun } from './GatewayNotificationRun'
import { EndpointNotificationRun, PublishNotificationRun } from './NotificationOperationRun'

const api = vi.hoisted(() => ({
  getPublishRun: vi.fn(), getMcpPublishRun: vi.fn(), getModelPoolRun: vi.fn(),
  getModelEndpointSyncRun: vi.fn(), getMcpEndpointSyncRun: vi.fn(),
  getSyncRun: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => api }))

function Location() {
  return <output data-testid="location">{useLocation().search}</output>
}
function mount(children: ReactNode, entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<FluentProvider theme={webLightTheme}><QueryClientProvider client={client}>
    <MemoryRouter initialEntries={[entry]}>{children}<Location /></MemoryRouter>
  </QueryClientProvider></FluentProvider>)
}
const publishRun = {
  id: 'run/one', publicationId: 'publication/one', status: 'failed', planId: 'plan-1',
  startedAt: '2026-01-01T10:00:00Z', completedAt: '2026-01-01T10:00:01Z', rolledBack: false,
  errors: ['Historical policy write failed'], steps: [{ name: 'Backend policy', action: 'create', status: 'failed', error: 'Provider rejected policy' }],
}
const endpointRun = {
  id: 'run/one', endpointId: 'endpoint/one', status: 'failed',
  startedAt: '2026-01-01T10:00:00Z', completedAt: null, errors: ['Historical endpoint authentication failed'],
}
const gatewayRun = {
  id: 'run/one', gatewayId: 'gateway/one', status: 'failed',
  startedAt: '2026-01-01T10:00:00Z', completedAt: null, errors: ['Historical gateway sync failed'],
}

describe('exact notification operation links', () => {
  it.each<[NotificationSubject, string]>([
    [{ publicationId: 'publication/one', publicationTarget: 'model', runId: 'run/one', gatewayId: 'gateway' }, '/models?tab=published&selected=publication%2Fone&runId=run%2Fone'],
    [{ publicationId: 'publication/one', publicationTarget: 'mcp', runId: 'run/one' }, '/mcps?tab=published&selected=publication%2Fone&runId=run%2Fone'],
    [{ publicationId: 'publication/one', publicationTarget: 'pool', runId: 'run/one' }, '/pools/publication%2Fone?tab=plans&runId=run%2Fone'],
    [{ endpointId: 'endpoint/one', endpointKind: 'model', runId: 'run/one', gatewayId: 'gateway' }, '/models?tab=endpoints&selected=endpoint%2Fone&syncRun=run%2Fone'],
    [{ endpointId: 'endpoint/one', endpointKind: 'mcp', runId: 'run/one' }, '/mcps?tab=registered&selected=endpoint%2Fone&syncRun=run%2Fone'],
    [{ gatewayId: 'gateway/one', runId: 'run/one', integration: 'gatewayInventory' }, '/gateways/gateway%2Fone?syncRun=run%2Fone'],
    [{ resource: { kind: 'mcpServer', id: 'server/one' } }, '/mcps?tab=imported&selected=server%2Fone'],
  ])('resolves source metadata without dropping exact target or run: %j', (subject, expected) => {
    expect(notificationTarget(subject)).toBe(expected)
  })
})

describe('historical notification operation evidence', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getPublishRun.mockResolvedValue(publishRun)
    api.getMcpPublishRun.mockResolvedValue(publishRun)
    api.getModelPoolRun.mockResolvedValue(publishRun)
    api.getModelEndpointSyncRun.mockResolvedValue(endpointRun)
    api.getMcpEndpointSyncRun.mockResolvedValue(endpointRun)
    api.getSyncRun.mockResolvedValue(gatewayRun)
  })

  it.each(['model', 'mcp', 'pool'] as const)('loads only the exact %s publication run without starting an apply', async target => {
    mount(<PublishNotificationRun publicationId="publication/one" target={target} />, '/?runId=run%2Fone')
    expect(await screen.findByText('Historical policy write failed')).toBeInTheDocument()
    expect(screen.getByText('Provider rejected policy')).toBeInTheDocument()
    const method = target === 'model' ? api.getPublishRun : target === 'mcp' ? api.getMcpPublishRun : api.getModelPoolRun
    expect(method).toHaveBeenCalledExactlyOnceWith('publication/one', 'run/one')
    expect(Object.values(api).reduce((total, mock) => total + mock.mock.calls.length, 0)).toBe(1)
  })

  it.each(['model', 'mcp'] as const)('loads the exact %s endpoint sync run, not a latest-list substitute', async kind => {
    mount(<EndpointNotificationRun endpointId="endpoint/one" kind={kind} />, '/?syncRun=run%2Fone')
    expect(await screen.findByText('Historical endpoint authentication failed')).toBeInTheDocument()
    expect(kind === 'model' ? api.getModelEndpointSyncRun : api.getMcpEndpointSyncRun).toHaveBeenCalledExactlyOnceWith('endpoint/one', 'run/one')
    expect(Object.values(api).reduce((total, mock) => total + mock.mock.calls.length, 0)).toBe(1)
  })

  it('keeps missing historical runs explicit rather than showing another run', async () => {
    api.getModelEndpointSyncRun.mockRejectedValue(new Error('Run not found (404)'))
    mount(<EndpointNotificationRun endpointId="endpoint/one" kind="model" />, '/?syncRun=run%2Fone')
    expect(await screen.findByText('Run not found (404)')).toBeInTheDocument()
    expect(screen.queryByText('Historical endpoint authentication failed')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry notified endpoint run' })).toBeInTheDocument()
  })

  it('rejects a mismatched endpoint run instead of attributing its evidence to this endpoint', async () => {
    api.getModelEndpointSyncRun.mockResolvedValue({ ...endpointRun, endpointId: 'different-endpoint' })
    mount(<EndpointNotificationRun endpointId="endpoint/one" kind="model" />, '/?syncRun=run%2Fone')
    expect(await screen.findByText(/returned run does not match the notified endpoint/)).toBeInTheDocument()
    expect(screen.queryByText('Historical endpoint authentication failed')).not.toBeInTheDocument()
  })

  it('rejects a mismatched publication run', async () => {
    api.getPublishRun.mockResolvedValue({ ...publishRun, publicationId: 'different-publication' })
    mount(<PublishNotificationRun publicationId="publication/one" target="model" />, '/?runId=run%2Fone')
    expect(await screen.findByText(/returned run does not match the notified publication/)).toBeInTheDocument()
    expect(screen.queryByText('Historical policy write failed')).not.toBeInTheDocument()
  })

  it('loads only the exact gateway sync run', async () => {
    mount(<GatewayNotificationRun gatewayId="gateway/one" />, '/?syncRun=run%2Fone')
    expect(await screen.findByText('Historical gateway sync failed')).toBeInTheDocument()
    expect(api.getSyncRun).toHaveBeenCalledExactlyOnceWith('gateway/one', 'run/one')
  })

  it.each([
    { gatewayId: 'different-gateway' },
    { id: 'different-run' },
  ])('rejects mismatched gateway evidence: %j', async mismatch => {
    api.getSyncRun.mockResolvedValue({ ...gatewayRun, ...mismatch })
    mount(<GatewayNotificationRun gatewayId="gateway/one" />, '/?syncRun=run%2Fone')
    expect(await screen.findByText(/returned run does not match the notified gateway/)).toBeInTheDocument()
    expect(screen.queryByText('Historical gateway sync failed')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry run details' })).toBeInTheDocument()
  })

  it('removes only the notified run when closing its evidence', async () => {
    const user = userEvent.setup()
    mount(<PublishNotificationRun publicationId="publication/one" target="model" />, '/models?tab=published&selected=publication%2Fone&runId=run%2Fone&q=retained')
    await screen.findByText('Historical policy write failed')
    await user.click(screen.getByRole('button', { name: 'Close notified run' }))
    const params = new URLSearchParams(screen.getByTestId('location').textContent!)
    expect(params.has('runId')).toBe(false)
    expect(params.get('selected')).toBe('publication/one')
    expect(params.get('tab')).toBe('published')
    expect(params.get('q')).toBe('retained')
    expect(screen.queryByRole('region', { name: 'Notified publication run' })).not.toBeInTheDocument()
  })

  it('does not fetch historical evidence on an ordinary detail page', () => {
    mount(<><PublishNotificationRun publicationId="publication/one" target="model" /><EndpointNotificationRun endpointId="endpoint/one" kind="model" /><GatewayNotificationRun gatewayId="gateway/one" /></>, '/')
    Object.values(api).forEach(mock => expect(mock).not.toHaveBeenCalled())
  })
})
