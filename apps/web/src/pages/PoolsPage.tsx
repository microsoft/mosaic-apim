import { Button, Field, MessageBar, MessageBarBody, Select, Text } from '@fluentui/react-components'
import { AddRegular, LayerRegular, LightbulbRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { Fragment, useMemo } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { PoolStatusBadge, ReadinessSummaryBadge, UnappliedChangesBadge } from '../components/PoolBadges'
import { PoolEditorDialog } from '../components/PoolEditorDialog'
import { ResourceWorkbench } from '../components/ResourceWorkbench'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { useWorkbenchLocation } from '../components/useWorkbenchLocation'
import { useEnvironmentCatalog } from '../environments'
import { plural } from '../labels'
import { poolFamilyLabel, poolPrefill } from '../pools'
import type { ModelPool, ModelPoolSummary, PoolSuggestion } from '../types'
import { PoolDetailPage } from './PoolDetailPage'
import styles from './PoolsPage.module.css'

const poolSections = [
  { value: 'pools', label: 'Pools', icon: <LayerRegular /> },
  { value: 'suggestions', label: 'Suggested pools', icon: <LightbulbRegular /> },
]

function suggestionId(item: PoolSuggestion) {
  return `${item.gatewayId}|${item.vendor ?? ''}|${item.apiShape}`
}

function SuggestedPoolEditor({ suggestion, pools, onClose, onSaved }: {
  suggestion: PoolSuggestion
  pools: ModelPoolSummary[]
  onClose: () => void
  onSaved: (pool: ModelPool, review: boolean) => void
}) {
  const api = useMosaicApi()
  const candidates = useQuery({
    queryKey: ['model-pools', 'candidates', suggestion.gatewayId],
    queryFn: () => api.getPoolCandidates(suggestion.gatewayId),
  })
  if (candidates.isPending) return <Loading label="Loading suggested deployments" />
  if (candidates.isError) return <><Button onClick={onClose}>Back to suggested pools</Button>
    <ErrorState error={candidates.error} title="MOSAIC couldn’t load the deployments for that pool" /></>
  const prefill = poolPrefill(suggestion, candidates.data, pools.map((item) => item.pool))
  return <PoolEditorDialog initialGatewayId={prefill.gatewayId} prefill={prefill}
    onClose={onClose} onSaved={onSaved} />
}

export function PoolsPage() {
  const [section, selectSection] = usePageSection(poolSections)
  const api = useMosaicApi()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const workbench = useWorkbenchLocation({ section })
  const catalog = useEnvironmentCatalog()
  const gatewayFilter = params.get('gateway') ?? 'all'

  const summaries = useQuery({ queryKey: ['model-pools', 'summaries'], queryFn: () => api.listModelPoolSummaries() })
  const suggestions = useQuery({
    queryKey: ['model-pools', 'suggestions'],
    queryFn: () => api.listModelPoolSuggestions(),
    enabled: section === 'suggestions',
  })
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })
  const gatewayOptions = useMemo(() => {
    const names = new Map<string, string>()
    for (const summary of summaries.data ?? []) names.set(summary.pool.gatewayId, summary.gatewayName ?? summary.pool.gatewayId)
    return [...names].sort((left, right) => left[1].localeCompare(right[1]))
  }, [summaries.data])
  const all = summaries.data ?? []
  const shown = gatewayFilter === 'all' ? all : all.filter((summary) => summary.pool.gatewayId === gatewayFilter)
  const suggested = (suggestions.data ?? []).filter((item) => gatewayFilter === 'all' || item.gatewayId === gatewayFilter)
  const selectedSuggestion = suggested.find((item) => suggestionId(item) === workbench.selectedId)
  const noGateways = gateways.data?.length === 0

  function saved(pool: ModelPool, review: boolean) {
    const next = new URLSearchParams(params)
    next.set('tab', 'pools')
    next.delete('workbench')
    next.set('selected', pool.id)
    next.set('poolTab', 'overview')
    if (review) next.set('mode', 'review')
    else next.delete('mode')
    if (gatewayFilter !== 'all' && pool.gatewayId !== gatewayFilter) next.delete('gateway')
    navigate({ pathname: '/pools', search: `?${next}` }, { replace: true, preventScrollReset: true })
  }

  const filter = gatewayOptions.length > 1 && <Field label="Gateway">
    <Select aria-label="Filter pools by gateway" value={gatewayFilter} onChange={(event) => {
      const next = new URLSearchParams(params)
      next.set('gateway', event.target.value)
      setParams(next)
    }}>
      <option value="all">All gateways</option>
      {gatewayOptions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
    </Select>
  </Field>
  const newAction = <Button appearance="primary" icon={<AddRegular />} disabled={!gateways.data || noGateways}
    onClick={() => workbench.open('new', null)}>Create pool</Button>

  let body
  if (workbench.mode === 'new') {
    body = section === 'suggestions' && workbench.selectedId
      ? selectedSuggestion
        ? summaries.isPending ? <Loading label="Loading existing pools" />
          : summaries.isError ? <><Button onClick={() => workbench.close()}>Back to suggested pools</Button><ErrorState error={summaries.error} /></>
          : <SuggestedPoolEditor suggestion={selectedSuggestion} pools={all} onClose={() => workbench.close()} onSaved={saved} />
        : suggestions.isPending ? <Loading label="Loading suggested pool" />
          : <><Button onClick={() => workbench.close()}>Back to suggested pools</Button>
            {suggestions.isError ? <ErrorState error={suggestions.error} /> : <EmptyState title="Suggestion no longer available">Return to the list and choose another suggestion.</EmptyState>}</>
      : <PoolEditorDialog initialGatewayId={gatewayFilter === 'all' ? null : gatewayFilter}
          onClose={() => workbench.close()} onSaved={saved} />
  } else if (section === 'pools' && (workbench.mode === 'edit' || workbench.mode === 'review') && workbench.selectedId) {
    body = <PoolDetailPage poolId={workbench.selectedId} embedded />
  } else if (section === 'pools') {
    body = <ResourceWorkbench title="Pools" actions={newAction} toolbar={filter}
      description={`${shown.length} of ${plural(all.length, 'pool')}`}
      items={shown} getId={(item) => item.pool.id} selectedId={workbench.selectedId} onSelect={workbench.select}
      listLabel="Model pools"
      emptyList={summaries.isPending ? <Loading label="Loading pools" /> : summaries.isError
        ? <ErrorState error={summaries.error} /> : all.length
          ? <EmptyState title="No pools match this gateway">Choose another gateway or show all gateways.</EmptyState>
          : <EmptyState title="No pools yet">Create a pool to serve models across endpoints and regions through one API.</EmptyState>}
      renderListItem={(item) => <div className={styles.resourceRow}>
        <Text weight="semibold">{item.pool.displayName}</Text>
        <Text size={200} className={styles.muted}>{item.gatewayName ?? item.pool.gatewayId} · {plural(item.pool.models.length, 'model')}</Text>
        <div className={styles.rowBadges}><PoolStatusBadge pool={item.pool} />
          <ReadinessSummaryBadge readiness={item.readiness} />
          {item.unappliedChanges && <UnappliedChangesBadge />}</div>
      </div>}
      renderDetail={(item) => <PoolDetailPage key={item.pool.id} poolId={item.pool.id} embedded />}
      missingSelection={<EmptyState title="Pool not in this list">It may have been removed or excluded by the gateway filter. Clear the selection or change the filter.</EmptyState>}
    />
  } else {
    body = <ResourceWorkbench title="Suggested pools" toolbar={filter}
      description="Models deployed on multiple reachable endpoints. Select a suggestion to review its members."
      items={suggested} getId={suggestionId} selectedId={workbench.selectedId} onSelect={workbench.select}
      listLabel="Suggested pools"
      emptyList={suggestions.isPending ? <Loading label="Loading suggested pools" /> : suggestions.isError
        ? <ErrorState error={suggestions.error} title="Couldn’t work out which pools to suggest" />
        : <EmptyState title="No suggested pools">No additional models are deployed on multiple endpoints this gateway can reach.</EmptyState>}
      renderListItem={(item) => <div className={styles.resourceRow}>
        <Text weight="semibold">{poolFamilyLabel(item.vendor, item.apiShape)}</Text>
        <Text size={200} className={styles.muted}>{item.gatewayName} · {plural(item.models.length, 'model')}</Text>
        <EnvironmentBadge azureCloud={item.azureCloud ?? 'commercial'} environment={item.gatewayEnvironment ?? null} catalog={catalog.data} size="small" />
      </div>}
      renderDetail={(item) => <section className={styles.suggestionDetail}>
        <div className={styles.detailHeading}><div>
          <h2>{poolFamilyLabel(item.vendor, item.apiShape)}</h2>
          <Text>{item.gatewayName}</Text>
        </div><Button appearance="primary" icon={<AddRegular />}
          aria-label={`Create pool for ${item.vendor || 'these models'} on ${item.gatewayName}`}
          onClick={() => workbench.open('new', suggestionId(item))}>Create pool</Button></div>
        <EnvironmentBadge azureCloud={item.azureCloud ?? 'commercial'} environment={item.gatewayEnvironment ?? null} catalog={catalog.data} />
        <ul className={styles.suggestedModels}>{item.models.map((model) =>
          <li key={`${model.modelFormat ?? ''}|${model.modelName}`}>
            <Text weight="semibold" block>{model.modelName}</Text>
            <Text size={200}>{plural(model.deploymentCount, 'deployment')} on {plural(model.endpointCount, 'endpoint')}
              {model.regions.length > 0 && ` · ${model.regions.join(', ')}`}</Text>
          </li>)}</ul>
        {item.familyPools.length > 0 && <p className={styles.familyNote}>Or add {item.models.length === 1 ? 'it' : 'them'} to{' '}
          {item.familyPools.map((pool, index) => <Fragment key={pool.id}>
            {index > 0 && (index === item.familyPools.length - 1 ? ' or ' : ', ')}
            <Link className={styles.poolLink} to={`/pools/${pool.id}`}>{pool.displayName}</Link>
          </Fragment>)}, which already {item.familyPools.length === 1 ? 'serves' : 'serve'}{' '}
          {item.vendor ? `${item.vendor} models` : 'models from the same vendor'} on this gateway.</p>}
        <Text className={styles.muted}>The editor starts with deployments the gateway can use. Saving records intent; review and apply a plan separately.</Text>
      </section>}
    />
  }

  return <div className={styles.page}>
    <PageHeader title="Model pools" description="Serve models across endpoints and regions through one API. Saved intent, reviewed plans, and applied gateway state stay separate." />
    {summaries.isError && summaries.data && <ErrorState error={summaries.error} />}
    {section === 'suggestions' && suggestions.isError && suggestions.data && <ErrorState error={suggestions.error} />}
    {noGateways && <MessageBar intent="info"><MessageBarBody>A pool runs on an API Management gateway.{' '}
      <Link to="/gateways">Onboard a gateway</Link> first, then sync its endpoints.</MessageBarBody></MessageBar>}
    <SectionLayout label="Pools" sections={poolSections} active={section} onSelect={selectSection}>
      {body}
    </SectionLayout>
  </div>
}
