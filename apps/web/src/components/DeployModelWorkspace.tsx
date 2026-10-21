import {
  Badge, Button, Checkbox, Field, Input, MessageBar, MessageBarBody, MessageBarTitle,
  Select, Text, Title3,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { formatTimestamp } from '../publication-state'
import type {
  DeployableModel, DeploymentRecoveryRequest, DeploymentTarget, ModelDeploymentCreate,
  ModelDeploymentPlan, ModelDeploymentRun, ModelEndpoint,
} from '../types'
import { EmptyState, ErrorState, Loading } from './AsyncState'
import { WorkspaceDialog } from './WorkspaceDialog'
import styles from './DeployModelWorkspace.module.css'

function modelKey(model: DeployableModel): string {
  return JSON.stringify([model.modelFormat, model.modelName, model.modelVersion,
    model.modelPublisher, model.source, model.sourceAccount])
}

function TargetSummary({ target }: { target: DeploymentTarget }) {
  return <dl className={styles.summary}>
    <div><dt>Azure account</dt><dd data-deployment-account>{target.accountName}</dd></div>
    <div><dt>Cloud and region</dt><dd>{target.azureCloud === 'government' ? 'Azure Government' : 'Azure Commercial'} · {target.location}</dd></div>
    <div><dt>Subscription</dt><dd data-deployment-identifier>{target.subscriptionId}</dd></div>
    <div><dt>Management identity</dt><dd>{target.azureConnectionId ? 'Selected Azure connection' : 'MOSAIC host managed identity'}</dd></div>
    <div className={styles.wide}><dt>Account resource ID</dt><dd data-deployment-identifier>{target.accountScope}</dd></div>
  </dl>
}

function ConfigurationSummary({ configuration }: { configuration: ModelDeploymentCreate }) {
  return <dl className={styles.summary}>
    <div><dt>New deployment</dt><dd data-deployment-identifier>{configuration.deploymentName}</dd></div>
    <div><dt>Model and exact version</dt><dd>{configuration.model.modelName} · {configuration.model.modelVersion}</dd></div>
    <div><dt>Model format</dt><dd>{configuration.model.modelFormat}{configuration.model.modelPublisher ? ` · ${configuration.model.modelPublisher}` : ''}</dd></div>
    <div><dt>Deployment type and capacity</dt><dd>{configuration.skuName} · {configuration.capacity} Azure capacity units</dd></div>
    <div><dt>Content filter</dt><dd>{configuration.raiPolicyName ?? 'Azure default; no override'}</dd></div>
    <div><dt>Version upgrades</dt><dd>{configuration.versionUpgradeOption === 'NoAutoUpgrade' ? 'No automatic upgrades'
      : configuration.versionUpgradeOption === 'OnceNewDefaultVersionAvailable' ? 'When a new default version is available'
      : configuration.versionUpgradeOption === 'OnceCurrentVersionExpired' ? 'When the current version expires'
      : 'Azure default; no override'}</dd></div>
    {configuration.model.source && <div className={styles.wide}><dt>Model source</dt><dd data-deployment-identifier>{configuration.model.source}</dd></div>}
    {configuration.model.sourceAccount && <div className={styles.wide}><dt>Source account</dt><dd data-deployment-identifier>{configuration.model.sourceAccount}</dd></div>}
  </dl>
}

export function DeployModelWorkspace({ endpoint, onClose }: {
  endpoint: ModelEndpoint
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [managementMode, setManagementMode] = useState(endpoint.managementMode ?? 'observe')
  const [optIn, setOptIn] = useState(false)
  const [search, setSearch] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [skuName, setSkuName] = useState('')
  const [capacity, setCapacity] = useState('')
  const [deploymentName, setDeploymentName] = useState('')
  const [raiPolicyName, setRaiPolicyName] = useState('')
  const [versionUpgradeOption, setVersionUpgradeOption] = useState<NonNullable<ModelDeploymentCreate['versionUpgradeOption']> | ''>('')
  const [review, setReview] = useState<ModelDeploymentPlan | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const [runId, setRunId] = useState('')
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false)
  const errorRef = useRef<HTMLDivElement>(null)
  const lastInvalidation = useRef('')
  const choseRun = useRef(false)

  const catalogue = useQuery({
    queryKey: ['deployable-models', endpoint.id],
    queryFn: () => api.getDeployableModels(endpoint.id),
    staleTime: 0, refetchOnMount: 'always',
  })
  const history = useQuery({
    queryKey: ['model-deployment-runs', endpoint.id],
    queryFn: () => api.listModelDeploymentRuns(endpoint.id),
  })
  const run = useQuery({
    queryKey: ['model-deployment-run', endpoint.id, runId],
    queryFn: () => api.getModelDeploymentRun(endpoint.id, runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => query.state.data?.status === 'running'
      || query.state.data?.refreshStatus === 'running' ? 2000 : false,
  })

  async function invalidateInventory() {
    await Promise.all([
      'model-endpoints', 'model-deployments', 'publishable-models', 'model-pools', 'pricing',
      'model-deployment-runs', 'available-models', 'deployable-models',
    ].map((key) => queryClient.invalidateQueries({ queryKey: [key] })))
  }

  useEffect(() => {
    if (catalogue.isPending || choseRun.current) return
    const unfinished = history.data?.find((item) => item.status === 'running' || item.status === 'interrupted')
    const operation = catalogue.data?.blockingRunId ?? unfinished?.id
    if (operation) {
      choseRun.current = true
      setRunId(operation)
    }
  }, [catalogue.data, catalogue.isPending, history.data])

  useEffect(() => {
    const result = run.data
    if (!result || result.status === 'running') return
    const key = `${result.id}:${result.status}:${result.refreshStatus}`
    if (lastInvalidation.current === key) return
    lastInvalidation.current = key
    void invalidateInventory()
  })

  const mode = useMutation({
    mutationKey: ['models-page'],
    mutationFn: (value: 'observe' | 'manage') => api.updateModelEndpoint(endpoint.id, { managementMode: value }),
    onSuccess: async (updated) => {
      setManagementMode(updated.managementMode ?? 'observe')
      setOptIn(false)
      await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
      await catalogue.refetch()
    },
  })
  const plan = useMutation({
    mutationKey: ['models-page'],
    mutationFn: (configuration: ModelDeploymentCreate) => api.planModelDeployment(endpoint.id, configuration),
    onSuccess: (result) => { setAcknowledged(false); setReview(result) },
  })
  function showRun(result: ModelDeploymentRun) {
    choseRun.current = true
    setRunId(result.id)
    queryClient.setQueryData(['model-deployment-run', endpoint.id, result.id], result)
    void queryClient.invalidateQueries({ queryKey: ['model-deployment-runs', endpoint.id] })
  }
  const apply = useMutation({
    mutationKey: ['models-page'],
    mutationFn: (planId: string) => api.applyModelDeployment(endpoint.id, planId),
    onSuccess: showRun,
    onError: () => queryClient.invalidateQueries({ queryKey: ['model-deployment-runs', endpoint.id] }),
  })
  const reconcile = useMutation({
    mutationKey: ['models-page'],
    mutationFn: (payload: DeploymentRecoveryRequest) => api.reconcileModelDeployment(endpoint.id, runId, payload),
    onSuccess: showRun,
  })
  const refresh = useMutation({
    mutationKey: ['models-page'],
    mutationFn: () => api.refreshModelDeployment(endpoint.id, runId),
    onSuccess: showRun,
  })
  const mutationError = mode.error ?? plan.error ?? apply.error ?? reconcile.error ?? refresh.error
  useEffect(() => { if (mutationError) errorRef.current?.focus() }, [mutationError])

  const model = catalogue.data?.models.find((item) => modelKey(item) === selectedModel)
  const sku = model?.skus.find((item) => item.name === skuName)
  const numericCapacity = Number(capacity)
  const validCapacity = capacity.trim() !== '' && Number.isSafeInteger(numericCapacity) && numericCapacity > 0
    && (sku?.capacity.minimum == null || numericCapacity >= sku.capacity.minimum)
    && (sku?.capacity.maximum == null || numericCapacity <= sku.capacity.maximum)
    && (!sku?.capacity.allowedValues.length || sku.capacity.allowedValues.includes(numericCapacity))
    && (!sku?.capacity.step || (numericCapacity - (sku.capacity.minimum ?? 0)) % sku.capacity.step === 0)
  const validName = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(deploymentName)
  const quota = sku?.quota
  const quotaVerified = quota && !quota.blocked && !catalogue.data?.quotaError
  const enoughQuota = quotaVerified && quota && numericCapacity <= quota.available
  const pending = mode.isPending || plan.isPending || apply.isPending || reconcile.isPending || refresh.isPending
  const canReview = managementMode === 'manage' && catalogue.isSuccess && catalogue.data.access.canDeploy
    && model && sku && validName && validCapacity && enoughQuota && !catalogue.isFetching
    && !history.isPending && !history.isError
    && !history.data?.some((item) => item.status === 'running' || item.status === 'interrupted')
  const filteredModels = catalogue.data?.models.filter((item) =>
    `${item.modelName} ${item.modelVersion} ${item.modelFormat} ${item.modelPublisher ?? ''}`
      .toLowerCase().includes(search.trim().toLowerCase())) ?? []

  function reviewConfiguration() {
    if (!model || !canReview) return
    plan.mutate({
      deploymentName,
      model: {
        modelFormat: model.modelFormat, modelName: model.modelName, modelVersion: model.modelVersion,
        modelPublisher: model.modelPublisher, source: model.source, sourceAccount: model.sourceAccount,
      },
      skuName, capacity: numericCapacity,
      raiPolicyName: raiPolicyName || null, versionUpgradeOption: versionUpgradeOption || null,
    })
  }

  return <WorkspaceDialog open title={runId ? 'Azure deployment operation' : review ? 'Review Azure model deployment' : 'Deploy a model'}
    description={`Create a new deployment on ${endpoint.name}. Existing deployments are never resized or replaced. API Management publication is a separate action.`}
    onOpenChange={(_, data) => { if (!data.open) onClose() }}
    dirty={!runId && Boolean(selectedModel || deploymentName || optIn)} pending={pending}>
    <div className={styles.workspace}>
      {mutationError && <div ref={errorRef} tabIndex={-1}><ErrorState error={mutationError} /></div>}
      {apply.isError && <Text>Submission may have reached the API. Retry this same reviewed plan or inspect its deployment history; do not submit a different plan to work around an unknown outcome.</Text>}
      {history.isError && <><ErrorState error={history.error} />
        <Button onClick={() => { void history.refetch() }}>Retry operation history</Button></>}
      {Boolean(history.data?.length) && <Field label="Deployment history">
        <Select aria-label="Deployment history" value={runId} onChange={(event) => {
          choseRun.current = true
          setRunId(event.target.value)
          setRecoveryConfirmed(false)
          setReview(null)
          apply.reset()
        }}>
          <option value="">New deployment</option>
          {history.data?.map((item) => <option key={item.id} value={item.id}>
            {item.configuration.deploymentName} · {item.status} · {formatTimestamp(item.startedAt)}
          </option>)}
        </Select>
      </Field>}
      {!runId && history.data?.some((item) => item.status === 'running' || item.status === 'interrupted') && <MessageBar intent="warning"><MessageBarBody>
        A previous deployment operation needs completion or reconciliation. Select it in deployment history before reviewing another create.
      </MessageBarBody></MessageBar>}
      {runId ? <>
        {run.isPending && <Loading label="Checking Azure deployment operation" />}
        {run.isError && <><ErrorState error={run.error} />
          <Button onClick={() => { void run.refetch() }}>Check operation status again</Button></>}
        {run.data && <>
          <div className={styles.sectionHeading}><Title3 as="h3">Deployment outcome</Title3>
            <Badge appearance="tint">{run.data.status === 'succeeded' ? 'Deployed to Azure'
              : run.data.status === 'interrupted' ? 'Outcome unconfirmed' : run.data.status === 'failed' ? 'Deployment failed' : 'Provisioning in Azure'}</Badge>
          </div>
          <TargetSummary target={run.data.target} />
          <ConfigurationSummary configuration={run.data.configuration} />
          {run.data.status === 'running' && <MessageBar intent="info"><MessageBarBody>
            Azure provisioning can take several minutes. You can leave this workspace and return through deployment history. MOSAIC will not retry an uncertain create.
          </MessageBarBody></MessageBar>}
          {run.data.error && <MessageBar intent={run.data.status === 'failed' ? 'error' : 'warning'}><MessageBarBody>{run.data.error}</MessageBarBody></MessageBar>}
          {run.data.status === 'succeeded' && <MessageBar intent={run.data.refreshStatus === 'failed' ? 'warning' : 'success'}><MessageBarBody>
            <MessageBarTitle>Azure deployment confirmed</MessageBarTitle>
            {run.data.refreshStatus === 'succeeded' ? 'MOSAIC inventory is up to date. Publish the model or add it to a compatible pool separately.'
              : run.data.refreshStatus === 'failed' ? `The model exists in Azure, but inventory refresh failed. ${run.data.refreshError ?? ''} Retry refresh, not deployment.`
              : 'Refreshing the endpoint inventory. No separate sync is needed.'}
          </MessageBarBody></MessageBar>}
          {run.data.status === 'succeeded' && run.data.refreshStatus === 'failed' && <Button onClick={() => refresh.mutate()} disabled={pending}>Retry inventory refresh</Button>}
          {run.data.status === 'succeeded' && run.data.refreshStatus !== 'succeeded' && <Button
            onClick={() => reconcile.mutate({ confirmWorkerStopped: false, confirmNoPendingOperation: false })} disabled={pending}>
            Reconcile interrupted refresh
          </Button>}
          {run.data.status === 'failed' && <Button
            onClick={() => reconcile.mutate({ confirmWorkerStopped: false, confirmNoPendingOperation: false })} disabled={pending}>
            Reconcile completed operation
          </Button>}
          {(run.data.status === 'interrupted' || run.data.status === 'running') && <section className={styles.section}>
            <Title3 as="h3">Reconcile with Azure</Title3>
            <Text>Checking the outcome only reads Azure; it never creates again or deletes a deployment. Before finalizing recovery, verify that the original deployment worker has stopped.</Text>
            <Button onClick={() => reconcile.mutate({ confirmWorkerStopped: false, confirmNoPendingOperation: false })} disabled={pending}>Check outcome in Azure</Button>
            <Checkbox checked={recoveryConfirmed} onChange={(_, data) => setRecoveryConfirmed(data.checked === true)}
              label="I verified the original deployment worker has stopped. If Azure reports no deployment, I also verified that no Azure operation for this deployment is pending." />
            <Button disabled={!recoveryConfirmed || pending}
              onClick={() => reconcile.mutate({ confirmWorkerStopped: true, confirmNoPendingOperation: true })}>
              Confirm worker-stopped recovery
            </Button>
          </section>}
        </>}
      </> : review ? <>
        <Title3 as="h3">Create one billable Azure deployment</Title3>
        <TargetSummary target={review.target} />
        <ConfigurationSummary configuration={review.configuration} />
        <Text>Verified quota: {review.checks.quota.available} available of {review.checks.quota.limit} {review.checks.quota.unit ?? 'Azure units'}.
          {review.checks.availableCapacity != null ? ` Reported physical capacity: ${review.checks.availableCapacity}.` : ''}
          {' '}Checks will run again immediately before creation.</Text>
        <MessageBar intent="warning"><MessageBarBody>
          <MessageBarTitle>Billing, availability, and prerequisites</MessageBarTitle>
          <ul className={styles.warnings}>{review.checks.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
          Marketplace agreements and partner-model prerequisites must already be satisfied in Azure. MOSAIC does not accept legal terms or buy quota.
        </MessageBarBody></MessageBar>
        <Checkbox checked={acknowledged} onChange={(_, data) => setAcknowledged(data.checked === true)}
          label="I reviewed the target account, quota, processing scope, prerequisites, and billing implications." />
        <div className={styles.actions}>
          <Button onClick={() => { setReview(null); apply.reset() }} disabled={pending}>Back to configuration</Button>
          <Button appearance="primary" disabled={!acknowledged || pending} onClick={() => apply.mutate(review.id)}>
            {apply.isPending ? 'Submitting deployment…' : 'Deploy to Azure'}
          </Button>
        </div>
      </> : <>
        {catalogue.isPending && <Loading label="Reading models, deployment permissions, and quota from Azure" />}
        {catalogue.isError && <><ErrorState error={catalogue.error} />
          <Button onClick={() => { void catalogue.refetch() }}>Retry Azure catalogue</Button></>}
        {catalogue.data && <>
          {catalogue.data.target && <TargetSummary target={catalogue.data.target} />}
          {endpoint.projectName && <Text>Deployments belong to this project's parent account, not the project itself.</Text>}
          <section className={styles.section}>
            <div className={styles.sectionHeading}><Title3 as="h3">Deployment management</Title3>
              <Badge appearance="tint">{managementMode === 'manage' ? 'Manage deployments' : 'Read-only'}</Badge>
            </div>
            <Text>{catalogue.data.access.message}</Text>
            {Boolean(catalogue.data.access.missingActions.length) && <ul className={styles.warnings}>
              {catalogue.data.access.missingActions.map((action) => <li key={action}><code>{action}</code></li>)}
            </ul>}
            {managementMode !== 'manage' ? <>
              <Checkbox checked={optIn} onChange={(_, data) => setOptIn(data.checked === true)}
                disabled={!catalogue.data.access.canDeploy}
                label="Allow MOSAIC to create billable deployments on this Azure account. This does not grant Azure roles or inference access." />
              <Button disabled={!optIn || !catalogue.data.access.canDeploy || pending} onClick={() => mode.mutate('manage')}>
                Enable Manage deployments
              </Button>
            </> : <Button onClick={() => mode.mutate('observe')} disabled={pending}>Return to read-only</Button>}
          </section>
          <section className={styles.section}>
            <div className={styles.sectionHeading}><Title3 as="h3">Model configuration</Title3>
              <Button onClick={() => { void catalogue.refetch() }} disabled={catalogue.isFetching}>Check catalogue again</Button>
            </div>
            <Text size={200}>Live Azure catalogue checked {formatTimestamp(catalogue.data.checkedAt)}. Choose an exact version and one of its advertised deployment types.</Text>
            {catalogue.data.quotaError && <MessageBar intent="warning"><MessageBarBody>{catalogue.data.quotaError} Deployment is blocked until quota can be verified.</MessageBarBody></MessageBar>}
            <div className={styles.formGrid}>
              <Field className={styles.wide} label="Search available models">
                <Input value={search} onChange={(_, data) => {
                  setSearch(data.value)
                  setSelectedModel('')
                  setSkuName('')
                  setCapacity('')
                }} placeholder="Search name, version, format, or publisher" />
              </Field>
              <Field className={styles.wide} label="Model and exact version" required>
                <Select aria-label="Model and exact version" value={selectedModel} onChange={(event) => {
                  const next = catalogue.data?.models.find((item) => modelKey(item) === event.target.value)
                  setSelectedModel(event.target.value)
                  setSkuName('')
                  setCapacity('')
                  setDeploymentName((current) => current || next?.modelName || '')
                  plan.reset()
                }}>
                  <option value="">Choose a model version</option>
                  {filteredModels.map((item) => <option key={modelKey(item)} value={modelKey(item)}>
                    {item.modelName} · {item.modelVersion} · {item.modelFormat}{item.modelPublisher ? ` · ${item.modelPublisher}` : ''}
                  </option>)}
                </Select>
              </Field>
              {!filteredModels.length && <div className={styles.wide}><EmptyState title={catalogue.data.models.length ? 'No models match this search' : 'Azure reported no deployable models'}>
                {catalogue.data.models.length ? 'Try a different model name or version.' : 'Check model availability for this account and region in Azure, then refresh the catalogue.'}
              </EmptyState></div>}
              {model?.lifecycleStatus && <Text className={styles.wide}>{model.lifecycleStatus}{model.deprecationInference ? ` · Inference deprecation: ${model.deprecationInference}` : ''}</Text>}
              {model && !model.skus.length && <Text className={styles.wide}>
                Azure advertises no deployment types for this model version on this account. Choose another version or check its availability in Azure.
              </Text>}
              <Field label="Deployment type" required>
                <Select aria-label="Deployment type" value={skuName} disabled={!model?.skus.length} onChange={(event) => {
                  setSkuName(event.target.value)
                  const next = model?.skus.find((item) => item.name === event.target.value)
                  setCapacity(String(next?.capacity.default ?? next?.capacity.minimum ?? 1))
                }}>
                  <option value="">Choose an Azure SKU</option>
                  {model?.skus.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
                </Select>
              </Field>
              <Field label="Capacity (Azure SKU units)" required hint={sku ? [
                sku.capacity.minimum != null ? `Minimum ${sku.capacity.minimum}.` : '',
                sku.capacity.maximum != null ? `Maximum ${sku.capacity.maximum}.` : '',
                sku.capacity.step ? `Increment ${sku.capacity.step}.` : '',
                sku.capacity.allowedValues.length ? `Allowed: ${sku.capacity.allowedValues.join(', ')}.` : '',
              ].filter(Boolean).join(' ') : 'Units depend on the selected Azure deployment type; they are not universally thousands of tokens per minute.'}
                validationState={capacity && !validCapacity ? 'error' : 'none'}
                validationMessage={capacity && !validCapacity ? 'Enter a positive whole number within Azure’s advertised capacity limits.' : undefined}>
                <Input type="number" value={capacity} disabled={!sku} min={sku?.capacity.minimum ?? 1}
                  max={sku?.capacity.maximum ?? undefined} step={sku?.capacity.step ?? 1}
                  onChange={(_, data) => setCapacity(data.value)} />
              </Field>
              {sku && <div className={styles.wide}><MessageBar intent={enoughQuota ? 'info' : 'warning'}><MessageBarBody>
                {sku.quota && quotaVerified
                  ? `Subscription quota: ${sku.quota.available} available of ${sku.quota.limit} ${sku.quota.unit ?? 'Azure units'}. ${enoughQuota ? 'This is not a capacity reservation.' : 'The requested capacity exceeds available quota.'}`
                  : 'Quota is unavailable, blocked, or has no verified mapping for this SKU. Deployment is blocked.'}
              </MessageBarBody></MessageBar></div>}
              <Field label="New deployment name" required hint="1–64 letters, digits, dots, hyphens, or underscores. Start with a letter or digit."
                validationState={deploymentName && !validName ? 'error' : 'none'}
                validationMessage={deploymentName && !validName ? 'Enter a valid new deployment name.' : undefined}>
                <Input value={deploymentName} maxLength={64} onChange={(_, data) => setDeploymentName(data.value)} />
              </Field>
              <Field label="Content-filter policy" hint={catalogue.data.policyError ?? 'Only existing policies can be selected. No new policy is created.'}>
                <Select aria-label="Content-filter policy" value={raiPolicyName} disabled={Boolean(catalogue.data.policyError)}
                  onChange={(event) => setRaiPolicyName(event.target.value)}>
                  <option value="">Azure default; no override</option>
                  {catalogue.data.raiPolicies.map((policy) => <option key={policy} value={policy}>{policy}</option>)}
                </Select>
              </Field>
              <Field className={styles.wide} label="Version upgrade behavior">
                <Select aria-label="Version upgrade behavior" value={versionUpgradeOption}
                  onChange={(event) => setVersionUpgradeOption(event.target.value as typeof versionUpgradeOption)}>
                  <option value="">Azure default; no override</option>
                  <option value="OnceNewDefaultVersionAvailable">When a new default version is available</option>
                  <option value="OnceCurrentVersionExpired">When the current version expires</option>
                  <option value="NoAutoUpgrade">No automatic upgrades</option>
                </Select>
              </Field>
            </div>
            <div className={styles.actions}>
              <Text size={200}>Read-only is the default. Permissions, quota, and model eligibility must be verified before review and again before Azure creation.</Text>
              <Button appearance="primary" onClick={reviewConfiguration} disabled={!canReview || pending}>
                {plan.isPending ? 'Checking deployment…' : 'Review deployment'}
              </Button>
            </div>
          </section>
        </>}
      </>}
    </div>
  </WorkspaceDialog>
}
