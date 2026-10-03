import {
  Badge,
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { useMosaicApi } from '../api'
import { describeAccessMethods, describeLimits } from '../entitlement-limits'
import { ENTITLEMENT_SUBJECT_KIND_LABELS, plural } from '../labels'
import { POOL_RESOURCE_KIND_LABELS, planProblems } from '../pools'
import { runtimeConfig } from '../runtime-config'
import type {
  ModelPool,
  PoolAccessSnapshot,
  PublishAction,
  PublishPlan,
  PublishRun,
  PublishRunStatus,
  PublishStepStatus,
} from '../types'
import { ErrorState, Loading } from './AsyncState'
import { ModelAccessRecovery } from './ModelAccessRecovery'
import { PolicyFacetItem } from './PolicyFacets'
import styles from './PoolDialogs.module.css'

export type PoolPlanMode = 'publish' | 'unpublish'

const actionLabels: Record<PublishAction, string> = {
  create: 'Create',
  update: 'Update',
  delete: 'Delete',
  noChange: 'No change',
}

const stepStatusLabels: Record<PoolPlanMode, Record<PublishStepStatus, string>> = {
  publish: {
    pending: 'Not attempted',
    succeeded: 'Succeeded',
    failed: 'Failed',
    skipped: 'Skipped. It replaced an existing resource, so MOSAIC left it in place.',
    rolledBack: 'Rolled back',
    rollbackFailed: 'Rollback failed',
  },
  unpublish: {
    pending: 'Not attempted',
    succeeded: 'Deleted',
    failed: 'Failed',
    skipped: 'Skipped',
    rolledBack: 'Rolled back',
    rollbackFailed: 'Rollback failed',
  },
}

const terminalRunStatuses: PublishRunStatus[] = ['succeeded', 'failed', 'rolledBack', 'rollbackFailed', 'interrupted']

function statusOf(error: unknown): number | undefined {
  return (error as { status?: number } | null)?.status
}

export interface PoolPlanDialogProps {
  pool: Pick<ModelPool, 'id' | 'displayName' | 'models'>
  mode: PoolPlanMode
  onClose: () => void
  /** Called once, when the run the dialog started finishes, whatever its outcome. */
  onFinished?: (run: PublishRun) => void
}

/**
 * Reviews a pool's plan before it runs. Opening the dialog asks MOSAIC for a plan, which changes
 * nothing in API Management; only the confirm button runs it. MOSAIC refuses a plan the pool has
 * outgrown, and the dialog then says why and shows a fresh plan in its place. Parents mount it to
 * open it, so each opening plans afresh.
 */
export function PoolPlanDialog({ pool, mode, onClose, onFinished }: PoolPlanDialogProps) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const session = useId()
  const introId = useId()
  const publishing = mode === 'publish'
  const [freshPlan, setFreshPlan] = useState<PublishPlan | null>(null)
  const [runId, setRunId] = useState('')
  const [refusal, setRefusal] = useState('')
  const [invalidPlan, setInvalidPlan] = useState(false)
  const [replanError, setReplanError] = useState<unknown>(null)
  const notifiedRef = useRef('')

  const createPlan = () => (publishing ? api.planModelPool(pool.id) : api.planUnpublishModelPool(pool.id))

  // Planning writes only a plan record, so each opening plans once. A query rather than a mutation,
  // so React's development double mount can't lose track of it. Its key sits outside
  // ['model-pools'], so refreshing the pools doesn't plan again behind the reviewer's back.
  const planning = useQuery({
    queryKey: ['pool-plan', mode, pool.id, session],
    queryFn: createPlan,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const plan = freshPlan ?? planning.data ?? null
  const refreshPools = () => queryClient.invalidateQueries({ queryKey: ['model-pools'] })

  const apply = useMutation({
    mutationFn: () => {
      if (!plan) throw new Error('Review the plan before you apply it.')
      return publishing ? api.applyModelPool(pool.id, plan.id) : api.unpublishModelPool(pool.id, plan.id)
    },
    onMutate: () => {
      setRefusal('')
      setReplanError(null)
    },
    onSuccess: (run) => {
      setRunId(run.id)
      void refreshPools()
    },
    onError: async (error) => {
      if (statusOf(error) !== 409) return
      // MOSAIC refused the plan that was reviewed. Say why, and review a fresh one in its place.
      setRefusal(error instanceof Error ? error.message : 'The plan is out of date.')
      setInvalidPlan(true)
      void refreshPools()
      try {
        setFreshPlan(await createPlan())
        setInvalidPlan(false)
      } catch (failure) {
        setReplanError(failure)
      }
    },
  })
  const confirmError = apply.error && statusOf(apply.error) !== 409 ? apply.error : null

  const run = useQuery({
    queryKey: ['pool-run', pool.id, runId],
    queryFn: () => api.getModelPoolRun(pool.id, runId),
    enabled: Boolean(runId),
    refetchInterval: (query) =>
      query.state.data && terminalRunStatuses.includes(query.state.data.status) ? false : 1000,
  })
  const currentRun = run.data ?? apply.data ?? null
  const finishedRun = currentRun && terminalRunStatuses.includes(currentRun.status) ? currentRun : null
  const phase = runId ? 'run' : 'review'

  useEffect(() => {
    if (!finishedRun || notifiedRef.current === finishedRun.id) return
    notifiedRef.current = finishedRun.id
    void queryClient.invalidateQueries({ queryKey: ['model-pools'] })
    onFinished?.(finishedRun)
  }, [finishedRun, onFinished, queryClient])

  // Focus follows what the dialog learns. The first plan takes it so it's read; a refusal, an error,
  // or a finished run takes it next. Starting a run keeps it on the busy button until the run's
  // status appears.
  const reviewRef = useRef<HTMLElement>(null)
  const statusRef = useRef<HTMLElement>(null)
  const outcomeRef = useRef<HTMLDivElement>(null)
  const firstPlanRef = useRef<string | null>(null)
  useEffect(() => {
    if (!plan || phase !== 'review' || firstPlanRef.current !== null) return
    firstPlanRef.current = plan.id
    reviewRef.current?.focus()
  }, [plan, phase])
  const outcome = phase === 'run' ? (finishedRun?.id ?? null) : (planning.error ?? apply.error ?? null)
  const shownRef = useRef({ phase, outcome })
  useEffect(() => {
    const shown = shownRef.current
    shownRef.current = { phase, outcome }
    if (phase === shown.phase && (outcome === null || Object.is(outcome, shown.outcome))) return
    const target = outcomeRef.current ?? statusRef.current
    target?.focus()
  }, [phase, outcome])

  const busy = apply.isPending
  const nothingToApply =
    publishing && plan != null && plan.steps.length > 0 && plan.steps.every((step) => step.action === 'noChange')
  const problems = planProblems(planning.error)
  const replanProblems = planProblems(replanError)

  return (
    <Dialog
      modalType={publishing ? 'modal' : 'alert'}
      open
      onOpenChange={(_, data) => {
        if (!data.open && !busy) onClose()
      }}
    >
      <DialogSurface className={styles.surface} aria-describedby={introId}>
        <DialogBody>
          <DialogTitle>{publishing ? `Publish ${pool.displayName}` : `Unpublish ${pool.displayName}?`}</DialogTitle>
          <DialogContent className={styles.content}>
            <Text id={introId}>
              {publishing
                ? 'MOSAIC has planned what it writes to API Management. Nothing changes until you apply the plan.'
                : 'MOSAIC deletes what it created in API Management for this pool. The gateway stops serving the pool’s models, and its subscription key stops working.'}
            </Text>

            {phase === 'review' && planning.isPending && (
              <Loading label={publishing ? 'Planning the pool' : 'Planning the unpublish'} />
            )}
            {phase === 'review' && planning.isError && (
              <div ref={outcomeRef} tabIndex={-1}>
                {problems.length > 0 ? (
                  <MessageBar intent="error">
                    <MessageBarBody>
                      <MessageBarTitle>Fix these before you publish {pool.displayName}</MessageBarTitle>
                      <ul className={styles.findings}>
                        {problems.map((problem) => (
                          <li key={problem}>{problem}</li>
                        ))}
                      </ul>
                      <Text block>Edit the pool to fix them, then review the plan again.</Text>
                    </MessageBarBody>
                  </MessageBar>
                ) : (
                  <ErrorState
                    error={planning.error}
                    title={
                      publishing
                        ? `MOSAIC can’t plan ${pool.displayName}`
                        : `MOSAIC can’t unpublish ${pool.displayName}`
                    }
                  />
                )}
              </div>
            )}
            {phase === 'review' && refusal && (
              <div ref={confirmError ? undefined : outcomeRef} tabIndex={-1}>
                <MessageBar intent="warning">
                  <MessageBarBody>
                    <MessageBarTitle>MOSAIC didn’t run the plan you reviewed</MessageBarTitle>
                    {refusal}
                    {!invalidPlan && (
                      <Text block>MOSAIC has planned again. Review the fresh plan below before you continue.</Text>
                    )}
                  </MessageBarBody>
                </MessageBar>
              </div>
            )}
            {phase === 'review' && plan && (
              <PlanReview
                plan={plan}
                mode={mode}
                name={pool.displayName}
                models={pool.models}
                nothingToApply={nothingToApply}
                reviewRef={reviewRef}
              />
            )}
            {phase === 'review' && confirmError && (
              <div ref={outcomeRef} tabIndex={-1}>
                <ErrorState
                  error={confirmError}
                  title={publishing ? 'MOSAIC didn’t apply the plan' : `MOSAIC didn’t unpublish ${pool.displayName}`}
                />
              </div>
            )}
            {phase === 'review' && replanError != null && (
              <MessageBar intent="error">
                <MessageBarBody>
                  <MessageBarTitle>MOSAIC couldn’t plan again</MessageBarTitle>
                  {replanProblems.length > 0 ? (
                    <ul className={styles.findings}>
                      {replanProblems.map((problem) => (
                        <li key={problem}>{problem}</li>
                      ))}
                    </ul>
                  ) : replanError instanceof Error ? (
                    replanError.message
                  ) : (
                    'An unexpected error occurred.'
                  )}
                </MessageBarBody>
              </MessageBar>
            )}

            {phase === 'run' && (
              <>
                <Text ref={statusRef} tabIndex={-1} weight="semibold">
                  {finishedRun
                    ? publishing
                      ? 'Apply finished'
                      : 'Unpublish finished'
                    : publishing
                      ? `Applying the plan for ${pool.displayName}`
                      : `Unpublishing ${pool.displayName}`}
                </Text>
                {!finishedRun && (
                  <Loading
                    label={
                      publishing
                        ? 'MOSAIC is writing the changes you reviewed'
                        : 'MOSAIC is deleting the resources you reviewed'
                    }
                  />
                )}
                {run.isError && <ErrorState error={run.error} />}
                {currentRun && (
                  <div ref={finishedRun ? outcomeRef : undefined} tabIndex={-1}>
                    <PoolRunResult run={currentRun} mode={mode} name={pool.displayName} poolId={pool.id} />
                  </div>
                )}
              </>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" disabled={busy} onClick={onClose}>
              {phase === 'review' && (planning.isPending || plan) ? 'Cancel' : 'Close'}
            </Button>
            {/* A browser takes focus off a button that becomes disabled, so a busy button stays focusable. */}
            {phase === 'review' && plan && (
              <Button
                appearance="primary"
                className={publishing ? undefined : styles.destructive}
                disabled={invalidPlan || nothingToApply}
                disabledFocusable={busy}
                onClick={() => apply.mutate()}
              >
                {publishing ? (busy ? 'Applying…' : 'Apply plan') : busy ? 'Unpublishing…' : 'Unpublish pool'}
              </Button>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}

function PlanReview({
  plan,
  mode,
  name,
  models,
  nothingToApply,
  reviewRef,
}: {
  plan: PublishPlan
  mode: PoolPlanMode
  name: string
  models: ModelPool['models']
  nothingToApply: boolean
  reviewRef: RefObject<HTMLElement | null>
}) {
  const publishing = mode === 'publish'
  const changes = plan.steps.filter((step) => step.action !== 'noChange').length
  return (
    <section ref={reviewRef} tabIndex={-1} aria-label="Plan review" className={styles.section}>
      {nothingToApply && (
        <MessageBar intent="info">
          <MessageBarBody>API Management already matches {name}. There’s nothing to apply.</MessageBarBody>
        </MessageBar>
      )}
      {plan.warnings.map((warning) => (
        <MessageBar key={warning} intent="warning">
          <MessageBarBody>{warning}</MessageBarBody>
        </MessageBar>
      ))}
      <Title3 as="h3">{publishing ? 'What MOSAIC writes' : 'What MOSAIC deletes'}</Title3>
      <Text>
        {publishing
          ? `${plural(changes, 'change')} across ${plural(plan.steps.length, 'resource')}, written in this order.`
          : `${plural(plan.steps.length, 'resource')} MOSAIC created, deleted in this order. MOSAIC leaves everything else in API Management alone.`}
      </Text>
      <div className={`table-scroll ${styles.tableScroll}`}>
        <Table size="small" aria-label={publishing ? 'Publish plan steps' : 'Unpublish plan steps'}>
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Resource</TableHeaderCell>
              <TableHeaderCell>Action</TableHeaderCell>
              <TableHeaderCell>Why</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {plan.steps.map((step, index) => (
              <TableRow key={`${step.kind}-${step.name}-${step.stage ?? 'default'}-${index}`}>
                <TableCell>
                  <div className={styles.cellStack}>
                    <Text weight="semibold">{POOL_RESOURCE_KIND_LABELS[step.kind]}</Text>
                    <Text size={200} className={styles.code}>
                      {step.name}
                    </Text>
                  </div>
                </TableCell>
                <TableCell>
                  <Badge appearance="tint" color={step.action === 'delete' ? 'danger' : 'brand'}>
                    {actionLabels[step.action]}
                  </Badge>
                </TableCell>
                <TableCell>{step.reason}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {publishing && plan.facets.length > 0 && (
        <>
          <Title3 as="h3">What the policy does</Title3>
          <ul className={styles.facetList}>
            {plan.facets.map((facet, index) => (
              <PolicyFacetItem key={`${facet.element}-${index}`} facet={facet} />
            ))}
          </ul>
        </>
      )}
      {publishing && plan.poolAccessSnapshot && (
        <PoolAccessReview
          snapshot={plan.poolAccessSnapshot}
          previousVersion={plan.previousAccessVersion ?? null}
          models={models}
        />
      )}
    </section>
  )
}

/**
 * The complete set of grants a governed pool's apply enforces. The first governed apply suspends
 * the pool's shared subscription, so the review says so before anyone confirms it.
 */
function PoolAccessReview({
  snapshot,
  previousVersion,
  models,
}: {
  snapshot: PoolAccessSnapshot
  previousVersion: number | null
  models: ModelPool['models']
}) {
  const modelName = (id: string) => models.find((model) => model.id === id)?.displayName ?? id
  const quotas = snapshot.quotas ?? []
  return (
    <section aria-label="Pool access review" className={styles.section}>
      <Title3 as="h3">Who can call the pool</Title3>
      <Text>
        The apply enforces every grant below, across all of the pool’s models, not only the ones that changed.
      </Text>
      {previousVersion == null && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>The shared subscription stops working</MessageBarTitle>
            This is the pool’s first governed apply. MOSAIC suspends the pool’s shared subscription, and only the
            callers below can call the pool. You can’t turn governed access off afterward.
          </MessageBarBody>
        </MessageBar>
      )}
      <dl className={styles.summaryList}>
        <dt>Sign-in</dt>
        <dd>{describeAccessMethods(snapshot.settings)}</dd>
        <dt>Access version</dt>
        <dd>
          {previousVersion ?? 'none'} → {snapshot.version}
        </dd>
      </dl>
      {snapshot.tokenMetering === false && (
        <Text size={200} className={styles.muted}>
          This gateway’s tier can’t count the pool’s tokens, so MOSAIC leaves out every token limit and quota.
        </Text>
      )}
      {snapshot.grants.length === 0 ? (
        <Text>The target has no grants, so the gateway refuses every caller.</Text>
      ) : (
        <div className={`table-scroll ${styles.tableScroll}`}>
          <Table size="small" aria-label="Pool grants">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Model</TableHeaderCell>
                <TableHeaderCell>Who</TableHeaderCell>
                <TableHeaderCell>Key</TableHeaderCell>
                <TableHeaderCell>Cost center</TableHeaderCell>
                <TableHeaderCell>Limits</TableHeaderCell>
                <TableHeaderCell>Access</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {snapshot.grants.map((grant) => (
                <TableRow key={grant.entitlementId}>
                  <TableCell>{modelName(grant.poolModelId)}</TableCell>
                  <TableCell>
                    <div className={styles.cellStack}>
                      <Text weight="semibold">{grant.displayName}</Text>
                      <Text size={200}>{ENTITLEMENT_SUBJECT_KIND_LABELS[grant.subject.kind]}</Text>
                    </div>
                  </TableCell>
                  <TableCell>
                    {grant.keyName ? <span className={styles.code}>{grant.keyName}</span> : 'None (Entra token)'}
                  </TableCell>
                  <TableCell>{grant.costCenterCode ?? '—'}</TableCell>
                  <TableCell>
                    <div className={styles.cellStack}>
                      {describeLimits(grant).map((limit) => (
                        <Text key={limit} size={200}>
                          {limit}
                        </Text>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>{grant.enabled ? 'Allowed' : 'Refused'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {quotas.length > 0 && (
        <>
          <Text weight="semibold">Pooled monthly quotas</Text>
          <ul className={styles.facetList}>
            {quotas.map((quota) => (
              <li key={`${quota.poolModelId}:${quota.costCenterId}`}>
                {modelName(quota.poolModelId)}, {quota.costCenterCode}:{' '}
                {[
                  quota.monthlyTokens ? `${quota.monthlyTokens.toLocaleString()} tokens` : null,
                  quota.monthlyCalls ? `${quota.monthlyCalls.toLocaleString()} calls` : null,
                ]
                  .filter(Boolean)
                  .join(' and ')}{' '}
                a month, shared by everyone the cost center grants it to
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

function PoolRunResult({
  run,
  mode,
  name,
  poolId,
}: {
  run: PublishRun
  mode: PoolPlanMode
  name: string
  poolId: string
}) {
  const publishing = mode === 'publish'
  const orphans = run.orphanedResources ?? []
  const local = runtimeConfig.authMode === 'local'
  return (
    <div className={styles.section}>
      {run.status === 'interrupted' && (
        <>
          <MessageBar intent="warning">
            <MessageBarBody>
              <MessageBarTitle>
                {publishing ? 'Apply interrupted' : 'Unpublish interrupted'}. The gateway’s state is unknown.
              </MessageBarTitle>
              Some changes may have reached API Management. Reconcile this run before you try again.
            </MessageBarBody>
          </MessageBar>
          <ModelAccessRecovery publicationId={poolId} runId={run.id} target="pool" />
        </>
      )}
      {run.status === 'failed' && (
        <MessageBar intent="error">
          <MessageBarBody>
            {publishing
              ? 'The apply failed. Don’t assume the gateway serves the pool as you planned it.'
              : `The unpublish failed, and some of what MOSAIC created may still be in API Management. Unpublish ${name} again to review what’s left and remove it.`}
          </MessageBarBody>
        </MessageBar>
      )}
      {run.status === 'succeeded' && (
        <MessageBar intent={local ? 'warning' : 'success'}>
          <MessageBarBody>
            {local
              ? 'The local development service reported completion. Live API Management changes aren’t verified.'
              : publishing
                ? 'The service reports the plan applied. Allow a minute for the gateway to pick it up. MOSAIC hasn’t sent a request through the pool.'
                : `The service reports ${name} is unpublished. Allow a minute for the gateway to pick it up.`}
          </MessageBarBody>
        </MessageBar>
      )}
      {run.rolledBack && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Rolled back</MessageBarTitle>
            MOSAIC undid what it created during this run.
          </MessageBarBody>
        </MessageBar>
      )}
      {(run.status === 'rollbackFailed' || orphans.length > 0) && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Resources left behind in API Management</MessageBarTitle>
            {orphans.map((resource) => resource.name).join(', ') || 'MOSAIC couldn’t tell which resources.'}
          </MessageBarBody>
        </MessageBar>
      )}
      {run.errors.map((error) => (
        <MessageBar key={error} intent="error">
          <MessageBarBody>{error}</MessageBarBody>
        </MessageBar>
      ))}
      <div className={`table-scroll ${styles.tableScroll}`}>
        <Table size="small" aria-label={publishing ? 'Apply run steps' : 'Unpublish run steps'}>
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Resource</TableHeaderCell>
              <TableHeaderCell>Action</TableHeaderCell>
              <TableHeaderCell>Result</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.steps.map((step, index) => (
              <TableRow key={`${step.kind}-${step.name}-${step.stage ?? 'default'}-${index}`}>
                <TableCell>
                  <div className={styles.cellStack}>
                    <Text weight="semibold">{POOL_RESOURCE_KIND_LABELS[step.kind]}</Text>
                    <Text size={200} className={styles.code}>
                      {step.name}
                    </Text>
                  </div>
                </TableCell>
                <TableCell>{actionLabels[step.action]}</TableCell>
                <TableCell>
                  {stepStatusLabels[mode][step.status]}
                  {step.error ? (
                    <Text block size={200}>
                      {step.error}
                    </Text>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
