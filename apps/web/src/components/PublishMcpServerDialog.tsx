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
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { describeLimits } from '../entitlement-limits'
import { environmentBlockedVerdict, lookupCompatibility, useEnvironmentCatalog } from '../environments'
import { ENTITLEMENT_SUBJECT_KIND_LABELS, PRINCIPAL_KIND_LABELS } from '../labels'
import { runtimeConfig } from '../runtime-config'
import type {
  Gateway,
  McpEndpoint,
  McpPublication,
  PublishAction,
  PublishedResourceKind,
  PublishPlan,
  PublishRun,
  PublishRunStatus,
  PublishStepStatus,
} from '../types'
import { ErrorState, Loading } from './AsyncState'
import { EnvironmentBadge } from './EnvironmentBadge'
import { ModelAccessRecovery } from './ModelAccessRecovery'
import { PolicyFacetItem } from './PolicyFacets'
import styles from './ImportFromGatewayDialog.module.css'

type Step = 'choose' | 'configure' | 'review' | 'apply'

type FormState = {
  displayName: string
  apiName: string
  apiPath: string
}

const actionLabels: Record<PublishAction, string> = {
  create: 'Create',
  update: 'Update',
  delete: 'Delete',
  noChange: 'No change',
}

const kindLabels: Record<PublishedResourceKind, string> = {
  namedValue: 'Named value',
  policyFragment: 'Policy fragment',
  backend: 'Backend',
  backendPool: 'Backend pool',
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
  'interrupted',
]

function slug(value: string): string {
  const cleaned = value
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return cleaned || 'server'
}

function defaultsFor(endpoint: McpEndpoint | null): FormState {
  const name = endpoint?.name ?? ''
  const base = slug(name)
  return {
    displayName: name,
    apiName: base ? `mosaic-mcp-${base}` : '',
    apiPath: base ? `mosaic/mcp/${base}` : '',
  }
}

function endpointBlockers(endpoint: McpEndpoint | null): string[] {
  if (!endpoint) return []
  const reasons: string[] = []
  if (endpoint.authMode === 'apiKey') {
    reasons.push("MOSAIC can't publish an MCP server that needs an API key yet.")
  }
  if (endpoint.authMode === 'managedIdentity' && !endpoint.resourceAudience) {
    reasons.push('Managed identity publishing needs the backend token audience on the registered server.')
  }
  if (endpoint.capabilities.transportType === 'sse') {
    reasons.push('SSE servers cannot be published. Register a Streamable HTTP endpoint instead.')
  }
  if (endpoint.status === 'unsupportedTransport') {
    reasons.push('This server answered with a transport MOSAIC cannot publish.')
  }
  if (endpoint.status === 'unsupportedProtocol') {
    reasons.push('This server speaks an MCP protocol version MOSAIC cannot publish.')
  }
  return reasons
}

function RunResult({ run }: { run: PublishRun }) {
  const orphans = run.orphanedResources ?? []
  return (
    <div className={styles.nameCell}>
      {run.status === 'interrupted' && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Apply interrupted — runtime state unknown</MessageBarTitle>
            Some changes may have reached API Management. Gateway access is not confirmed.
            Reconcile this run before attempting another apply.
          </MessageBarBody>
        </MessageBar>
      )}
      {run.status === 'interrupted' && <ModelAccessRecovery publicationId={run.publicationId} runId={run.id} target="mcp" />}
      {run.status === 'failed' && (
        <MessageBar intent="error">
          <MessageBarBody>Apply failed. The gateway should deny calls until this MCP publication is applied again.</MessageBarBody>
        </MessageBar>
      )}
      {run.status === 'succeeded' && (
        <MessageBar intent={runtimeConfig.authMode === 'local' ? 'warning' : 'success'}>
          <MessageBarBody>
            {runtimeConfig.authMode === 'local'
              ? 'Local development service reported completion; live APIM apply is not verified.'
              : 'The service reports the MCP plan applied. Allow for gateway propagation; live invocation is not verified.'}
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
        <Table size="small" aria-label="MCP publish run steps">
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

function McpAccessReview({ plan }: { plan: PublishPlan }) {
  const api = useMosaicApi()
  const snapshot = plan.mcpAccessSnapshot
  const principals = useQuery({
    queryKey: ['principals'],
    queryFn: () => api.listPrincipals(),
    enabled: Boolean(snapshot),
  })
  if (!snapshot) return null
  const subjectKindLabel = (subject: (typeof snapshot.grants)[number]['subject']) => {
    const principal = principals.data?.find((item) => item.id === subject.id)
    return principal ? PRINCIPAL_KIND_LABELS[principal.kind] : ENTITLEMENT_SUBJECT_KIND_LABELS[subject.kind]
  }
  return (
    <section className={styles.nameCell} aria-label="MCP access review">
      <Title3 as="h3">MCP access changes</Title3>
      <Text>
        This plan applies the complete target grant set for this MCP server. The gateway enforces
        Entra tokens and per-grant call limits; MCP token limits are not supported.
      </Text>
      <Text size={200}>Access version: {plan.previousAccessVersion ?? 'none'} → {snapshot.version}</Text>
      <Text size={200}>Audience: {snapshot.audience}</Text>
      <Text size={200}>Delegated scope: api://{snapshot.audience}/{snapshot.delegatedScope}</Text>
      <Text size={200}>Application role: {snapshot.applicationRole}</Text>
      {snapshot.grants.length === 0 ? (
        <Text>No grants are in this target. The gateway will deny callers.</Text>
      ) : (
        <div className={styles.tableScroll}>
          <Table size="small" aria-label="All target MCP grants">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Subject</TableHeaderCell>
                <TableHeaderCell>Target access</TableHeaderCell>
                <TableHeaderCell>Grant path</TableHeaderCell>
                <TableHeaderCell>Call limits</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {snapshot.grants.map((grant) => (
                <TableRow key={grant.entitlementId}>
                  <TableCell>
                    <Text block weight="semibold">{grant.displayName}</Text>
                    <Text block size={200}>{subjectKindLabel(grant.subject)} · {grant.objectId}</Text>
                    <Text block size={200}>Grant: {grant.entitlementId}</Text>
                  </TableCell>
                  <TableCell>{grant.enabled ? 'Enabled' : 'Disabled — revoke at the gateway'}</TableCell>
                  <TableCell>{grant.subject.kind === 'securityGroup' ? 'Security group' : 'Direct'}</TableCell>
                  <TableCell>
                    {describeLimits({ enforcement: grant.enforcement }).map((limit) => (
                      <Text block key={limit}>{limit}</Text>
                    ))}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </section>
  )
}

export function PublishMcpServerDialog({
  open,
  onClose,
  onPublished,
  initialReview,
}: {
  open: boolean
  onClose: () => void
  onPublished: (message: string) => void
  initialReview?: {
    publication: McpPublication
    plan: PublishPlan
    message?: string
  } | null
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [step, setStep] = useState<Step>('choose')
  const [gatewayId, setGatewayId] = useState('')
  const [endpointId, setEndpointId] = useState('')
  const [form, setForm] = useState<FormState>(() => defaultsFor(null))
  const [publication, setPublication] = useState<McpPublication | null>(null)
  const [plan, setPlan] = useState<PublishPlan | null>(null)
  const [reviewMessage, setReviewMessage] = useState('')
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
  const catalog = useEnvironmentCatalog()
  const selectedGateway = gatewayOptions.find((gateway) => gateway.id === gatewayId)

  useEffect(() => {
    if (!open || reviewingExistingPlan) {
      appliedGatewayRef.current = false
      return
    }
    if (!appliedGatewayRef.current && gatewayOptions.length > 0) {
      appliedGatewayRef.current = true
      setGatewayId(gatewayOptions.find((gateway) => gateway.managementMode === 'manage')?.id ?? '')
    }
  }, [gatewayOptions, open, reviewingExistingPlan])

  const endpoints = useQuery({
    queryKey: ['mcp-endpoints'],
    queryFn: () => api.listMcpEndpoints(),
    enabled: open && !reviewingExistingPlan,
  })
  const capability = useQuery({
    queryKey: ['mcp-publishing-capability', gatewayId],
    queryFn: () => api.getMcpPublishingCapability(gatewayId),
    enabled: open && !reviewingExistingPlan && gatewayId !== '',
  })
  const endpoint = (endpoints.data ?? []).find((item) => item.id === endpointId) ?? null
  const localBlockers = endpointBlockers(endpoint)
  const selectedEnvironmentVerdict = lookupCompatibility(catalog.data, selectedGateway?.environment, endpoint?.environment)
  const environmentBlocked = selectedEnvironmentVerdict?.level === 'blocked'

  useEffect(() => {
    if (!endpoint) return
    setForm(defaultsFor(endpoint))
  }, [endpoint])

  useEffect(() => {
    if (!open || !initialReview) return
    setPublication(initialReview.publication)
    setPlan(initialReview.plan)
    setReviewMessage(initialReview.message ?? '')
    setRunId('')
    setInvalidPlan(false)
    setRefreshError(null)
    setGatewayId(initialReview.publication.gatewayId)
    setStep('review')
  }, [initialReview, open])

  const createAndPlan = useMutation({
    mutationFn: async () => {
      if (!endpoint) throw new Error('Choose an MCP server before reviewing the plan.')
      const apiName = form.apiName.trim()
      const apiPath = form.apiPath.trim()
      const displayName = form.displayName.trim() || endpoint.name
      // Reuse the draft an earlier attempt in this dialog created, so going back or retrying a
      // failed plan doesn't collide with it. Names that shape API Management resources can't be
      // edited in place, so a draft that no longer matches the form is replaced. It was never
      // applied, and the service refuses the delete if it owns gateway state.
      let draft = publication
      if (
        draft &&
        (draft.gatewayId !== gatewayId ||
          draft.mcpEndpointId !== endpoint.id ||
          draft.apiName !== apiName ||
          draft.apiPath !== apiPath)
      ) {
        await api.deleteMcpPublication(draft.id)
        setPublication(null)
        draft = null
      }
      if (draft && draft.displayName !== displayName) {
        draft = await api.updateMcpPublication(draft.id, { displayName })
        setPublication(draft)
      }
      if (!draft) {
        draft = await api.createMcpPublication({
          gatewayId,
          mcpEndpointId: endpoint.id,
          displayName: form.displayName.trim() || undefined,
          apiName: apiName || undefined,
          apiPath: apiPath || undefined,
        })
        setPublication(draft)
      }
      const createdPlan = await api.planMcpPublication(draft.id)
      return { created: draft, createdPlan }
    },
    onSuccess: ({ created, createdPlan }) => {
      setPublication(created)
      setPlan(createdPlan)
      setReviewMessage('')
      setInvalidPlan(false)
      setRefreshError(null)
      setStep('review')
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
    },
  })

  const apply = useMutation({
    mutationFn: async () => {
      if (!publication || !plan) throw new Error('Review the plan before applying it.')
      return await api.applyMcpPublication(publication.id, plan.id)
    },
    onSuccess: (run) => {
      setRunId(run.id)
      setReviewMessage('')
      setStep('apply')
      void queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
    },
    onError: async (error) => {
      if (!publication || (error as { status?: number }).status !== 409) return
      if (environmentBlockedVerdict(error)) return
      setInvalidPlan(true)
      setReviewMessage(error instanceof Error ? error.message : 'The publish plan is stale.')
      try {
        const freshPlan = await api.planMcpPublication(publication.id)
        setPlan(freshPlan)
        setInvalidPlan(false)
        setStep('review')
      } catch (refreshFailure) {
        setRefreshError(refreshFailure instanceof Error ? refreshFailure : new Error('Unable to refresh this plan. Close and review again.'))
      }
      void queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
    },
  })
  const applyError =
    apply.error && ((apply.error as { status?: number }).status !== 409 || environmentBlockedVerdict(apply.error)) ? apply.error : null

  const run = useQuery({
    queryKey: ['mcp-publish-run', publication?.id, runId],
    queryFn: () => api.getMcpPublishRun(publication!.id, runId),
    enabled: Boolean(open && publication && runId),
    refetchInterval: (query) =>
      query.state.data && terminalRunStatuses.includes(query.state.data.status) ? false : 1000,
  })
  const currentRun = run.data ?? apply.data ?? null

  useEffect(() => {
    if (currentRun && terminalRunStatuses.includes(currentRun.status) && notifiedRunRef.current !== currentRun.id) {
      notifiedRunRef.current = currentRun.id
      void queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      void queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      void queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] })
      if (currentRun.status === 'succeeded') {
        onPublished(runtimeConfig.authMode === 'local'
          ? 'Local development service reported completion. Live APIM apply is not verified.'
          : 'The service reports the MCP plan applied. Allow for APIM propagation; live invocation is not verified.')
      }
    }
  }, [currentRun, onPublished, queryClient])

  function resetAndClose() {
    setStep('choose')
    setGatewayId('')
    setEndpointId('')
    setForm(defaultsFor(null))
    setPublication(null)
    setPlan(null)
    setReviewMessage('')
    setRunId('')
    setRefreshError(null)
    setInvalidPlan(false)
    notifiedRunRef.current = ''
    apply.reset()
    createAndPlan.reset()
    onClose()
  }

  const canConfigure = Boolean(
    gatewayId &&
    endpoint &&
    capability.data?.supported &&
    localBlockers.length === 0 &&
    !environmentBlocked,
  )
  const canReview = Boolean(form.apiName.trim() && form.apiPath.trim() && !environmentBlocked)
  const missingAccessReview = Boolean(plan && !plan.mcpAccessSnapshot)
  const createEnvironmentBlocked = environmentBlockedVerdict(createAndPlan.error)
  const applyEnvironmentBlocked = environmentBlockedVerdict(apply.error)
  const nothingToApply = Boolean(
    plan && plan.steps.length > 0 && plan.steps.every((planStep) => planStep.action === 'noChange'),
  )

  return (
    <Dialog open={open} onOpenChange={(_, data) => !data.open && resetAndClose()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{initialReview ? 'Review MCP access' : 'Publish an MCP server'}</DialogTitle>
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
                {selectedGateway && (
                  <Text size={200}>
                    Gateway environment:{' '}
                    <EnvironmentBadge environment={selectedGateway.environment} catalog={catalog.data} size="small" />
                  </Text>
                )}
                {gateways.isError && <ErrorState error={gateways.error} />}
                {capability.isPending && gatewayId && <Loading label="Checking gateway capability" />}
                {capability.isError && <ErrorState error={capability.error} />}
                {capability.data?.reasons.map((reason) => (
                  <MessageBar key={reason} intent="error">
                    <MessageBarBody><MessageBarTitle>Gateway cannot publish MCP servers</MessageBarTitle>{reason}</MessageBarBody>
                  </MessageBar>
                ))}
                {capability.data?.warnings.map((warning) => (
                  <MessageBar key={warning} intent="warning">
                    <MessageBarBody><MessageBarTitle>Capability warning</MessageBarTitle>{warning}</MessageBarBody>
                  </MessageBar>
                ))}
                {endpoints.isPending && <Loading label="Loading registered MCP servers" />}
                {endpoints.isError && <ErrorState error={endpoints.error} />}
                {endpoints.data && endpoints.data.length === 0 && (
                  <Text>No registered MCP servers are available. Register a server before publishing it.</Text>
                )}
                {endpoints.data && endpoints.data.length > 0 && (
                  <div className={styles.tableScroll}>
                    <Table size="small" aria-label="Publishable MCP servers">
                      <TableHeader>
                        <TableRow>
                          <TableHeaderCell>Choose</TableHeaderCell>
                          <TableHeaderCell>Server</TableHeaderCell>
                          <TableHeaderCell>Publishing readiness</TableHeaderCell>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {endpoints.data.map((item) => {
                          const blockers = endpointBlockers(item)
                          const environmentVerdict = lookupCompatibility(catalog.data, selectedGateway?.environment, item.environment)
                          const blockedByEnvironment = environmentVerdict?.level === 'blocked'
                          const warningByEnvironment = environmentVerdict?.level === 'warning'
                          return (
                            <TableRow key={item.id}>
                              <TableCell>
                                <Checkbox
                                  aria-label={`Publish ${item.name}`}
                                  checked={endpointId === item.id}
                                  disabled={blockers.length > 0 || blockedByEnvironment}
                                  onChange={(_, data) => setEndpointId(data.checked ? item.id : '')}
                                />
                              </TableCell>
                              <TableCell>
                                <div className={styles.nameCell}>
                                  <Text weight="semibold">{item.name}</Text>
                                  <Text size={200}>{item.endpoint}</Text>
                                  <Text size={200}>{item.inventory.tools} tools · {item.authMode}</Text>
                                  <Text size={200}>
                                    Server environment:{' '}
                                    <EnvironmentBadge environment={item.environment} catalog={catalog.data} size="small" />
                                  </Text>
                                </div>
                              </TableCell>
                              <TableCell>
                                {blockers.length === 0 && !blockedByEnvironment ? (
                                  <Badge appearance="tint">Ready to plan</Badge>
                                ) : (
                                  <div className={styles.nameCell}>
                                    <Badge appearance="tint" color="warning">Not publishable</Badge>
                                    {blockers.map((reason) => <Text key={reason} size={200}>{reason}</Text>)}
                                    {blockedByEnvironment && <Text size={200}>{environmentVerdict.reason}</Text>}
                                  </div>
                                )}
                                {warningByEnvironment && <Text block size={200}>{environmentVerdict.reason}</Text>}
                              </TableCell>
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
                <Text size={200}>
                  Defaults are shown below. Leave a field unchanged to use the computed publication names.
                </Text>
                <Field label="Display name">
                  <Input value={form.displayName} onChange={(_, data) => setForm({ ...form, displayName: data.value })} />
                </Field>
                <Field label="API name" required>
                  <Input value={form.apiName} onChange={(_, data) => setForm({ ...form, apiName: data.value })} />
                </Field>
                <Field label="API path" required>
                  <Input value={form.apiPath} onChange={(_, data) => setForm({ ...form, apiPath: data.value })} />
                </Field>
                {createAndPlan.isError && (
                  createEnvironmentBlocked ? (
                    <MessageBar intent="error">
                      <MessageBarBody>
                        <MessageBarTitle>Environment rules block this publication</MessageBarTitle>
                        {createEnvironmentBlocked.reason}
                      </MessageBarBody>
                    </MessageBar>
                  ) : (
                    <ErrorState error={createAndPlan.error} />
                  )
                )}
              </div>
            )}

            {step === 'review' && plan && (
              <div className={styles.nameCell}>
                {reviewMessage && (
                  <MessageBar intent="warning">
                    <MessageBarBody>
                      <MessageBarTitle>MOSAIC didn't apply the plan you reviewed</MessageBarTitle>
                      {reviewMessage}
                      {!invalidPlan && (
                        <Text block>MOSAIC has already re-planned. Review the fresh plan below before you apply it.</Text>
                      )}
                    </MessageBarBody>
                  </MessageBar>
                )}
                {nothingToApply && (
                  <MessageBar intent="info">
                    <MessageBarBody>API Management already matches this MCP publication. Nothing to apply.</MessageBarBody>
                  </MessageBar>
                )}
                {plan.warnings.map((warning) => (
                  <MessageBar key={warning} intent="warning">
                    <MessageBarBody><MessageBarTitle>Plan warning</MessageBarTitle>{warning}</MessageBarBody>
                  </MessageBar>
                ))}
                <McpAccessReview plan={plan} />
                {missingAccessReview && (
                  <MessageBar intent="error">
                    <MessageBarBody>The MCP access snapshot is missing. Close and request a fresh plan; access changes cannot be applied without reviewing all target grants.</MessageBarBody>
                  </MessageBar>
                )}
                <Title3 as="h3">Plan steps</Title3>
                <div className={styles.tableScroll}>
                  <Table size="small" aria-label="MCP publish plan steps">
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
                {applyEnvironmentBlocked && (
                  <MessageBar intent="error">
                    <MessageBarBody>
                      <MessageBarTitle>Environment rules block this publication</MessageBarTitle>
                      {applyEnvironmentBlocked.reason}
                    </MessageBarBody>
                  </MessageBar>
                )}
                {applyError && !applyEnvironmentBlocked && <ErrorState error={applyError} />}
                {refreshError && <ErrorState error={refreshError} />}
              </div>
            )}

            {step === 'apply' && (
              <div className={styles.nameCell}>
                {!currentRun || currentRun.status === 'running' ? <Loading label="Applying MCP publish plan" /> : null}
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
              <Button
                appearance="primary"
                disabled={!canReview || createAndPlan.isPending}
                onClick={() => {
                  apply.reset()
                  createAndPlan.mutate()
                }}
              >
                {createAndPlan.isPending ? 'Creating plan…' : 'Review plan'}
              </Button>
            )}
            {step === 'review' && (
              <Button appearance="primary" disabled={apply.isPending || invalidPlan || missingAccessReview || nothingToApply} onClick={() => apply.mutate()}>
                {apply.isPending ? 'Applying…' : 'Apply plan'}
              </Button>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
