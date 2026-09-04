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
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { PageHeader } from '../components/PageHeader'
import type {
  Gateway,
  McpAuthMode,
  McpEndpoint,
  McpEndpointStatus,
  McpServer,
  McpToolAnnotations,
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

function transportLabel(server: McpServer): string {
  if (server.kind === 'passthrough') {
    return server.transportType === 'unknown'
      ? 'Passthrough'
      : `Passthrough · ${server.transportType === 'sse' ? 'SSE' : 'Streamable HTTP'}`
  }
  return 'Backed by REST APIs'
}

function formatTimestamp(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : 'Never'
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

function RegisteredMcpServers({ onBanner }: { onBanner: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [mode, setMode] = useState<McpAuthMode>('none')
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [environmentLabel, setEnvironmentLabel] = useState('')
  const [secretUri, setSecretUri] = useState('')
  const [audience, setAudience] = useState('')

  const endpoints = useQuery({
    queryKey: ['mcp-endpoints'],
    queryFn: () => api.listMcpEndpoints(),
  })

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
    setEnvironmentLabel('')
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
        environmentLabel: environmentLabel.trim() || undefined,
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
    register.mutate()
  }

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
            <Table aria-label="Registered MCP servers">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Server</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Authentication</TableHeaderCell>
                  <TableHeaderCell>Tools</TableHeaderCell>
                  <TableHeaderCell>Last synced</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {endpoints.data.map((endpoint) => (
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

  const gatewayNames = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) {
      map.set(gateway.id, gateway)
    }
    return map
  }, [gateways.data])

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
                      <div className={styles.rowActions}>
                        <Button
                          appearance="subtle"
                          disabled={removeMutation.isPending}
                          onClick={() => removeMutation.mutate(server.id)}
                        >
                          Remove
                        </Button>
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
  const [banner, setBanner] = useState<string | null>(null)

  return (
    <section className={styles.page}>
      <PageHeader
        title="MCPs"
        description="Model Context Protocol servers MOSAIC governs, whether you registered them directly or imported them from a gateway."
        source="live"
      />

      {banner && (
        <MessageBar intent="success">
          <MessageBarBody>{banner}</MessageBarBody>
        </MessageBar>
      )}

      <RegisteredMcpServers onBanner={setBanner} />
      <ImportedMcpServers onBanner={setBanner} />
    </section>
  )
}
