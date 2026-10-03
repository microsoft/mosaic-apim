import {
  Button,
  Card,
  Field,
  MessageBar,
  MessageBarBody,
  Select,
  Text,
  Title3,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { PoolStatusBadge, ReadinessSummaryBadge, UnappliedChangesBadge } from '../components/PoolBadges'
import { PoolEditorDialog } from '../components/PoolEditorDialog'
import { useEnvironmentCatalog } from '../environments'
import { plural } from '../labels'
import {
  POOL_TYPE_LABELS,
  capacitySummary,
  poolAccessLabel,
  poolFamilyLabel,
  poolGrantCount,
  poolPrefill,
} from '../pools'
import type { PoolLocationState, PoolPrefill } from '../pools'
import type { EnvironmentCatalogView, ModelPoolSummary, PoolSuggestion } from '../types'
import styles from './PoolsPage.module.css'

function PoolRow({ summary, catalog }: { summary: ModelPoolSummary; catalog?: EnvironmentCatalogView }) {
  const { pool } = summary
  return (
    <tr>
      <td>
        <div className={styles.cellStack}>
          <Link className={styles.poolLink} to={`/pools/${pool.id}`}>
            {pool.displayName}
          </Link>
          <span className={styles.path}>/{pool.apiPath}</span>
          {pool.visibility === 'hidden' && (
            <Text size={200} className={styles.muted}>Hidden from the portal catalog</Text>
          )}
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <Text>{summary.gatewayName ?? pool.gatewayId}</Text>
          <EnvironmentBadge environment={summary.gatewayEnvironment ?? null} catalog={catalog} size="small" />
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <Text>{pool.models.length ? poolFamilyLabel(pool.vendor, pool.apiShape) : 'No models yet'}</Text>
          {pool.models.length > 0 && (
            <Text size={200} className={styles.muted}>{plural(pool.models.length, 'model')}</Text>
          )}
        </div>
      </td>
      <td>{POOL_TYPE_LABELS[pool.poolType]}</td>
      <td>{capacitySummary(summary.capacity)}</td>
      <td>
        <div className={styles.cellStack}>
          <Text>{poolAccessLabel(pool)}</Text>
          {pool.governedAccess && (
            <Text size={200} className={styles.muted}>{plural(poolGrantCount(pool), 'grant')} in force</Text>
          )}
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <ReadinessSummaryBadge readiness={summary.readiness} />
          {summary.problemCount > 0 && (
            <Text size={200} className={styles.problems}>
              {plural(summary.problemCount, 'problem')} to fix
            </Text>
          )}
          {summary.warningCount > 0 && (
            <Text size={200} className={styles.muted}>{plural(summary.warningCount, 'warning')}</Text>
          )}
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <PoolStatusBadge pool={pool} />
          {summary.unappliedChanges && <UnappliedChangesBadge />}
        </div>
      </td>
    </tr>
  )
}

function SuggestionRow({
  suggestion,
  catalog,
  busy,
  onCreate,
}: {
  suggestion: PoolSuggestion
  catalog?: EnvironmentCatalogView
  busy: boolean
  onCreate: () => void
}) {
  const restoreFocus = useRestoreFocusTarget()
  const { familyPools, models } = suggestion
  return (
    <tr>
      <td>
        <div className={styles.cellStack}>
          <Text>{suggestion.gatewayName}</Text>
          <EnvironmentBadge environment={suggestion.gatewayEnvironment ?? null} catalog={catalog} size="small" />
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <Text>{poolFamilyLabel(suggestion.vendor, suggestion.apiShape)}</Text>
          <Text size={200} className={styles.muted}>{plural(models.length, 'model')}</Text>
        </div>
      </td>
      <td>
        <ul className={styles.suggestedModels}>
          {models.map((model) => (
            <li key={`${model.modelFormat ?? ''}|${model.modelName}`} className={styles.cellStack}>
              <Text weight="semibold">{model.modelName}</Text>
              <Text size={200} className={styles.muted}>
                {plural(model.deploymentCount, 'deployment')} on {plural(model.endpointCount, 'endpoint')}
                {model.regions.length > 0 && ` · ${model.regions.join(', ')}`}
              </Text>
            </li>
          ))}
        </ul>
        {familyPools.length > 0 && (
          <Text as="p" size={200} className={styles.familyNote}>
            Or add {models.length === 1 ? 'it' : 'them'} to{' '}
            {familyPools.map((pool, index) => (
              <Fragment key={pool.id}>
                {index > 0 && (index === familyPools.length - 1 ? ' or ' : ', ')}
                <Link className={styles.poolLink} to={`/pools/${pool.id}`}>{pool.displayName}</Link>
              </Fragment>
            ))}
            , which already {familyPools.length === 1 ? 'serves' : 'serve'}{' '}
            {suggestion.vendor ? `${suggestion.vendor} models` : 'models from the same vendor'} on this gateway.
          </Text>
        )}
      </td>
      <td>
        <Button
          size="small"
          icon={<AddRegular />}
          aria-label={`Create pool for ${suggestion.vendor || 'these models'} on ${suggestion.gatewayName}`}
          disabledFocusable={busy}
          onClick={onCreate}
          {...restoreFocus}
        >
          Create pool
        </Button>
      </td>
    </tr>
  )
}

export function PoolsPage() {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const catalog = useEnvironmentCatalog()
  const [editor, setEditor] = useState<{ prefill?: PoolPrefill } | null>(null)
  const [gatewayFilter, setGatewayFilter] = useState('all')

  const summaries = useQuery({
    queryKey: ['model-pools', 'summaries'],
    queryFn: () => api.listModelPoolSummaries(),
  })
  const suggestions = useQuery({
    queryKey: ['model-pools', 'suggestions'],
    queryFn: () => api.listModelPoolSuggestions(),
  })
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })

  // A suggested pool opens in the editor with the deployments the gateway can use today.
  const createSuggested = useMutation({
    mutationFn: async (suggestion: PoolSuggestion) => {
      const candidates = await queryClient.fetchQuery({
        queryKey: ['model-pools', 'candidates', suggestion.gatewayId],
        queryFn: () => api.getPoolCandidates(suggestion.gatewayId),
      })
      return poolPrefill(suggestion, candidates, (summaries.data ?? []).map((summary) => summary.pool))
    },
    onSuccess: (prefill) => setEditor({ prefill }),
  })

  const gatewayOptions = useMemo(() => {
    const names = new Map<string, string>()
    for (const summary of summaries.data ?? []) {
      names.set(summary.pool.gatewayId, summary.gatewayName ?? summary.pool.gatewayId)
    }
    return [...names].sort((left, right) => left[1].localeCompare(right[1]))
  }, [summaries.data])

  const all = summaries.data ?? []
  const shown = gatewayFilter === 'all' ? all : all.filter((summary) => summary.pool.gatewayId === gatewayFilter)
  const suggested = (suggestions.data ?? []).filter(
    (suggestion) => gatewayFilter === 'all' || suggestion.gatewayId === gatewayFilter,
  )
  const noGateways = gateways.data?.length === 0

  return (
    <div className={styles.page}>
      <PageHeader
        title="Model pools"
        description="Serve one vendor’s models from deployments on many endpoints and regions through one API. Users see the pool’s models and never the deployments behind them; the gateway routes each request to a deployment that can take it."
        source="live"
        actions={
          <Button
            appearance="primary"
            icon={<AddRegular />}
            disabled={!gateways.data || noGateways}
            onClick={() => setEditor({})}
            {...restoreFocus}
          >
            Create pool
          </Button>
        }
      />

      {noGateways && (
        <MessageBar intent="info" className={styles.gatewayNote}>
          <MessageBarBody>
            A pool runs on an API Management gateway. <Link to="/gateways">Onboard a gateway</Link> first,
            then sync the endpoints whose deployments you want to pool.
          </MessageBarBody>
        </MessageBar>
      )}

      {summaries.isPending && <Loading label="Loading pools" />}
      {summaries.isError && <ErrorState error={summaries.error} />}
      {summaries.data?.length === 0 && (
        <EmptyState title="No pools yet">
          Create a pool to put deployments of the same models, on different endpoints or regions, behind
          one API. Breaker pools spread requests by weight and skip a deployment that throttles;
          preferential pools fill provisioned capacity first; linear pools try deployments in order.
        </EmptyState>
      )}

      {all.length > 0 && (
        <Card className={styles.listCard}>
          <div className={styles.cardHeader}>
            <div className={styles.cardHeaderText}>
              <Title3 as="h2">Pools</Title3>
              <Text size={200} className={styles.muted}>
                {shown.length} of {plural(all.length, 'pool')}
              </Text>
            </div>
            {gatewayOptions.length > 1 && (
              <Field label="Gateway">
                <Select
                  aria-label="Filter pools by gateway"
                  value={gatewayFilter}
                  onChange={(event) => setGatewayFilter(event.target.value)}
                >
                  <option value="all">All gateways</option>
                  {gatewayOptions.map(([id, name]) => (
                    <option key={id} value={id}>{name}</option>
                  ))}
                </Select>
              </Field>
            )}
          </div>
          <div className="table-scroll">
            <table aria-label="Model pools">
              <thead>
                <tr>
                  <th>Pool</th>
                  <th>Gateway</th>
                  <th>Serves</th>
                  <th>Routing</th>
                  <th>Active members</th>
                  <th>Access</th>
                  <th>Readiness</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((summary) => (
                  <PoolRow key={summary.pool.id} summary={summary} catalog={catalog.data} />
                ))}
              </tbody>
            </table>
          </div>
          <Text size={200} className={styles.footnote}>
            Saving a pool records what you want. Nothing changes in API Management until you review a plan
            and apply it.
          </Text>
        </Card>
      )}

      {suggestions.isError && (
        <ErrorState error={suggestions.error} title="Couldn’t work out which pools to suggest" />
      )}
      {suggested.length > 0 && (
        <Card className={styles.listCard}>
          <div className={styles.cardHeader}>
            <div className={styles.cardHeaderText}>
              <Title3 as="h2">Suggested pools</Title3>
              <Text size={200} className={styles.muted}>
                These models are deployed on two or more endpoints a gateway can reach, and no pool there
                serves them yet. A pool lets users call each model by one name while the gateway spreads
                their requests across the deployments.
              </Text>
            </div>
          </div>
          {createSuggested.isError && (
            <MessageBar intent="error" className={styles.suggestionError}>
              <MessageBarBody>
                MOSAIC couldn’t load the deployments for that pool.{' '}
                {createSuggested.error instanceof Error ? createSuggested.error.message : ''}
              </MessageBarBody>
            </MessageBar>
          )}
          <div className="table-scroll">
            <table aria-label="Suggested pools">
              <thead>
                <tr>
                  <th>Gateway</th>
                  <th>Serves</th>
                  <th>Models</th>
                  <th><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {suggested.map((suggestion) => (
                  <SuggestionRow
                    key={`${suggestion.gatewayId}|${suggestion.vendor ?? ''}|${suggestion.apiShape}`}
                    suggestion={suggestion}
                    catalog={catalog.data}
                    busy={createSuggested.isPending}
                    onCreate={() => createSuggested.mutate(suggestion)}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <Text size={200} className={styles.footnote}>
            Creating a pool from a suggestion opens the editor with every deployment the gateway can use.
            Review them before you save.
          </Text>
        </Card>
      )}

      {editor && (
        <PoolEditorDialog
          initialGatewayId={editor.prefill?.gatewayId}
          prefill={editor.prefill}
          onClose={() => setEditor(null)}
          onSaved={(pool, review) => {
            setEditor(null)
            const state: PoolLocationState = { openPlan: review }
            navigate(`/pools/${pool.id}`, { state })
          }}
        />
      )}
    </div>
  )
}
