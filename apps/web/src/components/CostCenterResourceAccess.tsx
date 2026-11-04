import { Badge, Button, Field, Input, MessageBar, MessageBarBody, Select, Text, Title3 } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { describeCostCenterAllowance, exactCostCenterResourceKey } from '../cost-centers'
import { describeLimits } from '../entitlement-limits'
import { holdsApi, type PublicationRecord } from '../publication-state'
import type { CostCenter, CostCenterLimit, CostCenterResourceAccess as ResourceAccess, CostCenterResourceAccessMode, ModelAccessSettings } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { EntitlementAccessState } from './EntitlementAccessState'
import { ResourceWorkbench } from './ResourceWorkbench'
import styles from './CostCenterSetup.module.css'

interface ResourceOption {
  resource: CostCenterLimit['resource']
  name: string
  context: string
  available: boolean
  automatic: boolean
  reason: string
  reviewLink?: string
  applyState?: string
  gatewayId?: string
  error?: string | null
  callerConfiguration?: string
  appliedCallerConfiguration?: string
}

const MODE_LABELS: Record<CostCenterResourceAccessMode, string> = {
  automatic: 'Automatic',
  requestOnly: 'Request only',
  notAvailable: 'Not available',
}

function offered(value: PublicationRecord | undefined) {
  return Boolean(value && holdsApi(value))
}

function callerConfiguration(settings: ModelAccessSettings | null | undefined) {
  if (!settings) return 'Governed caller access not configured'
  return [settings.entraEnabled && 'Entra tokens', settings.keysEnabled && 'Individual subscription keys'].filter(Boolean).join(' · ')
    || 'No caller credential enabled'
}

export function CostCenterResourceAccessFields({ value, onChange, costCenter, disabled = false, keysAllowed = costCenter?.keysAllowed ?? true }: {
  value: ResourceAccess[]
  onChange?: (value: ResourceAccess[]) => void
  costCenter?: CostCenter
  disabled?: boolean
  keysAllowed?: boolean
}) {
  const api = useMosaicApi()
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const models = useQuery({ queryKey: ['model-apis'], queryFn: () => api.listModelApis() })
  const mcps = useQuery({ queryKey: ['mcp-servers'], queryFn: () => api.listMcpServers() })
  const pools = useQuery({ queryKey: ['model-pools', 'list'], queryFn: () => api.listModelPools() })
  const publications = useQuery({ queryKey: ['publications'], queryFn: () => api.listPublications() })
  const mcpPublications = useQuery({ queryKey: ['mcp-publications'], queryFn: () => api.listMcpPublications() })
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })
  const grants = useQuery({
    queryKey: ['entitlements', { costCenter: costCenter?.id }],
    queryFn: () => api.listEntitlements({ costCenter: costCenter!.id }),
    enabled: Boolean(costCenter?.id),
  })
  const resources = useMemo<ResourceOption[]>(() => {
    const items: ResourceOption[] = [
      ...(models.data ?? []).map((model) => {
        const publication = publications.data?.find((item) => item.id === model.publicationId
          && item.gatewayId === model.gatewayId && item.apiName === model.apiName)
        const gateway = gateways.data?.find((item) => item.id === model.gatewayId)
        const government = publication?.azureCloud === 'government' || gateway?.azureCloud === 'government'
        const available = Boolean(gateway) && offered(publication) && !government && Boolean(publication?.governedAccess)
          && (publication?.governedAccess?.entraEnabled === true || (publication?.governedAccess?.keysEnabled === true && keysAllowed))
        const automatic = available && publication?.governedAccess?.entraEnabled === true
        return {
          resource: { kind: 'modelApi' as const, id: model.id },
          name: model.displayName, context: 'Model API', available, automatic,
          reason: government ? 'Government resources are unavailable in this workflow.'
            : !gateway ? 'Gateway metadata is unavailable. Verify this exact offering before enabling access.'
            : !offered(publication) ? 'Requires an available MOSAIC-published model API.'
              : !publication?.governedAccess ? 'Configure governed model access before enabling this offering for a custom center.'
                : !available ? 'No supported credential is enabled for both the resource and this center. Keys are not automatically enabled.'
              : !automatic ? 'Enable caller Entra tokens in model access settings, then review and apply the resource plan. Backend API-key authentication is independent.' : '',
          reviewLink: publication ? `/entitlements?tab=settings&selected=${encodeURIComponent(publication.id)}` : undefined,
          applyState: publication?.accessState, gatewayId: model.gatewayId, error: publication?.lastError,
          callerConfiguration: callerConfiguration(publication?.governedAccess),
          appliedCallerConfiguration: publication?.appliedAccess ? callerConfiguration(publication.appliedAccess.settings) : 'No applied caller configuration recorded',
        }
      }),
      ...(pools.data ?? []).flatMap((pool) => pool.models.map((model) => {
        const gateway = gateways.data?.find((item) => item.id === pool.gatewayId)
        const government = pool.azureCloud === 'government' || gateway?.azureCloud === 'government'
        const served = offered(pool) && Boolean(pool.appliedModelIds?.includes(model.id))
        const available = Boolean(gateway) && served && !government && Boolean(pool.governedAccess)
          && (pool.governedAccess?.entraEnabled === true || (pool.governedAccess?.keysEnabled === true && keysAllowed))
        const automatic = available && pool.governedAccess?.entraEnabled === true
        return {
          resource: { kind: 'poolModel' as const, id: model.id, scopeId: pool.id },
          name: model.displayName, context: `Pool model · ${pool.displayName}`, available, automatic,
          reason: government ? 'Government resources are unavailable in this workflow.'
            : !gateway ? 'Gateway metadata is unavailable. Verify this exact offering before enabling access.'
            : !served ? 'This exact pool model must be present in the last applied pool offering.'
              : !pool.governedAccess ? 'Configure governed pool access before enabling this offering for a custom center.'
                : !available ? 'No supported credential is enabled for both the pool and this center.'
              : !automatic ? 'Enable caller Entra tokens in governed pool access, then review and apply the pool plan.' : '',
          reviewLink: `/pools/${encodeURIComponent(pool.id)}`,
          applyState: pool.accessState, gatewayId: pool.gatewayId, error: pool.lastError,
          callerConfiguration: callerConfiguration(pool.governedAccess),
          appliedCallerConfiguration: pool.appliedAccess ? callerConfiguration(pool.appliedAccess.settings) : 'No applied caller configuration recorded',
        }
      })),
      ...(mcps.data ?? []).map((mcp) => {
        const publication = mcpPublications.data?.find((item) => item.id === mcp.publicationId
          && item.mcpServerId === mcp.id && item.gatewayId === mcp.gatewayId && item.apiName === mcp.apiName)
        const gateway = gateways.data?.find((item) => item.id === mcp.gatewayId)
        const government = publication?.azureCloud === 'government' || gateway?.azureCloud === 'government'
        const available = Boolean(gateway) && offered(publication) && !government
        return {
          resource: { kind: 'mcpServer' as const, id: mcp.id },
          name: mcp.displayName, context: 'MCP server', available, automatic: available,
          reason: government ? 'Government resources are unavailable in this workflow.'
            : !gateway ? 'Gateway metadata is unavailable. Verify this exact offering before enabling access.'
            : !available ? 'Requires an available MOSAIC-published MCP server; an imported-only server is not supported.' : '',
          reviewLink: `/mcps?tab=published&selected=${encodeURIComponent(publication?.id ?? mcp.id)}`,
          applyState: publication?.accessState, gatewayId: mcp.gatewayId, error: publication?.lastError,
          callerConfiguration: government ? 'Unavailable for custom centers'
            : publication ? 'Caller Entra tokens only · Backend credentials are independent' : 'No supported linked publication',
          appliedCallerConfiguration: government ? 'Outside this workflow'
            : publication?.appliedAccess ? 'Caller Entra tokens only (commercial MCP)' : 'No applied caller configuration recorded',
        }
      }),
    ]
    for (const definition of value) {
      if (items.some((item) => exactCostCenterResourceKey(item.resource) === exactCostCenterResourceKey(definition.resource))) continue
      items.push({
        resource: definition.resource, name: `Unavailable ${definition.resource.kind}`,
        context: `${definition.resource.id}${definition.resource.scopeId ? ` · Pool ${definition.resource.scopeId}` : ''}`,
        available: false, automatic: false, reason: 'Resource metadata is unavailable. The saved definition is retained until you explicitly change it.',
      })
    }
    return items
  }, [models.data, mcps.data, pools.data, publications.data, mcpPublications.data, gateways.data, keysAllowed, value])
  const byKey = new Map(value.map((item) => [exactCostCenterResourceKey(item.resource), item.mode]))
  const visible = resources.filter((item) => exactCostCenterResourceKey(item.resource) === selected
    || `${item.name} ${item.context} ${item.resource.id} ${item.resource.scopeId ?? ''}`.toLowerCase().includes(filter.toLowerCase()))
  const queries = [models, mcps, pools, publications, mcpPublications, gateways]
  const errors = queries.filter((query) => query.isError)
  const loading = queries.some((query) => query.isPending)
  return <section className={styles.stack} aria-label="Exact resource access">
    <Title3 as="h3">Resource access</Title3>
    <Text className={styles.prose}>
      Automatic uses one stable group grant per exact offering, not a grant or key for every member.
      Request only uses individual approval. Missing custom-center definitions mean Not available.
      Model and MCP authorization remain independent.
    </Text>
    <MessageBar intent="info"><MessageBarBody>
      Saving access intent is not gateway apply. Review the affected model, pool, or MCP plan separately.
      Membership edits do not create individual automatic grants or require an automatic-only policy rebuild.
      The API also verifies deployment-level group claims; this screen cannot prove that prerequisite from resource metadata.
    </MessageBarBody></MessageBar>
    {loading && <Loading label="Loading exact resource offerings" />}
    {errors.map((query, index) => <div key={index} className={styles.stack}><ErrorState title="Resource metadata unavailable" error={query.error} />
      <Button type="button" onClick={() => void query.refetch()}>Retry resource metadata</Button></div>)}
    {costCenter && grants.isError && <div className={styles.stack}><ErrorState title="Managed grants unavailable" error={grants.error} />
      <Button type="button" onClick={() => void grants.refetch()}>Retry managed grants</Button></div>}
    <ResourceWorkbench title="Exact offerings" boundedList
      description="Pool models stay scoped to both their model ID and pool ID."
      toolbar={<Field label="Filter resource offerings"><Input value={filter} onChange={(_, data) => setFilter(data.value)} /></Field>}
      items={visible} getId={(item) => exactCostCenterResourceKey(item.resource)} selectedId={selected} onSelect={setSelected}
      listLabel="Exact resource offerings" detailLabel="Selected resource access"
      emptyList={<Text>{loading ? 'Loading offerings…' : 'No offerings match. Saved definitions are never silently removed.'}</Text>}
      emptySelection={<Text>Select an offering to inspect its access mode, allowance, and reviewed-apply state.</Text>}
      renderListItem={(item) => <span className={styles.fields}>
        <strong>{item.name}</strong><span className={styles.muted}>{item.context}</span>
        <span><Badge appearance="outline">{MODE_LABELS[byKey.get(exactCostCenterResourceKey(item.resource)) ?? 'notAvailable']}</Badge></span>
      </span>}
      renderDetail={(item) => {
        const key = exactCostCenterResourceKey(item.resource)
        const mode = byKey.get(key) ?? 'notAvailable'
        const limit = costCenter?.limits.find((entry) => exactCostCenterResourceKey(entry.resource) === key)
        const managed = grants.data?.filter((grant) => grant.managedBy === 'costCenterAutomatic' && exactCostCenterResourceKey(grant.resource as CostCenterLimit['resource']) === key) ?? []
        return <div className={styles.stack}>
          <Title3 as="h4">{item.name}</Title3>
          <Text>{item.context}</Text>
          <Field label={`Access mode for ${item.name}`}>
            <Select value={mode} disabled={disabled || !onChange || loading || errors.length > 0} onChange={(_, data) => {
              const next = data.value as CostCenterResourceAccessMode
              onChange?.([...value.filter((entry) => exactCostCenterResourceKey(entry.resource) !== key), { resource: item.resource, mode: next }])
            }}>
              <option value="automatic" disabled={!item.automatic}>Automatic</option>
              <option value="requestOnly" disabled={!item.available}>Request only</option>
              <option value="notAvailable">Not available</option>
            </Select>
          </Field>
          {item.reason && <Text>{item.reason}</Text>}
          <dl className={styles.facts}>
            <div><dt>Exact resource ID</dt><dd>{item.resource.id}</dd></div>
            {item.resource.scopeId && <div><dt>Pool scope ID</dt><dd>{item.resource.scopeId}</dd></div>}
            <div><dt>Cost-center allowance</dt><dd>{describeCostCenterAllowance(limit)}</dd></div>
            <div><dt>Center individual keys</dt><dd>{keysAllowed ? 'Allowed when the offering enables them; never inherited from Automatic access' : 'Off for this center'}</dd></div>
            {item.callerConfiguration && <div><dt>Saved resource caller configuration</dt><dd>{item.callerConfiguration}</dd></div>}
            {item.appliedCallerConfiguration && <div><dt>Last applied caller configuration</dt><dd>{item.appliedCallerConfiguration}</dd></div>}
            {item.gatewayId && <div><dt>Gateway</dt><dd>{item.gatewayId}</dd></div>}
            <div><dt>Resource apply state</dt><dd>{item.applyState ?? 'Not reported'} · not a live authorization test</dd></div>
          </dl>
          {item.error && <MessageBar intent="warning"><MessageBarBody>{item.error} Review the resource outcome before another apply.</MessageBarBody></MessageBar>}
          {costCenter && grants.isPending && <Text role="status">Loading managed grant state…</Text>}
          {managed.map((grant) => <section key={grant.id} className={styles.section} aria-label="Managed group grant">
            <Text weight="semibold">Managed automatic group grant</Text>
            <Text>Grant ID: {grant.id}</Text>
            {describeLimits(grant).map((text) => <Text key={text}>{text}</Text>)}
            <EntitlementAccessState entitlement={grant} />
          </section>)}
          {costCenter && grants.isSuccess && mode === 'automatic' && managed.length === 0 && <Text>
            No managed group grant is recorded. Finish verified group setup and refresh; saved intent is not proof of active access.
          </Text>}
          {item.reviewLink && <Link to={item.reviewLink}>Review resource configuration and plan/apply</Link>}
        </div>
      }} />
  </section>
}
