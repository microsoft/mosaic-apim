import { Badge, Button, Input, Textarea, Text } from '@fluentui/react-components'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { DetailPanel } from '../components/DetailPanel'
import { ResourceRow } from '../components/ResourceRow'
import { CloudBadge } from '../components/CloudBadge'
import { cloudLabel } from '../cloud-label'
import { catalogTitle, groupResources, modelGroupKey, resourceKey, uniqueValues } from '../model-presentation'
import { environmentLabel } from '../environments'
import {
  apiStyleLabel,
  capacityLabel,
  isModelResource,
  resourceFromCatalog,
  resourceKindLabel,
  sameResource,
} from '../entitlement-format'
import { usePortalEnvironments } from '../environments'
import type { CatalogEntry, PortalCostCenter, PortalEnvironment } from '../types'

type KindFilter = 'all' | 'model' | 'mcpServer'

function matchesKind(entry: CatalogEntry, filter: KindFilter) {
  if (filter === 'all') return true
  return filter === 'model' ? isModelResource(entry.kind) : entry.kind === filter
}

function entryDescription(entry: CatalogEntry) {
  return [
    resourceKindLabel(entry.kind),
    entry.apiStyle ? apiStyleLabel(entry.apiStyle) : null,
    entry.gatewayName ?? 'Gateway not available',
  ]
    .filter((part): part is string => part !== null)
    .join(' · ')
}

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
  const government = entry.azureCloud === 'government'
  const keysDisallowed = government && costCenters.isSuccess &&
    loadedCostCenters.find((center) => center.id === effectiveCostCenterId)?.keysAllowed === false
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
        {government && <Text className="connection-note">Azure Government requires a direct grant and a subscription key.
          MOSAIC prepares your key after an administrator applies reviewed access. Your selected cost center is charged.
          You must have network access to the gateway; portal sign-in does not connect you to it.</Text>}
        {keysDisallowed && <Text className="form-error">This cost center does not allow keys. Choose a key-enabled cost center or contact your administrator.</Text>}
        <Badge appearance="filled">Already entitled</Badge>
      </div>
    )
  }

  return (
    <div className="action-stack">
      {government && <Text className="connection-note">Azure Government requires a direct grant and a subscription key.
        MOSAIC prepares your key after an administrator applies reviewed access. Your selected cost center is charged.
        You must have network access to the gateway; portal sign-in does not connect you to it.</Text>}
      {keysDisallowed && <Text className="form-error">This cost center does not allow keys. Choose a key-enabled cost center or contact your administrator.</Text>}
      {costCenters.isLoading && <Text size={200}>Loading cost centers…</Text>}
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
          <Button appearance="primary" onClick={() => create.mutate()} disabled={create.isPending || keysDisallowed}>
            Request access
          </Button>
        </>
      )}
      {create.isError && <Text className="form-error">{create.error.message}</Text>}
      {withdraw.isError && <Text className="form-error">{withdraw.error.message}</Text>}
      {create.isSuccess && pendingRequest?.id === create.data.id && (
        <Text>Request submitted. <Link to="/requests">Track it in My requests</Link>. Gateway access may still need to be applied after approval.</Text>
      )}
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

function CatalogCapacityBadge({ entry }: { entry: CatalogEntry }) {
  if (!entry.capacity) return null
  return <Badge appearance="outline">{capacityLabel(entry.capacity)}</Badge>
}

export function CatalogPage() {
  const api = usePortalApi()
  const catalog = useQuery({ queryKey: ['portal', 'catalog'], queryFn: api.listCatalog })
  const environments = usePortalEnvironments()
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [cloudFilter, setCloudFilter] = useState('all')
  const [kindFilter, setKindFilter] = useState<KindFilter>('all')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [offeringId, setOfferingId] = useState<string | null>(null)
  const filteredCatalog = useMemo(() => {
    if (!catalog.data) return []
    return catalog.data.filter((entry) => {
      const matchesEnvironment =
        environmentFilter === 'all' ||
        (environmentFilter === 'unclassified' ? entry.environment === null : entry.environment === environmentFilter)
      return matchesEnvironment && (cloudFilter === 'all' || (entry.azureCloud ?? 'commercial') === cloudFilter) && matchesKind(entry, kindFilter) &&
        `${catalogTitle(entry)} ${entry.displayName} ${entry.summary ?? ''} ${entry.gatewayName ?? ''}`.toLowerCase().includes(search.trim().toLowerCase())
    })
  }, [catalog.data, environmentFilter, cloudFilter, kindFilter, search])
  const groups = groupResources(filteredCatalog, (entry) => ({
    key: modelGroupKey(resourceFromCatalog(entry), entry.model), title: catalogTitle(entry),
  })).sort((a, b) => Number(!a.items.some((entry) => entry.model?.name.trim())) -
    Number(!b.items.some((entry) => entry.model?.name.trim())))
  const selected = groups.find((group) => group.key === selectedId)
  const offering = selected?.items.length === 1 ? selected.items[0] :
    selected?.items.find((entry) => resourceKey(resourceFromCatalog(entry)) === offeringId)
  const filterOptions = useMemo(
    () => environmentOptions(catalog.data ?? [], environments.data),
    [catalog.data, environments.data],
  )
  const clearFilters = () => {
    setEnvironmentFilter('all')
    setCloudFilter('all')
    setKindFilter('all')
    setSearch('')
  }

  return (
    <>
      <PageHeader
        title="Catalog"
        description="Find a model or MCP server."
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
            <Input className="resource-search" aria-label="Search catalog" placeholder="Search models and MCP servers" value={search}
              onChange={(_, data) => setSearch(data.value)} />
            <label><span>Cloud</span><select value={cloudFilter} onChange={(event) => setCloudFilter(event.currentTarget.value)}>
              <option value="all">All clouds</option><option value="commercial">Azure Commercial</option><option value="government">Azure Government</option>
            </select></label>
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
                <option value="model">Models</option>
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
            <>
              <div className="resource-list-caption">{groups.length} {groups.length === 1 ? 'resource' : 'resources'}</div>
              <div className="resource-list-heading" aria-hidden="true">
                <span>Model or server</span><span>Environment</span><span>Your access</span><span />
              </div>
              <ul className="resource-list catalog-list">
                {groups.map((group) => {
                  const granted = group.items.filter((entry) => entry.entitled).length
                  const pending = group.items.filter((entry) => entry.requestState === 'pending').length
                  return (
                    <ResourceRow key={group.key} title={group.title}
                      subtitle={<>{isModelResource(group.items[0].kind) ? 'Model' : 'MCP server'}
                        {group.items.length > 1 && ` · ${group.items.length} options`}</>}
                      environments={<>{uniqueValues(group.items.map((entry) => entry.azureCloud ?? 'commercial')).map((cloud) =>
                        <CloudBadge key={cloud} cloud={cloud} />)}{uniqueValues(group.items.map((entry) => entry.environment)).map((key) =>
                        <EnvironmentBadge key={key ?? 'unclassified'} environment={key} environments={environments.data} />)}</>}
                      status={<>
                        <span>{granted > 0 ? (group.items.length === 1 ? 'Access granted' : `${granted} of ${group.items.length} options granted`) : 'Not granted'}</span>
                        {pending > 0 && <small>{pending} {pending === 1 ? 'request' : 'requests'} pending</small>}
                      </>}
                      action="View options" actionLabel={`View access options for ${group.title}`}
                      onSelect={() => { setSelectedId(group.key); setOfferingId(null) }} />
                  )
                })}
              </ul>
            </>
          )}
          {selected && <DetailPanel title={`Access to ${selected.title}`} onClose={() => setSelectedId(null)}>
            {selected.items.length > 1 && (
              <label className="detail-select">
                <span>Access option</span>
                <select value={offering ? resourceKey(resourceFromCatalog(offering)) : ''}
                  onChange={(event) => setOfferingId(event.currentTarget.value || null)}>
                  <option value="">Choose an environment and offering</option>
                  {selected.items.map((entry) => (
                    <option key={resourceKey(resourceFromCatalog(entry))} value={resourceKey(resourceFromCatalog(entry))}>
                      {cloudLabel(entry.azureCloud)} · {environmentLabel(environments.data, entry.environment)} · {entry.displayName}
                      {entry.model?.version ? ` · ${entry.model.version}` : ''}
                      {entry.apiStyle ? ` · ${apiStyleLabel(entry.apiStyle)}` : ''}
                      {entry.gatewayName ? ` · ${entry.gatewayName}` : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {offering ? <div key={resourceKey(resourceFromCatalog(offering))} className="drawer-content">
              <div className="badge-row">
                <EnvironmentBadge azureCloud={offering.azureCloud ?? 'commercial'} environment={offering.environment} environments={environments.data} />
                <CatalogEnforcementBadge entry={offering} />
                <CatalogCapacityBadge entry={offering} />
              </div>
              <p className="resource-context">{entryDescription(offering)}</p>
              {offering.summary && <p>{offering.summary}</p>}
              <p className="connection-note">Choose the cost center for your calls. An administrator reviews your request.</p>
              <CatalogAction entry={offering} />
            </div> : <p className="connection-note">Select an option to check access or make a request.</p>}
          </DetailPanel>}
        </>
      )}
    </>
  )
}
