import {
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
  useId,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { ENTITLEMENT_SUBJECT_KIND_LABELS, PRINCIPAL_KIND_LABELS, plural } from '../labels'
import { runtimeConfig } from '../runtime-config'
import type {
  EntitlementSubject,
  PublishAction,
  PublishedResourceKind,
  PublishPlan,
  PublishRun,
  PublishRunStatus,
  PublishStepStatus,
} from '../types'
import { ErrorState, Loading } from './AsyncState'
import { ModelAccessRecovery } from './ModelAccessRecovery'
import styles from './UnpublishDialog.module.css'

export type UnpublishTarget = 'model' | 'mcp'

export interface UnpublishSubject {
  id: string
  displayName: string
}

interface UnpublishDialogProps {
  open: boolean
  target: UnpublishTarget
  /** The publication to unpublish. The dialog keeps showing the last one while it closes. */
  publication: UnpublishSubject | null
  onClose: () => void
  onUnpublished: (message: string) => void
}

const actionLabels: Record<PublishAction, string> = {
  create: 'Create',
  update: 'Update',
  delete: 'Delete',
  noChange: 'No change',
}

const kindLabels: Record<PublishedResourceKind, string> = {
  policyFragment: 'Policy fragment',
  backend: 'Backend',
  api: 'API',
  apiOperation: 'Operation',
  apiPolicy: 'API policy',
  product: 'Product',
  productApi: 'Product link',
  subscription: 'Subscription',
}

const stepStatusLabels: Record<PublishStepStatus, string> = {
  pending: 'Not attempted',
  succeeded: 'Deleted',
  failed: 'Failed',
  skipped: 'Skipped',
  rolledBack: 'Rolled back',
  rollbackFailed: 'Rollback failed',
}

const terminalRunStatuses: PublishRunStatus[] = [
  'succeeded',
  'failed',
  'rolledBack',
  'rollbackFailed',
  'interrupted',
]

const nouns: Record<UnpublishTarget, string> = { model: 'model', mcp: 'MCP server' }

function statusOf(error: unknown): number | undefined {
  return (error as { status?: number } | null)?.status
}

/**
 * Reviews an unpublish before it runs. Opening it asks MOSAIC for an unpublish plan, which removes
 * nothing. The review shows who loses access and what MOSAIC deletes, and only its confirm button
 * runs that plan. MOSAIC refuses a plan the publication has outgrown, and the review then shows why
 * with a fresh plan in its place. Each opening starts afresh; closing keeps what was shown until the
 * dialog next opens, so nothing changes while it animates out.
 */
export function UnpublishDialog(props: UnpublishDialogProps) {
  const { open, publication } = props
  const [session, setSession] = useState({ open, publication, key: 0 })
  if (open && (!session.open || publication?.id !== session.publication?.id)) {
    setSession({ open, publication, key: session.key + 1 })
  } else if (!open && session.open) {
    setSession({ ...session, open })
  }
  if (!session.publication) return null
  return (
    <UnpublishSession
      key={session.key}
      {...props}
      publication={session.publication}
      sessionKey={session.key}
    />
  )
}

function UnpublishSession({
  open,
  target,
  publication,
  sessionKey,
  onClose,
  onUnpublished,
}: UnpublishDialogProps & { publication: UnpublishSubject; sessionKey: number }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const introId = useId('unpublish-intro-')
  const noun = nouns[target]
  const [freshPlan, setFreshPlan] = useState<PublishPlan | null>(null)
  const [runId, setRunId] = useState('')
  const [refusal, setRefusal] = useState('')
  const [invalidPlan, setInvalidPlan] = useState(false)
  const [refreshError, setRefreshError] = useState<Error | null>(null)
  const notifiedRunRef = useRef('')

  const createPlan = () =>
    target === 'model'
      ? api.planUnpublishPublication(publication.id)
      : api.planUnpublishMcpPublication(publication.id)

  // Planning writes nothing but a plan record, so each opening plans once, as it opens. A query
  // rather than a mutation, so React's development double mount can't lose track of it.
  const planning = useQuery({
    queryKey: ['unpublish-plan', target, publication.id, sessionKey],
    queryFn: createPlan,
    enabled: open,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const plan = freshPlan ?? planning.data ?? null

  const refresh = useCallback(async () => {
    const queryKeys =
      target === 'model'
        ? [['publications'], ['publishable-models'], ['model-apis']]
        : [['mcp-publications'], ['mcp-servers']]
    await Promise.all(
      [...queryKeys, ['entitlements'], ['entitlement-connection']].map((queryKey) =>
        queryClient.invalidateQueries({ queryKey }),
      ),
    )
  }, [queryClient, target])

  const unpublish = useMutation({
    mutationFn: () => {
      if (!plan) throw new Error('Review the unpublish plan before you unpublish.')
      return target === 'model'
        ? api.unpublishPublication(publication.id, plan.id)
        : api.unpublishMcpPublication(publication.id, plan.id)
    },
    onMutate: () => setRefusal(''),
    onSuccess: (run) => {
      setRunId(run.id)
      void refresh()
    },
    onError: async (error) => {
      if (statusOf(error) !== 409) return
      // MOSAIC refused the plan that was reviewed. Say why, and review a fresh one in its place.
      setRefusal(error instanceof Error ? error.message : 'The unpublish plan is out of date.')
      setInvalidPlan(true)
      setRefreshError(null)
      void refresh()
      try {
        setFreshPlan(await createPlan())
        setInvalidPlan(false)
      } catch (failure) {
        setRefreshError(
          failure instanceof Error ? failure : new Error('MOSAIC could not plan the unpublish again.'),
        )
      }
    },
  })
  const confirmError = unpublish.error && statusOf(unpublish.error) !== 409 ? unpublish.error : null

  const run = useQuery({
    queryKey: [target === 'model' ? 'publish-run' : 'mcp-publish-run', publication.id, runId],
    queryFn: () =>
      target === 'model'
        ? api.getPublishRun(publication.id, runId)
        : api.getMcpPublishRun(publication.id, runId),
    enabled: Boolean(open && runId),
    refetchInterval: (query) =>
      query.state.data && terminalRunStatuses.includes(query.state.data.status) ? false : 1000,
  })
  const currentRun = run.data ?? unpublish.data ?? null
  const finishedRun =
    currentRun && terminalRunStatuses.includes(currentRun.status) ? currentRun : null
  const phase = runId ? 'run' : 'review'

  useEffect(() => {
    // A closed session stays mounted until the dialog next opens; it doesn't announce a run that
    // finishes after it closed.
    if (!open || !finishedRun || notifiedRunRef.current === finishedRun.id) return
    notifiedRunRef.current = finishedRun.id
    void refresh()
    if (finishedRun.status === 'succeeded') {
      onUnpublished(
        runtimeConfig.authMode === 'local'
          ? 'Local development service reported completion. Live APIM changes are not verified.'
          : `The service reports ${publication.displayName} is unpublished. Allow for APIM propagation.`,
      )
    }
  }, [finishedRun, onUnpublished, open, publication.displayName, refresh])

  // Focus follows what the dialog learns, as in the publish dialog. The first plan takes focus so
  // it is read. A refusal, an error or a finished run takes it next. Starting to unpublish keeps it
  // on the busy button, and the run's status takes it once the run starts.
  const reviewRef = useRef<HTMLElement>(null)
  const statusRef = useRef<HTMLElement>(null)
  const outcomeRef = useRef<HTMLDivElement>(null)
  const firstPlanRef = useRef<string | null>(null)
  useEffect(() => {
    if (!open || !plan || phase !== 'review' || firstPlanRef.current !== null) return
    firstPlanRef.current = plan.id
    reviewRef.current?.focus()
  }, [open, plan, phase])
  const outcome = phase === 'run' ? (finishedRun?.id ?? null) : (planning.error ?? unpublish.error ?? null)
  const shownRef = useRef({ phase, outcome })
  useEffect(() => {
    if (!open) return
    const shown = shownRef.current
    shownRef.current = { phase, outcome }
    if (phase === shown.phase && (outcome === null || Object.is(outcome, shown.outcome))) return
    const target = outcomeRef.current ?? statusRef.current
    target?.focus()
  }, [open, phase, outcome])

  const busy = unpublish.isPending
  return (
    <Dialog
      modalType="alert"
      open={open}
      onOpenChange={(_, data) => {
        if (!data.open && !busy) onClose()
      }}
    >
      <DialogSurface aria-describedby={introId}>
        <DialogBody>
          <DialogTitle>Unpublish {publication.displayName}?</DialogTitle>
          <DialogContent className={styles.content}>
            <Text id={introId}>
              MOSAIC deletes what it created in API Management for this {noun}. The gateway stops
              serving it, and the portal stops listing it until you publish it again.
            </Text>

            {phase === 'review' && planning.isPending && <Loading label="Planning the unpublish" />}
            {phase === 'review' && planning.isError && (
              <div ref={outcomeRef} tabIndex={-1}>
                <MessageBar intent="error">
                  <MessageBarBody>
                    <MessageBarTitle>MOSAIC can&apos;t unpublish {publication.displayName}</MessageBarTitle>
                    {planning.error instanceof Error ? planning.error.message : 'An unexpected error occurred.'}
                  </MessageBarBody>
                </MessageBar>
              </div>
            )}
            {phase === 'review' && refusal && (
              <div ref={confirmError ? undefined : outcomeRef} tabIndex={-1}>
                <MessageBar intent="warning">
                  <MessageBarBody>
                    <MessageBarTitle>MOSAIC didn&apos;t unpublish with the plan you reviewed</MessageBarTitle>
                    {refusal}
                    {!invalidPlan && (
                      <Text block>MOSAIC has planned again. Review the fresh plan below before you unpublish.</Text>
                    )}
                  </MessageBarBody>
                </MessageBar>
              </div>
            )}
            {phase === 'review' && plan && (
              <UnpublishReview plan={plan} target={target} reviewRef={reviewRef} />
            )}
            {phase === 'review' && confirmError && (
              <div ref={outcomeRef} tabIndex={-1}>
                <ErrorState
                  title={`MOSAIC didn't unpublish ${publication.displayName}`}
                  error={confirmError}
                />
              </div>
            )}
            {phase === 'review' && refreshError && <ErrorState error={refreshError} />}

            {phase === 'run' && (
              <>
                <Text ref={statusRef} tabIndex={-1} weight="semibold">
                  {finishedRun ? 'Unpublish finished' : `Unpublishing ${publication.displayName}`}
                </Text>
                {!finishedRun && <Loading label="MOSAIC is deleting the resources you reviewed" />}
                {run.isError && <ErrorState error={run.error} />}
                {currentRun && (
                  <div ref={finishedRun ? outcomeRef : undefined} tabIndex={-1}>
                    <UnpublishResult run={currentRun} target={target} name={publication.displayName} />
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
                className={styles.destructive}
                disabled={invalidPlan}
                disabledFocusable={busy}
                onClick={() => unpublish.mutate()}
              >
                {busy ? 'Unpublishing…' : `Unpublish ${noun}`}
              </Button>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}

interface LosingGrant {
  entitlementId: string
  subject: EntitlementSubject
  displayName: string
  loses: string
}

interface AppliedGrant {
  entitlementId: string
  subject: EntitlementSubject
  displayName: string
  enabled: boolean
  /** Whether the grant has a subscription key that works today. */
  key: boolean
}

function UnpublishReview({
  plan,
  target,
  reviewRef,
}: {
  plan: PublishPlan
  target: UnpublishTarget
  reviewRef: RefObject<HTMLElement | null>
}) {
  const api = useMosaicApi()
  const noun = nouns[target]
  const model = plan.accessSnapshot ?? null
  const mcp = plan.mcpAccessSnapshot ?? null
  const principals = useQuery({
    queryKey: ['principals'],
    queryFn: () => api.listPrincipals(),
    enabled: Boolean(model || mcp),
  })
  const kindLabel = (subject: EntitlementSubject) => {
    const principal = principals.data?.find((item) => item.id === subject.id)
    return principal ? PRINCIPAL_KIND_LABELS[principal.kind] : ENTITLEMENT_SUBJECT_KIND_LABELS[subject.kind]
  }

  // The access the gateway applied last is who can call it now, so it is who loses access.
  const keys = Boolean(model?.settings.keysEnabled)
  const tokens = target === 'mcp' || Boolean(model?.settings.entraEnabled)
  const applied: AppliedGrant[] = model
    ? model.grants.map((grant) => ({
        entitlementId: grant.entitlementId,
        subject: grant.subject,
        displayName: grant.displayName,
        enabled: grant.enabled,
        key: keys && Boolean(grant.subscriptionName),
      }))
    : (mcp?.grants ?? []).map((grant) => ({
        entitlementId: grant.entitlementId,
        subject: grant.subject,
        displayName: grant.displayName,
        enabled: grant.enabled,
        key: false,
      }))
  const losing: LosingGrant[] = applied
    .filter((grant) => grant.enabled && (grant.key || tokens))
    .map((grant) => {
      const methods = [grant.key ? 'Key' : null, tokens ? 'Entra token' : null].filter(Boolean)
      const members = grant.subject.kind === 'securityGroup' ? ', for every member' : ''
      return {
        entitlementId: grant.entitlementId,
        subject: grant.subject,
        displayName: grant.displayName,
        loses: `${methods.join(' and ')}${members}`,
      }
    })
  const one = losing.length === 1
  const stops = [
    losing.some((grant) => grant.loses.startsWith('Key'))
      ? `subscription keys for ${one ? 'this grant' : 'these grants'} stop working`
      : null,
    tokens ? `the gateway refuses ${one ? 'its' : 'their'} Entra tokens` : null,
  ].filter((part): part is string => part !== null)
  const stopsSentence = stops.join(', and ')
  const grantNames = new Map(applied.map((grant) => [grant.entitlementId, grant.displayName]))

  return (
    <section ref={reviewRef} tabIndex={-1} aria-label="Unpublish review" className={styles.content}>
      <div className={styles.section}>
        <Title3 as="h3">Who loses access</Title3>
        {!model && !mcp && target === 'model' ? (
          <Text>
            This model doesn&apos;t use governed access, so MOSAIC can&apos;t list who calls it.
            Everyone who calls it with a key for its product loses access.
          </Text>
        ) : losing.length === 0 ? (
          <Text>No grant has access to this {noun} at the gateway right now.</Text>
        ) : (
          <>
            <Text>
              {plural(losing.length, 'grant')} {one ? 'loses' : 'lose'} access.{' '}
              {stopsSentence.charAt(0).toUpperCase()}
              {stopsSentence.slice(1)}.
            </Text>
            <div className={styles.tableScroll}>
              <Table size="small" aria-label="Grants that lose access">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Grantee</TableHeaderCell>
                    <TableHeaderCell>Type</TableHeaderCell>
                    <TableHeaderCell>Stops working</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {losing.map((grant) => (
                    <TableRow key={grant.entitlementId}>
                      <TableCell>{grant.displayName}</TableCell>
                      <TableCell>{kindLabel(grant.subject)}</TableCell>
                      <TableCell>{grant.loses}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </>
        )}
        <Text size={200}>
          Grants stay in MOSAIC. Publish the {noun} again, and apply its plan, to restore access.
        </Text>
      </div>

      <div className={styles.section}>
        <Title3 as="h3">What MOSAIC deletes</Title3>
        <Text>
          {plural(plan.steps.length, 'resource')} MOSAIC created, deleted in this order. MOSAIC
          leaves everything else in API Management alone.
        </Text>
        {plan.warnings.map((warning) => (
          <MessageBar key={warning} intent="warning">
            <MessageBarBody>{warning}</MessageBarBody>
          </MessageBar>
        ))}
        <div className={styles.tableScroll}>
          <Table size="small" aria-label="Unpublish plan steps">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Resource</TableHeaderCell>
                <TableHeaderCell>Action</TableHeaderCell>
                <TableHeaderCell>Why</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {plan.steps.map((step, index) => (
                <TableRow key={`${step.kind}-${step.name}-${index}`}>
                  <TableCell>
                    <div className={styles.cellStack}>
                      <Text weight="semibold">{kindLabels[step.kind]}</Text>
                      <Text size={200} className={styles.resourceName}>{step.name}</Text>
                      {step.entitlementId && (
                        <Text size={200}>Grant: {grantNames.get(step.entitlementId) ?? step.entitlementId}</Text>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>{actionLabels[step.action]}</TableCell>
                  <TableCell>{step.reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </section>
  )
}

function UnpublishResult({ run, target, name }: { run: PublishRun; target: UnpublishTarget; name: string }) {
  const orphans = run.orphanedResources ?? []
  return (
    <div className={styles.content}>
      {run.status === 'interrupted' && (
        <>
          <MessageBar intent="warning">
            <MessageBarBody>
              <MessageBarTitle>Unpublish interrupted — runtime state unknown</MessageBarTitle>
              Some deletes may have reached API Management, and MOSAIC can&apos;t confirm who still has
              access. Reconcile this run before you try again.
            </MessageBarBody>
          </MessageBar>
          <ModelAccessRecovery publicationId={run.publicationId} runId={run.id} target={target} />
        </>
      )}
      {run.status === 'failed' && (
        <MessageBar intent="error">
          <MessageBarBody>
            Unpublish failed, and some of what MOSAIC created may still be in API Management.
            Unpublish {name} again to review what is left and remove it.
          </MessageBarBody>
        </MessageBar>
      )}
      {run.status === 'succeeded' && (
        <MessageBar intent={runtimeConfig.authMode === 'local' ? 'warning' : 'success'}>
          <MessageBarBody>
            {runtimeConfig.authMode === 'local'
              ? 'Local development service reported completion; live APIM changes are not verified.'
              : `The service reports ${name} is unpublished. Allow for gateway propagation.`}
          </MessageBarBody>
        </MessageBar>
      )}
      {orphans.length > 0 && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Resources left behind in API Management</MessageBarTitle>
            {orphans.map((resource) => resource.name).join(', ')}
          </MessageBarBody>
        </MessageBar>
      )}
      {run.errors.map((error) => (
        <MessageBar key={error} intent="error">
          <MessageBarBody>{error}</MessageBarBody>
        </MessageBar>
      ))}
      <div className={styles.tableScroll}>
        <Table size="small" aria-label="Unpublish run steps">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Resource</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.steps.map((step, index) => (
              <TableRow key={`${step.kind}-${step.name}-${index}`}>
                <TableCell>
                  <div className={styles.cellStack}>
                    <Text weight="semibold">{kindLabels[step.kind]}</Text>
                    <Text size={200} className={styles.resourceName}>{step.name}</Text>
                  </div>
                </TableCell>
                <TableCell>
                  {stepStatusLabels[step.status]}
                  {step.error ? <Text block size={200}>{step.error}</Text> : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
