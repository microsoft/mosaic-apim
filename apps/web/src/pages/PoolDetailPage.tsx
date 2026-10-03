import {
  Button,
  Card,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Switch,
  Text,
  Title3,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { CheckmarkRegular, CopyRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { ModelAccessRecovery } from '../components/ModelAccessRecovery'
import { PageHeader } from '../components/PageHeader'
import { PolicyFacetItem } from '../components/PolicyFacets'
import { PoolAccessCard } from '../components/PoolAccessCard'
import {
  PoolCapacityBadgeView,
  PoolMemberAccessBadges,
  PoolReadinessBadge,
  PoolRunStatusBadge,
  PoolStatusBadge,
  UnappliedChangesBadge,
} from '../components/PoolBadges'
import { PoolEditorDialog } from '../components/PoolEditorDialog'
import { PoolPlanDialog } from '../components/PoolPlanDialog'
import type { PoolPlanMode } from '../components/PoolPlanDialog'
import { PoolTypeDiagram } from '../components/PoolTypeDiagram'
import { RemovalDialog } from '../components/RemovalDialog'
import { useEnvironmentCatalog } from '../environments'
import { CAPACITY_TYPE_LABELS, PROCESSING_SCOPE_LABELS, plural } from '../labels'
import {
  BREAKER_PRESET_DESCRIPTIONS,
  BREAKER_PRESET_LABELS,
  LINEAR_PRESET_DESCRIPTIONS,
  POOL_TYPE_DESCRIPTIONS,
  POOL_TYPE_LABELS,
  POOL_VISIBILITY_LABELS,
  describeRetries,
  describeSafeguard,
  formatRunDuration,
  keyedRouting,
  keyedTurn,
  memberPositions,
  memberShares,
  newestRunsFirst,
  poolFamilyLabel,
  poolRequestExample,
  poolRunOperation,
  poolWriteBlocker,
  specsWithMemberDrained,
} from '../pools'
import type { PoolLocationState } from '../pools'
import { formatTimestamp, isUnpublished, lastAppliedLabel } from '../publication-state'
import type {
  EnvironmentCatalogView,
  ModelPool,
  ModelPoolDetail,
  ModelPoolType,
  PoolMemberView,
  PoolModelView,
  PublishRun,
} from '../types'
import styles from './PoolDetailPage.module.css'

/** How many runs the history shows. Older runs stay in the audit log. */
const RUN_HISTORY_LIMIT = 10

interface DrainChange {
  pool: ModelPool
  model: PoolModelView
  member: PoolMemberView
  drained: boolean
}

function memberLabel(member: PoolMemberView): string {
  return `${member.deploymentName} on ${member.endpointName ?? member.modelEndpointId}`
}

/** What a saved drain does, and when the gateway catches up with it. */
function drainNotice({ member, drained }: DrainChange, published: boolean): string {
  const subject = memberLabel(member)
  if (!published) {
    return drained
      ? `${subject} is drained. It takes effect when you publish the pool.`
      : `${subject} is back in the pool. It takes effect when you publish the pool.`
  }
  return drained
    ? `${subject} is drained in the saved pool. The gateway keeps sending it requests until you review and apply a plan.`
    : `${subject} is back in the saved pool. The gateway sends it requests once you review and apply a plan.`
}

function shareLabel(poolType: ModelPoolType, member: PoolMemberView, share: number | null): string {
  if (share == null) return 'No requests'
  if (poolType === 'preferential' && (member.priority ?? 1) > 1) return `${share}% of overflow`
  return `${share}% of requests`
}

function capacityDetail(member: PoolMemberView): string {
  const parts: string[] = []
  if (member.skuName) parts.push(member.skuCapacity != null ? `${member.skuName} ${member.skuCapacity}` : member.skuName)
  if (member.processingScope !== 'unknown') parts.push(PROCESSING_SCOPE_LABELS[member.processingScope])
  return parts.join(' · ')
}

function MemberRow({
  poolType,
  member,
  position,
  share,
  turn,
  catalog,
  drainDisabled,
  onDrain,
}: {
  poolType: ModelPoolType
  member: PoolMemberView
  position: string
  share: number | null
  /** When a breaker or preferential pool tries a member it reaches with an API key, which has no weight. */
  turn: string | null
  catalog?: EnvironmentCatalogView
  drainDisabled: boolean
  onDrain: (drained: boolean) => void
}) {
  const detail = capacityDetail(member)
  const verdict = member.environmentVerdict
  return (
    <tr className={member.drained ? styles.drainedRow : undefined}>
      {poolType !== 'breaker' && <td>{position}</td>}
      <td>
        <div className={styles.cellStack}>
          <Text weight="semibold">{member.deploymentName}</Text>
          <Text size={200} className={styles.muted}>
            {member.endpointName ?? member.modelEndpointId}
            {member.modelVersion ? ` · version ${member.modelVersion}` : ''}
          </Text>
          <PoolMemberAccessBadges
            apiKey={member.apiKey}
            declared={member.declared}
            provider={member.provider}
          />
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <Text>{member.region ?? 'Region unknown'}</Text>
          <EnvironmentBadge environment={member.environment ?? null} catalog={catalog} size="small" />
        </div>
      </td>
      <td>
        <div className={styles.cellStack}>
          <Text>{CAPACITY_TYPE_LABELS[member.capacityType]}</Text>
          {detail && <Text size={200} className={styles.muted}>{detail}</Text>}
        </div>
      </td>
      {poolType !== 'linear' && (
        <td>
          <div className={styles.cellStack}>
            <Text>{turn == null ? member.weight : '—'}</Text>
            <Text size={200} className={styles.muted}>{turn ?? shareLabel(poolType, member, share)}</Text>
          </div>
        </td>
      )}
      <td>
        <div className={styles.cellStack}>
          <PoolReadinessBadge readiness={member.readiness} />
          {member.readinessMessage && <Text size={200}>{member.readinessMessage}</Text>}
          {verdict && verdict.level !== 'allowed' && <Text size={200}>{verdict.reason}</Text>}
        </div>
      </td>
      <td>
        <Switch
          checked={member.drained}
          disabled={drainDisabled}
          aria-label={`Drain ${memberLabel(member)}`}
          onChange={(_, data) => onDrain(data.checked)}
        />
      </td>
    </tr>
  )
}

function PoolModelCard({
  poolType,
  model,
  catalog,
  drainDisabled,
  onDrain,
}: {
  poolType: ModelPoolType
  model: PoolModelView
  catalog?: EnvironmentCatalogView
  drainDisabled: boolean
  onDrain: (member: PoolMemberView, drained: boolean) => void
}) {
  const shares = memberShares(poolType, model.members)
  const positions = memberPositions(poolType, model.members)
  const active = model.members.filter((member) => !member.drained).length
  // A breaker or preferential pool tries each active member it reaches with an API key once, after
  // its backend pool's attempts, or in order when every active member has a key.
  const keyed = model.members.filter((member) => member.apiKey && !member.drained).length
  const afterBackendPool = model.members.some((member) => !member.apiKey && !member.drained)
  const turn = (member: PoolMemberView) =>
    poolType !== 'linear' && member.apiKey
      ? keyedTurn(member.drained ? null : member.order, keyed, afterBackendPool)
      : null
  const headingId = `pool-model-${model.id}`
  return (
    <Card className={styles.card} aria-labelledby={headingId}>
      <div className={styles.modelHeader}>
        <div className={styles.modelHeading}>
          <Title3 as="h3" id={headingId}>{model.displayName}</Title3>
          <Text size={200}>
            Callers send <span className={styles.code}>{model.publicName}</span>
            {model.modelName && model.modelName !== model.publicName ? ` · ${model.modelName}` : ''}
            {model.expectedVersion ? ` · version ${model.expectedVersion}` : ''}
          </Text>
          <Text size={200} className={styles.muted}>
            {active} of {plural(model.members.length, 'deployment')} active
            {model.listed ? '' : ' · Hidden from the portal catalog'}
          </Text>
        </div>
        <div className={styles.modelBadges}>
          <PoolCapacityBadgeView capacity={model.capacity} />
        </div>
      </div>
      <div className="table-scroll">
        <table aria-label={`Deployments serving ${model.displayName}`}>
          <thead>
            <tr>
              {poolType === 'linear' && <th>Order</th>}
              {poolType === 'preferential' && <th>Priority</th>}
              <th>Deployment</th>
              <th>Region</th>
              <th>Capacity</th>
              {poolType !== 'linear' && <th>Weight</th>}
              <th>Readiness</th>
              <th>Drained</th>
            </tr>
          </thead>
          <tbody>
            {model.members.map((member, index) => (
              <MemberRow
                key={`${member.modelEndpointId}::${member.deploymentName}`}
                poolType={poolType}
                member={member}
                position={positions[index]}
                share={shares[index]}
                turn={turn(member)}
                catalog={catalog}
                drainDisabled={drainDisabled}
                onDrain={(drained) => onDrain(member, drained)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

/** A copy button. `label` names what it copies, mid-sentence, such as "base URL". */
function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard?.writeText(value)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <Button
      size="small"
      icon={copied ? <CheckmarkRegular /> : <CopyRegular />}
      aria-label={copied ? `Copied ${label}` : `Copy ${label}`}
      onClick={() => void copy()}
    >
      {copied ? 'Copied' : 'Copy'}
    </Button>
  )
}

function ConnectionCard({ detail, published }: { detail: ModelPoolDetail; published: boolean }) {
  const { pool } = detail
  const model = detail.models.find((candidate) => candidate.listed) ?? detail.models[0]
  const example = detail.baseUrl && model ? poolRequestExample(pool.apiShape, detail.baseUrl, model.publicName) : null
  return (
    <Card className={styles.card} aria-labelledby="pool-connection-heading">
      <div className={styles.sectionHeading}>
        <Title3 as="h2" id="pool-connection-heading">How callers connect</Title3>
        <Text size={200} className={styles.muted}>
          Callers see one API and the pool’s model names. They never see the deployments behind them.
        </Text>
      </div>
      {detail.baseUrl ? (
        <div className={styles.urlRow}>
          <Text weight="semibold">Base URL</Text>
          <span className={styles.code}>{detail.baseUrl}</span>
          <CopyButton value={detail.baseUrl} label="base URL" />
        </div>
      ) : (
        <Text>
          MOSAIC hasn’t read the gateway’s URL yet. Sync the gateway, and the pool’s base URL appears here.
        </Text>
      )}
      {!published && (
        <Text>The pool isn’t published, so the gateway doesn’t serve it yet. Publish it to give callers this URL.</Text>
      )}
      {example && model && (
        <div className={styles.example}>
          <Text weight="semibold">Calling {model.displayName}</Text>
          <pre className={styles.request}>
            {`${example.method} ${example.url}`}
            {example.body ? `\n\n${example.body}` : ''}
          </pre>
          <Text size={200}>{example.note}</Text>
        </div>
      )}
      {governedConnectionText(pool) ?? (
        <Text size={200}>
          Callers use the pool’s subscription, <span className={styles.code}>{pool.subscriptionName}</span>, on the{' '}
          <span className={styles.code}>{pool.productName}</span> product. Copy its key from the Azure portal and send it
          in the Ocp-Apim-Subscription-Key header. To give each caller their own access instead, turn on governed access.
        </Text>
      )}
    </Card>
  )
}

/** How callers sign in once a plan has applied governed access, or null while they share the pool's key. */
function governedConnectionText(pool: ModelPool) {
  const settings = pool.appliedAccess?.settings
  if (!settings) return null
  const methods = [
    settings.keysEnabled && 'the key from their grant in the Ocp-Apim-Subscription-Key header',
    settings.entraEnabled && 'a Microsoft Entra token in the Authorization header',
  ].filter(Boolean)
  return (
    <Text size={200}>
      {methods.length
        ? `Each caller uses their own grant: ${methods.join(', or ')}. People find their models, keys, and examples in the portal.`
        : 'Both sign-in methods are off, so the gateway refuses every call.'}
    </Text>
  )
}

function RunHistory({ runs }: { runs: PublishRun[] }) {
  const shown = newestRunsFirst(runs).slice(0, RUN_HISTORY_LIMIT)
  if (shown.length === 0) {
    return <Text>No runs yet. Publishing the pool records a run here.</Text>
  }
  return (
    <div className="table-scroll">
      <table aria-label="Pool runs">
        <thead>
          <tr>
            <th>Started</th>
            <th>Operation</th>
            <th>Status</th>
            <th>Duration</th>
            <th>Changes</th>
            <th>Errors</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((run) => {
            const changes = run.steps.filter((step) => step.action !== 'noChange').length
            return (
              <tr key={run.id}>
                <td>{formatTimestamp(run.startedAt)}</td>
                <td>{poolRunOperation(run) === 'unpublish' ? 'Unpublish' : 'Publish'}</td>
                <td>
                  <PoolRunStatusBadge status={run.status} />
                </td>
                <td>{formatRunDuration(run.durationMs)}</td>
                <td>{changes ? plural(changes, 'change') : 'No changes'}</td>
                <td>
                  {run.errors.length ? (
                    <div className={styles.cellStack}>
                      {run.errors.map((error, index) => (
                        <Text key={index} size={200}>{error}</Text>
                      ))}
                    </div>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function PoolDetail({ poolId }: { poolId: string }) {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const restoreFocus = useRestoreFocusTarget()
  // A pool the list just saved with "Save and review plan" opens its plan once it loads.
  const [planMode, setPlanMode] = useState<PoolPlanMode | null>(() =>
    (location.state as PoolLocationState | null)?.openPlan ? 'publish' : null,
  )
  const [editing, setEditing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if ((location.state as PoolLocationState | null)?.openPlan) {
      navigate(location.pathname, { replace: true, state: null })
    }
  }, [location.pathname, location.state, navigate])

  const detail = useQuery({
    queryKey: ['model-pools', 'detail', poolId],
    queryFn: () => api.getModelPoolDetail(poolId),
    refetchInterval: (query) => (query.state.data?.pool.status === 'applying' ? 5000 : false),
  })
  const applying = detail.data?.pool.status === 'applying'
  const runs = useQuery({
    queryKey: ['model-pools', 'runs', poolId],
    queryFn: () => api.listModelPoolRuns(poolId),
    refetchInterval: applying ? 5000 : false,
  })
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })

  const drain = useMutation({
    mutationFn: (change: DrainChange) =>
      api.updateModelPool(poolId, {
        models: specsWithMemberDrained(
          change.pool,
          change.model.id,
          change.member.modelEndpointId,
          change.member.deploymentName,
          change.drained,
        ),
      }),
    onMutate: () => setNotice(''),
    onSuccess: async (saved, change) => {
      setNotice(drainNotice(change, saved.resources.some((resource) => resource.createdByMosaic)))
      await queryClient.invalidateQueries({ queryKey: ['model-pools'] })
    },
  })

  const remove = useMutation({
    mutationFn: () => api.deleteModelPool(poolId),
    onSuccess: () => {
      navigate('/pools', { replace: true })
      queryClient.removeQueries({ queryKey: ['model-pools', 'detail', poolId] })
      queryClient.removeQueries({ queryKey: ['model-pools', 'runs', poolId] })
      void queryClient.invalidateQueries({ queryKey: ['model-pools'] })
    },
  })

  const backLink = (
    <Link className={styles.backLink} to="/pools">
      ← Back to pools
    </Link>
  )

  if (detail.isPending) {
    return (
      <div className={styles.page}>
        {backLink}
        <Loading label="Loading pool" />
      </div>
    )
  }
  if (detail.isError) {
    return (
      <div className={styles.page}>
        {backLink}
        <ErrorState error={detail.error} title="Unable to load the pool" />
      </div>
    )
  }

  const { pool } = detail.data
  const gateway = gateways.data?.find((candidate) => candidate.id === pool.gatewayId)
  const published = pool.resources.some((resource) => resource.createdByMosaic)
  const writeBlocker = poolWriteBlocker(gateway)
  const { problems, warnings } = detail.data
  const planBlocked = applying || problems.length > 0 || Boolean(writeBlocker)
  const lastRun = runs.data?.find((run) => run.id === pool.lastRunId)
  const interrupted = lastRun?.status === 'interrupted' ? lastRun : null
  const family = pool.models.length ? poolFamilyLabel(pool.vendor, pool.apiShape) : 'No models yet'
  const preset = BREAKER_PRESET_LABELS[pool.breakerPreset]
  const presetDescription =
    pool.poolType === 'linear'
      ? LINEAR_PRESET_DESCRIPTIONS[pool.breakerPreset]
      : BREAKER_PRESET_DESCRIPTIONS[pool.breakerPreset]
  const activeMembers = detail.data.models.reduce(
    (count, model) => count + model.members.filter((member) => !member.drained).length,
    0,
  )

  return (
    <div className={styles.page}>
      {backLink}
      <PageHeader
        title={pool.displayName}
        description={pool.description || `${family} · ${POOL_TYPE_LABELS[pool.poolType]} routing`}
        source="live"
        actions={
          <div className={styles.headerActions}>
            <PoolStatusBadge pool={pool} />
            {detail.data.unappliedChanges && <UnappliedChangesBadge />}
            <EnvironmentBadge environment={detail.data.gatewayEnvironment ?? null} catalog={catalog.data} />
            <Button appearance="secondary" disabled={applying} onClick={() => setEditing(true)} {...restoreFocus}>
              Edit
            </Button>
            {published ? (
              <Button
                appearance="secondary"
                disabled={applying || Boolean(writeBlocker)}
                onClick={() => setPlanMode('unpublish')}
                {...restoreFocus}
              >
                Unpublish
              </Button>
            ) : (
              <Button appearance="secondary" disabled={applying} onClick={() => setRemoving(true)} {...restoreFocus}>
                Remove
              </Button>
            )}
            <Button appearance="primary" disabled={planBlocked} onClick={() => setPlanMode('publish')} {...restoreFocus}>
              {published ? 'Review plan' : 'Publish'}
            </Button>
          </div>
        }
      />

      <div className={styles.messages}>
        {interrupted && (
          <>
            <MessageBar intent="warning">
              <MessageBarBody>
                <MessageBarTitle>The last run was interrupted. The gateway’s state is unknown.</MessageBarTitle>
                Some changes may have reached API Management. Reconcile the run before you change the pool again.
              </MessageBarBody>
            </MessageBar>
            <ModelAccessRecovery publicationId={pool.id} runId={interrupted.id} target="pool" />
          </>
        )}
        {applying && !interrupted && (
          <MessageBar intent="info">
            <MessageBarBody>
              <MessageBarTitle>Applying a plan</MessageBarTitle>
              MOSAIC is changing this pool in API Management. This page refreshes when the run finishes.
            </MessageBarBody>
          </MessageBar>
        )}
        {writeBlocker && (
          <MessageBar intent="warning">
            <MessageBarBody>
              <MessageBarTitle>MOSAIC can’t change this pool in API Management</MessageBarTitle>
              {writeBlocker}
            </MessageBarBody>
          </MessageBar>
        )}
        {problems.length > 0 && (
          <MessageBar intent="error">
            <MessageBarBody>
              <MessageBarTitle>{published ? 'Fix these before you apply changes' : 'Fix these before you publish'}</MessageBarTitle>
              <ul className={styles.findings}>
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </MessageBarBody>
          </MessageBar>
        )}
        {detail.data.unappliedChanges && (
          <MessageBar intent="info">
            <MessageBarBody>
              <MessageBarTitle>Saved changes aren’t running yet</MessageBarTitle>
              The gateway still runs the pool as it was last applied. Review a plan to apply your changes.
            </MessageBarBody>
            <MessageBarActions>
              <Button size="small" disabled={planBlocked} onClick={() => setPlanMode('publish')} {...restoreFocus}>
                Review plan
              </Button>
            </MessageBarActions>
          </MessageBar>
        )}
        {warnings.length > 0 && (
          <MessageBar intent="warning">
            <MessageBarBody>
              <MessageBarTitle>{published ? 'Worth checking' : 'Before you publish'}</MessageBarTitle>
              <ul className={styles.findings}>
                {warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </MessageBarBody>
          </MessageBar>
        )}
        {pool.lastError && (pool.status === 'failed' || pool.status === 'rolledBack') && (
          <MessageBar intent="error">
            <MessageBarBody>
              <MessageBarTitle>
                {pool.status === 'failed' ? 'The last run failed' : 'The last run was rolled back'}
              </MessageBarTitle>
              {pool.lastError}
            </MessageBarBody>
          </MessageBar>
        )}
        {drain.isError && <ErrorState error={drain.error} title="MOSAIC didn’t save the change" />}
        <div role="status">
          {notice && (
            <MessageBar intent="success">
              <MessageBarBody>{notice}</MessageBarBody>
            </MessageBar>
          )}
        </div>
      </div>

      <div className={styles.overview}>
        <Card className={styles.card} aria-labelledby="pool-routing-heading">
          <Title3 as="h2" id="pool-routing-heading">Routing</Title3>
          <div className={styles.routingType}>
            <PoolTypeDiagram type={pool.poolType} className={styles.diagram} />
            <div>
              <Text block weight="semibold">{POOL_TYPE_LABELS[pool.poolType]}</Text>
              <Text block size={200}>{POOL_TYPE_DESCRIPTIONS[pool.poolType]}</Text>
            </div>
          </div>
          <dl className={styles.summaryList}>
            <dt>When a deployment fails</dt>
            <dd>
              {preset}. {presetDescription}
            </dd>
            <dt>Retries</dt>
            <dd>{describeRetries(pool.poolType, pool.maxRetries, keyedRouting(detail.data.models))}</dd>
            <dt>Safeguard</dt>
            <dd>{describeSafeguard(pool.safeguard)}</dd>
          </dl>
        </Card>
        <Card className={styles.card} aria-labelledby="pool-gateway-heading">
          <Title3 as="h2" id="pool-gateway-heading">In API Management</Title3>
          <dl className={styles.summaryList}>
            <dt>Gateway</dt>
            <dd>
              <Link to={`/gateways/${pool.gatewayId}`}>{detail.data.gatewayName ?? pool.gatewayId}</Link>
            </dd>
            <dt>Serves</dt>
            <dd>{family}</dd>
            <dt>API</dt>
            <dd>
              <span className={styles.code}>{pool.apiName}</span> at{' '}
              <span className={styles.code}>/{pool.apiPath}</span>
            </dd>
            <dt>Product</dt>
            <dd className={styles.code}>{pool.productName}</dd>
            <dt>Subscription</dt>
            <dd>
              <span className={styles.code}>{pool.subscriptionName}</span>
              {pool.appliedAccess
                ? ', suspended by governed access'
                : pool.governedAccess
                  ? ', suspended when a plan applies governed access'
                  : ''}
            </dd>
            <dt>Portal</dt>
            <dd>
              {POOL_VISIBILITY_LABELS[pool.visibility]}.{' '}
              {pool.showCapacity ? 'Users see each model’s capacity.' : 'Users don’t see capacity.'}
            </dd>
            <dt>Last applied</dt>
            <dd>{lastAppliedLabel(pool)}</dd>
          </dl>
        </Card>
      </div>

      <section className={styles.section} aria-labelledby="pool-models-heading">
        <div className={styles.sectionHeading}>
          <Title3 as="h2" id="pool-models-heading">Models</Title3>
          <Text size={200} className={styles.muted}>
            {plural(detail.data.models.length, 'model')}, served by {plural(activeMembers, 'active deployment')}.
            Draining a deployment saves the change; the gateway follows once you apply a plan.
          </Text>
        </div>
        {detail.data.models.length === 0 ? (
          <EmptyState title="No models yet">
            Edit the pool to add models. Each model gathers deployments of the same model, on any endpoint this
            gateway can reach, behind the one name callers send.
          </EmptyState>
        ) : (
          detail.data.models.map((model) => (
            <PoolModelCard
              key={model.id}
              poolType={pool.poolType}
              model={model}
              catalog={catalog.data}
              drainDisabled={applying || drain.isPending}
              onDrain={(member, drained) => drain.mutate({ pool, model, member, drained })}
            />
          ))
        )}
      </section>

      <PoolAccessCard
        key={JSON.stringify(pool.governedAccess ?? null)}
        pool={pool}
        onSaved={setNotice}
      />

      <ConnectionCard detail={detail.data} published={published} />

      {detail.data.facets.length > 0 && (
        <Card className={styles.card} aria-labelledby="pool-policy-heading">
          <div className={styles.sectionHeading}>
            <Title3 as="h2" id="pool-policy-heading">What the policy does</Title3>
            <Text size={200} className={styles.muted}>From the saved pool, as a plan would write it.</Text>
          </div>
          <ul className={styles.facetList}>
            {detail.data.facets.map((facet, index) => (
              <PolicyFacetItem key={`${facet.element}-${index}`} facet={facet} />
            ))}
          </ul>
        </Card>
      )}

      <Card className={styles.card} aria-labelledby="pool-runs-heading">
        <div className={styles.sectionHeading}>
          <Title3 as="h2" id="pool-runs-heading">Run history</Title3>
          <Text size={200} className={styles.muted}>
            Each time MOSAIC published or unpublished the pool, newest first.
          </Text>
        </div>
        {runs.isPending && <Loading label="Loading runs" />}
        {runs.isError && <ErrorState error={runs.error} title="Unable to load runs" />}
        {runs.data && <RunHistory runs={runs.data} />}
      </Card>

      {editing && (
        <PoolEditorDialog
          pool={pool}
          onClose={() => setEditing(false)}
          onSaved={(_saved, review) => {
            setEditing(false)
            if (review) setPlanMode('publish')
          }}
        />
      )}
      {planMode && <PoolPlanDialog pool={pool} mode={planMode} onClose={() => setPlanMode(null)} />}
      <RemovalDialog
        open={removing}
        title={`Remove ${pool.displayName}?`}
        confirmLabel="Remove pool"
        refusalTitle="MOSAIC didn’t remove this pool"
        pending={remove.isPending}
        error={remove.error}
        onConfirm={() => remove.mutate()}
        onCancel={() => {
          setRemoving(false)
          remove.reset()
        }}
      >
        {isUnpublished(pool)
          ? 'The pool was unpublished, so nothing changes in API Management.'
          : 'The pool was never published, so nothing changes in API Management.'}{' '}
        MOSAIC forgets its models and routing, and records the removal in the audit log.
      </RemovalDialog>
    </div>
  )
}

/** One pool: its models and the deployments behind them, how it routes, and how callers reach it. */
export function PoolDetailPage() {
  const { poolId = '' } = useParams()
  // Keyed so moving between pools starts each with fresh state.
  return <PoolDetail key={poolId} poolId={poolId} />
}
