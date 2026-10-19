import {
  Badge,
  Button,
  Field,
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
  Tooltip,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { AddRegular, ArrowDownloadRegular, PlugConnectedRegular, ServerRegular } from '@fluentui/react-icons'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { mcpCallerReviewValues, rememberEdit } from '../change-review'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { CloudBadge } from '../components/CloudBadge'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { PageHeader } from '../components/PageHeader'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { PublishMcpServerDialog } from '../components/PublishMcpServerDialog'
import { McpRegistrationForm } from '../components/McpRegistrationForm'
import { McpEnvironmentForm } from '../components/McpEnvironmentForm'
import { ResourceWorkbench } from '../components/ResourceWorkbench'
import { WorkspaceEditorFrame } from '../components/WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from '../components/WorkspaceLeaveGuard'
import { useWorkbenchLocation } from '../components/useWorkbenchLocation'
import { RemovalDialog } from '../components/RemovalDialog'
import { RowActions } from '../components/RowActions'
import { EndpointNotificationRun, PublishNotificationRun } from '../components/NotificationOperationRun'
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

const MCP_PAGE_MUTATION_KEY = ['mcp-page'] as const

function useWorkspaceReturnFocus(mode: string | null, detailLabel: string) {
  const previousMode = useRef(mode)
  useEffect(() => {
    if (previousMode.current && !mode) {
      document.querySelector<HTMLElement>(`[aria-label="${detailLabel}"]`)?.focus({ preventScroll: true })
    }
    previousMode.current = mode
  }, [mode, detailLabel])
}

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
      <Text size={200} className={styles.muted}>
        {label}: not stated
      </Text>
    )
  }
  return (
    <Text size={200}>
      {label}: {value ? 'yes' : 'no'}
    </Text>
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
    <section className={styles.toolsPanel}>
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
    </section>
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
  const restoreFocus = useRestoreFocusTarget()
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const workbench = useWorkbenchLocation({ section: 'registered' })
  useWorkspaceReturnFocus(workbench.mode, 'Registered server details')
  const location = useLocation()
  const navigate = useNavigate()
  const environmentFilter = new URLSearchParams(location.search).get('environment') ?? 'all'

  const endpoints = useQuery({
    queryKey: ['mcp-endpoints'],
    queryFn: () => api.listMcpEndpoints(),
  })
  const catalog = useEnvironmentCatalog()

  const selected = useMemo(
    () => endpoints.data?.find((item) => item.id === workbench.selectedId) ?? null,
    [endpoints.data, workbench.selectedId],
  )

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ['mcp-endpoints'] })
    await queryClient.invalidateQueries({ queryKey: ['mcp-endpoint-tools'] })
  }

  const recheck = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: (id: string) => api.preflightMcpEndpoint(id),
    onSuccess: invalidate,
  })

  const sync = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: (id: string) => api.syncMcpEndpoint(id),
    onSuccess: async () => {
      await invalidate()
      onBanner('Discovery started. Tools appear once the server has answered.')
    },
  })

  const remove = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: (id: string) => api.deleteMcpEndpoint(id),
    onSuccess: async () => {
      await invalidate()
      workbench.select(null)
      onBanner('Stopped governing that MCP server. The server itself is unchanged.')
    },
  })

  const filteredEndpoints = (endpoints.data ?? []).filter((endpoint) => {
    if (environmentFilter === 'all') return true
    if (environmentFilter === '__unclassified__') return endpoint.environment == null
    return endpoint.environment === environmentFilter
  })
  if (workbench.mode === 'new') return (
    <McpRegistrationForm mutationKey={MCP_PAGE_MUTATION_KEY} onClose={() => workbench.close()}
      onRegistered={(endpoint) => {
        completeWorkspaceNavigation(() => workbench.close(endpoint.id))
        onBanner(`Registered ${endpoint.name}. MOSAIC recorded it as governed; nothing was created in Azure.`)
      }} />
  )
  if (workbench.mode === 'edit') {
    if (endpoints.isPending) return <Loading label="Loading registered server" />
    if (endpoints.isError) return <ErrorState error={endpoints.error} />
    return selected ? (
      <McpEnvironmentForm endpoint={selected} mutationKey={MCP_PAGE_MUTATION_KEY}
        onClose={() => workbench.close()} onChanged={() => {
          completeWorkspaceNavigation(() => workbench.close())
          onBanner(`Updated ${selected.name}'s environment.`)
        }} />
    ) : <WorkspaceEditorFrame title="Server unavailable" onBack={() => workbench.close()}>
      <EmptyState title="This registered server is no longer available">Return to the list and select another server.</EmptyState>
    </WorkspaceEditorFrame>
  }
  if (workbench.mode === 'publish') return (
    <PublishMcpServerDialog surface="workspace" open mutationKey={MCP_PAGE_MUTATION_KEY}
      initialEndpointId={workbench.selectedId} onClose={() => workbench.close()} onPublished={onBanner} />
  )

  return (
    <>
      {recheck.isError && <ErrorState error={recheck.error} />}
      {sync.isError && <ErrorState error={sync.error} />}
      {remove.isError && <ErrorState error={remove.error} />}
      {endpoints.isError && <ErrorState error={endpoints.error} />}
      <ResourceWorkbench title="Registered MCP servers"
        description="Servers MOSAIC governs directly. Connect to read what a server offers; never call its tools."
        actions={<Button appearance="primary" icon={<AddRegular />} onClick={() => workbench.open('new')}>Register server</Button>}
        toolbar={<Field label="Environment filter">
          <Select aria-label="Filter MCP servers by environment" value={environmentFilter}
            onChange={(event) => {
              const params = new URLSearchParams(location.search)
              params.set('environment', event.target.value)
              navigate({ pathname: location.pathname, search: params.toString() })
            }}>
            <option value="all">All</option>
            {catalog.data?.environments.map((item) => <option key={item.key} value={item.key}>{item.displayName}</option>)}
            <option value="__unclassified__">Unclassified</option>
          </Select>
        </Field>}
        items={filteredEndpoints} getId={(endpoint) => endpoint.id}
        selectedId={workbench.selectedId} onSelect={workbench.select}
        listLabel="Registered MCP servers" detailLabel="Registered server details"
        missingSelection={endpoints.isPending ? <Loading label="Loading selected server" /> : <EmptyState title="Server not in this view">
          This server is outside the selected environment or is no longer available. Change the environment filter or select another server.
        </EmptyState>}
        emptyList={endpoints.isPending ? <Loading label="Loading MCP servers" />
          : endpoints.isError ? <Text>Server inventory could not be loaded.</Text>
          : <EmptyState title={endpoints.data?.length ? 'No servers match this environment' : 'No MCP servers registered yet'}>
          Register a Model Context Protocol server by URL to record its tools and put it under MOSAIC governance.
        </EmptyState>}
        renderListItem={(endpoint) => <div className={styles.resourceRow}>
          <span className={styles.resourceName}>{endpoint.name}</span>
          <CloudBadge cloud={endpoint.azureCloud} />
          <span className={styles.secondaryCell}>{endpoint.inventory.tools} tools · {environmentLabel(catalog.data, endpoint.environment)}</span>
          <Badge appearance="tint" className={statusClasses[endpoint.status]}>{statusLabels[endpoint.status]}</Badge>
        </div>}
        renderDetail={(endpoint) => <div className={styles.detailStack}>
          <EndpointNotificationRun endpointId={endpoint.id} kind="mcp" />
          <div className={styles.detailHeader}>
            <Title3 as="h3">{endpoint.name}</Title3>
            <RowActions label={`More actions for ${endpoint.name}`}
              primary={<Button disabled={recheck.isPending} onClick={() => recheck.mutate(endpoint.id)} {...restoreFocus}>Check connection</Button>}
              actions={[
                { label: 'Sync tools', disabled: sync.isPending || !endpoint.access.canDiscover, onClick: () => sync.mutate(endpoint.id) },
                { label: 'Publish through a gateway', onClick: () => workbench.open('publish', endpoint.id) },
                { label: 'Change environment', onClick: () => workbench.open('edit', endpoint.id) },
              ]}
              destructiveActions={[{ label: 'Remove MOSAIC record', disabled: remove.isPending, onClick: () => remove.mutate(endpoint.id) }]} />
          </div>
          <dl className={styles.metadata}>
            <div><dt>Status</dt><dd><Badge appearance="tint" className={statusClasses[endpoint.status]}>{statusLabels[endpoint.status]}</Badge></dd></div>
            <div><dt>Server URL</dt><dd>{endpoint.endpoint}</dd></div>
            <div><dt>Protocol and transport</dt><dd>
              {endpoint.capabilities.protocolVersion ? `Protocol ${endpoint.capabilities.protocolVersion} · ` : ''}
              {endpoint.capabilities.transportType === 'sse' ? 'SSE'
                : endpoint.capabilities.transportType === 'streamable' ? 'Streamable HTTP' : 'Transport not known'}
            </dd></div>
            <div><dt>Authentication</dt><dd>{authLabels[endpoint.authMode]}</dd></div>
            {endpoint.resourceAudience && <div><dt>Token audience</dt><dd>{endpoint.resourceAudience}</dd></div>}
            <div><dt>Environment</dt><dd><EnvironmentBadge azureCloud={endpoint.azureCloud ?? 'commercial'} environment={endpoint.environment} catalog={catalog.data} />
              {endpoint.environmentLabel && <Text block size={200}>Legacy label: {endpoint.environmentLabel}</Text>}
            </dd></div>
            <div><dt>Last synced</dt><dd>{formatTimestamp(endpoint.lastSyncedAt)}</dd></div>
            {endpoint.inventory.unannotatedTools > 0 && <div><dt>Behaviour annotations</dt><dd>{endpoint.inventory.unannotatedTools} state no behaviour</dd></div>}
          </dl>
          {endpoint.lastSyncError && <MessageBar intent="error"><MessageBarBody>{endpoint.lastSyncError}</MessageBarBody></MessageBar>}
          <AccessNotice endpoint={endpoint} />
          <Text size={200} className={styles.muted}>Removing a server deletes only what MOSAIC stored about it. The server itself is never modified.</Text>
          <ToolsPanel endpoint={endpoint} />
        </div>} />
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
    return `Still live as ${modelCallerLabel(live, principals, publication)}. Review changes and apply the plan to stop attributing its model calls.`
  }
  if (live) {
    return `Still live as ${modelCallerLabel(live, principals, publication)}. Review changes and apply the plan to switch.`
  }
  return 'Review changes and apply the plan to start attributing its model calls.'
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

function ModelCallerForm({ publication, principals, pending, error, onClose, onSave }: {
  publication: McpPublication
  principals: Principal[]
  pending: boolean
  error: unknown
  onClose: () => void
  onSave: (principalId: string, before: McpPublication) => void
}) {
  const editBase = useRef(publication)
  const eligible = principals.filter(canCallModelsAs).sort((left, right) =>
    principalName(left).localeCompare(principalName(right), undefined, { sensitivity: 'base' }))
  const [principalId, setPrincipalId] = useState(publication.modelCallerId ?? eligible[0]?.id ?? '')
  return (
    <WorkspaceEditorFrame title="Model-calling identity"
      description="The identity this server uses when it calls a model."
      onBack={onClose} onCancel={onClose}
      dirty={principalId !== (publication.modelCallerId ?? '')} pending={pending}
      primaryAction={{
        label: 'Save',
        disabled: Boolean(principalId) && !eligible.some((principal) => principal.id === principalId),
        onClick: () => onSave(principalId, editBase.current),
      }}>
      <div className={styles.editorForm}>
        <Text>Its own model grants decide access and pay for the calls. MOSAIC records the person each MCP call served, for usage only.</Text>
        <Text>Saving changes the configured identity. Nothing changes at the gateway until you review and apply all pending changes.</Text>
        <Field label="Application principal">
          <Select value={principalId} onChange={(event) => setPrincipalId(event.target.value)}>
            <option value="">None</option>
            {publication.modelCallerId && !eligible.some((principal) => principal.id === publication.modelCallerId) &&
              <option value={publication.modelCallerId} disabled>Current identity unavailable</option>}
            {eligible.map((principal) => <option key={principal.id} value={principal.id}>
              {principalName(principal)} — {PRINCIPAL_KIND_LABELS[principal.kind]}
            </option>)}
          </Select>
        </Field>
        {error != null && <ErrorState error={error} />}
      </div>
    </WorkspaceEditorFrame>
  )
}

function PublishedMcpServers({ onBanner }: { onBanner: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const [removing, setRemoving] = useState<McpPublication | null>(null)
  const [unpublishing, setUnpublishing] = useState<McpPublication | null>(null)
  const [review, setReview] = useState<{ publication: McpPublication; plan: PublishPlan } | null>(null)
  const workbench = useWorkbenchLocation({ section: 'published' })
  useWorkspaceReturnFocus(workbench.mode, 'Published server details')
  const requestedReview = useRef<string | null>(null)

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
  const servers = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => api.listMcpServers(),
  })
  const selected = publications.data?.find((item) => item.id === workbench.selectedId) ?? null

  const gatewaysById = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) map.set(gateway.id, gateway)
    return map
  }, [gateways.data])
  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] }),
    ])
  }

  const reviewPlan = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: async (publication: McpPublication) => ({
      publication,
      plan: await api.planMcpPublication(publication.id),
    }),
    onSuccess: ({ publication, plan }) => {
      setReview({ publication, plan })
      if (workbench.mode !== 'review') workbench.open('review', publication.id)
      void queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
    },
  })

  const remove = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: (publicationId: string) => api.deleteMcpPublication(publicationId),
    onSuccess: async () => {
      setRemoving(null)
      await refresh()
      workbench.select(null)
      onBanner('Removed the MCP publication record. Nothing changed in API Management.')
    },
    onError: async () => {
      await refresh()
    },
  })

  const setModelCaller = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: ({ publication, principalId }: { publication: McpPublication; principalId: string }) =>
      api.setMcpModelCaller(publication.id, principalId),
    onSuccess: async (updated, { publication, principalId }) => {
      rememberEdit(updated.id, mcpCallerReviewValues(publication, principals.data ?? []),
        mcpCallerReviewValues(updated, principals.data ?? []))
      await queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
      completeWorkspaceNavigation(() => workbench.close())
      onBanner(`${publication.displayName}'s model-calling identity is now configured as ${modelCallerLabel(principalId, principals.data, updated)}. Review changes and apply the plan to start attributing its model calls.`)
    },
  })

  const clearModelCaller = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: (publication: McpPublication) => api.clearMcpModelCaller(publication.id),
    onSuccess: async (updated, publication) => {
      rememberEdit(updated.id, mcpCallerReviewValues(publication, principals.data ?? []),
        mcpCallerReviewValues(updated, principals.data ?? []))
      await queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
      completeWorkspaceNavigation(() => workbench.close())
      onBanner(`${publication.displayName}'s model-calling identity is cleared. Review changes and apply the plan to stop attributing model calls.`)
    },
  })

  function openModelCaller(publication: McpPublication) {
    setModelCaller.reset()
    clearModelCaller.reset()
    workbench.open('edit', publication.id)
  }
  const requestReview = reviewPlan.mutate
  useEffect(() => {
    if (workbench.mode !== 'review') { requestedReview.current = null; return }
    if (!selected || review?.publication.id === selected.id || requestedReview.current === selected.id) return
    if (selected.status === 'applying' || selected.accessState === 'applying' || selected.accessState === 'unknown') return
    requestedReview.current = selected.id
    requestReview(selected)
  }, [workbench.mode, selected, review?.publication.id, requestReview])

  if (workbench.mode === 'publish') return (
    <PublishMcpServerDialog surface="workspace" open mutationKey={MCP_PAGE_MUTATION_KEY}
      onClose={() => workbench.close()} onPublished={onBanner} />
  )
  if (workbench.mode === 'edit' || workbench.mode === 'review') {
    if (publications.isPending || (workbench.mode === 'edit' && principals.isPending)) return <Loading label="Loading MCP publication" />
    if (publications.isError) return <ErrorState error={publications.error} />
    if (workbench.mode === 'edit' && principals.isError) return <ErrorState error={principals.error} />
    if (!selected) return (
      <WorkspaceEditorFrame title="Publication unavailable" onBack={() => workbench.close()}>
        <EmptyState title="This MCP publication is no longer available">Return to the list and select another publication.</EmptyState>
      </WorkspaceEditorFrame>
    )
    const locked = selected.status === 'applying' || selected.accessState === 'applying' || selected.accessState === 'unknown'
    if (workbench.mode === 'edit' && isUnpublished(selected)) return (
      <WorkspaceEditorFrame title="Model-calling identity" onBack={() => workbench.close()}>
        <Text>Publish this server again before changing its model-calling identity.</Text>
      </WorkspaceEditorFrame>
    )
    if (locked && (workbench.mode === 'edit' || review?.publication.id !== selected.id)) return (
      <WorkspaceEditorFrame title={selected.displayName} onBack={() => workbench.close()}>
        <Text>Resolve the current gateway operation before changing this publication.</Text>
        {selected.accessState === 'unknown' && <ModelAccessRecovery publicationId={selected.id} runId={selected.lastRunId} target="mcp" />}
      </WorkspaceEditorFrame>
    )
    if (workbench.mode === 'edit') return (
      <ModelCallerForm key={selected.id} publication={selected} principals={principals.data ?? []}
        pending={setModelCaller.isPending || clearModelCaller.isPending}
        error={setModelCaller.error ?? clearModelCaller.error} onClose={() => workbench.close()}
        onSave={(principalId, before) => principalId
          ? setModelCaller.mutate({ publication: before, principalId })
          : clearModelCaller.mutate(before)} />
    )
    if (review?.publication.id === selected.id) return (
      <PublishMcpServerDialog surface="workspace" open mutationKey={MCP_PAGE_MUTATION_KEY}
        initialReview={review} onClose={() => { setReview(null); workbench.close() }} onPublished={onBanner} />
    )
    return <WorkspaceEditorFrame title="Review MCP access" onBack={() => workbench.close()} pending={reviewPlan.isPending}>
      {reviewPlan.isError ? <ErrorState error={reviewPlan.error} /> : <Loading label="Preparing MCP review" />}
      {reviewPlan.isError && <Button onClick={() => reviewPlan.mutate(selected)}>Retry plan</Button>}
    </WorkspaceEditorFrame>
  }

  return (
    <>
        {reviewPlan.isError && <ErrorState error={reviewPlan.error} />}
        {setModelCaller.isError && <ErrorState error={setModelCaller.error} />}
        {clearModelCaller.isError && <ErrorState error={clearModelCaller.error} />}
        {publications.isError && <ErrorState error={publications.error} />}
      <ResourceWorkbench title="Published MCP servers"
        description="Registered servers MOSAIC publishes in API Management through a reviewed plan and explicit Apply."
        actions={<Button appearance="primary" icon={<AddRegular />} onClick={() => workbench.open('publish')}>Publish an MCP server</Button>}
        items={publications.data ?? []} getId={(publication) => publication.id}
        selectedId={workbench.selectedId} onSelect={workbench.select}
        listLabel="Published MCP servers" detailLabel="Published server details"
        missingSelection={publications.isPending ? <Loading label="Loading selected publication" />
          : <EmptyState title="Publication not available">Select another publication or return to the list.</EmptyState>}
        emptyList={publications.isPending ? <Loading label="Loading published MCP servers" />
          : publications.isError ? <Text>Publication inventory could not be loaded.</Text>
          : <EmptyState title="No MCP servers published yet">
              Publish a registered MCP server to create APIM resources through an explicit plan and
              apply flow.
            </EmptyState>}
        renderListItem={(publication) => <div className={styles.resourceRow}>
          <span className={styles.resourceName}>{publication.displayName}</span>
          <CloudBadge cloud={publication.azureCloud ?? gatewaysById.get(publication.gatewayId)?.azureCloud} />
          <span className={styles.secondaryCell}>
            {gatewaysById.get(publication.gatewayId)?.name ?? 'Gateway unavailable'} ·{' '}
            {servers.data?.find((server) => server.id === publication.mcpServerId)?.toolCount ?? 'Unknown'} tools
          </span>
          <PublicationStatusBadge publication={publication} />
        </div>}
        renderDetail={(publication) => <div className={styles.detailStack}>
          <PublishNotificationRun publicationId={publication.id} target="mcp" />
          <div className={styles.detailHeader}>
            <Title3 as="h3">{publication.displayName}</Title3>
            <RowActions label={`More actions for ${publication.displayName}`}
              primary={<Tooltip relationship="description" content="Preview the changes MOSAIC would apply. Nothing changes until you select Apply plan.">
                <Button disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown'}
                  disabledFocusable={reviewPlan.isPending}
                  onClick={() => { setReview(null); workbench.open('review', publication.id) }} {...restoreFocus}>
                  {reviewPlan.isPending && reviewPlan.variables?.id === publication.id ? 'Planning…' : publication.status === 'published' || holdsApi(publication) ? 'Review changes' : 'Review publish plan'}
                </Button>
              </Tooltip>}
              destructiveActions={[
                { label: 'Unpublish', disabled: publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown', onClick: () => setUnpublishing(publication) },
                { label: 'Remove MOSAIC record', disabled: remove.isPending, onClick: () => setRemoving(publication) },
              ]} />
          </div>
          <Text size={200}>Preview the changes MOSAIC would apply. Nothing changes until you select Apply plan.</Text>
          <dl className={styles.metadata}>
            <div><dt>Publication</dt><dd>
                        <PublicationStatusBadge publication={publication} />
            </dd></div>
            <div><dt>Gateway access</dt><dd>
                        {!isUnpublished(publication) && <AccessStateBadge publication={publication} />}
                        {publication.accessState === 'unknown' && (
                          <ModelAccessRecovery
                            publicationId={publication.id}
                            runId={publication.lastRunId}
                            target="mcp"
                          />
                        )}
              {isUnpublished(publication) && <Text>No published API to apply access to.</Text>}
            </dd></div>
            <div><dt>Gateway</dt><dd>
                      <Link className={styles.gatewayLink} to={`/gateways/${publication.gatewayId}`}>
                        {gatewaysById.get(publication.gatewayId)?.name ?? publication.gatewayId}
                      </Link>
            </dd></div>
            <div><dt>Server URL</dt><dd>{mcpServerUrl(publication, gatewaysById.get(publication.gatewayId))}</dd></div>
            <div><dt>API name</dt><dd>{publication.apiName}</dd></div>
            <div><dt>Last applied</dt><dd>{lastAppliedLabel(publication)}</dd></div>
            <div><dt>Tools</dt><dd>{servers.data?.find((server) => server.id === publication.mcpServerId)?.toolCount ?? 'Tool count unavailable'}</dd></div>
          </dl>
          {servers.isError && <ErrorState error={servers.error} />}
          {publication.lastError && <MessageBar intent="error"><MessageBarBody>{publication.lastError}</MessageBarBody></MessageBar>}
          <section className={styles.detailSection} aria-label="Model-calling identity">
            <div className={styles.detailHeader}>
              <h3>Model-calling identity</h3>
              {!isUnpublished(publication) && <Button
                disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown' || !principals.isSuccess ||
                  (!publication.modelCallerId && !(principals.data ?? []).some(canCallModelsAs))}
                onClick={() => openModelCaller(publication)}>
                {publication.modelCallerId ? 'Change' : 'Choose'}
              </Button>}
            </div>
            <Text size={200}>The identity this server uses when it calls a model.</Text>
                        <ModelCallerValue publication={publication} principals={principals.data} />
            {principals.isError && <ErrorState error={principals.error} />}
          </section>
        </div>} />
      <UnpublishDialog
        open={unpublishing !== null}
        target="mcp"
        mutationKey={MCP_PAGE_MUTATION_KEY}
        publication={unpublishing}
        onClose={() => setUnpublishing(null)}
        onUnpublished={onBanner}
      />
      <RemovalDialog
        open={removing !== null}
        title={`Remove ${removing?.displayName ?? 'this MCP publication'}?`}
        confirmLabel="Remove publication"
        refusalTitle="MOSAIC didn't remove this MCP publication"
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
  const workbench = useWorkbenchLocation({ section: 'imported' })
  useWorkspaceReturnFocus(workbench.mode, 'Imported server details')

  const requestedGatewayId = useMemo(
    () => new URLSearchParams(location.search).get('importGateway'),
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

  function clearImportQuery(importedId?: string) {
    const params = new URLSearchParams(location.search)
    params.delete('mode')
    params.delete('import')
    params.delete('importGateway')
    if (importedId) {
      params.set('selected', importedId)
      params.set('workbench', `${location.pathname}:imported`)
    }
    if (!params.has('selected')) params.delete('workbench')
    const nextSearch = params.toString()
    navigate(
      { pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '', hash: location.hash },
      { replace: true, preventScrollReset: true },
    )
  }

  const removeMutation = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: (id: string) => api.deleteMcpServer(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      workbench.select(null)
      onBanner('Stopped governing that MCP server. Nothing changed in API Management.')
    },
  })

  const visibilityMutation = useMutation({
    mutationKey: MCP_PAGE_MUTATION_KEY,
    mutationFn: ({ id, visibility }: { id: string; visibility: CatalogVisibility }) =>
      api.updateMcpServerCatalog(id, { visibility }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      onBanner('Updated who can discover this MCP server in the portal catalog.')
    },
  })

  if (workbench.mode === 'import') return (
    <ImportFromGatewayDialog surface="workspace" kind="mcpServers" mutationKey={MCP_PAGE_MUTATION_KEY}
      open initialGatewayId={requestedGatewayId} onClose={clearImportQuery}
      onImported={(count) => onBanner(`Imported ${count} MCP server${count === 1 ? '' : 's'}. MOSAIC recorded them as governed; API Management is unchanged.`)} />
  )
  return (
    <>
      {removeMutation.isError && <ErrorState error={removeMutation.error} />}
      {visibilityMutation.isError && <ErrorState error={visibilityMutation.error} />}
      {servers.isError && <ErrorState error={servers.error} />}
      <ResourceWorkbench title="Imported from gateways"
        description="Servers already hosted by a gateway. Importing records governance; it never creates or changes a server in Azure."
        actions={<Button appearance="primary" icon={<ArrowDownloadRegular />} onClick={() => workbench.open('import')}>Import from gateway</Button>}
        items={servers.data ?? []} getId={(server) => server.id}
        selectedId={workbench.selectedId} onSelect={workbench.select}
        listLabel="Imported MCP servers" detailLabel="Imported server details"
        missingSelection={servers.isPending ? <Loading label="Loading selected import" />
          : <EmptyState title="Import not available">Select another import or return to the list.</EmptyState>}
        emptyList={servers.isPending ? <Loading label="Loading MCP servers..." />
          : servers.isError ? <Text>Imported server inventory could not be loaded.</Text>
          : <EmptyState title="No MCP servers imported yet">
              Import the MCP servers you want MOSAIC to govern from a synchronised gateway. MCP
              servers require an API Management service on a management API version that supports
              them.
            </EmptyState>}
        renderListItem={(server) => <div className={styles.resourceRow}>
          <span className={styles.resourceName}>{server.displayName}</span>
          <CloudBadge cloud={gatewayNames.get(server.gatewayId)?.azureCloud} />
          <span className={styles.secondaryCell}>{server.toolCount} tool{server.toolCount === 1 ? '' : 's'} · {gatewayNames.get(server.gatewayId)?.name ?? 'Gateway unavailable'}</span>
          <span className={styles.secondaryCell}>{server.publicationId ? 'Published by MOSAIC' : 'Imported'}</span>
        </div>}
        renderDetail={(server) => <div className={styles.detailStack}>
          <div className={styles.detailHeader}>
            <Title3 as="h3">{server.displayName}</Title3>
            {server.publicationId ? <Text size={200}>Remove the publication record instead.</Text> : (
              <Button appearance="subtle" disabled={removeMutation.isPending} onClick={() => removeMutation.mutate(server.id)}>Remove import</Button>
            )}
          </div>
          <Text size={200}>Grants are recorded in MOSAIC. This imported server&apos;s own gateway policy decides who can call it.</Text>
          <dl className={styles.metadata}>
            <div><dt>Governance</dt><dd>{server.publicationId ? 'Published by MOSAIC' : 'Imported from gateway'}</dd></div>
            <div><dt>Transport</dt><dd>{transportLabel(server)}</dd></div>
            <div><dt>Path</dt><dd>/{server.path}</dd></div>
            {server.serviceUrl && <div><dt>Backend URL</dt><dd>{server.serviceUrl}</dd></div>}
            <div><dt>Gateway</dt><dd><Link className={styles.gatewayLink} to={`/gateways/${server.gatewayId}`}>
              {gatewayNames.get(server.gatewayId)?.name ?? server.gatewayId}
            </Link></dd></div>
            <div><dt>Environment</dt><dd>{(() => {
              const gateway = gatewayNames.get(server.gatewayId)
              return gateway ? `${environmentLabel(catalog.data, gateway.environment)} (from gateway ${gateway.name})` : 'Gateway environment unavailable'
            })()}</dd></div>
            <div><dt>Imported</dt><dd>{formatTimestamp(server.importedAt)}</dd></div>
          </dl>
          <Field label="Catalog visibility" hint="Controls discovery in the portal, not gateway authorization.">
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
          </Field>
          <section className={styles.detailSection}>
            <h3>Tools</h3>
            <Text>{server.toolCount} tool{server.toolCount === 1 ? '' : 's'}</Text>
            {server.tools.length > 0 && <ul className={styles.toolList}>
              {server.tools.map((tool) => <li key={tool.name}>
                <Text weight="semibold">{tool.displayName}</Text>
                {tool.description && <Text block size={200}>{tool.description}</Text>}
              </li>)}
            </ul>}
          </section>
          <Text size={200}>Removing an import deletes only the MOSAIC record. Nothing changes in API Management.</Text>
        </div>} />
    </>
  )
}

const mcpSections = [
  { value: 'published', label: 'Published MCP servers', icon: <ServerRegular /> },
  { value: 'imported', label: 'Imported MCP servers', icon: <ArrowDownloadRegular /> },
  { value: 'registered', label: 'Registered MCP servers', icon: <PlugConnectedRegular /> },
]

export function McpsPage() {
  const pendingOperations = useIsMutating({ mutationKey: MCP_PAGE_MUTATION_KEY })
  const location = useLocation()
  const navigate = useNavigate()
  const params = useMemo(() => new URLSearchParams(location.search), [location.search])
  const [section, selectSection] = usePageSection(mcpSections, params.has('import') ? 'imported' : 'published')
  const [banner, setBanner] = useState<string | null>(null)
  useEffect(() => {
    if (!params.has('import')) return
    const next = new URLSearchParams(params)
    const gatewayId = next.get('import')
    next.delete('import')
    if (gatewayId) next.set('importGateway', gatewayId)
    if (next.get('tab') !== 'imported') next.delete('selected')
    next.set('tab', 'imported')
    next.set('mode', 'import')
    next.set('workbench', `${location.pathname}:imported`)
    navigate({ pathname: location.pathname, search: next.toString(), hash: location.hash }, { replace: true, preventScrollReset: true })
  }, [params, navigate, location.pathname, location.hash])

  return (
    <section className={styles.page}>
      <PageHeader
        title="MCPs"
        description="Model Context Protocol servers MOSAIC governs, whether you registered them directly or imported them from a gateway."
        source="live"
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

      {pendingOperations > 0 && <Text role="status">Finish the current operation before changing sections.</Text>}
      <SectionLayout label="MCPs" sections={mcpSections} active={section}
        onSelect={(value) => { if (!pendingOperations) selectSection(value) }}>
        {section === 'published' && <PublishedMcpServers onBanner={setBanner} />}
        {section === 'registered' && <RegisteredMcpServers onBanner={setBanner} />}
        {section === 'imported' && <ImportedMcpServers onBanner={setBanner} />}
      </SectionLayout>
    </section>
  )
}
