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
import type { CatalogEntry, PortalCostCenter, PortalEnvironment, PortalResourceKind } from '../types'

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

function costCenterLabel(costCenter: PortalCostCenter) {
  return `${costCenter.name} (${costCenter.code})`
}

function CatalogAction({ entry }: { entry: CatalogEntry }) {
  const api = usePortalApi()
  const queryClient = useQueryClient()
  const requests = useQuery({ queryKey: ['portal', 'requests'], queryFn: api.listAccessRequests })
  const costCenters = useQuery({ queryKey: ['portal', 'cost-centers'], queryFn: api.listCostCenters })
  const [justification, setJustification] = useState('')
  const [selectedCostCenterId, setSelectedCostCenterId] = useState<string | undefined>(undefined)
  const resource = resourceFromCatalog(entry)
  const entitledCostCenters = new Set(entry.entitledCostCenterIds ?? [])
  const requestedCostCenters = new Set(entry.requestedCostCenterIds ?? [])
  const olderEntitledState = !entry.entitledCostCenterIds && entry.entitled
  const olderPendingState = !entry.requestedCostCenterIds && entry.requestState === 'pending'
  const loadedCostCenters = costCenters.data ?? []
  const defaultCostCenterId = costCenters.data
    ? (costCenters.data.find((costCenter) => costCenter.isDefault) ?? costCenters.data[0])?.id
    : undefined
  const effectiveCostCenterId = selectedCostCenterId ?? defaultCostCenterId
  const allCostCentersUsed =
    costCenters.isSuccess &&
    loadedCostCenters.length > 0 &&
    loadedCostCenters.every((costCenter) => entitledCostCenters.has(costCenter.id) || requestedCostCenters.has(costCenter.id))
  const selectedAlreadyGranted = effectiveCostCenterId ? entitledCostCenters.has(effectiveCostCenterId) || olderEntitledState : entry.entitled
  const selectedAlreadyRequested = effectiveCostCenterId
    ? requestedCostCenters.has(effectiveCostCenterId) || olderPendingState
    : entry.requestState === 'pending'
  const selectedUnavailable = selectedAlreadyGranted || selectedAlreadyRequested
  const pendingRequest = requests.data?.find(
    (request) =>
      request.state === 'pending' &&
      sameResource(request.resource, resource) &&
      (!effectiveCostCenterId || request.costCenterId === effectiveCostCenterId || olderPendingState),
  )
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['portal', 'catalog'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'requests'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'profile'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'cost-centers'] }),
    ])
  }

  const create = useMutation({
    mutationFn: () =>
      api.createAccessRequest({
        resource,
        costCenterId: costCenters.isSuccess ? effectiveCostCenterId : undefined,
        justification: justification.trim() || undefined,
      }),
    onSuccess: invalidate,
  })
  const withdraw = useMutation({
    mutationFn: (requestId: string) => api.withdrawAccessRequest(requestId),
    onSuccess: invalidate,
  })

  if (entry.entitled && !entry.entitledCostCenterIds && !costCenters.isSuccess) {
    return (
      <div className="action-stack">
        <Badge appearance="filled">Already entitled</Badge>
      </div>
    )
  }

  return (
    <div className="action-stack">
      {costCenters.isLoading && <Text size={200}>Loading cost centers. The request can use your default if needed.</Text>}
      {costCenters.isError && (
        <Text size={200} className="connection-note">
          Cost centers could not load. You can still request access; MOSAIC will use your default cost center.
        </Text>
      )}
      {costCenters.isSuccess && loadedCostCenters.length > 0 && (
        <label className="cost-center-select">
          <span>Cost center</span>
          <select
            aria-label={`Cost center for ${entry.displayName}`}
            value={effectiveCostCenterId ?? ''}
            onChange={(event) => setSelectedCostCenterId(event.currentTarget.value)}
          >
            {loadedCostCenters.map((costCenter) => {
              const suffix = entitledCostCenters.has(costCenter.id)
                ? ' — already entitled'
                : requestedCostCenters.has(costCenter.id)
                  ? ' — request open'
                  : ''
              return (
                <option key={costCenter.id} value={costCenter.id}>
                  {costCenterLabel(costCenter)}
                  {suffix}
                </option>
              )
            })}
          </select>
        </label>
      )}
      {selectedAlreadyGranted && <Badge appearance="filled">Already entitled for this cost center</Badge>}
      {selectedAlreadyRequested && !selectedAlreadyGranted && <Text>A request is already open for this cost center.</Text>}
      {selectedAlreadyRequested && pendingRequest && (
        <Button
          onClick={() => withdraw.mutate(pendingRequest.id)}
          disabled={withdraw.isPending}
        >
          Withdraw
        </Button>
      )}
      {allCostCentersUsed && (
        <Text size={200} className="connection-note">
          All cost centers already have access or an open request.
        </Text>
      )}
      {!selectedUnavailable && !allCostCentersUsed && (
        <>
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
        </>
      )}
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
              Clear filters to see all catalog entries.
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
