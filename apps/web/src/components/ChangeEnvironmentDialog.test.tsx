import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import type { EnvironmentCatalogView } from '../types'
import { ChangeEnvironmentDialog } from './ChangeEnvironmentDialog'

const api = {
  getEnvironmentCatalog: vi.fn(),
  assignEnvironments: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

const catalog: EnvironmentCatalogView = {
  environments: [
    {
      key: 'production',
      displayName: 'Production',
      description: null,
      color: 'danger',
      production: true,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 10,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

function renderDialog(onChanged = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    onChanged,
    ...render(
      <QueryClientProvider client={queryClient}>
        <ChangeEnvironmentDialog
          open
          resource={{
            resourceKind: 'gateway',
            resourceId: 'gateway_1',
            resourceName: 'Gateway',
            environment: null,
          }}
          onClose={vi.fn()}
          onChanged={onChanged}
        />
      </QueryClientProvider>,
    ),
  }
}

describe('ChangeEnvironmentDialog', () => {
  beforeEach(() => {
    api.getEnvironmentCatalog.mockResolvedValue(catalog)
    api.assignEnvironments.mockReset()
  })

  it('submits a single assignment on the happy path', async () => {
    api.assignEnvironments.mockResolvedValue({
      results: [
        {
          resourceKind: 'gateway',
          resourceId: 'gateway_1',
          resourceName: 'Gateway',
          previousEnvironment: null,
          environment: null,
          status: 'unchanged',
          message: null,
        },
      ],
      grantsCarried: 0,
      warnings: [],
    })
    const { onChanged } = renderDialog()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(api.assignEnvironments).toHaveBeenCalledWith({
      assignments: [{ resourceKind: 'gateway', resourceId: 'gateway_1', environment: null }],
      acknowledgeGrants: false,
    })
  })

  it('offers also classify and resubmits blocked suggestions in one batch', async () => {
    api.assignEnvironments
      .mockRejectedValueOnce(
        new ApiError('Blocked', 409, {
          details: {
            reason: 'publicationsBlocked',
            publications: [
              {
                publicationId: 'pub_1',
                status: 'applied',
                gatewayId: 'gateway_1',
                gatewayName: 'Gateway',
                gatewayEnvironment: 'production',
                modelEndpointId: 'endpoint_1',
                modelEndpointName: 'Endpoint',
                endpointEnvironment: null,
                deploymentName: 'gpt-4o',
                verdict: {
                  level: 'blocked',
                  reason: 'A Production gateway cannot front an unclassified endpoint.',
                  gatewayEnvironment: 'production',
                  endpointEnvironment: null,
                  viaException: false,
                },
              },
            ],
            suggestedAssignments: [
              {
                resourceKind: 'modelEndpoint',
                resourceId: 'endpoint_1',
                environment: 'production',
              },
            ],
          },
        }),
      )
      .mockResolvedValueOnce({ results: [], grantsCarried: 0, warnings: [] })
    renderDialog()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(
      await screen.findByRole('button', { name: /Also classify these 1 endpoints/ }),
    ).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: /Also classify these 1 endpoints/ }))
    await waitFor(() => expect(api.assignEnvironments).toHaveBeenCalledTimes(2))
    expect(api.assignEnvironments.mock.calls[1][0]).toEqual({
      assignments: [
        { resourceKind: 'gateway', resourceId: 'gateway_1', environment: null },
        { resourceKind: 'modelEndpoint', resourceId: 'endpoint_1', environment: 'production' },
      ],
      acknowledgeGrants: undefined,
    })
  })

  it('requires grant acknowledgment and resubmits with acknowledgeGrants', async () => {
    api.assignEnvironments
      .mockRejectedValueOnce(
        new ApiError('Acknowledge grants', 409, {
          details: {
            reason: 'grantsAcknowledgmentRequired',
            grants: [
              {
                entitlementId: 'grant_1',
                subject: { kind: 'user', id: 'user_1' },
                subjectLabel: 'Alice',
                resource: { kind: 'modelApi', id: 'api_1' },
                resourceName: 'Chat',
                movedResource: {
                  resourceKind: 'gateway',
                  resourceId: 'gateway_1',
                  resourceName: 'Gateway',
                },
                fromEnvironment: 'development',
                toEnvironment: 'production',
              },
            ],
            grantCount: 1,
            principalCount: 1,
            truncated: false,
          },
        }),
      )
      .mockResolvedValueOnce({ results: [], grantsCarried: 1, warnings: [] })
    renderDialog()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await userEvent.click(await screen.findByRole('checkbox', { name: /active grants/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm and save' }))
    await waitFor(() => expect(api.assignEnvironments).toHaveBeenCalledTimes(2))
    expect(api.assignEnvironments.mock.calls[1][0]).toEqual({
      assignments: [{ resourceKind: 'gateway', resourceId: 'gateway_1', environment: null }],
      acknowledgeGrants: true,
    })
  })
})
