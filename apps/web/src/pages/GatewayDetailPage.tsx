import {
  Badge,
  Button,
  Card,
  Menu,
  MenuButton,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Tab,
  TabList,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { AI_KIND_LABELS } from '../labels'
import { PolicyDocumentCard, PolicyFragmentCard } from '../components/PolicyFacets'
import type { AiBackendKind, Gateway, PublicationStatus } from '../types'
import { PageHeader } from '../components/PageHeader'
import { AccessPanel, GatewayStatusBadge } from './GatewaysPage'
import styles from './GatewayDetailPage.module.css'

type TabKey =
  | 'overview'
  | 'apis'
  | 'mcpServers'
  | 'products'
  | 'subscriptions'
  | 'identities'
  | 'policies'
  | 'backends'


const publicationStatusLabels: Record<PublicationStatus, string> = {
  draft: 'Draft',
  planned: 'Planned',
  applying: 'Applying',
  published: 'Published',
  failed: 'Failed',
  rolledBack: 'Rolled back',
}

function PublishedModelsSection({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const publications = useQuery({
    queryKey: ['publications', gatewayId],
    queryFn: () => api.listPublications(gatewayId),
  })

  if (publications.isPending) return <Loading label="Loading published models" />
  if (publications.isError) return <ErrorState error={publications.error} />
  if (publications.data.length === 0) {
    return (
      <EmptyState title="No models published into this gateway">
        Publish a model from the Models page to create APIM resources here.
      </EmptyState>
    )
  }

  return (
    <Card className={styles.apiCard}>
      <Title3 as="h2">Published by MOSAIC</Title3>
      <Text size={200}>Resources MOSAIC created through the model publication workflow.</Text>
      <div className="table-scroll">
        <table aria-label="Gateway published models">
          <thead>
            <tr>
              <th>Publication</th>
              <th>Status</th>
              <th>API path</th>
              <th>Last applied</th>
            </tr>
          </thead>
          <tbody>
            {publications.data.map((publication) => (
              <tr key={publication.id}>
                <td>{publication.displayName}</td>
                <td>{publicationStatusLabels[publication.status]}</td>
                <td>/{publication.apiPath}</td>
                <td>{publication.lastAppliedAt ? new Date(publication.lastAppliedAt).toLocaleString() : 'Never'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function AiBadge({ kind }: { kind: AiBackendKind }) {
  if (kind === 'none') {
    return null
  }
  return (
    <Badge appearance="tint" className={styles.aiBadge}>
      {AI_KIND_LABELS[kind]}
    </Badge>
  )
}

function Overview({ gateway }: { gateway: Gateway }) {
  const { inventory, capabilities } = gateway
  return (
    <>
      <div className={styles.metricGrid}>
        <Card className={styles.metricCard}>
          <Text size={200}>AI surface</Text>
          <Title3 as="p">
            {inventory.aiApis} of {inventory.apis}
          </Title3>
          <Text size={200}>APIs front Azure AI backends</Text>
        </Card>
        <Card className={styles.metricCard}>
          <Text size={200}>Endpoints</Text>
          <Title3 as="p">{inventory.operations}</Title3>
          <Text size={200}>operations across all APIs</Text>
        </Card>
        <Card className={styles.metricCard}>
          <Text size={200}>MCP servers</Text>
          <Title3 as="p">{inventory.mcpServers}</Title3>
          <Text size={200}>
            {capabilities.mcpServers === 'unavailable'
              ? 'not supported on this service'
              : 'hosted by this gateway'}
          </Text>
        </Card>
        <Card className={styles.metricCard}>
          <Text size={200}>Access paths</Text>
          <Title3 as="p">{inventory.subscriptions}</Title3>
          <Text size={200}>
            subscriptions across {inventory.products} product
            {inventory.products === 1 ? '' : 's'}
          </Text>
        </Card>
        <Card className={styles.metricCard}>
          <Text size={200}>Policy rules</Text>
          <Title3 as="p">{inventory.recognizedFacets}</Title3>
          <Text size={200}>
            understood, {inventory.unrecognizedFacets} authored outside MOSAIC
          </Text>
        </Card>
      </div>
      <Card className={styles.detailCard}>
        <Title3 as="h3">Service</Title3>
        <Text block>
          {gateway.serviceName} · {capabilities.skuName ?? 'unknown tier'}
          {capabilities.skuCapacity ? ` × ${capabilities.skuCapacity}` : ''} ·{' '}
          {capabilities.location ?? 'unknown region'}
        </Text>
        {capabilities.gatewayUrl && <Text block>Gateway URL: {capabilities.gatewayUrl}</Text>}
        <Text block size={200}>
          Resource group {gateway.resourceGroup} in subscription {gateway.subscriptionId}.
        </Text>
        <Text block size={200}>
          AI gateway policies: {capabilities.aiGatewayPolicies === 'available'
            ? 'observed in use on this gateway'
            : 'not observed yet'}
          .
        </Text>
        {capabilities.notes.map((note) => (
          <Text key={note} block size={200}>
            {note}
          </Text>
        ))}
      </Card>
      <AccessPanel gateway={gateway} />
    </>
  )
}

function ApisTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const apis = useQuery({
    queryKey: ['gateway-apis', gatewayId],
    queryFn: () => api.listGatewayApis(gatewayId),
  })
  const operations = useQuery({
    queryKey: ['gateway-operations', gatewayId],
    queryFn: () => api.listGatewayOperations(gatewayId),
  })

  if (apis.isPending) return <Loading label="Loading APIs" />
  if (apis.isError) return <ErrorState error={apis.error} />
  if (apis.data.length === 0) {
    return <EmptyState title="No APIs">Sync this gateway to load its APIs.</EmptyState>
  }

  return (
    <>
      {apis.data.map((item) => (
        <Card key={item.id} className={styles.apiCard}>
          <div className={styles.apiHeader}>
            <div>
              <Title3 as="h3">{item.displayName}</Title3>
              <Text size={200}>
                /{item.path} · {item.operationCount} endpoint
                {item.operationCount === 1 ? '' : 's'}
                {item.productNames.length > 0 && ` · in ${item.productNames.join(', ')}`}
              </Text>
            </div>
            <AiBadge kind={item.aiKind} />
          </div>
          {item.aiSignals.map((signal) => (
            <Text key={signal} block size={200}>
              {signal}
            </Text>
          ))}
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Method</th>
                  <th>Endpoint</th>
                  <th>Name</th>
                </tr>
              </thead>
              <tbody>
                {(operations.data ?? [])
                  .filter((operation) => operation.apiName === item.name)
                  .map((operation) => (
                    <tr key={operation.id}>
                      <td>{operation.method}</td>
                      <td>{operation.urlTemplate}</td>
                      <td>{operation.displayName}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
    </>
  )
}

function McpServersTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const servers = useQuery({
    queryKey: ['gateway-mcp-servers', gatewayId],
    queryFn: () => api.listGatewayMcpServers(gatewayId),
  })

  if (servers.isPending) return <Loading label="Loading MCP servers" />
  if (servers.isError) return <ErrorState error={servers.error} />
  if (servers.data.length === 0) {
    return (
      <EmptyState title="No MCP servers">
        MOSAIC observed no MCP servers here. They also require an API Management service on a
        management API version that supports them.
      </EmptyState>
    )
  }

  return (
    <>
      {servers.data.map((server) => (
        <Card key={server.id} className={styles.apiCard}>
          <div className={styles.apiHeader}>
            <div>
              <Title3 as="h3">{server.displayName}</Title3>
              <Text size={200}>
                /{server.path} · {server.toolCount} tool{server.toolCount === 1 ? '' : 's'}
                {server.productNames.length > 0 && ` · in ${server.productNames.join(', ')}`}
              </Text>
            </div>
            <Badge appearance="tint" className={styles.aiBadge}>
              {server.kind === 'passthrough'
                ? `Passthrough · ${server.transportType === 'sse' ? 'SSE' : 'Streamable HTTP'}`
                : 'Backed by REST APIs'}
            </Badge>
          </div>
          {server.endpoints.length > 0 && (
            <Text block size={200}>
              Endpoints:{' '}
              {server.endpoints.map((endpoint) => `${endpoint.name} ${endpoint.uriTemplate}`).join(', ')}
            </Text>
          )}
          {server.tools.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Description</th>
                    <th>Backing operation</th>
                  </tr>
                </thead>
                <tbody>
                  {server.tools.map((tool) => (
                    <tr key={tool.name}>
                      <td>{tool.displayName}</td>
                      <td>{tool.description ?? '—'}</td>
                      <td>
                        {tool.backingApiName
                          ? `${tool.backingApiName}/${tool.backingOperationName ?? ''}`
                          : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ))}
    </>
  )
}

function ProductsTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const products = useQuery({
    queryKey: ['gateway-products', gatewayId],
    queryFn: () => api.listGatewayProducts(gatewayId),
  })

  if (products.isPending) return <Loading label="Loading products" />
  if (products.isError) return <ErrorState error={products.error} />
  if (products.data.length === 0) {
    return <EmptyState title="No products">This gateway groups no APIs into products.</EmptyState>
  }

  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Product</th>
            <th>State</th>
            <th>Access</th>
            <th>APIs</th>
          </tr>
        </thead>
        <tbody>
          {products.data.map((product) => (
            <tr key={product.id}>
              <td>
                {product.displayName}
                {product.description && (
                  <Text block size={200}>
                    {product.description}
                  </Text>
                )}
              </td>
              <td>{product.state ?? 'unknown'}</td>
              <td>
                {product.subscriptionRequired ? 'Subscription required' : 'Open'}
                {product.approvalRequired ? ', approval required' : ''}
              </td>
              <td>{product.apiNames.join(', ') || 'None'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SubscriptionsTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const subscriptions = useQuery({
    queryKey: ['gateway-subscriptions', gatewayId],
    queryFn: () => api.listGatewaySubscriptions(gatewayId),
  })

  if (subscriptions.isPending) return <Loading label="Loading subscriptions" />
  if (subscriptions.isError) return <ErrorState error={subscriptions.error} />
  if (subscriptions.data.length === 0) {
    return (
      <EmptyState title="No subscriptions">
        Nothing currently holds a key for this gateway.
      </EmptyState>
    )
  }

  return (
    <>
      <Text block>
        Subscriptions are how callers reach this gateway today. MOSAIC never reads subscription
        keys.
      </Text>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Subscription</th>
              <th>Applies to</th>
              <th>State</th>
              <th>Owner</th>
            </tr>
          </thead>
          <tbody>
            {subscriptions.data.map((subscription) => (
              <tr key={subscription.id}>
                <td>{subscription.displayName ?? subscription.name}</td>
                <td>
                  {subscription.scopeKind === 'allApis'
                    ? 'All APIs'
                    : `${subscription.scopeKind}: ${subscription.scopeName ?? 'unknown'}`}
                </td>
                <td>{subscription.state ?? 'unknown'}</td>
                <td>{subscription.ownerLabel ?? 'Unassigned'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function IdentitiesTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const users = useQuery({
    queryKey: ['gateway-users', gatewayId],
    queryFn: () => api.listGatewayUsers(gatewayId),
  })
  const groups = useQuery({
    queryKey: ['gateway-groups', gatewayId],
    queryFn: () => api.listGatewayGroups(gatewayId),
  })

  if (users.isPending || groups.isPending) return <Loading label="Loading identities" />
  if (users.isError) return <ErrorState error={users.error} />
  if (groups.isError) return <ErrorState error={groups.error} />

  return (
    <>
      <Text block>
        These are gateway-local identities from API Management, not MOSAIC principals. MOSAIC
        governs access through Entra principals and subscriptions.
      </Text>
      <Title3 as="h3">Users</Title3>
      {users.data.length === 0 ? (
        <EmptyState title="No gateway users">
          This gateway has no developer-portal users.
        </EmptyState>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>User</th>
                <th>Email</th>
                <th>Entra object ID</th>
                <th>Groups</th>
              </tr>
            </thead>
            <tbody>
              {users.data.map((user) => (
                <tr key={user.id}>
                  <td>{user.displayName ?? user.name}</td>
                  <td>{user.email ?? '—'}</td>
                  <td>{user.entraObjectId ?? 'Not linked to Entra'}</td>
                  <td>{user.groupNames.join(', ') || 'None'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Title3 as="h3">Groups</Title3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Group</th>
              <th>Type</th>
            </tr>
          </thead>
          <tbody>
            {groups.data.map((group) => (
              <tr key={group.id}>
                <td>{group.displayName}</td>
                <td>{group.builtIn ? 'Built in' : (group.groupType ?? 'custom')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function PoliciesTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const policies = useQuery({
    queryKey: ['gateway-policies', gatewayId],
    queryFn: () => api.getGatewayPolicies(gatewayId),
  })

  if (policies.isPending) return <Loading label="Loading policies" />
  if (policies.isError) return <ErrorState error={policies.error} />

  const { documents, fragments, recognizedCount, unrecognizedCount } = policies.data
  if (documents.length === 0 && fragments.length === 0) {
    return (
      <EmptyState title="No policies">
        Sync this gateway to read the rules that govern it.
      </EmptyState>
    )
  }

  return (
    <>
      <Text block>
        MOSAIC describes what each rule does. It never shows or stores the underlying policy
        markup, which can contain credentials.
      </Text>
      <Text block size={200}>
        {recognizedCount} rule{recognizedCount === 1 ? '' : 's'} understood,{' '}
        {unrecognizedCount} authored outside MOSAIC.
      </Text>
      {documents.map((document) => (
        <PolicyDocumentCard key={document.id} document={document} />
      ))}
      {fragments.length > 0 && <Title3 as="h3">Shared rule sets</Title3>}
      {fragments.map((fragment) => (
        <PolicyFragmentCard key={fragment.id} fragment={fragment} />
      ))}
    </>
  )
}

function BackendsTab({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const backends = useQuery({
    queryKey: ['gateway-backends', gatewayId],
    queryFn: () => api.listGatewayBackends(gatewayId),
  })
  const namedValues = useQuery({
    queryKey: ['gateway-named-values', gatewayId],
    queryFn: () => api.listGatewayNamedValues(gatewayId),
  })

  if (backends.isPending || namedValues.isPending) return <Loading label="Loading backends" />
  if (backends.isError) return <ErrorState error={backends.error} />
  if (namedValues.isError) return <ErrorState error={namedValues.error} />

  return (
    <>
      <Title3 as="h3">Backends</Title3>
      {backends.data.length === 0 ? (
        <EmptyState title="No backends">
          This gateway routes directly to service URLs rather than named backends.
        </EmptyState>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Backend</th>
                <th>URL</th>
                <th>Kind</th>
              </tr>
            </thead>
            <tbody>
              {backends.data.map((backend) => (
                <tr key={backend.id}>
                  <td>{backend.title ?? backend.name}</td>
                  <td>{backend.url ?? '—'}</td>
                  <td>
                    <AiBadge kind={backend.aiKind} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Title3 as="h3">Named values</Title3>
      <Text block size={200}>
        MOSAIC records only the name and whether a value is a secret. Secret values are never read.
      </Text>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Secret</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {namedValues.data.map((value) => (
              <tr key={value.id}>
                <td>{value.displayName}</td>
                <td>{value.secret ? 'Yes' : 'No'}</td>
                <td>{value.keyVaultSecretIdentifier ? 'Key Vault' : 'Inline'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export function GatewayDetailPage() {
  const { gatewayId = '' } = useParams()
  const api = useMosaicApi()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<TabKey>('overview')

  const gateway = useQuery({
    queryKey: ['gateway', gatewayId],
    queryFn: () => api.getGateway(gatewayId),
  })

  const sync = useMutation({
    mutationFn: () => api.syncGateway(gatewayId),
    onSuccess: () => queryClient.invalidateQueries(),
  })

  if (gateway.isPending) return <Loading label="Loading gateway" />
  if (gateway.isError) return <ErrorState error={gateway.error} />

  return (
    <div className={styles.page}>
      <Link className={styles.backLink} to="/gateways">
        ← Back to gateways
      </Link>
      <PageHeader
        title={gateway.data.name}
        description={`${gateway.data.serviceName} · observe only${
          gateway.data.environmentLabel ? ` · ${gateway.data.environmentLabel}` : ''
        }`}
        source="live"
        actions={
          <div className={styles.headerActions}>
            <GatewayStatusBadge status={gateway.data.status} />
            <Menu>
              <MenuTrigger disableButtonEnhancement>
                <MenuButton appearance="secondary" disabled={!gateway.data.lastSyncedAt}>
                  Import
                </MenuButton>
              </MenuTrigger>
              <MenuPopover>
                <MenuList>
                  {/* Both entries hand off to the section that owns the resource, so the import
                      workflow is the same wherever an administrator starts it. */}
                  <MenuItem onClick={() => navigate(`/models?import=${gatewayId}`)}>
                    APIs
                  </MenuItem>
                  <MenuItem onClick={() => navigate(`/mcps?import=${gatewayId}`)}>
                    MCP servers
                  </MenuItem>
                </MenuList>
              </MenuPopover>
            </Menu>
            <Button
              appearance="primary"
              onClick={() => sync.mutate()}
              disabled={sync.isPending || !gateway.data.access.canRead}
            >
              {sync.isPending ? 'Syncing…' : 'Sync now'}
            </Button>
          </div>
        }
      />
      {gateway.data.lastSyncError && (
        <Text block size={200} className={styles.syncError}>
          Last sync reported: {gateway.data.lastSyncError}
        </Text>
      )}

      <TabList
        className={styles.tabs}
        selectedValue={tab}
        onTabSelect={(_, data) => setTab(data.value as TabKey)}
      >
        <Tab value="overview">Overview</Tab>
        <Tab value="apis">APIs and endpoints</Tab>
        <Tab value="mcpServers">MCP servers</Tab>
        <Tab value="products">Products</Tab>
        <Tab value="subscriptions">Subscriptions</Tab>
        <Tab value="identities">Users and groups</Tab>
        <Tab value="policies">Policies</Tab>
        <Tab value="backends">Backends</Tab>
      </TabList>

      <div className={styles.tabPanel}>
        {tab === 'overview' && (
          <>
            <Overview gateway={gateway.data} />
            <PublishedModelsSection gatewayId={gatewayId} />
          </>
        )}
        {tab === 'apis' && <ApisTab gatewayId={gatewayId} />}
        {tab === 'mcpServers' && <McpServersTab gatewayId={gatewayId} />}
        {tab === 'products' && <ProductsTab gatewayId={gatewayId} />}
        {tab === 'subscriptions' && <SubscriptionsTab gatewayId={gatewayId} />}
        {tab === 'identities' && <IdentitiesTab gatewayId={gatewayId} />}
        {tab === 'policies' && <PoliciesTab gatewayId={gatewayId} />}
        {tab === 'backends' && <BackendsTab gatewayId={gatewayId} />}
      </div>
    </div>
  )
}
