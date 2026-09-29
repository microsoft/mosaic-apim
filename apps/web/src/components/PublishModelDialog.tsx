import {
  Badge,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Select,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Textarea,
  Title3,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { CANNOT_INVOKE, NOT_CONFIRMED, runtimeVerdict } from '../runtime-access'
import { runtimeConfig } from '../runtime-config'
import type {
  ApiShape,
  Gateway,
  PublishedResourceKind,
  Publication,
  PublishableModel,
  PublishAction,
  PublishPlan,
  PublishRun,
  PublishRunStatus,
  PublishStepStatus,
  TokenEnforcement,
} from '../types'
import { ErrorState, Loading } from './AsyncState'
import { PolicyFacetItem } from './PolicyFacets'
import { ModelAccessReview } from './ModelAccessReview'
import { ModelAccessRecovery } from './ModelAccessRecovery'
import styles from './ImportFromGatewayDialog.module.css'

type Step = 'choose' | 'configure' | 'review' | 'apply'
type QuotaPeriod = NonNullable<TokenEnforcement['tokenQuotaPeriod']>

type FormState = {
  displayName: string
  apiName: string
  apiPath: string
  productName: string
  subscriptionRequired: boolean
  counterKeyExpression: string
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: '' | QuotaPeriod
  estimatePromptTokens: boolean
}

const quotaPeriods: QuotaPeriod[] = ['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly']

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

const shapeLabels: Record<ApiShape, string> = {
  azureOpenAi: 'Azure OpenAI API',
  foundryModels: 'Foundry Models API',
  anthropicMessages: 'Anthropic Messages API',
}

// Older services omit these fields; they then meant "publishable, with token limits".
function isPublishable(model: PublishableModel): boolean {
  return model.publishable !== false
}

function supportsTokenLimits(model: PublishableModel | null): boolean {
  return model?.tokenLimitsSupported !== false
}

const stepStatusLabels: Record<PublishStepStatus, string> = {
  pending: 'Not attempted',
  succeeded: 'Succeeded',
  failed: 'Failed',
  skipped: 'Skipped — replaced an existing resource, so it was left in place',
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

function parsePositiveInteger(value: string): number | undefined {
  const trimmed = value.trim()
  if (!trimmed) return undefined
  const parsed = Number(trimmed)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined
}

function buildEnforcement(form: FormState): TokenEnforcement {
  const tokenQuota = parsePositiveInteger(form.tokenQuota)
  return {
    counterKeyExpression: form.counterKeyExpression.trim(),
    tokensPerMinute: parsePositiveInteger(form.tokensPerMinute),
    tokenQuota,
    tokenQuotaPeriod: tokenQuota === undefined ? undefined : (form.tokenQuotaPeriod || undefined),
    estimatePromptTokens: form.estimatePromptTokens,
  }
}

function initialForm(model: PublishableModel | null): FormState {
  return {
    displayName: model ? `${model.endpointName} ${model.deploymentName}` : '',
    apiName: model?.suggestedApiName ?? '',
    apiPath: model?.suggestedApiPath ?? '',
    productName: '',
    subscriptionRequired: true,
    counterKeyExpression: '@(context.Subscription.Id)',
    tokensPerMinute: '12000',
    tokenQuota: '',
    tokenQuotaPeriod: '',
    estimatePromptTokens: true,
  }
}

function RuntimeAccessNote({ model }: { model: PublishableModel }) {
  const access = model.runtimeAccess
  if (!access) {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>Runtime access not evaluated</MessageBarTitle>
          MOSAIC has not evaluated whether this gateway can call the model.
        </MessageBarBody>
      </MessageBar>
    )
  }
  const verdict = runtimeVerdict(access)
  if (verdict === NOT_CONFIRMED) {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>Runtime access not confirmed</MessageBarTitle>
          {access.message ?? 'MOSAIC has not evaluated whether this gateway can call the model.'}
        </MessageBarBody>
      </MessageBar>
    )
  }
  if (verdict === CANNOT_INVOKE) {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>Gateway may not be able to call this model</MessageBarTitle>
          {access.message ?? 'MOSAIC evaluated runtime access and did not observe the required access.'}
        </MessageBarBody>
      </MessageBar>
    )
  }
  return (
    <MessageBar intent="success">
      <MessageBarBody>
        <MessageBarTitle>Runtime permissions observed</MessageBarTitle>
        {access.message ?? 'MOSAIC observed runtime permissions for this gateway.'}
        {' '}Permission metadata is not a successful live model invocation.
      </MessageBarBody>
    </MessageBar>
  )
}

function RunResult({ run }: { run: PublishRun }) {
  const orphans = run.orphanedResources ?? []
  return (
    <div className={styles.nameCell}>
      {run.status === 'interrupted' && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Apply interrupted — runtime state unknown</MessageBarTitle>
            Some changes may have reached API Management. Access and revocation are not confirmed.
            Reconcile this run before attempting another apply.
          </MessageBarBody>
        </MessageBar>
      )}
      {run.status === 'interrupted' && <ModelAccessRecovery publicationId={run.publicationId} runId={run.id} />}
      {run.status === 'failed' && (
        <MessageBar intent="error">
          <MessageBarBody>Apply failed. Do not assume the target access or revocation is active.</MessageBarBody>
        </MessageBar>
      )}
      {run.status === 'succeeded' && (
        <MessageBar intent={runtimeConfig.authMode === 'local' ? 'warning' : 'success'}>
          <MessageBarBody>
            {runtimeConfig.authMode === 'local'
              ? 'Local development service reported completion; live APIM apply is not verified.'
              : 'The service reports the plan applied. Allow for gateway propagation; a live model invocation has not been verified.'}
          </MessageBarBody>
        </MessageBar>
      )}
      {run.rolledBack && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Rolled back</MessageBarTitle>
            {run.accessSnapshot
              ? 'Created resources were rolled back where possible. Restrictive policy changes may remain; consult the recorded runtime state before retrying.'
              : 'MOSAIC undid what it created during this publish run.'}
          </MessageBarBody>
        </MessageBar>
      )}
      {(run.status === 'rollbackFailed' || orphans.length > 0) && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Resources left behind in API Management</MessageBarTitle>
            These resources were left behind in API Management:{' '}
            {orphans.map((resource) => resource.name).join(', ') || 'unknown resources'}.
          </MessageBarBody>
        </MessageBar>
      )}
      {run.errors.map((error) => (
        <MessageBar key={error} intent="error">
          <MessageBarBody>{error}</MessageBarBody>
        </MessageBar>
      ))}
      <div className={styles.tableScroll}>
        <Table size="small" aria-label="Publish run steps">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Resource</TableHeaderCell>
              <TableHeaderCell>Action</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.steps.map((step, index) => (
              <TableRow key={`${step.kind}-${step.name}-${step.stage ?? 'default'}-${index}`}>
                <TableCell>
                  <div className={styles.nameCell}>
                    <Text weight="semibold">{kindLabels[step.kind]}</Text>
                    <Text size={200}>{step.name}</Text>
                    {step.stage && <Text size={200}>Stage: {step.stage}</Text>}
                  </div>
                </TableCell>
                <TableCell>{actionLabels[step.action]}</TableCell>
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

type ModelReview = {
  publication: Publication
  plan: PublishPlan
  message?: string
}

type PublishModelDialogProps = {
  open: boolean
  onClose: () => void
  onPublished: (message: string) => void
  initialReview?: ModelReview | null
}

// Every opening starts a new session, so the first frame the dialog commits is already the step it opens on,
// and Fluent moves focus into that step. Closing keeps the session, with the review it opened, until the
// dialog next opens, so nothing in it changes while Fluent animates the dialog out.
export function PublishModelDialog({ open, onClose, onPublished, initialReview }: PublishModelDialogProps) {
  const review = initialReview ?? null
  const [session, setSession] = useState({ open, review, key: 0 })
  if (open && (!session.open || review !== session.review)) {
    setSession({ open, review, key: session.key + 1 })
  } else if (!open && session.open) {
    setSession({ ...session, open })
  }
  return (
    <PublishModelSession
      key={session.key}
      open={open}
      initialReview={session.review}
      onClose={onClose}
      onPublished={onPublished}
    />
  )
}

function PublishModelSession({ open, onClose, onPublished, initialReview }: PublishModelDialogProps) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [step, setStep] = useState<Step>(initialReview ? 'review' : 'choose')
  const [gatewayId, setGatewayId] = useState(initialReview?.publication.gatewayId ?? '')
  const [modelKey, setModelKey] = useState('')
  const [form, setForm] = useState<FormState>(() => initialForm(null))
  const [publication, setPublication] = useState<Publication | null>(initialReview?.publication ?? null)
  const [plan, setPlan] = useState<PublishPlan | null>(initialReview?.plan ?? null)
  const [reviewMessage, setReviewMessage] = useState(initialReview?.message ?? '')
  const [runId, setRunId] = useState('')
  const [refreshError, setRefreshError] = useState<Error | null>(null)
  const [invalidPlan, setInvalidPlan] = useState(false)
  const appliedGatewayRef = useRef(false)
  const notifiedRunRef = useRef('')
  const reviewingExistingPlan = Boolean(initialReview)

  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
    enabled: open && !reviewingExistingPlan,
  })

  const gatewayOptions: Gateway[] = useMemo(() => gateways.data ?? [], [gateways.data])

  useEffect(() => {
    if (!open || reviewingExistingPlan) {
      appliedGatewayRef.current = false
      return
    }
    if (!appliedGatewayRef.current && gatewayOptions.length > 0) {
      appliedGatewayRef.current = true
      setGatewayId(gatewayOptions.find((gateway) => gateway.managementMode === 'manage')?.id ?? '')
    }
  }, [open, gatewayOptions, reviewingExistingPlan])

  const publishable = useQuery({
    queryKey: ['publishable-models', gatewayId],
    queryFn: () => api.listPublishableModels(gatewayId),
    enabled: open && !reviewingExistingPlan && gatewayId !== '',
  })

  const models = publishable.data ?? []
  const selectedModel = models.find(
    (model) => `${model.modelEndpointId}:${model.deploymentName}` === modelKey,
  ) ?? null

  useEffect(() => {
    if (!selectedModel) return
    setForm(initialForm(selectedModel))
  }, [selectedModel])

  const createAndPlan = useMutation({
    mutationFn: async () => {
      if (!selectedModel) throw new Error('Choose a model before reviewing the plan.')
      const created = await api.createPublication({
        gatewayId,
        modelEndpointId: selectedModel.modelEndpointId,
        deploymentName: selectedModel.deploymentName,
        displayName: form.displayName.trim() || undefined,
        apiName: form.apiName.trim() || undefined,
        apiPath: form.apiPath.trim() || undefined,
        productName: form.productName.trim() || undefined,
        subscriptionRequired: form.subscriptionRequired,
        enforcement: supportsTokenLimits(selectedModel) ? buildEnforcement(form) : null,
      })
      const createdPlan = await api.createPublishPlan(created.id)
      return { created, createdPlan }
    },
    onSuccess: ({ created, createdPlan }) => {
      setPublication(created)
      setPlan(createdPlan)
      setReviewMessage('')
      setStep('review')
      void queryClient.invalidateQueries({ queryKey: ['publications'] })
    },
  })

  const apply = useMutation({
    mutationFn: async () => {
      if (!publication || !plan) throw new Error('Review the plan before applying it.')
      return await api.applyPublishPlan(publication.id, plan.id)
    },
    onSuccess: (run) => {
      setRunId(run.id)
      setReviewMessage('')
      setStep('apply')
      void queryClient.invalidateQueries({ queryKey: ['publications'] })
    },
    onError: async (error) => {
      if (!publication || (error as { status?: number }).status !== 409) return
      setInvalidPlan(true)
      setRefreshError(null)
      setReviewMessage(error instanceof Error ? error.message : 'The publish plan is stale.')
      try {
        const freshPlan = await api.createPublishPlan(publication.id)
        setPlan(freshPlan)
        setInvalidPlan(false)
        setStep('review')
      } catch (refreshFailure) {
        setRefreshError(refreshFailure instanceof Error ? refreshFailure : new Error('Unable to refresh this plan. Close and review again.'))
      }
      void queryClient.invalidateQueries({ queryKey: ['publications'] })
    },
  })
  const applyError =
    apply.error && (apply.error as { status?: number }).status !== 409 ? apply.error : null

  const run = useQuery({
    queryKey: ['publish-run', publication?.id, runId],
    queryFn: () => api.getPublishRun(publication!.id, runId),
    enabled: Boolean(open && publication && runId),
    refetchInterval: (query) =>
      query.state.data && terminalRunStatuses.includes(query.state.data.status) ? false : 1000,
  })

  const currentRun = run.data ?? apply.data ?? null

  useEffect(() => {
    // A closed session stays mounted until the dialog next opens. Don't announce an apply that finishes
    // after the dialog closed.
    if (!open) return
    if (currentRun && terminalRunStatuses.includes(currentRun.status) && notifiedRunRef.current !== currentRun.id) {
      notifiedRunRef.current = currentRun.id
      void queryClient.invalidateQueries({ queryKey: ['publications'] })
      void queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      void queryClient.invalidateQueries({ queryKey: ['model-apis'] })
      void queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] })
      if (currentRun.status === 'succeeded') {
        onPublished(runtimeConfig.authMode === 'local'
          ? 'Local development service reported completion. Live APIM apply is not verified.'
          : 'The service reports the model plan applied. Allow for APIM propagation; live invocation is not verified.')
      }
    }
  }, [currentRun, onPublished, open, queryClient])

  // Moving to another step replaces the controls that had focus, such as the button that moved it, and a
  // plan or apply that finishes brings an outcome with it. Focus would otherwise fall out of the dialog,
  // where Escape no longer closes it, and a screen reader wouldn't be taken to what changed. So move focus
  // to the new outcome, which a screen reader then reads, or else to the top of the new step. Fluent moves
  // focus into the step the dialog opens on, and a field the administrator is typing in keeps focus.
  const stepRef = useRef<HTMLElement>(null)
  const outcomeRef = useRef<HTMLDivElement>(null)
  const finishedRun = currentRun && terminalRunStatuses.includes(currentRun.status) ? currentRun : null
  const outcome = {
    choose: null,
    configure: createAndPlan.error,
    review: apply.error,
    apply: finishedRun?.id ?? null,
  }[step]
  const shownRef = useRef({ step, outcome })
  useEffect(() => {
    if (!open) return
    const shown = shownRef.current
    shownRef.current = { step, outcome }
    // Trying again clears the last outcome, and focus stays on the busy button until the next one.
    if (step === shown.step && (outcome === null || Object.is(outcome, shown.outcome))) return
    if (document.activeElement?.matches('input, select, textarea')) return
    const target = outcomeRef.current ?? stepRef.current
    target?.focus()
  }, [open, step, outcome])

  const canConfigure = Boolean(gatewayId && selectedModel && isPublishable(selectedModel))
  const tokenLimits = supportsTokenLimits(selectedModel)
  const canReview = Boolean(
    form.apiName.trim() && form.apiPath.trim() && (!tokenLimits || form.counterKeyExpression.trim()),
  )
  const missingAccessReview = Boolean(publication?.governedAccess && !plan?.accessSnapshot)
  // Applying writes every step, unchanged ones included, so a plan that changes nothing can't be applied.
  const nothingToApply = Boolean(
    plan && plan.steps.length > 0 && plan.steps.every((planStep) => planStep.action === 'noChange'),
  )

  return (
    <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{initialReview?.plan.accessSnapshot ? 'Review model access' : 'Publish a model'}</DialogTitle>
          <DialogContent>
            <div className={styles.intro}>
              <Text>Plan the API Management resources first, then explicitly apply the plan.</Text>
              {runtimeConfig.authMode === 'local' && (
                <Text>Local development mode: responses may be simulated and do not prove a live APIM change.</Text>
              )}
              <Text ref={stepRef} tabIndex={-1} size={200}>
                Step {['choose', 'configure', 'review', 'apply'].indexOf(step) + 1} of 4
              </Text>
            </div>

            {step === 'choose' && (
              <div className={styles.nameCell}>
                <Field label="Gateway">
                  <Select
                    aria-label="Gateway"
                    value={gatewayId}
                    onChange={(event) => setGatewayId(event.target.value)}
                  >
                    {gatewayOptions.length === 0 && <option value="">No gateways registered</option>}
                    {gatewayOptions.map((gateway) => (
                      <option
                        key={gateway.id}
                        value={gateway.id}
                        disabled={gateway.managementMode !== 'manage'}
                      >
                        {gateway.name}
                        {gateway.managementMode !== 'manage'
                          ? ' — switch to managed mode and verify write access first'
                          : ''}
                      </option>
                    ))}
                  </Select>
                </Field>
                {gateways.isPending && <Loading label="Loading gateways" />}
                {gateways.isError && <ErrorState error={gateways.error} />}
                {publishable.isPending && gatewayId && <Loading label="Loading publishable models" />}
                {publishable.isError && <ErrorState error={publishable.error} />}
                {publishable.isSuccess && models.length === 0 && (
                  <Text>No publishable models were found for this gateway.</Text>
                )}
                {models.length > 0 && (
                  <div className={styles.tableScroll}>
                    <Table size="small" aria-label="Publishable models">
                      <TableHeader>
                        <TableRow>
                          <TableHeaderCell>Choose</TableHeaderCell>
                          <TableHeaderCell>Model</TableHeaderCell>
                          <TableHeaderCell>Runtime access</TableHeaderCell>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {models.map((model) => {
                          const key = `${model.modelEndpointId}:${model.deploymentName}`
                          const publishableRow = isPublishable(model)
                          return (
                            <TableRow key={key}>
                              <TableCell>
                                <Checkbox
                                  aria-label={`Publish ${model.deploymentName}`}
                                  checked={modelKey === key}
                                  disabled={!publishableRow}
                                  onChange={(_, data) => setModelKey(data.checked ? key : '')}
                                />
                              </TableCell>
                              <TableCell>
                                <div className={styles.nameCell}>
                                  <Text weight="semibold">{model.deploymentName}</Text>
                                  <Text size={200}>
                                    {model.modelName ?? 'Unknown model'}
                                    {model.modelFormat ? ` · ${model.modelFormat}` : ''}
                                  </Text>
                                  <Text size={200}>/{model.suggestedApiPath}</Text>
                                  {model.apiShape && <Badge appearance="outline">{shapeLabels[model.apiShape]}</Badge>}
                                  {model.publicationStatus && <Badge appearance="tint">{model.publicationStatus}</Badge>}
                                  {!publishableRow && (
                                    <>
                                      <Badge appearance="tint" color="warning">Not publishable</Badge>
                                      <Text size={200}>
                                        {model.unpublishableReason ?? "MOSAIC can't publish this deployment yet."}
                                      </Text>
                                    </>
                                  )}
                                </div>
                              </TableCell>
                              <TableCell><RuntimeAccessNote model={model} /></TableCell>
                            </TableRow>
                          )
                        })}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </div>
            )}

            {step === 'configure' && (
              <div className={styles.nameCell}>
                <Field label="Display name">
                  <Input value={form.displayName} onChange={(_, data) => setForm({ ...form, displayName: data.value })} />
                </Field>
                <Field label="API name" required>
                  <Input value={form.apiName} onChange={(_, data) => setForm({ ...form, apiName: data.value })} />
                </Field>
                <Field label="API path" required>
                  <Input value={form.apiPath} onChange={(_, data) => setForm({ ...form, apiPath: data.value })} />
                </Field>
                <Field label="Product name">
                  <Input value={form.productName} onChange={(_, data) => setForm({ ...form, productName: data.value })} />
                </Field>
                <Switch
                  checked={form.subscriptionRequired}
                  label="Subscription required"
                  onChange={(_, data) => setForm({ ...form, subscriptionRequired: Boolean(data.checked) })}
                />
                {!tokenLimits && (
                  <MessageBar intent="warning">
                    <MessageBarBody>
                      <MessageBarTitle>Token limits unavailable</MessageBarTitle>
                      {selectedModel?.tokenLimitsNote
                        ?? "This gateway can't apply token limits to this model, so this publication applies none."}
                    </MessageBarBody>
                  </MessageBar>
                )}
                {tokenLimits && (
                  <>
                    <Field label="Counter key expression" required>
                      <Textarea
                        resize="vertical"
                        value={form.counterKeyExpression}
                        onChange={(_, data) => setForm({ ...form, counterKeyExpression: data.value })}
                      />
                    </Field>
                    <div className={styles.controls}>
                      <Field label="Tokens per minute" className={styles.gatewayField}>
                        <Input type="number" min={1} value={form.tokensPerMinute} onChange={(_, data) => setForm({ ...form, tokensPerMinute: data.value })} />
                      </Field>
                      <Field label="Token quota" className={styles.gatewayField}>
                        <Input type="number" min={1} value={form.tokenQuota} onChange={(_, data) => setForm({ ...form, tokenQuota: data.value })} />
                      </Field>
                      <Field label="Quota period" className={styles.gatewayField}>
                        <Select value={form.tokenQuotaPeriod} onChange={(event) => setForm({ ...form, tokenQuotaPeriod: event.target.value as FormState['tokenQuotaPeriod'] })}>
                          <option value="">None</option>
                          {quotaPeriods.map((period) => <option key={period} value={period}>{period}</option>)}
                        </Select>
                      </Field>
                    </div>
                    <Switch
                      checked={form.estimatePromptTokens}
                      label="Estimate prompt tokens"
                      onChange={(_, data) => setForm({ ...form, estimatePromptTokens: Boolean(data.checked) })}
                    />
                  </>
                )}
                {createAndPlan.isError && (
                  <div ref={outcomeRef} tabIndex={-1}>
                    <ErrorState error={createAndPlan.error} />
                  </div>
                )}
              </div>
            )}

            {step === 'review' && plan && (
              <div className={styles.nameCell}>
                {reviewMessage && (
                  <div ref={applyError ? undefined : outcomeRef} tabIndex={-1}>
                    <MessageBar intent="warning">
                      <MessageBarBody>
                        <MessageBarTitle>MOSAIC didn't apply the plan you reviewed</MessageBarTitle>
                        {reviewMessage}
                        {!invalidPlan && (
                          <Text block>MOSAIC has already re-planned. Review the fresh plan below before you apply it.</Text>
                        )}
                      </MessageBarBody>
                    </MessageBar>
                  </div>
                )}
                {nothingToApply && (
                  <MessageBar intent="info">
                    <MessageBarBody>API Management already matches this publication. Nothing to apply.</MessageBarBody>
                  </MessageBar>
                )}
                {plan.warnings.map((warning) => (
                  <MessageBar key={warning} intent="warning">
                    <MessageBarBody><MessageBarTitle>Plan warning</MessageBarTitle>{warning}</MessageBarBody>
                  </MessageBar>
                ))}
                <ModelAccessReview plan={plan} />
                {missingAccessReview && (
                  <MessageBar intent="error">
                    <MessageBarBody>The governed-access snapshot is missing. Close and request a fresh model plan; access changes cannot be applied without reviewing all target grants.</MessageBarBody>
                  </MessageBar>
                )}
                <Title3 as="h3">Plan steps</Title3>
                <div className={styles.tableScroll}>
                  <Table size="small" aria-label="Publish plan steps">
                    <TableHeader>
                      <TableRow>
                        <TableHeaderCell>Resource</TableHeaderCell>
                        <TableHeaderCell>Action</TableHeaderCell>
                        <TableHeaderCell>Reason</TableHeaderCell>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {plan.steps.map((planStep, index) => (
                        <TableRow key={`${planStep.kind}-${planStep.name}-${planStep.stage ?? 'default'}-${index}`}>
                          <TableCell>
                            {kindLabels[planStep.kind]} · {planStep.name}
                            {planStep.stage && <Text block size={200}>Stage: {planStep.stage}</Text>}
                            {planStep.entitlementId && <Text block size={200}>Grant: {planStep.entitlementId}</Text>}
                            {planStep.subscriptionState && <Text block size={200}>Subscription state: {planStep.subscriptionState}</Text>}
                          </TableCell>
                          <TableCell><Badge appearance="tint">{actionLabels[planStep.action]}</Badge></TableCell>
                          <TableCell>{planStep.reason}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
                {plan.facets.length > 0 && (
                  <>
                    <Title3 as="h3">Policy facets</Title3>
                    <ul>
                      {plan.facets.map((facet, index) => (
                        <PolicyFacetItem key={`${facet.element}-${index}`} facet={facet} />
                      ))}
                    </ul>
                  </>
                )}
                {applyError && (
                  <div ref={outcomeRef} tabIndex={-1}>
                    <ErrorState error={applyError} />
                  </div>
                )}
                {refreshError && <ErrorState error={refreshError} />}
              </div>
            )}

            {step === 'apply' && (
              <div className={styles.nameCell}>
                {!currentRun || currentRun.status === 'running' ? <Loading label="Applying publish plan" /> : null}
                {run.isError && <ErrorState error={run.error} />}
                {currentRun && (
                  <div ref={finishedRun ? outcomeRef : undefined} tabIndex={-1}>
                    <RunResult run={currentRun} />
                  </div>
                )}
              </div>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={onClose}>Close</Button>
            {step === 'configure' && <Button appearance="secondary" onClick={() => setStep('choose')}>Back</Button>}
            {step === 'review' && !reviewingExistingPlan && <Button appearance="secondary" onClick={() => setStep('configure')}>Back</Button>}
            {step === 'choose' && (
              <Button appearance="primary" disabled={!canConfigure} onClick={() => setStep('configure')}>Configure</Button>
            )}
            {/* A browser takes focus off a button that becomes disabled, so a busy button stays focusable. */}
            {step === 'configure' && (
              <Button
                appearance="primary"
                disabled={!canReview}
                disabledFocusable={createAndPlan.isPending}
                onClick={() => createAndPlan.mutate()}
              >
                {createAndPlan.isPending ? 'Creating plan…' : 'Review plan'}
              </Button>
            )}
            {step === 'review' && (
              <Button
                appearance="primary"
                disabled={invalidPlan || missingAccessReview || nothingToApply}
                disabledFocusable={apply.isPending}
                onClick={() => apply.mutate()}
              >
                {apply.isPending ? 'Applying…' : 'Apply plan'}
              </Button>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
