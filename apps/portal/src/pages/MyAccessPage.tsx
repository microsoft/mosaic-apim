import {
  Badge,
  Button,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Text,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { BudgetBanner } from '../components/BudgetBanner'
import { ConnectionDetails } from '../components/ConnectionDetails'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { DetailPanel } from '../components/DetailPanel'
import { ResourceRow } from '../components/ResourceRow'
import { grantTitle, groupResources, modelGroupKey, uniqueValues } from '../model-presentation'
import {
  describeAttribution,
  describeBinding,
  describeLimits,
  describeRuntime,
  gatewayLabel,
  isModelResource,
  isSecurityGroupGrant,
  withResourceKind,
} from '../entitlement-format'
import { environmentLabel, usePortalEnvironments } from '../environments'
import type { ResolvedEntitlement } from '../types'

function costCenterText(resolved: ResolvedEntitlement) {
  return resolved.costCenter ? `${resolved.costCenter.name} · ${resolved.costCenter.code}` : 'Default cost center'
}

export function MyAccessPage() {
  const api = usePortalApi()
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [costCenterFilter, setCostCenterFilter] = useState('all')
  const profile = useQuery({ queryKey: ['portal', 'profile'], queryFn: api.getProfile })
  const entitlements = useQuery({
    queryKey: ['portal', 'entitlements'],
    queryFn: api.listEntitlements,
  })
  const visibleEntitlements =
    entitlements.data
      ?.filter((resolved) => resolved.effective !== false)
      .sort((a, b) => {
        const nameA = grantTitle(a)
        const nameB = grantTitle(b)
        return nameA.localeCompare(nameB) || costCenterText(a).localeCompare(costCenterText(b))
      }) ?? []
  const environments = usePortalEnvironments()
  const selected = visibleEntitlements.find((resolved) => resolved.entitlement.id === params.get('grant'))
  const describe = (resolved: ResolvedEntitlement) => ({
    key: modelGroupKey(resolved.entitlement.resource, resolved.model), title: grantTitle(resolved),
  })
  const allGroups = groupResources(visibleEntitlements, describe)
  const selectedGroup = allGroups.find((group) =>
    selected ? group.items.includes(selected) : group.key === params.get('resource'))
  const filtered = visibleEntitlements.filter((resolved) => {
    const environment = resolved.resourceSummary?.environment ?? null
    return (environmentFilter === 'all' || (environmentFilter === 'unclassified' ? environment === null : environment === environmentFilter)) &&
      (costCenterFilter === 'all' || (resolved.costCenter?.id ?? 'default') === costCenterFilter) &&
      `${grantTitle(resolved)} ${resolved.resourceDisplayName ?? ''} ${resolved.resourceSummary?.displayName ?? ''} ${resolved.resourceSummary?.gatewayName ?? ''} ${costCenterText(resolved)} ${resolved.viaGroupName ?? ''}`
        .toLowerCase().includes(search.trim().toLowerCase())
  })
  const groups = groupResources(filtered, describe)
  const costCenters = [...new Map(visibleEntitlements.map((resolved) =>
    [resolved.costCenter?.id ?? 'default', costCenterText(resolved)])).entries()]
  const environmentKeys = uniqueValues(visibleEntitlements.map((resolved) => resolved.resourceSummary?.environment ?? null))
  function selectGrant(id: string | null) {
    const next = new URLSearchParams(params)
    next.delete('resource')
    if (id) next.set('grant', id)
    else next.delete('grant')
    setParams(next)
  }
  const closeDetails = () => selectGrant(null)
  function selectGroup(key: string, items: ResolvedEntitlement[]) {
    if (items.length === 1) return selectGrant(items[0].entitlement.id)
    const next = new URLSearchParams(params)
    next.delete('grant')
    next.set('resource', key)
    setParams(next)
  }

  return (
    <>
      <PageHeader
        title="My access"
        description="Your models, servers, and connection details."
      />
      <BudgetBanner />
      {profile.data?.groupsOverage && (
        <MessageBar intent="warning" className="access-overage-notice">
          <MessageBarBody>
            <MessageBarTitle>Group access may be missing</MessageBarTitle>
            You belong to too many groups for your sign-in token to list them, so access granted to
            your groups can&apos;t be applied. Ask an administrator for a direct grant.
          </MessageBarBody>
        </MessageBar>
      )}
      {entitlements.isLoading && <Loading label="Loading your access" />}
      {entitlements.isError && <ErrorState error={entitlements.error} />}
      {entitlements.isSuccess && params.has('grant') && !selected && (
        <MessageBar intent="warning">
          <MessageBarBody>This grant is not in your current access. <Button appearance="transparent" onClick={closeDetails}>Dismiss</Button></MessageBarBody>
        </MessageBar>
      )}
      {entitlements.isSuccess && visibleEntitlements.length === 0 && (
        <EmptyState title="No access granted yet">
          Your account has the portal role, but no model APIs or MCP servers are entitled to you yet.
          {' '}<Link to="/catalog">Discover resources in the catalog</Link>.
        </EmptyState>
      )}
      {entitlements.isSuccess && visibleEntitlements.length > 0 && (
        <>
          <div className="filter-row" role="group" aria-label="My access filters">
            <Input className="resource-search" aria-label="Search my access" placeholder="Search your models and servers"
              value={search} onChange={(_, data) => setSearch(data.value)} />
            <label><span>Environment</span>
              <select value={environmentFilter} onChange={(event) => setEnvironmentFilter(event.currentTarget.value)}>
                <option value="all">All environments</option>
                {environmentKeys.map((key) => <option key={key ?? 'unclassified'} value={key ?? 'unclassified'}>
                  {environmentLabel(environments.data, key)}
                </option>)}
              </select>
            </label>
            <label><span>Cost center</span>
              <select value={costCenterFilter} onChange={(event) => setCostCenterFilter(event.currentTarget.value)}>
                <option value="all">All cost centers</option>
                {costCenters.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
              </select>
            </label>
          </div>
          {groups.length === 0 ? <EmptyState title="No access matches these filters">
            <Button onClick={() => { setSearch(''); setEnvironmentFilter('all'); setCostCenterFilter('all') }}>Clear filters</Button>
          </EmptyState> : <>
            <div className="resource-list-caption">{groups.length} {groups.length === 1 ? 'resource' : 'resources'} · {filtered.length} {filtered.length === 1 ? 'grant' : 'grants'}</div>
            <div className="resource-list-heading" aria-hidden="true">
              <span>Model or server</span><span>Environment</span><span>Gateway state</span><span />
            </div>
            <ul className="resource-list access-list">
              {groups.map((group) => {
                const statuses = uniqueValues(group.items.map((resolved) => describeRuntime(resolved.entitlement)))
                const unavailable = group.items.filter((resolved) => resolved.resourceSummary?.available === false).length
                return <ResourceRow key={group.key} title={group.title}
                  subtitle={<>{group.items.length} {group.items.length === 1 ? 'grant' : 'grants'} · {uniqueValues(group.items.map(costCenterText)).join(', ')}</>}
                  environments={uniqueValues(group.items.map((resolved) => resolved.resourceSummary?.environment ?? null)).map((key) =>
                    <EnvironmentBadge key={key ?? 'unclassified'} environment={key} environments={environments.data} />)}
                  status={<>
                    <span>{statuses.length === 1 ? statuses[0] : 'Mixed gateway state'}</span>
                    {unavailable > 0 && <Badge appearance="outline" color="warning">No longer available{group.items.length > 1 ? ` (${unavailable})` : ''}</Badge>}
                    {statuses.length > 1 && <small>{statuses.join(' · ')}</small>}
                  </>}
                  action="View access" actionLabel={`View access for ${group.title}`}
                  onSelect={() => selectGroup(group.key, group.items)} />
              })}
            </ul>
          </>}
        </>
      )}
      {selectedGroup && (
          <DetailPanel title={`Access to ${selectedGroup.title}`} onClose={closeDetails}>
            {selectedGroup.items.length > 1 && <label className="detail-select">
              <span>Grant</span>
              <select value={selected?.entitlement.id ?? ''} onChange={(event) => {
                if (event.currentTarget.value) selectGrant(event.currentTarget.value)
                else selectGroup(selectedGroup.key, selectedGroup.items)
              }}>
                <option value="">Choose a cost center and grant</option>
                {selectedGroup.items.map((resolved) => <option key={resolved.entitlement.id} value={resolved.entitlement.id}>
                  {costCenterText(resolved)} · {environmentLabel(environments.data, resolved.resourceSummary?.environment ?? null)}
                  {` · ${resolved.resourceDisplayName ?? resolved.resourceSummary?.displayName ?? grantTitle(resolved)}`}
                  {resolved.model?.version && ` · ${resolved.model.version}`}
                  {` · ${describeAttribution(resolved)} · ${describeRuntime(resolved.entitlement)}`}
                </option>)}
              </select>
            </label>}
            {selected ? <div key={selected.entitlement.id} className="drawer-content">
              <div className="badge-row">
                <Badge appearance="tint">{costCenterText(selected)}</Badge>
                <EnvironmentBadge environment={selected.resourceSummary?.environment ?? null} environments={environments.data} />
                <Badge appearance="outline">{describeRuntime(selected.entitlement)}</Badge>
              </div>
              <Text>{withResourceKind(selected.entitlement.resource, selected.resourceDisplayName,
                describeAttribution(selected), selected.resourceSummary)}</Text>
              <p>{runtimeGuidance(selected)}</p>
              <div className="access-card-grid">
                <section>
                  <h3>Configured grant limits</h3>
                  <ul className="plain-list">
                    {describeLimits(selected.entitlement).map((limit) => (
                      <li key={limit}>{limit}</li>
                    ))}
                  </ul>
                  <Text size={200}>
                    {selected.entitlement.resource.kind === 'poolModel' ? 'Model' : 'Publication'} and
                    gateway limits may also apply. Pending changes are not yet enforced by the gateway.
                    {isSecurityGroupGrant(selected) && ' Group access limits apply to each person individually.'}
                  </Text>
                </section>
                <section>
                  <h3>Usage attribution</h3>
                  <Text>{describeBinding(selected.entitlement)}</Text>
                  <dl className="compact-facts">
                    <div>
                      <dt>Gateway</dt>
                      <dd>{gatewayLabel(selected.resourceSummary)}</dd>
                    </div>
                  </dl>
                </section>
              </div>
              {(isModelResource(selected.entitlement.resource.kind) ||
                selected.entitlement.resource.kind === 'mcpServer') && (
                <ConnectionDetails resolved={selected} />
              )}
            </div> : <p className="connection-note">Select a grant to see its limits and connection details.</p>}
          </DetailPanel>
      )}
    </>
  )
}

function runtimeGuidance(resolved: ResolvedEntitlement) {
  switch (resolved.entitlement.runtime?.status) {
    case 'pending': return 'Waiting for an administrator to review and apply gateway access.'
    case 'applying': return 'Gateway access is being applied. Check again before connecting.'
    case 'applied': return 'Gateway configuration is applied. Connection details show how to make a call; they do not confirm connectivity.'
    case 'failed': return 'Gateway access could not be applied. Ask an administrator to review the failure.'
    case 'revocationPending': return 'Removal is awaiting application at the gateway.'
    case 'revoked': return 'Gateway access has been revoked. Request new access from the catalog if needed.'
    default: return 'Gateway readiness has not been confirmed. Check connection details or ask an administrator.'
  }
}
