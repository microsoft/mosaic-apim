import {
  Badge,
  Button,
  Card,
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
  Tab,
  TabList,
  Text,
  Title3,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { PublishModelDialog } from '../components/PublishModelDialog'
import { PageHeader } from '../components/PageHeader'
import { AI_KIND_LABELS } from '../labels'
import type {
  CatalogVisibility,
  Gateway,
  GatewayRuntimeAccess,
  ModelEndpoint,
  ModelEndpointStatus,
  ModelProvider,
  Publication,
  PublishPlan,
  PublicationStatus,
  PublishRun,
  SuggestionSource,
} from '../types'
import styles from './ModelsPage.module.css'

const statusLabels: Record<ModelEndpointStatus, string> = {
  pending: 'Not checked',
  connected: 'Connected',
  degraded: 'Partial data',
  unauthorized: 'Access needed',
  unreachable: 'Unreachable',
}

const statusClasses: Record<ModelEndpointStatus, string> = {
  pending: styles.pendingBadge,
  connected: styles.connectedBadge,
  degraded: styles.degradedBadge,
  unauthorized: styles.attentionBadge,
  unreachable: styles.attentionBadge,
}

const providerLabels: Record<ModelProvider, string> = {
  azureOpenAi: 'Azure OpenAI',
  azureAiFoundry: 'Azure AI Foundry',
  openAiCompatible: 'OpenAI compatible',
}

const sourceLabels: Record<SuggestionSource, string> = {
  bootstrap: 'Deployed with MOSAIC',
  gatewayBackend: 'Used by a gateway',
  subscriptionScan: 'Found in a subscription',
}


const publicationStatusLabels: Record<PublicationStatus, string> = {
  draft: 'Draft',
  planned: 'Planned',
  applying: 'Applying',
  published: 'Published',
  failed: 'Failed',
  rolledBack: 'Rolled back',
}

function PublicationStatusBadge({ status }: { status: PublicationStatus }) {
  const attention = status === 'failed' || status === 'rolledBack'
  const active = status === 'applying' || status === 'planned'
  return (
    <Badge
      appearance="tint"
      className={attention ? styles.statusAttention : active ? styles.statusSyncing : styles.statusReady}
    >
      {publicationStatusLabels[status]}
    </Badge>
  )
}

function PublishedModels({ onMessage }: { onMessage: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [review, setReview] = useState<{
    publication: Publication
    plan: PublishPlan
    message?: string
  } | null>(null)

  const publications = useQuery({
    queryKey: ['publications'],
    queryFn: () => api.listPublications(),
  })
  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
  })

  const gatewaysById = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) map.set(gateway.id, gateway)
    return map
  }, [gateways.data])

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ['publications'] })
    await queryClient.invalidateQueries({ queryKey: ['publishable-models'] })
  }

  const replan = useMutation({
    mutationFn: (publicationId: string) => api.createPublishPlan(publicationId),
    onSuccess: async () => {
      await refresh()
      onMessage('Created a fresh publish plan. Review it before applying.')
    },
  })
  const reviewPlan = useMutation({
    mutationFn: async (publication: Publication) => ({
      publication,
      plan: await api.createPublishPlan(publication.id),
    }),
    onSuccess: ({ publication, plan }) => {
      setReview({ publication, plan })
    },
  })
  const apply = useMutation({
    mutationFn: async (publication: Publication): Promise<PublishRun> => {
      if (!publication.lastPlanId) throw new Error('Review the publish plan before applying it.')
      return await api.applyPublishPlan(publication.id, publication.lastPlanId)
    },
    onSuccess: async (run) => {
      await refresh()
      onMessage(
        run.status === 'succeeded'
          ? 'Published model to API Management.'
          : 'Publish run started. Refresh this page to see the latest status.',
      )
    },
    onError: async (error, publication) => {
      if (!(error instanceof ApiError) || error.status !== 409) return
      // The server rejected the plan because the publication changed under it. Produce a fresh
      // plan and put the administrator back in front of it rather than retrying silently.
      try {
        const plan = await api.createPublishPlan(publication.id)
        setReview({ publication, plan, message: error.message })
      } catch (replanError) {
        onMessage(
          replanError instanceof Error
            ? `The publish plan is stale and MOSAIC could not produce a new one: ${replanError.message}`
            : 'The publish plan is stale and MOSAIC could not produce a new one.',
        )
      }
      await refresh()
    },
  })
  const applyError =
    apply.error && !(apply.error instanceof ApiError && apply.error.status === 409)
      ? apply.error
      : null
  const unpublish = useMutation({
    mutationFn: (publicationId: string) => api.unpublishPublication(publicationId),
    onSuccess: async () => {
      await refresh()
      onMessage('Started unpublishing resources from API Management.')
    },
  })
  const remove = useMutation({
    mutationFn: (publicationId: string) => api.deletePublication(publicationId),
    onSuccess: async () => {
      await refresh()
      onMessage('Removed the publication record.')
    },
  })

  return (
    <>
    <Card className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <Title3 as="h2">Published models</Title3>
          <Text>Model deployments MOSAIC has planned or published into API Management.</Text>
        </div>
      </div>
      {(replan.isError || reviewPlan.isError || applyError || unpublish.isError || remove.isError) && (
        <ErrorState error={replan.error ?? reviewPlan.error ?? applyError ?? unpublish.error ?? remove.error} />
      )}
      {publications.isPending && <Loading label="Loading published models" />}
      {publications.isError && <ErrorState error={publications.error} />}
      {publications.isSuccess &&
        (publications.data.length === 0 ? (
          <EmptyState title="No models published yet">
            Publish a registered model deployment to create APIM resources through an explicit plan and apply flow.
          </EmptyState>
        ) : (
          <div className={styles.tableWrap}>
            <Table aria-label="Published models">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Publication</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Gateway</TableHeaderCell>
                  <TableHeaderCell>API path</TableHeaderCell>
                  <TableHeaderCell>Last applied</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {publications.data.map((publication) => (
                  <TableRow key={publication.id}>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <span className={styles.primaryCell}>{publication.displayName}</span>
                        <span className={styles.secondaryCell}>{publication.deploymentName}</span>
                      </div>
                    </TableCell>
                    <TableCell><PublicationStatusBadge status={publication.status} /></TableCell>
                    <TableCell>
                      <Link to={`/gateways/${publication.gatewayId}`}>
                        {gatewaysById.get(publication.gatewayId)?.name ?? publication.gatewayId}
                      </Link>
                    </TableCell>
                    <TableCell>/{publication.apiPath}</TableCell>
                    <TableCell>{formatTimestamp(publication.lastAppliedAt)}</TableCell>
                    <TableCell>
                      <div className={styles.actionRow}>
                        <Button appearance="secondary" disabled={replan.isPending} onClick={() => replan.mutate(publication.id)}>Re-plan</Button>
                        <Button
                          appearance="secondary"
                          disabled={reviewPlan.isPending || apply.isPending}
                          onClick={() => publication.lastPlanId ? apply.mutate(publication) : reviewPlan.mutate(publication)}
                        >
                          Apply
                        </Button>
                        <Button appearance="secondary" disabled={unpublish.isPending} onClick={() => unpublish.mutate(publication.id)}>Unpublish</Button>
                        <Button appearance="subtle" disabled={remove.isPending} onClick={() => remove.mutate(publication.id)}>Remove</Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ))}
    </Card>
    <PublishModelDialog
      open={review !== null}
      initialReview={review}
      onClose={() => setReview(null)}
      onPublished={onMessage}
    />
    </>
  )
}

function formatTimestamp(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : 'Never'
}

function EndpointStatusBadge({ status }: { status: ModelEndpointStatus }) {
  return (
    <Badge appearance="tint" className={statusClasses[status]}>
      {statusLabels[status]}
    </Badge>
  )
}

function CommandBlock({ command }: { command: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard?.writeText(command)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className={styles.remediation}>
      <pre className={styles.commandBlock}>{command}</pre>
      <Button appearance="secondary" onClick={() => void copy()}>
        {copied ? 'Copied' : 'Copy command'}
      </Button>
    </div>
  )
}

function ImportedModelApis({ onRemoved }: { onRemoved: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()

  const modelApis = useQuery({
    queryKey: ['model-apis'],
    queryFn: () => api.listModelApis(),
  })
  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
  })

  const gatewaysById = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) {
      map.set(gateway.id, gateway)
    }
    return map
  }, [gateways.data])

  const removeMutation = useMutation({
    mutationFn: (id: string) => api.deleteModelApi(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['model-apis'] })
      onRemoved('Stopped governing that API. Nothing changed in API Management.')
    },
  })

  const visibilityMutation = useMutation({
    mutationFn: ({ id, visibility }: { id: string; visibility: CatalogVisibility }) =>
      api.updateModelApiCatalog(id, { visibility }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['model-apis'] })
      onRemoved('Updated who can discover this API in the portal catalog.')
    },
  })

  return (
    <Card className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <Title3 as="h2">Imported model APIs</Title3>
          <Text>
            APIs an administrator adopted from a gateway. Importing records governance intent and
            never changes API Management.
          </Text>
        </div>
      </div>
      {removeMutation.isError && <ErrorState error={removeMutation.error} />}
      {modelApis.isPending && <Loading label="Loading imported model APIs..." />}
      {modelApis.isError && <ErrorState error={modelApis.error} />}
      {modelApis.isSuccess &&
        (modelApis.data.length === 0 ? (
          <EmptyState title="No model APIs imported yet">
            Synchronise a gateway, then import the APIs that front your models. MOSAIC pre-selects
            the ones it recognises, and you decide what to adopt.
          </EmptyState>
        ) : (
          <div className={styles.tableWrap}>
            <Table aria-label="Imported model APIs">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>API</TableHeaderCell>
                  <TableHeaderCell>Provider</TableHeaderCell>
                  <TableHeaderCell>Operations</TableHeaderCell>
                  <TableHeaderCell>Gateway</TableHeaderCell>
                  <TableHeaderCell>Catalog</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {modelApis.data.map((record) => (
                  <TableRow key={record.id}>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <span className={styles.primaryCell}>{record.displayName}</span>
                        <span className={styles.secondaryCell}>/{record.path}</span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <span className={styles.secondaryCell}>
                          {AI_KIND_LABELS[record.aiKind] || 'Not recognised'}
                        </span>
                        {record.selection === 'manual' && (
                          <Badge appearance="tint" className={styles.statusSyncing}>
                            Chosen by an administrator
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>{record.operationCount}</TableCell>
                    <TableCell>
                      <Link to={`/gateways/${record.gatewayId}`}>
                        {gatewaysById.get(record.gatewayId)?.name ?? record.gatewayId}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Select
                        aria-label={`Catalog visibility for ${record.displayName}`}
                        value={record.visibility}
                        disabled={visibilityMutation.isPending}
                        onChange={(_, data) =>
                          visibilityMutation.mutate({
                            id: record.id,
                            visibility: data.value as CatalogVisibility,
                          })
                        }
                      >
                        <option value="catalog">Discoverable</option>
                        <option value="private">Entitled users only</option>
                      </Select>
                    </TableCell>
                    <TableCell>
                      <Button
                        appearance="subtle"
                        disabled={removeMutation.isPending}
                        onClick={() => removeMutation.mutate(record.id)}
                      >
                        Remove
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ))}
    </Card>
  )
}

/**
 * A model endpoint has two independent access relationships, held by two different identities.
 * Collapsing them into one verdict would hide the common failure where MOSAIC can read an
 * endpoint perfectly well but the gateway still cannot call it.
 */
function AccessPanel({ endpoint }: { endpoint: ModelEndpoint }) {
  const { access, runtimeAccess } = endpoint

  return (
    <Card className={styles.accessCard}>
      <Title3 as="h2">Access</Title3>

      <section className={styles.accessSection}>
        <MessageBar intent={access.canRead ? 'success' : 'error'}>
          <MessageBarBody>
            <MessageBarTitle>
              {access.canRead
                ? 'MOSAIC can read this endpoint'
                : 'MOSAIC cannot read this endpoint'}
            </MessageBarTitle>
            {access.message}
          </MessageBarBody>
        </MessageBar>
        <Text size={200} className={styles.muted}>
          This is what lets MOSAIC list the models deployed here. It grants no ability to call
          them.
        </Text>
        {access.remediation && (
          <>
            <Text block>
              Grant the <strong>{access.remediation.roleName}</strong> role to MOSAIC at this
              scope. Someone with permission to assign roles must run:
            </Text>
            <CommandBlock command={access.remediation.command} />
            {access.remediation.customRoleDefinition && (
              <Text size={200} className={styles.muted}>
                Reader is the narrowest built-in role that grants this without also granting
                inference or key access. A tighter custom role is possible, but it omits the
                role-assignment read that the gateway check below depends on.
              </Text>
            )}
          </>
        )}
      </section>

      <section className={styles.accessSection}>
        <Text weight="semibold">Gateways calling this endpoint</Text>
        <Text size={200} className={styles.muted}>
          At runtime the gateway authenticates as itself, not as MOSAIC, so it needs its own role
          on this endpoint. MOSAIC reports this and never assigns it.
        </Text>
        {runtimeAccess.length === 0 ? (
          <Text size={200}>No gateways are registered yet.</Text>
        ) : (
          runtimeAccess.map((entry) => (
            <RuntimeAccessRow key={entry.gatewayId} access={entry} />
          ))
        )}
      </section>
    </Card>
  )
}

function RuntimeAccessRow({ access }: { access: GatewayRuntimeAccess }) {
  const intent =
    access.evaluation === 'notEvaluated'
      ? 'warning'
      : access.canInvoke
        ? 'success'
        : 'error'

  return (
    <div className={styles.runtimeRow}>
      <MessageBar intent={intent}>
        <MessageBarBody>
          <MessageBarTitle>{access.gatewayName}</MessageBarTitle>
          {access.message}
        </MessageBarBody>
      </MessageBar>
      {access.inherited && access.assignmentScope && (
        <Text size={200} className={styles.muted}>
          Inherited from {access.assignmentScope}. It works, but it is broader than an assignment
          made directly on this endpoint.
        </Text>
      )}
      {access.remediation && <CommandBlock command={access.remediation.command} />}
    </div>
  )
}

function ModelEndpoints() {
  const api = useMosaicApi()
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const hasConsumedRegisterQueryRef = useRef(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [mode, setMode] = useState<'azure' | 'compatible'>('azure')
  const [resourceId, setResourceId] = useState('')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [secretUri, setSecretUri] = useState('')
  const [name, setName] = useState('')
  const [environmentLabel, setEnvironmentLabel] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const endpoints = useQuery({
    queryKey: ['model-endpoints'],
    queryFn: api.listModelEndpoints,
  })
  const suggestions = useQuery({
    queryKey: ['model-endpoint-suggestions'],
    queryFn: api.listSuggestedModelEndpoints,
  })

  const selected =
    endpoints.data?.find((item) => item.id === selectedId) ?? endpoints.data?.[0] ?? null

  const deployments = useQuery({
    queryKey: ['model-deployments', selected?.id],
    queryFn: () => api.listModelDeployments(selected!.id),
    enabled: Boolean(selected),
  })

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ['model-endpoints'] })
    await queryClient.invalidateQueries({ queryKey: ['model-endpoint-suggestions'] })
    await queryClient.invalidateQueries({ queryKey: ['model-deployments'] })
  }

  const register = useMutation({
    mutationFn: api.registerModelEndpoint,
    onSuccess: async (endpoint) => {
      setResourceId('')
      setEndpointUrl('')
      setSecretUri('')
      setName('')
      setEnvironmentLabel('')
      closeDialog()
      setSelectedId(endpoint.id)
      await refresh()
    },
  })

  const sync = useMutation({ mutationFn: api.syncModelEndpoint, onSuccess: refresh })
  const recheck = useMutation({ mutationFn: api.preflightModelEndpoint, onSuccess: refresh })
  const remove = useMutation({
    mutationFn: api.deleteModelEndpoint,
    onSuccess: async () => {
      setSelectedId(null)
      await refresh()
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    if (mode === 'azure') {
      register.mutate({
        azureResourceId: resourceId.trim(),
        name: name.trim() || undefined,
        environmentLabel: environmentLabel.trim() || undefined,
      })
      return
    }
    register.mutate({
      endpoint: endpointUrl.trim(),
      credentialSecretUri: secretUri.trim(),
      name: name.trim() || undefined,
      environmentLabel: environmentLabel.trim() || undefined,
    })
  }

  const pending = (suggestions.data?.suggestions ?? []).filter(
    (item) => !item.alreadyRegistered,
  )
  const scanIssues = suggestions.data?.scanIssues ?? []

  // The shell's "Add model endpoint" action lands here with ?register=1, so the button opens the
  // real registration form rather than dropping the administrator on the page with no next step.
  const requestedRegister = useMemo(
    () => new URLSearchParams(location.search).get('register') === '1',
    [location.search],
  )

  useEffect(() => {
    if (requestedRegister && !hasConsumedRegisterQueryRef.current) {
      hasConsumedRegisterQueryRef.current = true
      setDialogOpen(true)
    }
    if (!requestedRegister) {
      hasConsumedRegisterQueryRef.current = false
    }
  }, [requestedRegister])

  function closeDialog() {
    setDialogOpen(false)
    const params = new URLSearchParams(location.search)
    if (!params.has('register')) {
      return
    }
    params.delete('register')
    const nextSearch = params.toString()
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : '',
        hash: location.hash,
      },
      { replace: true },
    )
  }

  return (
    <>
      <Card className={styles.panel}>
        <div className={styles.panelHeader}>
          <div>
            <Title3 as="h2">Model endpoints</Title3>
            <Text>
              Azure OpenAI and Azure AI Foundry resources your gateways front. MOSAIC reads the
              models deployed on them; it never changes them and never calls a model.
            </Text>
          </div>
          <Button
            appearance="primary"
            icon={<AddRegular />}
            onClick={() => setDialogOpen(true)}
          >
            Register endpoint
          </Button>
        </div>

        {!dialogOpen && register.isError && <ErrorState error={register.error} />}

        {endpoints.isPending && <Loading label="Loading model endpoints" />}
        {endpoints.isError && <ErrorState error={endpoints.error} />}
        {endpoints.data?.length === 0 && (
          <EmptyState title="No model endpoints yet">
            Register an Azure OpenAI or Azure AI Foundry resource to see the models deployed on it.
          </EmptyState>
        )}

        {endpoints.data && endpoints.data.length > 0 && (
          <>
            <div className={styles.tableWrap}>
              <Table aria-label="Registered model endpoints">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Endpoint</TableHeaderCell>
                    <TableHeaderCell>Provider</TableHeaderCell>
                    <TableHeaderCell>Status</TableHeaderCell>
                    <TableHeaderCell>Models</TableHeaderCell>
                    <TableHeaderCell>Last synced</TableHeaderCell>
                    <TableHeaderCell>Actions</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {endpoints.data.map((endpoint) => (
                    <TableRow
                      key={endpoint.id}
                      className={endpoint.id === selected?.id ? styles.selectedRow : undefined}
                    >
                      <TableCell>
                        <div className={styles.cellStack}>
                          <button
                            type="button"
                            className={styles.rowButton}
                            onClick={() => setSelectedId(endpoint.id)}
                          >
                            {endpoint.name}
                          </button>
                          <span className={styles.secondaryCell}>{endpoint.endpoint}</span>
                        </div>
                      </TableCell>
                      <TableCell>{providerLabels[endpoint.provider]}</TableCell>
                      <TableCell>
                        <EndpointStatusBadge status={endpoint.status} />
                      </TableCell>
                      <TableCell>{endpoint.inventory.deployments}</TableCell>
                      <TableCell>{formatTimestamp(endpoint.lastSyncedAt)}</TableCell>
                      <TableCell>
                        <div className={styles.actionRow}>
                          <Button
                            appearance="secondary"
                            onClick={() => recheck.mutate(endpoint.id)}
                            disabled={recheck.isPending}
                          >
                            Check access
                          </Button>
                          <Button
                            appearance="secondary"
                            onClick={() => sync.mutate(endpoint.id)}
                            disabled={sync.isPending || !endpoint.access.canRead}
                          >
                            Sync models
                          </Button>
                          <Button
                            appearance="subtle"
                            onClick={() => remove.mutate(endpoint.id)}
                            disabled={remove.isPending}
                          >
                            Remove
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <Text size={200} className={styles.muted}>
              Removing an endpoint deletes only what MOSAIC stored about it. The Azure resource and
              its deployments are never modified.
            </Text>
          </>
        )}
      </Card>

      {pending.length > 0 && (
        <Card className={styles.panel}>
          <Title3 as="h2">Endpoints MOSAIC found</Title3>
          {pending.map((item) => (
            <div
              key={item.azureResourceId ?? item.endpoint ?? item.reason}
              className={styles.suggestionRow}
            >
              <div className={styles.suggestionText}>
                <div className={styles.suggestionHeading}>
                  <Text weight="semibold">
                    {item.accountName ?? item.endpoint ?? 'Unidentified endpoint'}
                  </Text>
                  <Badge appearance="outline">{sourceLabels[item.source]}</Badge>
                </div>
                <Text size={200} className={styles.muted}>
                  {item.reason}
                  {item.provider ? ` ${providerLabels[item.provider]}.` : ''}
                </Text>
              </div>
              {item.azureResourceId ? (
                <Button
                  appearance="primary"
                  onClick={() => register.mutate({ azureResourceId: item.azureResourceId! })}
                  disabled={register.isPending}
                >
                  Register
                </Button>
              ) : (
                <Text size={200} className={styles.muted}>
                  Needs a resource ID
                </Text>
              )}
            </div>
          ))}
        </Card>
      )}

      {scanIssues.length > 0 && (
        <Card className={styles.panel}>
          <Title3 as="h2">Subscriptions MOSAIC could not scan</Title3>
          {scanIssues.map((issue) => (
            <div key={issue.subscriptionId} className={styles.scanIssue}>
              <Text size={200}>
                <strong>{issue.displayName ?? issue.subscriptionId}</strong> — {issue.message}
              </Text>
              {issue.remediation && <CommandBlock command={issue.remediation.command} />}
            </div>
          ))}
        </Card>
      )}

      {selected && <AccessPanel endpoint={selected} />}

      {selected && (
        <Card className={styles.panel}>
          <Title3 as="h2">Models on {selected.name}</Title3>
          {selected.lastSyncError && (
            <MessageBar intent="warning">
              <MessageBarBody>
                <MessageBarTitle>The last sync was incomplete</MessageBarTitle>
                {selected.lastSyncError}
              </MessageBarBody>
            </MessageBar>
          )}
          {deployments.isPending && <Loading label="Loading models" />}
          {deployments.isError && <ErrorState error={deployments.error} />}
          {deployments.data?.length === 0 && (
            <EmptyState title="No models discovered yet">
              Run a sync to read the deployments on this endpoint.
            </EmptyState>
          )}
          {deployments.data && deployments.data.length > 0 && (
            <div className={styles.tableWrap}>
              <Table aria-label="Discovered model deployments">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Deployment</TableHeaderCell>
                    <TableHeaderCell>Model</TableHeaderCell>
                    <TableHeaderCell>Version</TableHeaderCell>
                    <TableHeaderCell>Capacity</TableHeaderCell>
                    <TableHeaderCell>State</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {deployments.data.map((deployment) => (
                    <TableRow key={deployment.id}>
                      <TableCell>
                        <div className={styles.cellStack}>
                          <span className={styles.primaryCell}>
                            {deployment.deploymentName}
                          </span>
                          {deployment.requestPaths.length > 0 && (
                            <span className={styles.secondaryCell}>
                              {deployment.requestPaths.join(', ')}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>{deployment.modelName ?? 'Unknown'}</TableCell>
                      <TableCell>{deployment.modelVersion ?? '—'}</TableCell>
                      <TableCell>
                        {deployment.skuCapacity != null
                          ? `${deployment.skuName ?? ''} ${deployment.skuCapacity}`.trim()
                          : (deployment.skuName ?? '—')}
                      </TableCell>
                      <TableCell>{deployment.provisioningState ?? 'Unknown'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </Card>
      )}

      <Dialog open={dialogOpen} onOpenChange={(_, data) => (data.open ? setDialogOpen(true) : closeDialog())}>
        <DialogSurface>
          <form onSubmit={submit}>
            <DialogBody>
              <DialogTitle>Register model endpoint</DialogTitle>
              <DialogContent className={styles.dialogForm}>
                <TabList
                  selectedValue={mode}
                  onTabSelect={(_, data) => setMode(data.value as 'azure' | 'compatible')}
                >
                  <Tab value="azure">Azure AI</Tab>
                  <Tab value="compatible">OpenAI compatible</Tab>
                </TabList>

                {mode === 'azure' ? (
                  <Field
                    label="Azure resource ID"
                    required
                    hint="An Azure OpenAI or Azure AI Foundry resource. MOSAIC reads it with its own managed identity."
                  >
                    <Input
                      value={resourceId}
                      onChange={(_, data) => setResourceId(data.value)}
                      placeholder="/subscriptions/.../providers/Microsoft.CognitiveServices/accounts/my-account"
                    />
                  </Field>
                ) : (
                  <>
                    <Field label="Endpoint URL" required>
                      <Input
                        value={endpointUrl}
                        onChange={(_, data) => setEndpointUrl(data.value)}
                        placeholder="https://models.example.com/v1"
                      />
                    </Field>
                    <Field
                      label="Key Vault secret URI"
                      required
                      hint="Store the API key in Key Vault and paste its secret identifier. MOSAIC stores only this URI, never the key."
                    >
                      <Input
                        value={secretUri}
                        onChange={(_, data) => setSecretUri(data.value)}
                        placeholder="https://my-vault.vault.azure.net/secrets/model-key"
                      />
                    </Field>
                  </>
                )}

                <Field label="Display name">
                  <Input value={name} onChange={(_, data) => setName(data.value)} />
                </Field>
                <Field label="Environment label">
                  <Input
                    value={environmentLabel}
                    onChange={(_, data) => setEnvironmentLabel(data.value)}
                    placeholder="Production"
                  />
                </Field>

                {register.isError && <ErrorState error={register.error} />}
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" onClick={closeDialog}>
                  Cancel
                </Button>
                <Button appearance="primary" type="submit" disabled={register.isPending}>
                  Register
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>
    </>
  )
}

export function ModelsPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const hasConsumedImportQueryRef = useRef(false)
  const [importOpen, setImportOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [liveBanner, setLiveBanner] = useState<string | null>(null)

  const requestedImportGatewayId = useMemo(
    () => new URLSearchParams(location.search).get('import'),
    [location.search],
  )

  useEffect(() => {
    if (requestedImportGatewayId && !hasConsumedImportQueryRef.current) {
      hasConsumedImportQueryRef.current = true
      setImportOpen(true)
    }
    if (!requestedImportGatewayId) {
      hasConsumedImportQueryRef.current = false
    }
  }, [requestedImportGatewayId])

  function removeImportQueryIfPresent() {
    const params = new URLSearchParams(location.search)
    if (!params.has('import')) {
      return
    }
    params.delete('import')
    const nextSearch = params.toString()
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : '',
        hash: location.hash,
      },
      { replace: true },
    )
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Models"
        description="Model APIs MOSAIC governs, and the provider endpoints they are served from. Reading and importing do not change Azure. Publishing changes API Management only after a reviewed plan is applied, and only for a gateway switched to managed mode."
        source="live"
        actions={
          <div className={styles.actionRow}>
            <Button appearance="primary" icon={<AddRegular />} onClick={() => setPublishOpen(true)}>
              Publish a model
            </Button>
            <Button appearance="secondary" onClick={() => setImportOpen(true)}>
              Import from gateway
            </Button>
          </div>
        }
      />

      {liveBanner && (
        <MessageBar intent="success">
          <MessageBarBody>{liveBanner}</MessageBarBody>
        </MessageBar>
      )}

      <PublishedModels onMessage={setLiveBanner} />

      <ImportedModelApis onRemoved={setLiveBanner} />

      <ModelEndpoints />

      <PublishModelDialog
        open={publishOpen}
        onClose={() => setPublishOpen(false)}
        onPublished={setLiveBanner}
      />

      <ImportFromGatewayDialog
        kind="apis"
        open={importOpen}
        initialGatewayId={requestedImportGatewayId}
        onClose={() => {
          setImportOpen(false)
          removeImportQueryIfPresent()
        }}
        onImported={(count) =>
          setLiveBanner(
            `Imported ${count} model API${count === 1 ? '' : 's'}. MOSAIC recorded them as ` +
              'governed; API Management is unchanged.',
          )
        }
      />
    </section>
  )
}
