import { Badge, Button, Card, CardHeader, Textarea, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import {
  requestStateLabel,
  resourceFromCatalog,
  resourceKindLabel,
  sameResource,
} from '../entitlement-format'
import { usePortalEnvironments } from '../environments'
import type { CatalogEntry, PortalEnvironment, PortalResourceKind } from '../types'

function environmentOptions(entries: CatalogEntry[], environments: PortalEnvironment[] | undefined) {
  const present = new Set(entries.map((entry) => entry.environment).filter((key): key is string => key !== null))
  const known = (environments ?? []).filter((environment) => present.has(environment.key))
  const knownKeys = new Set(known.map((environment) => environment.key))
  const unknown = entries
    .map((entry) => entry.environment)
    .filter((key): key is string => key !== null && !knownKeys.has(key))
    .filter((key, index, list) => list.indexOf(key) === index)
  return { known, unknown, hasUnclassified: entries.some((entry) => entry.environment === null) }
}

function CatalogAction({ entry }: { entry: CatalogEntry }) {
  const api = usePortalApi()
  const queryClient = useQueryClient()
  const requests = useQuery({ queryKey: ['portal', 'requests'], queryFn: api.listAccessRequests })
  const [justification, setJustification] = useState('')
  const resource = resourceFromCatalog(entry)
  const pendingRequest = requests.data?.find(
    (request) => request.state === 'pending' && sameResource(request.resource, resource),
  )
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['portal', 'catalog'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'requests'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'profile'] }),
    ])
  }

  const create = useMutation({
    mutationFn: () =>
      api.createAccessRequest({
        resource,
        justification: justification.trim() || undefined,
      }),
    onSuccess: invalidate,
  })
  const withdraw = useMutation({
    mutationFn: (requestId: string) => api.withdrawAccessRequest(requestId),
    onSuccess: invalidate,
  })

  if (entry.entitled) {
    return (
      <div className="action-stack">
        <Badge appearance="filled">Already entitled</Badge>
      </div>
    )
  }
  if (entry.requestState === 'pending') {
    return (
      <div className="action-stack">
        <Text>A request is already open.</Text>
        <Button
          onClick={() => pendingRequest && withdraw.mutate(pendingRequest.id)}
          disabled={!pendingRequest || withdraw.isPending}
        >
          Withdraw
        </Button>
        {withdraw.isError && <Text className="form-error">{withdraw.error.message}</Text>}
      </div>
    )
  }

  return (
    <div className="action-stack">
      <Textarea
        value={justification}
        aria-label={`Justification for ${entry.displayName}`}
        placeholder="Optional justification"
        resize="vertical"
        onChange={(_, data) => setJustification(data.value)}
      />
      <Button appearance="primary" onClick={() => create.mutate()} disabled={create.isPending}>
        Request access
      </Button>
      {create.isError && <Text className="form-error">{create.error.message}</Text>}
    </div>
  )
}

function CatalogEnforcementBadge({ entry }: { entry: CatalogEntry }) {
  if (entry.kind !== 'mcpServer' || entry.enforced == null) return null
  return (
    <Badge appearance={entry.enforced ? 'filled' : 'tint'}>
      {entry.enforced ? 'Enforced by the gateway' : 'Recorded, not enforced'}
    </Badge>
  )
}

export function CatalogPage() {
  const api = usePortalApi()
  const catalog = useQuery({ queryKey: ['portal', 'catalog'], queryFn: api.listCatalog })
  const environments = usePortalEnvironments()
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [kindFilter, setKindFilter] = useState<'all' | PortalResourceKind>('all')
  const filteredCatalog = useMemo(() => {
    if (!catalog.data) return []
    return catalog.data.filter((entry) => {
      const matchesEnvironment =
        environmentFilter === 'all' ||
        (environmentFilter === 'unclassified' ? entry.environment === null : entry.environment === environmentFilter)
      const matchesKind = kindFilter === 'all' || entry.kind === kindFilter
      return matchesEnvironment && matchesKind
    })
  }, [catalog.data, environmentFilter, kindFilter])
  const filterOptions = useMemo(
    () => environmentOptions(catalog.data ?? [], environments.data),
    [catalog.data, environments.data],
  )
  const clearFilters = () => {
    setEnvironmentFilter('all')
    setKindFilter('all')
  }

  return (
    <>
      <PageHeader
        title="Catalog"
        description="Model APIs and MCP servers published for portal users. Request access when a resource is not already granted."
      />
      {catalog.isLoading && <Loading label="Loading catalog" />}
      {catalog.isError && <ErrorState error={catalog.error} />}
      {catalog.isSuccess && catalog.data.length === 0 && (
        <EmptyState title="No catalog entries">
          The portal catalog is empty. This is not an access denial state.
        </EmptyState>
      )}
      {catalog.isSuccess && catalog.data.length > 0 && (
        <>
          <div className="filter-row" role="group" aria-label="Catalog filters">
            <label>
              <span>Environment</span>
              <select
                value={environmentFilter}
                onChange={(event) => setEnvironmentFilter(event.currentTarget.value)}
              >
                <option value="all">All environments</option>
                {filterOptions.known.map((environment) => (
                  <option key={environment.key} value={environment.key}>
                    {environment.displayName}
                  </option>
                ))}
                {filterOptions.unknown.map((environment) => (
                  <option key={environment} value={environment}>
                    {environment}
                  </option>
                ))}
                {filterOptions.hasUnclassified && (
                  <option value="unclassified">Unclassified</option>
                )}
              </select>
            </label>
            <label>
              <span>Resource type</span>
              <select
                value={kindFilter}
                onChange={(event) => setKindFilter(event.currentTarget.value as typeof kindFilter)}
              >
                <option value="all">All types</option>
                <option value="modelApi">Model APIs</option>
                <option value="mcpServer">MCP servers</option>
              </select>
            </label>
          </div>
          {filteredCatalog.length === 0 ? (
            <EmptyState title="No catalog entries match these filters">
              <p>Clear filters to see all catalog entries.</p>
              <Button onClick={clearFilters}>Clear filters</Button>
            </EmptyState>
          ) : (
            <div className="catalog-grid">
              {filteredCatalog.map((entry) => (
                <Card key={`${entry.kind}:${entry.id}`} className="catalog-card">
                  <CardHeader
                    header={<h2>{entry.displayName}</h2>}
                    description={`${resourceKindLabel(entry.kind)} · ${entry.gatewayName ?? 'Gateway not available'}`}
                    action={
                      entry.requestState && entry.requestState !== 'pending' ? (
                        <Badge appearance="tint">{requestStateLabel(entry.requestState)}</Badge>
                      ) : undefined
                    }
                  />
                  <div className="badge-row">
                    <EnvironmentBadge
                      environment={entry.environment}
                      environments={environments.data}
                    />
                    <CatalogEnforcementBadge entry={entry} />
                  </div>
                  <Text>{entry.summary ?? 'No summary provided.'}</Text>
                  <CatalogAction entry={entry} />
                </Card>
              ))}
            </div>
          )}
        </>
      )}
    </>
  )
}
