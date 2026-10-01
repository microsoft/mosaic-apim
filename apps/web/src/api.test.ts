import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useMosaicApi } from './api'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({
    accounts: [],
    instance: {
      acquireTokenSilent: vi.fn(),
    },
  }),
}))

describe('useMosaicApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the policy preview contract to the live API', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          policyXml: '<policies />',
          contentSha256: 'digest',
          warnings: [],
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    const preview = await result.current.previewPolicy({
      backendResource: 'https://cognitiveservices.azure.com',
      enforcement: {
        counterKeyExpression: '@(context.Subscription.Id)',
        tokensPerMinute: 1_000,
        estimatePromptTokens: true,
      },
    })

    expect(preview.contentSha256).toBe('digest')
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(options.method).toBe('POST')
    expect(JSON.parse(String(options.body))).toEqual({
      backendResource: 'https://cognitiveservices.azure.com',
      enforcement: {
        counterKeyExpression: '@(context.Subscription.Id)',
        tokensPerMinute: 1000,
        estimatePromptTokens: true,
      },
    })
  })

  it.each(['admin', 'self'] as const)('fetches each %s key only on explicit invocation with no-store', async (caller) => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({
      entitlementId: 'grant_1', subscriptionName: 'dedicated-sub', slot: 'primary', key: 'test-only-key',
    }), { status: 200, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())
    expect(fetchMock).not.toHaveBeenCalled()
    const controller = new AbortController()
    const reveal = caller === 'admin' ? result.current.revealEntitlementKey : result.current.revealMyEntitlementKey
    await reveal('grant_1', 'primary', controller.signal)
    await reveal('grant_1', 'secondary', controller.signal)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [url, options] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(url).toContain(`/api/v1/${caller === 'self' ? 'me/' : ''}entitlements/grant_1/keys/reveal`)
    expect(options).toMatchObject({ method: 'POST', cache: 'no-store', signal: controller.signal })
    expect(JSON.parse(String(options.body))).toEqual({ slot: 'secondary' })
  })

  it('calls the cost center and admin key endpoints with contracted paths', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ id: 'ok' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.listCostCenters()
    await result.current.createCostCenter({ name: 'Support', code: 'support', description: null, owners: ['owner@example.com'], keysAllowed: true })
    await result.current.getCostCenter('cc 1')
    await result.current.updateCostCenter('cc 1', { keysAllowed: false })
    await result.current.addCostCenterMember('cc 1', 'principal 1')
    await result.current.removeCostCenterMember('cc 1', 'principal 1')
    await result.current.updateCostCenterLimits('cc 1', [])
    await result.current.getCostCenterSettings()
    await result.current.updateCostCenterSettings({ defaultCostCenterId: 'cc 1' })
    await result.current.listEntitlements({ costCenter: 'cc 1' })
    await result.current.createEntitlement({ subject: { kind: 'user', id: 'p1' }, resource: { kind: 'modelApi', id: 'm1' }, costCenterId: 'cc 1' })
    await result.current.createEntitlementKey('grant 1')
    await result.current.rotateEntitlementKey('grant 1', 'secondary')
    await result.current.deleteEntitlementKey('grant 1')
    await result.current.deleteCostCenter('cc 1')
    await result.current.recheckCostCenter('cc 1')

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/cost-centers',
      '/api/v1/cost-centers',
      '/api/v1/cost-centers/cc%201',
      '/api/v1/cost-centers/cc%201',
      '/api/v1/cost-centers/cc%201/members/principal%201',
      '/api/v1/cost-centers/cc%201/members/principal%201',
      '/api/v1/cost-centers/cc%201/limits',
      '/api/v1/cost-center-settings',
      '/api/v1/cost-center-settings',
      '/api/v1/entitlements?costCenter=cc+1',
      '/api/v1/entitlements',
      '/api/v1/entitlements/grant%201/keys',
      '/api/v1/entitlements/grant%201/keys/rotate',
      '/api/v1/entitlements/grant%201/keys',
      '/api/v1/cost-centers/cc%201',
      '/api/v1/cost-centers/cc%201/recheck',
    ])
    expect(fetchMock.mock.calls[15][1]).toMatchObject({ method: 'POST' })
    expect(fetchMock.mock.calls[6][1]).toMatchObject({ method: 'PUT' })
    expect(JSON.parse(fetchMock.mock.calls[6][1].body)).toEqual({ limits: [] })
    expect(fetchMock.mock.calls[12][1]).toMatchObject({ method: 'POST', cache: 'no-store' })
    expect(JSON.parse(fetchMock.mock.calls[12][1].body)).toEqual({ slot: 'secondary' })
    expect(fetchMock.mock.calls[13][1]).toMatchObject({ method: 'DELETE', cache: 'no-store' })
  })

  it('sends link, settings, connection and future self-service calls to their existing API prefix', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())
    await result.current.linkPublicationModelApi('pub_1')
    await result.current.updatePublication('pub_1', { governedAccess: { keysEnabled: false, entraEnabled: false } })
    await result.current.getEntitlementConnection('grant_1')
    await result.current.getMcpConnection('grant_1')
    await result.current.listMyEntitlements()
    await result.current.getMyEntitlementConnection('grant_1')
    await result.current.diagnosePublicationRecovery('pub_1', 'run_1')
    await result.current.getPublicationLock('pub_1')
    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/publications/pub_1/model-api',
      '/api/v1/publications/pub_1',
      '/api/v1/entitlements/grant_1/connection',
      '/api/v1/entitlements/grant_1/mcp-connection',
      '/api/v1/me/entitlements',
      '/api/v1/me/entitlements/grant_1/connection',
      '/api/v1/publications/pub_1/recover',
      '/api/v1/publications/pub_1/lock',
    ])
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      governedAccess: { keysEnabled: false, entraEnabled: false },
    })
    expect(fetchMock.mock.calls[6][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[6][1].body)).toEqual({
      runId: 'run_1', confirmQuiesced: false,
    })

  })

  it('plans an unpublish, then unpublishes only the plan it names', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.planUnpublishPublication('pub_1')
    await result.current.unpublishPublication('pub_1', 'plan 2')

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/publications/pub_1/unpublish-plan',
      '/api/v1/publications/pub_1/unpublish?plan=plan%202',
    ])
    expect(fetchMock.mock.calls.map(([, options]) => options.method)).toEqual(['POST', 'POST'])
  })

  it('calls the MCP publication endpoints with the contracted paths and shapes', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(
      JSON.stringify({ id: 'ok', supported: true, reasons: [], warnings: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.getMcpPublishingCapability('gateway 1')
    await result.current.listMcpPublications('gateway 1')
    await result.current.createMcpPublication({ gatewayId: 'gateway_1', mcpEndpointId: 'endpoint_1' })
    await result.current.getMcpPublication('mcp pub')
    await result.current.updateMcpPublication('mcp pub', { displayName: 'Tools' })
    await result.current.deleteMcpPublication('mcp pub')
    await result.current.planMcpPublication('mcp pub')
    await result.current.applyMcpPublication('mcp pub', 'plan 1')
    await result.current.planUnpublishMcpPublication('mcp pub')
    await result.current.unpublishMcpPublication('mcp pub', 'plan 2')
    await result.current.listMcpPublishRuns('mcp pub')
    await result.current.getMcpPublishRun('mcp pub', 'run 1')
    await result.current.recoverMcpPublication('mcp pub', { runId: 'run 1', confirmQuiesced: false })
    await result.current.getMcpPublicationLock('mcp pub')
    await result.current.getMcpPublishPlan('plan 1')

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/gateways/gateway%201/mcp-publishing',
      '/api/v1/mcp-publications?gateway=gateway%201',
      '/api/v1/mcp-publications',
      '/api/v1/mcp-publications/mcp%20pub',
      '/api/v1/mcp-publications/mcp%20pub',
      '/api/v1/mcp-publications/mcp%20pub',
      '/api/v1/mcp-publications/mcp%20pub/plan',
      '/api/v1/mcp-publications/mcp%20pub/apply?plan=plan%201',
      '/api/v1/mcp-publications/mcp%20pub/unpublish-plan',
      '/api/v1/mcp-publications/mcp%20pub/unpublish?plan=plan%202',
      '/api/v1/mcp-publications/mcp%20pub/runs',
      '/api/v1/mcp-publications/mcp%20pub/runs/run%201',
      '/api/v1/mcp-publications/mcp%20pub/recover',
      '/api/v1/mcp-publications/mcp%20pub/lock',
      '/api/v1/mcp-publish-plans/plan%201',
    ])
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'POST' })
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({
      gatewayId: 'gateway_1',
      mcpEndpointId: 'endpoint_1',
    })
    expect(fetchMock.mock.calls[4][1]).toMatchObject({ method: 'PATCH' })
    expect(fetchMock.mock.calls[8][1]).toMatchObject({ method: 'POST' })
    expect(fetchMock.mock.calls[9][1]).toMatchObject({ method: 'POST' })
    expect(JSON.parse(fetchMock.mock.calls[12][1].body)).toEqual({
      runId: 'run 1',
      confirmQuiesced: false,
    })
  })

  it('calls the directory and overlap endpoints with the contracted query strings', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => new Response(
      url.includes('/directory/status')
        ? JSON.stringify({ lookupEnabled: true, groupClaimsEnabled: true })
        : url.includes('/directory/search')
          ? JSON.stringify([])
          : url.includes('/directory/objects')
            ? JSON.stringify({ objectId: 'object-1', kind: 'user' })
            : url.includes('/members')
              ? JSON.stringify({ groupObjectId: 'group-object', members: [], truncated: false })
              : JSON.stringify({ overlaps: [], membershipChecked: true, skipped: [], generatedAt: '2026-09-01T12:00:00Z' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.getDirectoryStatus()
    await result.current.searchDirectory('agent', 'Ada Agent', 10)
    await result.current.getDirectoryObject('object-1')
    await result.current.listPrincipalMembers('principal-group', 50)
    await result.current.getGrantOverlaps({ resource: 'modelApi_1' })

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/directory/status',
      '/api/v1/directory/search?kind=agent&q=Ada%20Agent&limit=10',
      '/api/v1/directory/objects/object-1',
      '/api/v1/principals/principal-group/members?limit=50',
      '/api/v1/entitlements/overlaps?resource=modelApi_1',
    ])
  })

  it('sends only the management mode when switching a gateway, and surfaces a refusal', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'gateway_1', managementMode: 'manage' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        code: 'validation_error',
        message: 'MOSAIC cannot write to this gateway yet, so it cannot be managed.',
        details: { managementMode: 'manage', missingActions: [] },
      }), { status: 422, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    const updated = await result.current.updateGateway('gateway_1', { managementMode: 'manage' })

    expect(updated.managementMode).toBe('manage')
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(String(url).replace(/^https?:\/\/[^/]+/, '')).toBe('/api/v1/gateways/gateway_1')
    expect(options.method).toBe('PATCH')
    expect(new Headers(options.headers).get('Content-Type')).toBe('application/json')
    expect(JSON.parse(String(options.body))).toEqual({ managementMode: 'manage' })

    const refusal = result.current.updateGateway('gateway_1', { managementMode: 'manage' })
    await expect(refusal).rejects.toMatchObject({
      status: 422,
      message: 'MOSAIC cannot write to this gateway yet, so it cannot be managed.',
    })
  })

  it('sends a pasted key to register or replace, and reads why a request did not validate', async () => {
    const created = { id: 'endpoint_key', keyStoredByMosaic: true }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(created), {
        status: 201, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(created), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: [
          {
            type: 'value_error',
            loc: ['body', 'apiKey'],
            msg: 'Value error, An API key is 16 to 512 letters, digits and symbols, with no spaces or line breaks',
          },
          { type: 'missing', loc: ['body', 'endpoint'], msg: 'Field required' },
        ],
      }), { status: 422, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.registerModelEndpoint({
      endpoint: 'https://fabrikam-foundry.services.ai.azure.com',
      apiKey: 'fictional-key-for-a-test-only',
    })
    await result.current.updateModelEndpoint('endpoint_key', { apiKey: 'fictional-new-key-for-a-test' })

    const [registerUrl, registerOptions] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(String(registerUrl).replace(/^https?:\/\/[^/]+/, '')).toBe('/api/v1/model-endpoints')
    expect(JSON.parse(String(registerOptions.body))).toEqual({
      endpoint: 'https://fabrikam-foundry.services.ai.azure.com',
      apiKey: 'fictional-key-for-a-test-only',
    })
    const [replaceUrl, replaceOptions] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(String(replaceUrl).replace(/^https?:\/\/[^/]+/, '')).toBe('/api/v1/model-endpoints/endpoint_key')
    expect(replaceOptions.method).toBe('PATCH')
    expect(JSON.parse(String(replaceOptions.body))).toEqual({ apiKey: 'fictional-new-key-for-a-test' })

    await expect(
      result.current.registerModelEndpoint({ apiKey: 'has a space so it is refused' }),
    ).rejects.toMatchObject({
      status: 422,
      message:
        'An API key is 16 to 512 letters, digits and symbols, with no spaces or line breaks. ' +
        'Field required.',
    })
  })

  it('uses the environment catalog contract paths', async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(
        async () =>
          new Response(
            JSON.stringify({
              environments: [],
              requireClassification: false,
              unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
              compatibility: [],
              updatedAt: null,
              items: [],
              results: [],
              grantsCarried: 0,
              warnings: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())
    await result.current.getEnvironmentCatalog()
    await result.current.createEnvironment({ key: 'prod2', displayName: 'Prod 2', color: 'danger' })
    await result.current.updateEnvironment('prod2', { displayName: 'Prod 2' })
    await result.current.deleteEnvironment('prod2')
    await result.current.updateEnvironmentSettings({ requireClassification: true })
    await result.current.listEnvironmentSuggestions()
    await result.current.assignEnvironments({
      assignments: [
        { resourceKind: 'gateway', resourceId: 'gateway_1', environment: 'production' },
      ],
    })
    await result.current.listEnvironmentFindings('gateway_1')

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/environment-catalog',
      '/api/v1/environment-catalog/environments',
      '/api/v1/environment-catalog/environments/prod2',
      '/api/v1/environment-catalog/environments/prod2',
      '/api/v1/environment-catalog/settings',
      '/api/v1/environment-suggestions',
      '/api/v1/environment-assignments',
      '/api/v1/environment-findings?gatewayId=gateway_1',
    ])
    expect(fetchMock.mock.calls.map(([, options]) => options.method ?? 'GET')).toEqual([
      'GET',
      'POST',
      'PATCH',
      'DELETE',
      'PATCH',
      'GET',
      'POST',
      'GET',
    ])
    expect(JSON.parse(String(fetchMock.mock.calls[6][1].body))).toEqual({
      assignments: [
        { resourceKind: 'gateway', resourceId: 'gateway_1', environment: 'production' },
      ],
    })
  })

  it('calls analytics endpoints with filters and downloads CSV as a blob', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/export')) {
        return new Response('csv', {
          status: 200,
          headers: {
            'Content-Type': 'text/csv',
            'Content-Disposition': 'attachment; filename="mosaic-people-20260219-20260320.csv"',
          },
        })
      }
      return new Response(JSON.stringify({
        dataSource: 'logAnalytics',
        rollupsEnabled: true,
        generatedAt: '2026-03-18T15:30:00Z',
        freshness: { status: 'current', gateways: 1, intervalMinutes: 15 },
        gateways: [],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.getAnalyticsStatus()
    await result.current.refreshAnalytics()
    await result.current.getAnalyticsOverview({ range: 'custom', start: '2026-03-01', end: '2026-03-18', gatewayId: 'gateway 1', environment: 'production', resourceId: 'model-api-chat', subjectKind: 'user' })
    await result.current.getAnalyticsConsumers({ range: '7d' })
    await result.current.getAnalyticsModels({ range: '7d' })
    await result.current.getAnalyticsReliability({ range: '7d' })
    await result.current.getAnalyticsLimits({ range: '7d' })
    await result.current.getAnalyticsHygiene({ range: '7d' })
    await result.current.getAnalyticsUnattributed({ range: '7d' })
    const file = await result.current.exportAnalytics('people', { range: '30d' })

    expect(await file.blob.text()).toBe('csv')
    expect(file.filename).toBe('mosaic-people-20260219-20260320.csv')
    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/analytics/status',
      '/api/v1/analytics/refresh',
      '/api/v1/analytics/overview?range=custom&start=2026-03-01&end=2026-03-18&gatewayId=gateway+1&environment=production&resourceId=model-api-chat&subjectKind=user',
      '/api/v1/analytics/consumers?range=7d',
      '/api/v1/analytics/models?range=7d',
      '/api/v1/analytics/reliability?range=7d',
      '/api/v1/analytics/limits?range=7d',
      '/api/v1/analytics/hygiene?range=7d',
      '/api/v1/analytics/unattributed?range=7d',
      '/api/v1/analytics/export?range=30d&view=people',
    ])
    expect(fetchMock.mock.calls[1][1].method).toBe('POST')
    expect(new Headers(fetchMock.mock.calls[9][1].headers).get('Accept')).toBe('text/csv')
  })

  it('calls gateway telemetry action endpoints', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ gatewayId: 'gateway_1' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.getGatewayTelemetry('gateway 1')
    await result.current.enableGatewayTelemetry('gateway 1')
    await result.current.refreshGatewayTelemetry('gateway 1')
    await result.current.backfillGatewayTelemetry('gateway 1', 60)

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/gateways/gateway%201/telemetry',
      '/api/v1/gateways/gateway%201/telemetry/enable',
      '/api/v1/gateways/gateway%201/telemetry/refresh',
      '/api/v1/gateways/gateway%201/telemetry/backfill',
    ])
    expect(fetchMock.mock.calls.map(([, options]) => options.method ?? 'GET')).toEqual(['GET', 'POST', 'POST', 'POST'])
    expect(JSON.parse(String(fetchMock.mock.calls[3][1].body))).toEqual({ days: 60 })
  })

})
