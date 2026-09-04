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
import type {
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
  if (!access || access.evaluation === 'notEvaluated') {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>Runtime access not evaluated</MessageBarTitle>
          MOSAIC has not evaluated whether this gateway can call the model.
        </MessageBarBody>
      </MessageBar>
    )
  }
  if (!access.canInvoke) {
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
        <MessageBarTitle>Runtime access observed</MessageBarTitle>
        {access.message ?? 'MOSAIC observed that the gateway can call this model.'}
      </MessageBarBody>
    </MessageBar>
  )
}

function RunResult({ run }: { run: PublishRun }) {
  const orphans = run.orphanedResources ?? []
  return (
    <div className={styles.nameCell}>
      {run.rolledBack && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Rolled back</MessageBarTitle>
            MOSAIC undid what it created during this publish run.
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
            {run.steps.map((step) => (
              <TableRow key={`${step.kind}-${step.name}`}>
                <TableCell>
                  <div className={styles.nameCell}>
                    <Text weight="semibold">{kindLabels[step.kind]}</Text>
                    <Text size={200}>{step.name}</Text>
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

export function PublishModelDialog({
  open,
  onClose,
  onPublished,
  initialReview,
}: {
  open: boolean
  onClose: () => void
  onPublished: (message: string) => void
  initialReview?: {
    publication: Publication
    plan: PublishPlan
    message?: string
  } | null
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [step, setStep] = useState<Step>('choose')
  const [gatewayId, setGatewayId] = useState('')
  const [modelKey, setModelKey] = useState('')
  const [form, setForm] = useState<FormState>(() => initialForm(null))
  const [publication, setPublication] = useState<Publication | null>(null)
  const [plan, setPlan] = useState<PublishPlan | null>(null)
  const [reviewMessage, setReviewMessage] = useState('')
  const [runId, setRunId] = useState('')
  const appliedGatewayRef = useRef(false)
  const reviewingExistingPlan = Boolean(initialReview)

  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
    enabled: open,
  })

  const gatewayOptions: Gateway[] = useMemo(() => gateways.data ?? [], [gateways.data])

  useEffect(() => {
    if (!open) {
      appliedGatewayRef.current = false
      return
    }
    if (!appliedGatewayRef.current && gatewayOptions.length > 0) {
      appliedGatewayRef.current = true
      setGatewayId(gatewayOptions.find((gateway) => gateway.managementMode === 'manage')?.id ?? '')
    }
  }, [open, gatewayOptions])

  const publishable = useQuery({
    queryKey: ['publishable-models', gatewayId],
    queryFn: () => api.listPublishableModels(gatewayId),
    enabled: open && gatewayId !== '',
  })

  const models = publishable.data ?? []
  const selectedModel = models.find(
    (model) => `${model.modelEndpointId}:${model.deploymentName}` === modelKey,
  ) ?? null

  useEffect(() => {
    if (!open || !initialReview) return
    setPublication(initialReview.publication)
    setPlan(initialReview.plan)
    setReviewMessage(initialReview.message ?? '')
    setRunId('')
    setGatewayId(initialReview.publication.gatewayId)
    setStep('review')
  }, [open, initialReview])

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
        enforcement: buildEnforcement(form),
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
      const freshPlan = await api.createPublishPlan(publication.id)
      setPlan(freshPlan)
      setReviewMessage(error instanceof Error ? error.message : 'The publish plan is stale. Review the fresh plan before applying.')
      setStep('review')
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
    if (currentRun && terminalRunStatuses.includes(currentRun.status)) {
      void queryClient.invalidateQueries({ queryKey: ['publications'] })
      if (currentRun.status === 'succeeded') {
        onPublished('Published model to API Management.')
      }
    }
  }, [currentRun, onPublished, queryClient])

  function resetAndClose() {
    setStep('choose')
    setGatewayId('')
    setModelKey('')
    setForm(initialForm(null))
    setPublication(null)
    setPlan(null)
    setReviewMessage('')
    setRunId('')
    onClose()
  }

  const canConfigure = Boolean(gatewayId && selectedModel)
  const canReview = Boolean(form.apiName.trim() && form.apiPath.trim() && form.counterKeyExpression.trim())

  return (
    <Dialog open={open} onOpenChange={(_, data) => !data.open && resetAndClose()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>Publish a model</DialogTitle>
          <DialogContent>
            <div className={styles.intro}>
              <Text>Plan the API Management resources first, then explicitly apply the plan.</Text>
              <Text size={200}>Step {['choose', 'configure', 'review', 'apply'].indexOf(step) + 1} of 4</Text>
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
                          return (
                            <TableRow key={key}>
                              <TableCell>
                                <Checkbox
                                  aria-label={`Publish ${model.deploymentName}`}
                                  checked={modelKey === key}
                                  onChange={(_, data) => setModelKey(data.checked ? key : '')}
                                />
                              </TableCell>
                              <TableCell>
                                <div className={styles.nameCell}>
                                  <Text weight="semibold">{model.deploymentName}</Text>
                                  <Text size={200}>{model.modelName ?? 'Unknown model'}</Text>
                                  <Text size={200}>/{model.suggestedApiPath}</Text>
                                  {model.publicationStatus && <Badge appearance="tint">{model.publicationStatus}</Badge>}
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
                {createAndPlan.isError && <ErrorState error={createAndPlan.error} />}
              </div>
            )}

            {step === 'review' && plan && (
              <div className={styles.nameCell}>
                {reviewMessage && (
                  <MessageBar intent="warning">
                    <MessageBarBody>{reviewMessage}</MessageBarBody>
                  </MessageBar>
                )}
                {plan.warnings.map((warning) => (
                  <MessageBar key={warning} intent="warning">
                    <MessageBarBody><MessageBarTitle>Plan warning</MessageBarTitle>{warning}</MessageBarBody>
                  </MessageBar>
                ))}
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
                      {plan.steps.map((planStep) => (
                        <TableRow key={`${planStep.kind}-${planStep.name}`}>
                          <TableCell>{kindLabels[planStep.kind]} · {planStep.name}</TableCell>
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
                {applyError && <ErrorState error={applyError} />}
              </div>
            )}

            {step === 'apply' && (
              <div className={styles.nameCell}>
                {!currentRun || currentRun.status === 'running' ? <Loading label="Applying publish plan" /> : null}
                {run.isError && <ErrorState error={run.error} />}
                {currentRun && <RunResult run={currentRun} />}
              </div>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={resetAndClose}>Close</Button>
            {step === 'configure' && <Button appearance="secondary" onClick={() => setStep('choose')}>Back</Button>}
            {step === 'review' && !reviewingExistingPlan && <Button appearance="secondary" onClick={() => setStep('configure')}>Back</Button>}
            {step === 'choose' && (
              <Button appearance="primary" disabled={!canConfigure} onClick={() => setStep('configure')}>Configure</Button>
            )}
            {step === 'configure' && (
              <Button appearance="primary" disabled={!canReview || createAndPlan.isPending} onClick={() => createAndPlan.mutate()}>
                {createAndPlan.isPending ? 'Creating plan…' : 'Review plan'}
              </Button>
            )}
            {step === 'review' && (
              <Button appearance="primary" disabled={apply.isPending} onClick={() => apply.mutate()}>
                {apply.isPending ? 'Applying…' : 'Apply plan'}
              </Button>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
