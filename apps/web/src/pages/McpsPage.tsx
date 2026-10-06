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
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ChangeEnvironmentDialog } from '../components/ChangeEnvironmentDialog'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { EnvironmentPicker } from '../components/EnvironmentPicker'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { PageHeader } from '../components/PageHeader'
import { PublishMcpServerDialog } from '../components/PublishMcpServerDialog'
import { RemovalDialog } from '../components/RemovalDialog'
import { ModelAccessRecovery } from '../components/ModelAccessRecovery'
import { PrincipalKindBadge } from '../components/PrincipalKindBadge'
import { UnpublishDialog } from '../components/UnpublishDialog'
import { environmentLabel, useEnvironmentCatalog } from '../environments'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import {
  PUBLICATION_STATUS_LABELS,
  formatTimestamp,
  holdsApi,
  isUnpublished,
  lastAppliedLabel,
  publicationStatusLabel,
} from '../publication-state'
import type {
  CatalogVisibility,
  Gateway,
  McpAuthMode,
  McpEndpoint,
  McpEndpointStatus,
  McpPublication,
  McpServer,
  McpToolAnnotations,
  Principal,
  PublicationStatus,
  PublishPlan,
} from '../types'
import styles from './McpsPage.module.css'

const statusLabels: Record<McpEndpointStatus, string> = {
  pending: 'Not checked',
  connected: 'Connected',
  degraded: 'Partial data',
  unauthorized: 'Access needed',
  unreachable: 'Unreachable',
  unsupportedProtocol: 'Protocol not supported',
  unsupportedTransport: 'Transport not supported',
}

const statusClasses: Record<McpEndpointStatus, string> = {
  pending: styles.pendingBadge,
  connected: styles.connectedBadge,
  degraded: styles.degradedBadge,
  unauthorized: styles.attentionBadge,
  unreachable: styles.attentionBadge,
  // Not failures: the server answered, and the answer is that MOSAIC does not speak its dialect.
  unsupportedProtocol: styles.pendingBadge,
  unsupportedTransport: styles.pendingBadge,
}

const authLabels: Record<McpAuthMode, string> = {
  none: 'None',
  apiKey: 'Key Vault secret',
  managedIdentity: 'Managed identity',
}

const mcpModelCallerKinds = new Set(['servicePrincipal', 'managedIdentity', 'agentIdentity'])

function principalName(principal: Principal) {
  return principal.label?.trim() || principal.detail?.trim() || principal.objectId
}

function canCallModelsAs(principal: Principal) {
  return mcpModelCallerKinds.has(principal.kind)
}

function PublicationStatusBadge({ publication }: { publication: McpPublication }) {
  const { status } = publication
  const attention = status === 'failed' || status === 'rolledBack'
  const active = status === 'applying' || status === 'planned'
  const unpublished = status === 'draft' && isUnpublished(publication)
  return (
    <Badge
      appearance="tint"
      className={
        attention
          ? styles.attentionBadge
          : active
            ? styles.degradedBadge
            : unpublished
              ? styles.pendingBadge
              : styles.connectedBadge
      }
    >
      {publicationStatusLabel(publication)}
    </Badge>
  )
}

function AccessStateBadge({ publication }: { publication: McpPublication }) {
  const label: Record<McpPublication['accessState'], string> = {
    pending: 'Access pending',
    applying: 'Access applying',
    applied: 'Access applied',
    failed: 'Access failed',
    unknown: 'Access unknown',
  }
  const attention = publication.accessState === 'failed' || publication.accessState === 'unknown'
  return (
    <Badge appearance="tint" className={attention ? styles.attentionBadge : styles.claimBadge}>
      {label[publication.accessState]}
    </Badge>
  )
}

function mcpServerUrl(publication: McpPublication, gateway?: Gateway): string {
  const base = gateway?.capabilities.gatewayUrl?.replace(/\/+$/, '')
  return base ? `${base}/${publication.apiPath.replace(/^\/+/, '')}/mcp` : 'Gateway URL not known'
}

function transportLabel(server: McpServer): string {
  if (server.kind === 'passthrough') {
    return server.transportType === 'unknown'
      ? 'Passthrough'
      : `Passthrough · ${server.transportType === 'sse' ? 'SSE' : 'Streamable HTTP'}`
  }
  return 'Backed by REST APIs'
}

/**
 * Renders one annotation hint as the server's claim.
 *
 * Absent is rendered as "not stated", never as its specification default. `destructiveHint` and
 * `openWorldHint` default to *true*, so substituting defaults would either invent a warning or,
 * for the other two hints, invent a reassurance.
 */
function Hint({ label, value }: { label: string; value?: boolean | null }) {
  if (value == null) {
    return (
      <Badge appearance="outline" className={styles.unstatedBadge}>
        {label}: not stated
      </Badge>
    )
  }
  return (
    <Badge appearance="tint" className={styles.claimBadge}>
      {label}: {value ? 'yes' : 'no'}
    </Badge>
  )
}

function ToolHints({ annotations }: { annotations?: McpToolAnnotations | null }) {
  return (
    <div className={styles.hintList}>
      <Hint label="Read only" value={annotations?.readOnlyHint} />
      <Hint label="Destructive" value={annotations?.destructiveHint} />
      <Hint label="Idempotent" value={annotations?.idempotentHint} />
      <Hint label="Open world" value={annotations?.openWorldHint} />
    </div>
  )
}

function ToolsPanel({ endpoint }: { endpoint: McpEndpoint }) {
  const api = useMosaicApi()
  const tools = useQuery({
    queryKey: ['mcp-endpoint-tools', endpoint.id],
    queryFn: () => api.listMcpEndpointTools(endpoint.id),
  })

  return (
    <Card className={styles.panel}>
      <div className={styles.panelHeading}>
        <Title3 as="h2">Tools on {endpoint.name}</Title3>
        <Text size={200}>
          Read from the server itself. API Management exposes only a name, display name, and
          description per tool, so the schemas and behaviour hints below exist nowhere in the
          management plane.
        </Text>
      </div>

      {endpoint.capabilities.instructions && (
        <MessageBar intent="info">
          <MessageBarBody>
            <MessageBarTitle>Server instructions</MessageBarTitle>
            {endpoint.capabilities.instructions}
          </MessageBarBody>
        </MessageBar>
      )}

      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>These are the server&apos;s claims, not MOSAIC&apos;s findings</MessageBarTitle>
          The Model Context Protocol requires clients to treat tool annotations as untrusted, and a
          hint the server did not state is shown as &quot;not stated&quot; rather than assumed. Do
          not read &quot;Read only: yes&quot; as a guarantee.
        </MessageBarBody>
      </MessageBar>

      {tools.isPending && <Loading label="Loading tools" />}
      {tools.isError && <ErrorState error={tools.error} />}
      {tools.data?.length === 0 && (
        <EmptyState title="No tools recorded yet">
          {endpoint.capabilities.supportsTools === 'unavailable'
            ? 'This server did not advertise a tools capability, so MOSAIC did not ask for a list.'
            : 'Sync this server to read the tools it offers.'}
        </EmptyState>
      )}

      {tools.data && tools.data.length > 0 && (
        <Table aria-label={`Tools on ${endpoint.name}`}>
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Tool</TableHeaderCell>
              <TableHeaderCell>Schemas</TableHeaderCell>
              <TableHeaderCell>Stated behaviour</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tools.data.map((tool) => (
              <TableRow key={tool.id}>
                <TableCell>
                  <div className={styles.cellStack}>
                    <Text weight="semibold">{tool.displayName}</Text>
                    <span className={styles.secondaryCell}>{tool.name}</span>
                    {tool.description && (
                      <span className={styles.secondaryCell}>{tool.description}</span>
                    )}
                  </div>
                </TableCell>
                <TableCell>
                  <div className={styles.schemaCell}>
                    <Badge appearance="tint" className={styles.claimBadge}>
                      {tool.inputSchema ? 'Input schema' : 'No input schema'}
                    </Badge>
                    {tool.outputSchema && (
                      <Badge appearance="tint" className={styles.claimBadge}>
                        Output schema
                      </Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell>
                  <ToolHints annotations={tool.annotations} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  )
}

function AccessNotice({ endpoint }: { endpoint: McpEndpoint }) {
  if (endpoint.access.canDiscover) {
    return null
  }
  const challenge = endpoint.access.challenge
  return (
    <MessageBar intent={endpoint.access.evaluation === 'notEvaluated' ? 'warning' : 'error'}>
      <MessageBarBody>
        <MessageBarTitle>{statusLabels[endpoint.status]}</MessageBarTitle>
        {endpoint.access.message ?? 'MOSAIC could not read this server.'}
        {challenge?.scope ? ` The server asked for the scope ${challenge.scope}.` : ''}
        {challenge?.resourceMetadataUrl
          ? ` Its protected resource metadata is at ${challenge.resourceMetadataUrl}.`
          : ''}
      </MessageBarBody>
    </MessageBar>
  )
}

function RegisteredMcpServers({
  onBanner,
  onPublish,
}: {
  onBanner: (message: string) => void
  onPublish: (endpointId?: string) => void
}) {
  const restoreFocus = useRestoreFocusTarget()
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [mode, setMode] = useState<McpAuthMode>('none')
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [environment, setEnvironment] = useState<string | null>(null)
  const [environmentTouched, setEnvironmentTouched] = useState(false)
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [secretUri, setSecretUri] = useState('')
  const [audience, setAudience] = useState('')
  const [changingEndpoint, setChangingEndpoint] = useState<McpEndpoint | null>(null)

  const endpoints = useQuery({
    queryKey: ['mcp-endpoints'],
    queryFn: () => api.listMcpEndpoints(),
  })
  const catalog = useEnvironmentCatalog()

  const selected = useMemo(
    () => endpoints.data?.find((item) => item.id === selectedId) ?? null,
    [endpoints.data, selectedId],
  )

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ['mcp-endpoints'] })
    await queryClient.invalidateQueries({ queryKey: ['mcp-endpoint-tools'] })
  }

  function closeDialog() {
    setDialogOpen(false)
    setUrl('')
    setName('')
    setEnvironment(null)
    setEnvironmentTouched(false)
    setSecretUri('')
    setAudience('')
    setMode('none')
    register.reset()
  }

  const register = useMutation({
    mutationFn: () =>
      api.registerMcpEndpoint({
        endpoint: url.trim(),
        name: name.trim() || undefined,
        environment,
        authMode: mode,
        credentialSecretUri: mode === 'apiKey' ? secretUri.trim() : undefined,
        resourceAudience: mode === 'managedIdentity' ? audience.trim() : undefined,
      }),
    onSuccess: async (endpoint) => {
      await invalidate()
      setSelectedId(endpoint.id)
      closeDialog()
      onBanner(
        `Registered ${endpoint.name}. MOSAIC recorded it as governed; nothing was created in Azure.`,
      )
    },
  })

  const recheck = useMutation({
    mutationFn: (id: string) => api.preflightMcpEndpoint(id),
    onSuccess: invalidate,
  })

  const sync = useMutation({
    mutationFn: (id: string) => api.syncMcpEndpoint(id),
    onSuccess: async () => {
      await invalidate()
      onBanner('Discovery started. Tools appear once the server has answered.')
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteMcpEndpoint(id),
    onSuccess: async () => {
      await invalidate()
      setSelectedId(null)
      onBanner('Stopped governing that MCP server. The server itself is unchanged.')
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    setEnvironmentTouched(true)
    if (!environment) return
    register.mutate()
  }
  const filteredEndpoints = (endpoints.data ?? []).filter((endpoint) => {
    if (environmentFilter === 'all') return true
    if (environmentFilter === '__unclassified__') return endpoint.environment == null
    return endpoint.environment === environmentFilter
  })
  useEffect(() => {
    if (dialogOpen && !environment && catalog.data?.environments[0]) {
      setEnvironment(catalog.data.environments[0].key)
    }
  }, [dialogOpen, environment, catalog.data])

  return (
    <>
      <Card className={styles.panel}>
        <div className={styles.panelHeader}>
          <div className={styles.panelHeading}>
            <Title3 as="h2">Registered MCP servers</Title3>
            <Text size={200}>
              Servers MOSAIC governs directly, whether or not a gateway fronts them yet. MOSAIC
              connects to read what a server offers and never calls a tool.
            </Text>
          </div>
          <Button appearance="primary" icon={<AddRegular />} onClick={() => setDialogOpen(true)}>
            Register server
          </Button>
        </div>

        {!dialogOpen && register.isError && <ErrorState error={register.error} />}
        {recheck.isError && <ErrorState error={recheck.error} />}
        {sync.isError && <ErrorState error={sync.error} />}
        {remove.isError && <ErrorState error={remove.error} />}

        {endpoints.isPending && <Loading label="Loading MCP servers" />}
        {endpoints.isError && <ErrorState error={endpoints.error} />}
        {endpoints.data?.length === 0 && (
          <EmptyState title="No MCP servers registered yet">
            Register a Model Context Protocol server by URL to record its tools and put it under
            MOSAIC governance.
          </EmptyState>
        )}

        {endpoints.data && endpoints.data.length > 0 && (
          <>
            <div className={styles.actionRow}>
              <Field label="Environment filter">
                <Select
                  aria-label="Filter MCP servers by environment"
                  value={environmentFilter}
                  onChange={(event) => setEnvironmentFilter(event.target.value)}
                >
                  <option value="all">All</option>
                  {catalog.data?.environments.map((item) => (
                    <option key={item.key} value={item.key}>{item.displayName}</option>
                  ))}
                  <option value="__unclassified__">Unclassified</option>
                </Select>
              </Field>
            </div>
            <Table aria-label="Registered MCP servers">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Server</TableHeaderCell>
                  <TableHeaderCell>Environment</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Authentication</TableHeaderCell>
                  <TableHeaderCell>Tools</TableHeaderCell>
                  <TableHeaderCell>Last synced</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredEndpoints.map((endpoint) => (
                  <TableRow
                    key={endpoint.id}
                    className={endpoint.id === selectedId ? styles.selectedRow : undefined}
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
                        {endpoint.capabilities.protocolVersion && (
                          <span className={styles.secondaryCell}>
                            Protocol {endpoint.capabilities.protocolVersion} · Streamable HTTP
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <EnvironmentBadge environment={endpoint.environment} catalog={catalog.data} />
                        {endpoint.environmentLabel && <span className={styles.secondaryCell}>Legacy label: {endpoint.environmentLabel}</span>}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge appearance="tint" className={statusClasses[endpoint.status]}>
                        {statusLabels[endpoint.status]}
                      </Badge>
                    </TableCell>
                    <TableCell>{authLabels[endpoint.authMode]}</TableCell>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <Text size={200}>{endpoint.inventory.tools}</Text>
                        {endpoint.inventory.unannotatedTools > 0 && (
                          <span className={styles.secondaryCell}>
                            {endpoint.inventory.unannotatedTools} state no behaviour
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>{formatTimestamp(endpoint.lastSyncedAt)}</TableCell>
                    <TableCell>
                      <div className={styles.actionRow}>
                        <Button
                          appearance="secondary"
                          disabled={recheck.isPending}
                          onClick={() => recheck.mutate(endpoint.id)}
                        >
                          Check connection
                        </Button>
                        <Button
                          appearance="secondary"
                          disabled={sync.isPending || !endpoint.access.canDiscover}
                          onClick={() => sync.mutate(endpoint.id)}
                        >
                          Sync tools
                        </Button>
                        <Button
                          appearance="secondary"
                          onClick={() => onPublish(endpoint.id)}
                          {...restoreFocus}
                        >
                          Publish through a gateway
                        </Button>
                        <Button
                          appearance="subtle"
                          onClick={() => setChangingEndpoint(endpoint)}
                        >
                          Change environment
                        </Button>
                        <Button
                          appearance="subtle"
                          disabled={remove.isPending}
                          onClick={() => remove.mutate(endpoint.id)}
                        >
                          Remove
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Text size={200} className={styles.muted}>
              Removing a server deletes only what MOSAIC stored about it. The server itself is
              never modified.
            </Text>
          </>
        )}
      </Card>

      {selected && <AccessNotice endpoint={selected} />}
      {selected && <ToolsPanel endpoint={selected} />}

      <Dialog
        open={dialogOpen}
        onOpenChange={(_, data) => (data.open ? setDialogOpen(true) : closeDialog())}
      >
        <DialogSurface>
          <form onSubmit={submit}>
            <DialogBody>
              <DialogTitle>Register MCP server</DialogTitle>
              <DialogContent className={styles.dialogForm}>
                <Field
                  label="Server URL"
                  required
                  hint="The Streamable HTTP endpoint, usually ending in /mcp. MOSAIC will not connect to a private or loopback address."
                >
                  <Input
                    value={url}
                    onChange={(_, data) => setUrl(data.value)}
                    placeholder="https://contoso.azure-api.net/tools-mcp/mcp"
                  />
                </Field>

                <TabList
                  selectedValue={mode}
                  onTabSelect={(_, data) => setMode(data.value as McpAuthMode)}
                >
                  <Tab value="none">No auth</Tab>
                  <Tab value="apiKey">Key Vault secret</Tab>
                  <Tab value="managedIdentity">Managed identity</Tab>
                </TabList>

                {mode === 'apiKey' && (
                  <Field
                    label="Key Vault secret URI"
                    required
                    hint="Store the bearer token in Key Vault and paste its secret identifier. MOSAIC stores only this URI, never the token."
                  >
                    <Input
                      value={secretUri}
                      onChange={(_, data) => setSecretUri(data.value)}
                      placeholder="https://my-vault.vault.azure.net/secrets/mcp-token"
                    />
                  </Field>
                )}

                {mode === 'managedIdentity' && (
                  <Field
                    label="Token audience"
                    required
                    hint="Who the token is for. MOSAIC will not infer this: a token is only ever sent to an audience you named."
                  >
                    <Input
                      value={audience}
                      onChange={(_, data) => setAudience(data.value)}
                      placeholder="api://contoso-mcp"
                    />
                  </Field>
                )}

                <Field label="Display name">
                  <Input value={name} onChange={(_, data) => setName(data.value)} />
                </Field>
                <EnvironmentPicker
                  catalog={catalog.data}
                  value={environment}
                  onChange={(value) => { setEnvironment(value); setEnvironmentTouched(true) }}
                  required
                  label="Environment"
                  validationMessage={environmentTouched && !environment ? 'Choose an environment.' : undefined}
                />

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
      {changingEndpoint && (
        <ChangeEnvironmentDialog
          resource={{
            resourceKind: 'mcpEndpoint',
            resourceId: changingEndpoint.id,
            resourceName: changingEndpoint.name,
            environment: changingEndpoint.environment,
          }}
          open={changingEndpoint !== null}
          onClose={() => setChangingEndpoint(null)}
          onChanged={async () => {
            setChangingEndpoint(null)
            onBanner(`Updated ${changingEndpoint.name}'s environment.`)
            await invalidate()
          }}
        />
      )}
    </>
  )
}

function modelCallerLabel(
  principalId: string,
  principals: Principal[] | undefined,
  publication: McpPublication,
) {
  const principal = principals?.find((item) => item.id === principalId)
  if (principal) return principalName(principal)
  const applied = publication.appliedAccess?.modelCaller
  if (applied?.principalId === principalId) return applied.displayName
  return principalId
}

// What an administrator does next when the server's model caller differs from what's live.
function modelCallerNextStep(publication: McpPublication, principals: Principal[] | undefined) {
  const configured = publication.modelCallerId ?? null
  const live = publication.appliedAccess?.modelCaller?.principalId ?? null
  if (live && !configured) {
    return `Still live as ${modelCallerLabel(live, principals, publication)}. Plan and apply to stop attributing its model calls.`
  }
  if (live) {
    return `Still live as ${modelCallerLabel(live, principals, publication)}. Plan and apply to switch.`
  }
  return 'Plan and apply to start attributing its model calls.'
}

function ModelCallerValue({
  publication,
  principals,
}: {
  publication: McpPublication
  principals: Principal[] | undefined
}) {
  const configured = publication.modelCallerId ?? null
  const live = publication.appliedAccess?.modelCaller?.principalId ?? null
  if (!configured && !live) return <Text>None</Text>
  const principal = configured ? principals?.find((item) => item.id === configured) : undefined
  return (
    <div className={styles.cellStack}>
      <Text weight="semibold">
        {configured ? modelCallerLabel(configured, principals, publication) : 'None'}
      </Text>
      {principal && <PrincipalKindBadge kind={principal.kind} />}
      {configured === live ? (
        <Badge appearance="tint" className={styles.connectedBadge}>Applied</Badge>
      ) : (
        <>
          <Badge appearance="tint" className={styles.pendingBadge}>Not applied yet</Badge>
          <span className={styles.secondaryCell}>{modelCallerNextStep(publication, principals)}</span>
        </>
      )}
    </div>
  )
}

function PublishedMcpServers({ onBanner }: { onBanner: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const [removing, setRemoving] = useState<McpPublication | null>(null)
  const [unpublishing, setUnpublishing] = useState<McpPublication | null>(null)
  const [review, setReview] = useState<{ publication: McpPublication; plan: PublishPlan } | null>(null)
  const [modelCallerTarget, setModelCallerTarget] = useState<McpPublication | null>(null)
  const [modelCallerPrincipalId, setModelCallerPrincipalId] = useState('')

  const publications = useQuery({
    queryKey: ['mcp-publications'],
    queryFn: () => api.listMcpPublications(),
  })
  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
  })
  const principals = useQuery({
    queryKey: ['principals'],
    queryFn: () => api.listPrincipals(),
  })

  const gatewaysById = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) map.set(gateway.id, gateway)
    return map
  }, [gateways.data])
  const eligibleModelCallers = useMemo(
    () => (principals.data ?? []).filter(canCallModelsAs).sort((left, right) =>
      principalName(left).localeCompare(principalName(right), undefined, { sensitivity: 'base' }),
    ),
    [principals.data],
  )

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] }),
    ])
  }

  const reviewPlan = useMutation({
    mutationFn: async (publication: McpPublication) => ({
      publication,
      plan: await api.planMcpPublication(publication.id),
    }),
    onSuccess: ({ publication, plan }) => {
      setReview({ publication, plan })
      void queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
    },
  })

  const remove = useMutation({
    mutationFn: (publicationId: string) => api.deleteMcpPublication(publicationId),
    onSuccess: async () => {
      setRemoving(null)
      await refresh()
      onBanner('Removed the MCP publication record. Nothing changed in API Management.')
    },
    onError: async () => {
      await refresh()
    },
  })

  const setModelCaller = useMutation({
    mutationFn: ({ publication, principalId }: { publication: McpPublication; principalId: string }) =>
      api.setMcpModelCaller(publication.id, principalId),
    onSuccess: async (updated, { publication, principalId }) => {
      setModelCallerTarget(null)
      await queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
      onBanner(`${publication.displayName} now calls models as ${modelCallerLabel(principalId, principals.data, updated)}. Plan and apply it to start attributing its model calls.`)
    },
  })

  const clearModelCaller = useMutation({
    mutationFn: (publication: McpPublication) => api.clearMcpModelCaller(publication.id),
    onSuccess: async (_updated, publication) => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
      onBanner(`${publication.displayName} no longer calls models as an application. Plan and apply it to stop attributing model calls.`)
    },
  })

  function openModelCallerDialog(publication: McpPublication) {
    setModelCaller.reset()
    setModelCallerTarget(publication)
    setModelCallerPrincipalId(
      publication.modelCallerId && eligibleModelCallers.some((principal) => principal.id === publication.modelCallerId)
        ? publication.modelCallerId
        : (eligibleModelCallers[0]?.id ?? ''),
    )
  }

  return (
    <>
      <Card className={styles.panel}>
        <div className={styles.panelHeader}>
          <div className={styles.panelHeading}>
            <Title3 as="h2">Published MCP servers</Title3>
            <Text size={200}>
              Registered MCP servers MOSAIC owns in API Management. Publishing creates the MCP API,
              protected resource metadata, and gateway-enforced grant policy.
            </Text>
          </div>
        </div>
        {reviewPlan.isError && <ErrorState error={reviewPlan.error} />}
        {setModelCaller.isError && <ErrorState error={setModelCaller.error} />}
        {clearModelCaller.isError && <ErrorState error={clearModelCaller.error} />}
        {publications.isPending && <Loading label="Loading published MCP servers" />}
        {publications.isError && <ErrorState error={publications.error} />}
        {publications.isSuccess &&
          (publications.data.length === 0 ? (
            <EmptyState title="No MCP servers published yet">
              Publish a registered MCP server to create APIM resources through an explicit plan and
              apply flow.
            </EmptyState>
          ) : (
            <Table aria-label="Published MCP servers">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Publication</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Gateway</TableHeaderCell>
                  <TableHeaderCell>Calls models as</TableHeaderCell>
                  <TableHeaderCell>Server URL</TableHeaderCell>
                  <TableHeaderCell>Last applied</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {publications.data.map((publication) => (
                  <TableRow key={publication.id}>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <Text weight="semibold">{publication.displayName}</Text>
                        <span className={styles.secondaryCell}>{publication.apiName}</span>
                        {publication.lastError && (
                          <span className={styles.secondaryCell}>{publication.lastError}</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <PublicationStatusBadge publication={publication} />
                        {/* Once unpublished, the gateway has no MCP API for access to apply to. */}
                        {!isUnpublished(publication) && <AccessStateBadge publication={publication} />}
                        {publication.accessState === 'unknown' && (
                          <ModelAccessRecovery
                            publicationId={publication.id}
                            runId={publication.lastRunId}
                            target="mcp"
                          />
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Link className={styles.gatewayLink} to={`/gateways/${publication.gatewayId}`}>
                        {gatewaysById.get(publication.gatewayId)?.name ?? publication.gatewayId}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <div className={styles.cellStack}>
                        <ModelCallerValue publication={publication} principals={principals.data} />
                        {!isUnpublished(publication) && (
                          <div className={styles.actionRow}>
                            <Button
                              appearance="secondary"
                              size="small"
                              disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown' || eligibleModelCallers.length === 0}
                              onClick={() => openModelCallerDialog(publication)}
                            >
                              {publication.modelCallerId ? 'Change' : 'Choose'}
                            </Button>
                            {publication.modelCallerId && (
                              <Button
                                appearance="subtle"
                                size="small"
                                disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown' || clearModelCaller.isPending}
                                onClick={() => clearModelCaller.mutate(publication)}
                              >
                                Clear
                              </Button>
                            )}
                          </div>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>{mcpServerUrl(publication, gatewaysById.get(publication.gatewayId))}</TableCell>
                    <TableCell>{lastAppliedLabel(publication)}</TableCell>
                    <TableCell>
                      <div className={styles.actionRow}>
                        <Button
                          appearance="secondary"
                          disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown'}
                          disabledFocusable={reviewPlan.isPending}
                          onClick={() => reviewPlan.mutate(publication)}
                          {...restoreFocus}
                        >
                          {reviewPlan.isPending && reviewPlan.variables?.id === publication.id ? 'Planning…' : 'Plan and apply'}
                        </Button>
                        <Button
                          appearance="secondary"
                          disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown'}
                          onClick={() => setUnpublishing(publication)}
                          {...restoreFocus}
                        >
                          Unpublish
                        </Button>
                        <Button
                          appearance="subtle"
                          disabled={remove.isPending}
                          onClick={() => setRemoving(publication)}
                          {...restoreFocus}
                        >
                          Delete
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ))}
      </Card>
      <PublishMcpServerDialog
        open={review !== null}
        initialReview={review}
        onClose={() => setReview(null)}
        onPublished={onBanner}
      />
      <UnpublishDialog
        open={unpublishing !== null}
        target="mcp"
        publication={unpublishing}
        onClose={() => setUnpublishing(null)}
        onUnpublished={onBanner}
      />
      <Dialog open={modelCallerTarget !== null} onOpenChange={(_, data) => !data.open && setModelCallerTarget(null)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Calls models as</DialogTitle>
            <DialogContent>
              <div className={styles.dialogForm}>
                <Text>
                  Choose the application this MCP server&apos;s tools call governed models as. Its own
                  model grants decide access and pay for the calls. MOSAIC records the person each MCP
                  call served, for usage only.
                </Text>
                <Field label="Application principal">
                  <Select
                    value={modelCallerPrincipalId}
                    onChange={(event) => setModelCallerPrincipalId(event.target.value)}
                  >
                    {eligibleModelCallers.map((principal) => (
                      <option key={principal.id} value={principal.id}>
                        {principalName(principal)} — {PRINCIPAL_KIND_LABELS[principal.kind]}
                      </option>
                    ))}
                  </Select>
                </Field>
                {setModelCaller.isError && <ErrorState error={setModelCaller.error} />}
              </div>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setModelCallerTarget(null)}>
                Cancel
              </Button>
              <Button
                appearance="primary"
                disabled={!modelCallerTarget || !modelCallerPrincipalId || setModelCaller.isPending}
                onClick={() =>
                  modelCallerTarget &&
                  setModelCaller.mutate({ publication: modelCallerTarget, principalId: modelCallerPrincipalId })
                }
              >
                Save
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
      <RemovalDialog
        open={removing !== null}
        title={`Delete ${removing?.displayName ?? 'this MCP publication'}?`}
        confirmLabel="Delete publication"
        refusalTitle="MOSAIC didn't delete this MCP publication"
        pending={remove.isPending}
        error={remove.error}
        statusLabel={(status) => PUBLICATION_STATUS_LABELS[status as PublicationStatus] ?? status}
        onConfirm={() => removing && remove.mutate(removing.id)}
        onCancel={() => setRemoving(null)}
      >
        <Text block>
          MOSAIC deletes its record of this MCP publication and its generated MCP server record.
          Nothing changes in API Management.
        </Text>
        <Text block>
          MOSAIC refuses while the publication can still own gateway state or while grants still
          reference its MCP server. Unpublish it and remove or disable dependent grants first.
        </Text>
      </RemovalDialog>
    </>
  )
}

function ImportedMcpServers({ onBanner }: { onBanner: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const hasConsumedImportQueryRef = useRef(false)
  const [importOpen, setImportOpen] = useState(false)

  const requestedGatewayId = useMemo(
    () => new URLSearchParams(location.search).get('import'),
    [location.search],
  )

  const servers = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => api.listMcpServers(),
  })
  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
  })
  // A server MOSAIC publishes is listed in the portal only while its publication holds its MCP API.
  const publications = useQuery({
    queryKey: ['mcp-publications'],
    queryFn: () => api.listMcpPublications(),
  })

  const gatewayNames = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) {
      map.set(gateway.id, gateway)
    }
    return map
  }, [gateways.data])
  const catalog = useEnvironmentCatalog()

  function hiddenFromPortal(server: McpServer): boolean {
    if (!server.publicationId || !publications.isSuccess) return false
    const publication = publications.data.find((item) => item.id === server.publicationId)
    return !publication || !holdsApi(publication)
  }

  useEffect(() => {
    if (requestedGatewayId && !hasConsumedImportQueryRef.current) {
      hasConsumedImportQueryRef.current = true
      setImportOpen(true)
    }
    if (!requestedGatewayId) {
      hasConsumedImportQueryRef.current = false
    }
  }, [requestedGatewayId])

  function clearImportQuery() {
    const params = new URLSearchParams(location.search)
    if (!params.has('import')) {
      return
    }
    params.delete('import')
    const nextSearch = params.toString()
    navigate(
      { pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '' },
      { replace: true },
    )
  }

  const removeMutation = useMutation({
    mutationFn: (id: string) => api.deleteMcpServer(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      onBanner('Stopped governing that MCP server. Nothing changed in API Management.')
    },
  })

  const visibilityMutation = useMutation({
    mutationFn: ({ id, visibility }: { id: string; visibility: CatalogVisibility }) =>
      api.updateMcpServerCatalog(id, { visibility }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      onBanner('Updated who can discover this MCP server in the portal catalog.')
    },
  })

  return (
    <>
      <Card className={styles.panel}>
        <div className={styles.panelHeader}>
          <div className={styles.panelHeading}>
            <Title3 as="h2">Imported from gateways</Title3>
            <Text size={200}>
              MCP servers your API Management gateways already host. Importing records that MOSAIC
              governs a server. It never creates or changes one in Azure.
            </Text>
          </div>
          <Button appearance="secondary" icon={<AddRegular />} onClick={() => setImportOpen(true)}>
            Import from gateway
          </Button>
        </div>

        {removeMutation.isError && <ErrorState error={removeMutation.error} />}

        {servers.isPending && <Loading label="Loading MCP servers..." />}
        {servers.isError && <ErrorState error={servers.error} />}
        {servers.isSuccess &&
          (servers.data.length === 0 ? (
            <EmptyState title="No MCP servers imported yet">
              Import the MCP servers you want MOSAIC to govern from a synchronised gateway. MCP
              servers require an API Management service on a management API version that supports
              them.
            </EmptyState>
          ) : (
            <Table aria-label="Imported MCP servers">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Server</TableHeaderCell>
                  <TableHeaderCell>Transport</TableHeaderCell>
                  <TableHeaderCell>Tools</TableHeaderCell>
                  <TableHeaderCell>Gateway</TableHeaderCell>
                  <TableHeaderCell>Environment</TableHeaderCell>
                  <TableHeaderCell>Catalog</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {servers.data.map((server) => (
                  <TableRow key={server.id}>
                    <TableCell>
                      <div className={styles.nameCell}>
                        <Text weight="semibold">{server.displayName}</Text>
                        <Text size={200} className={styles.pathText}>
                          /{server.path}
                        </Text>
                        {server.publicationId && (
                          <Badge appearance="tint" className={styles.claimBadge}>
                            Published by MOSAIC
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge appearance="tint" className={styles.transportBadge}>
                        {transportLabel(server)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className={styles.nameCell}>
                        <Text size={200}>
                          {server.toolCount} tool{server.toolCount === 1 ? '' : 's'}
                        </Text>
                        {server.tools.length > 0 && (
                          <ul className={styles.toolList}>
                            {server.tools.slice(0, 3).map((tool) => (
                              <li key={tool.name}>
                                <Text size={200}>{tool.displayName}</Text>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Link className={styles.gatewayLink} to={`/gateways/${server.gatewayId}`}>
                        {gatewayNames.get(server.gatewayId)?.name ?? server.gatewayId}
                      </Link>
                    </TableCell>
                    <TableCell>
                      {(() => {
                        const gateway = gatewayNames.get(server.gatewayId)
                        return gateway ? (
                          <Text size={200}>
                            Environment: {environmentLabel(catalog.data, gateway.environment)} (from
                            gateway {gateway.name})
                          </Text>
                        ) : (
                          '—'
                        )
                      })()}
                    </TableCell>
                    <TableCell>
                      <Select
                        aria-label={`Catalog visibility for ${server.displayName}`}
                        value={server.visibility}
                        disabled={visibilityMutation.isPending}
                        onChange={(_, data) =>
                          visibilityMutation.mutate({
                            id: server.id,
                            visibility: data.value as CatalogVisibility,
                          })
                        }
                      >
                        <option value="catalog">Discoverable</option>
                        <option value="private">Entitled users only</option>
                      </Select>
                      {hiddenFromPortal(server) && (
                        <Text block size={200} className={styles.muted}>
                          Hidden from the portal catalog while it isn&apos;t published.
                        </Text>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className={styles.rowActions}>
                        {server.publicationId ? (
                          <Text size={200} className={styles.muted}>
                            Delete the publication instead.
                          </Text>
                        ) : (
                          <Button
                            appearance="subtle"
                            disabled={removeMutation.isPending}
                            onClick={() => removeMutation.mutate(server.id)}
                          >
                            Remove
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ))}
      </Card>

      <ImportFromGatewayDialog
        kind="mcpServers"
        open={importOpen}
        initialGatewayId={requestedGatewayId}
        onClose={() => {
          setImportOpen(false)
          clearImportQuery()
        }}
        onImported={(count) =>
          onBanner(
            `Imported ${count} MCP server${count === 1 ? '' : 's'}. MOSAIC recorded them as ` +
              'governed; API Management is unchanged.',
          )
        }
      />
    </>
  )
}

export function McpsPage() {
  const restoreFocus = useRestoreFocusTarget()
  const [banner, setBanner] = useState<string | null>(null)
  const [publishOpen, setPublishOpen] = useState(false)

  return (
    <section className={styles.page}>
      <PageHeader
        title="MCPs"
        description="Model Context Protocol servers MOSAIC governs, whether you registered them directly or imported them from a gateway."
        source="live"
        actions={
          <Button {...restoreFocus} appearance="primary" icon={<AddRegular />} onClick={() => setPublishOpen(true)}>
            Publish an MCP server
          </Button>
        }
      />

      <MessageBar intent="info">
        <MessageBarBody>
          <MessageBarTitle>MCP grant enforcement depends on how the server is governed</MessageBarTitle>
          Grants on MCP servers MOSAIC publishes are enforced by the gateway. Grants on MCP servers
          imported from a gateway are recorded in MOSAIC, but the imported server&apos;s own policy
          decides who can call it.
        </MessageBarBody>
      </MessageBar>

      {banner && (
        <MessageBar intent="success">
          <MessageBarBody>{banner}</MessageBarBody>
        </MessageBar>
      )}

      <PublishedMcpServers onBanner={setBanner} />
      <RegisteredMcpServers onBanner={setBanner} onPublish={() => setPublishOpen(true)} />
      <ImportedMcpServers onBanner={setBanner} />
      <PublishMcpServerDialog
        open={publishOpen}
        onClose={() => setPublishOpen(false)}
        onPublished={setBanner}
      />
    </section>
  )
}
