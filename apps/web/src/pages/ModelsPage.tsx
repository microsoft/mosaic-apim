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
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import { AI_KIND_LABELS } from '../labels'
import type { Gateway } from '../types'
import styles from './ModelsPage.module.css'

const foundryProviders = [
  'Azure AI Foundry',
  'Azure OpenAI',
  'OpenAI compatible',
] as const

type FoundryProvider = (typeof foundryProviders)[number]
type ConnectionSource = 'sample' | 'local'
type ConnectionState = 'connected' | 'preview'
type DeploymentStatus = 'ready' | 'syncing' | 'attention' | 'stopped'

interface PreviewFoundryConnection {
  id: string
  displayName: string
  provider: FoundryProvider
  endpoint: string
  azureResourceId: string
  createdAt: string
  source: ConnectionSource
  state: ConnectionState
}

interface PreviewModelDeployment {
  id: string
  foundryConnectionId: string
  deploymentName: string
  modelName: string
  modelVersion: string
  endpoint: string
  requestPath: '/chat/completions' | '/embeddings'
  regionLabel: string
  status: DeploymentStatus
  updatedAt: string
  source: ConnectionSource
  notes: string
}

interface ModelDeployDraft {
  foundryConnectionId: string
  deploymentName: string
  modelName: string
  modelVersion: string
  requestPath: '/chat/completions' | '/embeddings'
}

interface BannerState {
  intent: 'success' | 'warning' | 'error'
  message: string
}

const sampleConnections: PreviewFoundryConnection[] = [
  {
    id: 'conn-eastus-preview',
    displayName: 'Azure AI Foundry / East US',
    provider: 'Azure AI Foundry',
    endpoint: 'https://mosaic-eastus.services.ai.azure.com',
    azureResourceId:
      '/subscriptions/sample-sub/resourceGroups/mosaic-preview/providers/Microsoft.CognitiveServices/accounts/mosaic-eastus',
    createdAt: '2026-08-08T15:20:00Z',
    source: 'sample',
    state: 'connected',
  },
  {
    id: 'conn-openai-europe',
    displayName: 'Azure OpenAI / West Europe',
    provider: 'Azure OpenAI',
    endpoint: 'https://contoso-weu.openai.azure.com',
    azureResourceId:
      '/subscriptions/sample-sub/resourceGroups/contoso-preview/providers/Microsoft.CognitiveServices/accounts/contoso-weu',
    createdAt: '2026-08-09T09:10:00Z',
    source: 'sample',
    state: 'connected',
  },
] satisfies PreviewFoundryConnection[]

const sampleDeployments: PreviewModelDeployment[] = [
  {
    id: 'dep-gpt41-prod',
    foundryConnectionId: 'conn-eastus-preview',
    deploymentName: 'gpt-4.1-prod',
    modelName: 'gpt-4.1',
    modelVersion: '2026-07-18',
    endpoint:
      'https://mosaic-eastus.services.ai.azure.com/openai/deployments/gpt-4.1-prod/chat/completions',
    requestPath: '/chat/completions',
    regionLabel: 'East US',
    status: 'ready',
    updatedAt: '2026-08-12T14:02:00Z',
    source: 'sample',
    notes: 'Sample endpoint metadata only. MOSAIC is not polling Foundry in this preview.',
  },
  {
    id: 'dep-embed-3-large',
    foundryConnectionId: 'conn-eastus-preview',
    deploymentName: 'text-embedding-3-large',
    modelName: 'text-embedding-3-large',
    modelVersion: '2026-05-30',
    endpoint:
      'https://mosaic-eastus.services.ai.azure.com/openai/deployments/text-embedding-3-large/embeddings',
    requestPath: '/embeddings',
    regionLabel: 'East US',
    status: 'syncing',
    updatedAt: '2026-08-12T10:35:00Z',
    source: 'sample',
    notes: 'Local sync state can move between preview statuses without changing Azure.',
  },
  {
    id: 'dep-gpt4o-mini',
    foundryConnectionId: 'conn-openai-europe',
    deploymentName: 'gpt-4o-mini',
    modelName: 'gpt-4o-mini',
    modelVersion: '2026-06-22',
    endpoint:
      'https://contoso-weu.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions',
    requestPath: '/chat/completions',
    regionLabel: 'West Europe',
    status: 'attention',
    updatedAt: '2026-08-11T18:40:00Z',
    source: 'sample',
    notes: 'Sample health warnings are browser-only and do not reflect live validation.',
  },
  {
    id: 'dep-legacy-assistant',
    foundryConnectionId: 'conn-openai-europe',
    deploymentName: 'assistant-archive',
    modelName: 'gpt-35-turbo',
    modelVersion: '2025-12-01',
    endpoint:
      'https://contoso-weu.openai.azure.com/openai/deployments/assistant-archive/chat/completions',
    requestPath: '/chat/completions',
    regionLabel: 'West Europe',
    status: 'stopped',
    updatedAt: '2026-08-10T12:15:00Z',
    source: 'sample',
    notes: 'Stopped indicates a local preview state, not a Foundry shutdown.',
  },
] satisfies PreviewModelDeployment[]

const deployableModelsByProvider: Record<FoundryProvider, { name: string; version: string }[]> = {
  'Azure AI Foundry': [
    { name: 'gpt-4.1', version: '2026-07-18' },
    { name: 'text-embedding-3-large', version: '2026-05-30' },
    { name: 'phi-4', version: '2026-06-01' },
  ],
  'Azure OpenAI': [
    { name: 'gpt-4o-mini', version: '2026-06-22' },
    { name: 'gpt-4.1-mini', version: '2026-07-11' },
    { name: 'text-embedding-3-small', version: '2026-03-12' },
  ],
  'OpenAI compatible': [
    { name: 'llama-4-maverick', version: 'preview' },
    { name: 'mistral-large', version: 'preview' },
    { name: 'custom-chat', version: 'preview' },
  ],
}

function isFoundryProvider(value: string): value is FoundryProvider {
  return foundryProviders.some((provider) => provider === value)
}

function isRequestPath(
  value: string,
): value is PreviewModelDeployment['requestPath'] {
  return value === '/chat/completions' || value === '/embeddings'
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function normalizeEndpoint(value: string): string | null {
  try {
    const url = new URL(value.trim())
    const normalizedPath = url.pathname.replace(/\/+$/, '')
    return `${url.origin}${normalizedPath}` || url.origin
  } catch {
    return null
  }
}

function buildPreviewPath(
  baseEndpoint: string,
  deploymentName: string,
  requestPath: PreviewModelDeployment['requestPath'],
): string {
  return `${baseEndpoint.replace(/\/+$/, '')}/openai/deployments/${deploymentName}${requestPath}`
}

function derivePreviewRegion(endpoint: string): string {
  try {
    const host = new URL(endpoint).host
    const firstSegment = host.split('.')[0] ?? 'custom'
    return firstSegment.replace(/[-_]/g, ' ')
  } catch {
    return 'Custom'
  }
}

function deploymentStatusLabel(status: DeploymentStatus): string {
  switch (status) {
    case 'ready':
      return 'Ready'
    case 'syncing':
      return 'Preview syncing'
    case 'attention':
      return 'Needs attention'
    case 'stopped':
      return 'Stopped'
  }
}

function deploymentStatusClass(status: DeploymentStatus): string {
  switch (status) {
    case 'ready':
      return styles.statusReady
    case 'syncing':
      return styles.statusSyncing
    case 'attention':
      return styles.statusAttention
    case 'stopped':
      return styles.statusStopped
  }
}

function connectionStateClass(state: ConnectionState): string {
  return state === 'connected' ? styles.statusReady : styles.statusSyncing
}

function createDeploymentDraft(
  connectionId: string,
  provider: FoundryProvider,
): ModelDeployDraft {
  const defaultModel = deployableModelsByProvider[provider][0]
  return {
    foundryConnectionId: connectionId,
    deploymentName: `${defaultModel.name.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}-preview`,
    modelName: defaultModel.name,
    modelVersion: defaultModel.version,
    requestPath: '/chat/completions',
  }
}

/** The live half of this page: model APIs adopted from a gateway, backed by the MOSAIC API. */
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

export function ModelsPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const secretInputRef = useRef<HTMLInputElement | null>(null)
  const hasConsumedDeployQueryRef = useRef(false)
  const hasConsumedImportQueryRef = useRef(false)
  const [importOpen, setImportOpen] = useState(false)
  const [liveBanner, setLiveBanner] = useState<string | null>(null)

  const [provider, setProvider] = useState<FoundryProvider>('Azure AI Foundry')
  const [baseEndpoint, setBaseEndpoint] = useState('')
  const [connections, setConnections] = useState<PreviewFoundryConnection[]>(sampleConnections)
  const [deployments, setDeployments] = useState<PreviewModelDeployment[]>(sampleDeployments)
  const [selectedDeploymentId, setSelectedDeploymentId] = useState(sampleDeployments[0]?.id ?? '')
  const [providerFilter, setProviderFilter] = useState<'all' | FoundryProvider>('all')
  const [statusFilter, setStatusFilter] = useState<'all' | DeploymentStatus>('all')
  const [searchText, setSearchText] = useState('')
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [deployDialogOpen, setDeployDialogOpen] = useState(false)
  const [deployDraft, setDeployDraft] = useState<ModelDeployDraft>(() =>
    createDeploymentDraft(sampleConnections[0]?.id ?? '', sampleConnections[0]?.provider ?? 'Azure AI Foundry'),
  )
  const [deployError, setDeployError] = useState<string | null>(null)

  const selectedDeployment = useMemo(
    () => deployments.find((deployment) => deployment.id === selectedDeploymentId) ?? null,
    [deployments, selectedDeploymentId],
  )

  const queryRequestsDeployDialog = useMemo(() => {
    const params = new URLSearchParams(location.search)
    return params.get('deploy') === '1'
  }, [location.search])

  const requestedImportGatewayId = useMemo(
    () => new URLSearchParams(location.search).get('import'),
    [location.search],
  )

  const filteredDeployments = useMemo(() => {
    const normalizedSearch = searchText.trim().toLowerCase()
    return deployments.filter((deployment) => {
      const connection = connections.find(
        (item) => item.id === deployment.foundryConnectionId,
      )
      const matchesProvider =
        providerFilter === 'all' || connection?.provider === providerFilter
      const matchesStatus = statusFilter === 'all' || deployment.status === statusFilter
      const matchesSearch =
        normalizedSearch.length === 0 ||
        deployment.deploymentName.toLowerCase().includes(normalizedSearch) ||
        deployment.modelName.toLowerCase().includes(normalizedSearch) ||
        deployment.endpoint.toLowerCase().includes(normalizedSearch) ||
        connection?.displayName.toLowerCase().includes(normalizedSearch) === true
      return matchesProvider && matchesStatus && matchesSearch
    })
  }, [connections, deployments, providerFilter, searchText, statusFilter])

  useEffect(() => {
    if (queryRequestsDeployDialog && !hasConsumedDeployQueryRef.current) {
      hasConsumedDeployQueryRef.current = true
      setDeployDialogOpen(true)
    }
    if (!queryRequestsDeployDialog) {
      hasConsumedDeployQueryRef.current = false
    }
  }, [queryRequestsDeployDialog])

  useEffect(() => {
    if (requestedImportGatewayId && !hasConsumedImportQueryRef.current) {
      hasConsumedImportQueryRef.current = true
      setImportOpen(true)
    }
    if (!requestedImportGatewayId) {
      hasConsumedImportQueryRef.current = false
    }
  }, [requestedImportGatewayId])

  useEffect(() => {
    if (!filteredDeployments.length) {
      if (selectedDeploymentId !== '') {
        setSelectedDeploymentId('')
      }
      return
    }
    if (!filteredDeployments.some((deployment) => deployment.id === selectedDeploymentId)) {
      setSelectedDeploymentId(filteredDeployments[0].id)
    }
  }, [filteredDeployments, selectedDeploymentId])

  useEffect(() => {
    const selectedConnection = connections.find(
      (connection) => connection.id === deployDraft.foundryConnectionId,
    )
    if (!selectedConnection && connections[0]) {
      setDeployDraft(createDeploymentDraft(connections[0].id, connections[0].provider))
    }
  }, [connections, deployDraft.foundryConnectionId])

  function removeDeployQueryIfPresent() {
    const params = new URLSearchParams(location.search)
    if (!params.has('deploy')) {
      return
    }
    params.delete('deploy')
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

  function openDeployDialogForConnection(connectionId?: string) {
    const selectedConnection =
      connections.find((connection) => connection.id === connectionId) ?? connections[0]
    if (!selectedConnection) {
      setBanner({
        intent: 'warning',
        message: 'Add a preview connection first. Deploy actions only create local sample rows.',
      })
      return
    }
    setDeployDraft(createDeploymentDraft(selectedConnection.id, selectedConnection.provider))
    setDeployError(null)
    setDeployDialogOpen(true)
  }

  function closeDeployDialog() {
    setDeployDialogOpen(false)
    setDeployError(null)
    removeDeployQueryIfPresent()
  }

  function updateDeployment(
    deploymentId: string,
    nextStatus: DeploymentStatus,
    message: string,
  ) {
    setDeployments((current) =>
      current.map((deployment) =>
        deployment.id === deploymentId
          ? {
              ...deployment,
              status: nextStatus,
              updatedAt: new Date().toISOString(),
            }
          : deployment,
      ),
    )
    setBanner({ intent: 'success', message })
  }

  function removeDeployment(deploymentId: string) {
    setDeployments((current) => current.filter((deployment) => deployment.id !== deploymentId))
    setBanner({
      intent: 'warning',
      message: 'Removed the preview row locally. No Foundry deployment or endpoint was deleted.',
    })
  }

  function submitConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalizedEndpoint = normalizeEndpoint(baseEndpoint)
    if (!normalizedEndpoint) {
      setFormError('Enter a valid base URL for the preview connection.')
      return
    }

    const now = new Date().toISOString()
    const connectionId = `conn-local-${Date.now()}`
    const host = new URL(normalizedEndpoint).host
    const deploymentName = `preview-${host.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`
    const secretProvided = Boolean(secretInputRef.current?.value.trim())
    const connection: PreviewFoundryConnection = {
      id: connectionId,
      displayName: `${provider} / ${host}`,
      provider,
      endpoint: normalizedEndpoint,
      azureResourceId: `/local-preview/${host}`,
      createdAt: now,
      source: 'local',
      state: 'preview',
    }
    const deployment: PreviewModelDeployment = {
      id: `dep-local-${Date.now()}`,
      foundryConnectionId: connectionId,
      deploymentName,
      modelName: deployableModelsByProvider[provider][0].name,
      modelVersion: deployableModelsByProvider[provider][0].version,
      endpoint: buildPreviewPath(normalizedEndpoint, deploymentName, '/chat/completions'),
      requestPath: '/chat/completions',
      regionLabel: derivePreviewRegion(normalizedEndpoint),
      status: 'syncing',
      updatedAt: now,
      source: 'local',
      notes:
        'Created from the browser preview form. No connection validation, secret persistence, or Foundry deployment occurred.',
    }

    setConnections((current) => [connection, ...current])
    setDeployments((current) => [deployment, ...current])
    setSelectedDeploymentId(deployment.id)
    setBaseEndpoint('')
    setFormError(null)
    if (secretInputRef.current) {
      secretInputRef.current.value = ''
    }
    setBanner({
      intent: 'success',
      message: secretProvided
        ? 'Added a local preview connection and draft endpoint. The submitted secret was discarded immediately.'
        : 'Added a local preview connection and draft endpoint. No Foundry validation or deployment call occurred.',
    })
  }

  function submitDeployDialog(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const selectedConnection = connections.find(
      (connection) => connection.id === deployDraft.foundryConnectionId,
    )
    if (!selectedConnection) {
      setDeployError('Choose a preview connection before adding a deployment row.')
      return
    }

    const trimmedName = deployDraft.deploymentName.trim()
    const trimmedModelName = deployDraft.modelName.trim()
    const trimmedVersion = deployDraft.modelVersion.trim()

    if (!trimmedName || !trimmedModelName || !trimmedVersion) {
      setDeployError('Deployment name, model name, and model version are required.')
      return
    }

    const now = new Date().toISOString()
    const deployment: PreviewModelDeployment = {
      id: `dep-local-${Date.now()}`,
      foundryConnectionId: selectedConnection.id,
      deploymentName: trimmedName,
      modelName: trimmedModelName,
      modelVersion: trimmedVersion,
      endpoint: buildPreviewPath(
        selectedConnection.endpoint,
        trimmedName,
        deployDraft.requestPath,
      ),
      requestPath: deployDraft.requestPath,
      regionLabel: derivePreviewRegion(selectedConnection.endpoint),
      status: 'syncing',
      updatedAt: now,
      source: 'local',
      notes:
        'Deploy Model adds a local preview endpoint row only. Azure AI Foundry was not contacted from this page.',
    }

    setDeployments((current) => [deployment, ...current])
    setSelectedDeploymentId(deployment.id)
    setDeployDialogOpen(false)
    setDeployError(null)
    removeDeployQueryIfPresent()
    setBanner({
      intent: 'success',
      message: 'Added a local preview deployment row. No model deployment was created in Azure.',
    })
  }

  const readyCount = deployments.filter((deployment) => deployment.status === 'ready').length
  const localCount = deployments.filter((deployment) => deployment.source === 'local').length

  return (
    <section className={styles.page}>
      <PageHeader
        title="Models"
        description="Model APIs MOSAIC governs, imported from your API Management gateways."
        actions={
          <>
            <Button
              appearance="primary"
              icon={<AddRegular />}
              onClick={() => setImportOpen(true)}
            >
              Import from gateway
            </Button>
            <Button appearance="secondary" onClick={() => openDeployDialogForConnection()}>
              Deploy model
            </Button>
          </>
        }
      />

      {liveBanner && (
        <MessageBar intent="success">
          <MessageBarBody>{liveBanner}</MessageBarBody>
        </MessageBar>
      )}

      <ImportedModelApis onRemoved={setLiveBanner} />

      <PreviewNotice kind="sample">
        Everything below this line is a preview. It mixes typed sample endpoints with browser-only
        edits. MOSAIC does not validate Foundry connections, persist secrets, or deploy models from
        this page yet.
      </PreviewNotice>

      {banner && (
        <MessageBar intent={banner.intent}>
          <MessageBarBody>{banner.message}</MessageBarBody>
        </MessageBar>
      )}

      <div className={styles.heroGrid}>
        <Card className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <Title3 as="h2">Add preview connection</Title3>
              <Text>Capture a provider endpoint and create a local draft deployment row.</Text>
            </div>
          </div>
          <form className={styles.connectionForm} onSubmit={submitConnection}>
            <Field label="Provider" required>
              <Select
                aria-label="Foundry provider"
                value={provider}
                onChange={(event) => {
                  if (isFoundryProvider(event.target.value)) {
                    setProvider(event.target.value)
                  }
                }}
              >
                {foundryProviders.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Base URL" required>
              <Input
                aria-label="Foundry base URL"
                placeholder="https://example.openai.azure.com"
                type="url"
                value={baseEndpoint}
                onChange={(_, data) => setBaseEndpoint(data.value)}
              />
            </Field>
            <Field label="Connection secret (optional)">
              <Input
                ref={secretInputRef}
                aria-label="Optional connection secret"
                placeholder="Paste a key or token for local form testing"
                type="password"
                autoComplete="new-password"
              />
            </Field>
            {formError && (
              <MessageBar intent="error">
                <MessageBarBody>{formError}</MessageBarBody>
              </MessageBar>
            )}
            <div className={styles.formActions}>
              <Button appearance="primary" type="submit">
                Add preview connection
              </Button>
              <Text size={200}>
                Secrets are never shown back and are cleared after submit.
              </Text>
            </div>
          </form>
        </Card>

        <div className={styles.summaryGrid}>
          <Card className={styles.summaryCard}>
            <Text>Preview connections</Text>
            <strong>{connections.length}</strong>
            <Text>Sample plus local browser rows.</Text>
          </Card>
          <Card className={styles.summaryCard}>
            <Text>Ready endpoints</Text>
            <strong>{readyCount}</strong>
            <Text>Local status only. Azure is unchanged.</Text>
          </Card>
          <Card className={styles.summaryCard}>
            <Text>Local additions</Text>
            <strong>{localCount}</strong>
            <Text>Connections and deployments created in this browser.</Text>
          </Card>
        </div>
      </div>

      <Card className={styles.panel}>
        <div className={styles.panelHeader}>
          <div>
            <Title3 as="h2">Active model endpoints</Title3>
            <Text>Filter the preview inventory and inspect connection or status details.</Text>
          </div>
        </div>

        <div className={styles.filterBar}>
          <Field className={styles.searchField} label="Search endpoints">
            <Input
              aria-label="Search endpoints"
              placeholder="Search deployments, models, or endpoints"
              value={searchText}
              onChange={(_, data) => setSearchText(data.value)}
            />
          </Field>
          <Field label="Provider">
            <Select
              aria-label="Filter by provider"
              value={providerFilter}
              onChange={(event) => {
                const nextValue = event.target.value
                if (nextValue === 'all' || isFoundryProvider(nextValue)) {
                  setProviderFilter(nextValue)
                }
              }}
            >
              <option value="all">All providers</option>
              {foundryProviders.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Status">
            <Select
              aria-label="Filter by deployment status"
              value={statusFilter}
              onChange={(event) => {
                const nextValue = event.target.value
                if (
                  nextValue === 'all' ||
                  nextValue === 'ready' ||
                  nextValue === 'syncing' ||
                  nextValue === 'attention' ||
                  nextValue === 'stopped'
                ) {
                  setStatusFilter(nextValue)
                }
              }}
            >
              <option value="all">All statuses</option>
              <option value="ready">Ready</option>
              <option value="syncing">Preview syncing</option>
              <option value="attention">Needs attention</option>
              <option value="stopped">Stopped</option>
            </Select>
          </Field>
        </div>

        <div className={styles.resultsLayout}>
          <div className={styles.tableWrap}>
            <Table aria-label="Preview model deployments">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Deployment</TableHeaderCell>
                  <TableHeaderCell>Model</TableHeaderCell>
                  <TableHeaderCell>Provider</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Source</TableHeaderCell>
                  <TableHeaderCell>Updated</TableHeaderCell>
                  <TableHeaderCell>
                    <span className={styles.srOnly}>Actions</span>
                  </TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredDeployments.map((deployment) => {
                  const connection = connections.find(
                    (item) => item.id === deployment.foundryConnectionId,
                  )
                  const isSelected = deployment.id === selectedDeploymentId
                  return (
                    <TableRow
                      key={deployment.id}
                      aria-selected={isSelected}
                      className={isSelected ? styles.selectedRow : undefined}
                    >
                      <TableCell>
                        <button
                          className={styles.rowButton}
                          type="button"
                          onClick={() => setSelectedDeploymentId(deployment.id)}
                        >
                          <span className={styles.primaryCell}>{deployment.deploymentName}</span>
                          <span className={styles.secondaryCell}>{deployment.endpoint}</span>
                        </button>
                      </TableCell>
                      <TableCell>
                        <div className={styles.cellStack}>
                          <span>{deployment.modelName}</span>
                          <span className={styles.secondaryCell}>{deployment.modelVersion}</span>
                        </div>
                      </TableCell>
                      <TableCell>{connection?.provider ?? 'Unknown connection'}</TableCell>
                      <TableCell>
                        <Badge
                          appearance="filled"
                          className={deploymentStatusClass(deployment.status)}
                        >
                          {deploymentStatusLabel(deployment.status)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge appearance="tint">
                          {deployment.source === 'sample' ? 'Sample' : 'Local'}
                        </Badge>
                      </TableCell>
                      <TableCell>{formatTimestamp(deployment.updatedAt)}</TableCell>
                      <TableCell>
                        <Button
                          appearance="subtle"
                          onClick={() => openDeployDialogForConnection(deployment.foundryConnectionId)}
                        >
                          Clone
                        </Button>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
            {filteredDeployments.length === 0 && (
              <div className={styles.emptyState}>
                <Title3 as="h3">No endpoints match these filters</Title3>
                <Text>Adjust the preview filters or add another local connection.</Text>
              </div>
            )}
          </div>

          {selectedDeployment && (
            <Card className={styles.detailCard}>
              <div className={styles.detailHeader}>
                <div>
                  <Title3 as="h2">{selectedDeployment.deploymentName}</Title3>
                  <Text>{selectedDeployment.modelName}</Text>
                </div>
                <Badge
                  appearance="filled"
                  className={deploymentStatusClass(selectedDeployment.status)}
                >
                  {deploymentStatusLabel(selectedDeployment.status)}
                </Badge>
              </div>

              <dl className={styles.detailList}>
                <div>
                  <dt>Connection</dt>
                  <dd>
                    {connections.find(
                      (connection) => connection.id === selectedDeployment.foundryConnectionId,
                    )?.displayName ?? 'Unknown connection'}
                  </dd>
                </div>
                <div>
                  <dt>Endpoint</dt>
                  <dd className={styles.breakValue}>{selectedDeployment.endpoint}</dd>
                </div>
                <div>
                  <dt>Route</dt>
                  <dd>{selectedDeployment.requestPath}</dd>
                </div>
                <div>
                  <dt>Region label</dt>
                  <dd>{selectedDeployment.regionLabel}</dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>{formatTimestamp(selectedDeployment.updatedAt)}</dd>
                </div>
                <div>
                  <dt>Connection state</dt>
                  <dd>
                    <Badge
                      appearance="filled"
                      className={connectionStateClass(
                        connections.find(
                          (connection) =>
                            connection.id === selectedDeployment.foundryConnectionId,
                        )?.state ?? 'preview',
                      )}
                    >
                      {connections.find(
                        (connection) =>
                          connection.id === selectedDeployment.foundryConnectionId,
                      )?.state === 'connected'
                        ? 'Connected preview'
                        : 'Preview added locally'}
                    </Badge>
                  </dd>
                </div>
              </dl>

              <div className={styles.noteCard}>
                <Text>{selectedDeployment.notes}</Text>
              </div>

              <div className={styles.actionRow}>
                {selectedDeployment.status === 'ready' && (
                  <Button
                    onClick={() =>
                      updateDeployment(
                        selectedDeployment.id,
                        'stopped',
                        'Marked the endpoint as stopped in local preview state. Azure was not updated.',
                      )
                    }
                  >
                    Pause locally
                  </Button>
                )}
                {selectedDeployment.status === 'stopped' && (
                  <Button
                    onClick={() =>
                      updateDeployment(
                        selectedDeployment.id,
                        'ready',
                        'Returned the endpoint to ready in local preview state only.',
                      )
                    }
                  >
                    Resume locally
                  </Button>
                )}
                {selectedDeployment.status === 'attention' && (
                  <Button
                    onClick={() =>
                      updateDeployment(
                        selectedDeployment.id,
                        'syncing',
                        'Queued a local retry state. No Azure deployment action was triggered.',
                      )
                    }
                  >
                    Retry locally
                  </Button>
                )}
                {selectedDeployment.status === 'syncing' && (
                  <Button
                    onClick={() =>
                      updateDeployment(
                        selectedDeployment.id,
                        'ready',
                        'Marked the preview sync as complete locally. Azure is unchanged.',
                      )
                    }
                  >
                    Mark ready
                  </Button>
                )}
                <Button
                  appearance="secondary"
                  onClick={() => openDeployDialogForConnection(selectedDeployment.foundryConnectionId)}
                >
                  Deploy similar model
                </Button>
                <Button
                  appearance="subtle"
                  onClick={() => removeDeployment(selectedDeployment.id)}
                >
                  Remove preview row
                </Button>
              </div>
            </Card>
          )}
        </div>
      </Card>

      <Dialog
        open={deployDialogOpen}
        onOpenChange={(_, data) => {
          if (!data.open) {
            closeDeployDialog()
          }
        }}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Deploy model</DialogTitle>
            <DialogContent>
              <form className={styles.dialogForm} onSubmit={submitDeployDialog}>
                <Text>
                  This dialog adds a preview deployment row only. It does not call Azure AI
                  Foundry.
                </Text>
                <Field label="Connection" required>
                  <Select
                    aria-label="Choose a connection for the preview deployment"
                    value={deployDraft.foundryConnectionId}
                    onChange={(event) => {
                      const connection = connections.find(
                        (item) => item.id === event.target.value,
                      )
                      if (connection) {
                        setDeployDraft(createDeploymentDraft(connection.id, connection.provider))
                      }
                    }}
                  >
                    {connections.map((connection) => (
                      <option key={connection.id} value={connection.id}>
                        {connection.displayName}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Deployment name" required>
                  <Input
                    aria-label="Deployment name"
                    value={deployDraft.deploymentName}
                    onChange={(_, data) =>
                      setDeployDraft((current) => ({
                        ...current,
                        deploymentName: data.value,
                      }))
                    }
                  />
                </Field>
                <Field label="Model name" required>
                  <Input
                    aria-label="Model name"
                    value={deployDraft.modelName}
                    onChange={(_, data) =>
                      setDeployDraft((current) => ({
                        ...current,
                        modelName: data.value,
                      }))
                    }
                  />
                </Field>
                <Field label="Model version" required>
                  <Input
                    aria-label="Model version"
                    value={deployDraft.modelVersion}
                    onChange={(_, data) =>
                      setDeployDraft((current) => ({
                        ...current,
                        modelVersion: data.value,
                      }))
                    }
                  />
                </Field>
                <Field label="Endpoint route">
                  <Select
                    aria-label="Preview endpoint route"
                    value={deployDraft.requestPath}
                    onChange={(event) => {
                      const nextPath = event.target.value
                      if (isRequestPath(nextPath)) {
                        setDeployDraft((current) => ({
                          ...current,
                          requestPath: nextPath,
                        }))
                      }
                    }}
                  >
                    <option value="/chat/completions">/chat/completions</option>
                    <option value="/embeddings">/embeddings</option>
                  </Select>
                </Field>
                {deployError && (
                  <MessageBar intent="error">
                    <MessageBarBody>{deployError}</MessageBarBody>
                  </MessageBar>
                )}
                <DialogActions>
                  <Button appearance="secondary" onClick={closeDeployDialog} type="button">
                    Cancel
                  </Button>
                  <Button appearance="primary" type="submit">
                    Add preview deployment
                  </Button>
                </DialogActions>
              </form>
            </DialogContent>
          </DialogBody>
        </DialogSurface>
      </Dialog>

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
