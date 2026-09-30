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
import { environmentLabel, lookupCompatibility, useEnvironmentCatalog } from '../environments'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ChangeEnvironmentDialog } from '../components/ChangeEnvironmentDialog'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { EnvironmentPicker } from '../components/EnvironmentPicker'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { DeclarationFields, DeclaredDeploymentsCard } from '../components/KeyEndpoint'
import {
  blankDeclaration,
  keyedProvider,
  toDeclarations,
  usesBackendKey,
  type DeclarationDraft,
} from '../key-endpoint'
import { PublishModelDialog } from '../components/PublishModelDialog'
import { PageHeader } from '../components/PageHeader'
import { RemovalDialog } from '../components/RemovalDialog'
import { UnpublishDialog } from '../components/UnpublishDialog'
import { AI_KIND_LABELS } from '../labels'
import {
  PUBLICATION_STATUS_LABELS,
  formatTimestamp,
  holdsApi,
  isUnpublished,
  lastAppliedLabel,
  publicationStatusLabel,
} from '../publication-state'
import { CAN_INVOKE, describeScope, environmentRuntimeVerdict, findingSummary, runtimeVerdict } from '../runtime-access'
import type {
  CatalogVisibility,
  Gateway,
  GatewayRuntimeAccess,
  EnvironmentCompatibilityCell,
  ModelEndpoint,
  ModelEndpointCapabilities,
  ModelEndpointSuggestion,
  ModelEndpointStatus,
  ModelProvider,
  Publication,
  PublishPlan,
  PublicationStatus,
  SubscriptionScanStatus,
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

// A scan with nothing to read is explained. One that was never configured has nothing to say.
const scanVisibilityTitles: Partial<Record<SubscriptionScanStatus, string>> = {
  noVisibleSubscriptions: "MOSAIC can't see any subscriptions",
  listFailed: "MOSAIC couldn't list subscriptions",
}

const REGISTRATION_REFUSED = "MOSAIC didn't register this endpoint"

// Registering by resource ID is the default. The key path is the explicit alternative for a
// resource MOSAIC's managed identity can't reach, such as one in another Microsoft Entra tenant.
type RegistrationMode = 'azure' | 'key' | 'compatible'


function PublicationStatusBadge({ publication }: { publication: Publication }) {
  const { status } = publication
  const attention = status === 'failed' || status === 'rolledBack'
  const active = status === 'applying' || status === 'planned'
  const unpublished = status === 'draft' && isUnpublished(publication)
  return (
    <Badge
      appearance="tint"
      className={
        attention
          ? styles.statusAttention
          : active
            ? styles.statusSyncing
            : unpublished
              ? styles.statusStopped
              : styles.statusReady
      }
    >
      {publicationStatusLabel(publication)}
    </Badge>
  )
}

function PublishedModels({ onMessage }: { onMessage: (message: string) => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const [removing, setRemoving] = useState<Publication | null>(null)
  const [unpublishing, setUnpublishing] = useState<Publication | null>(null)
  const [review, setReview] = useState<{ publication: Publication; plan: PublishPlan } | null>(null)

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
    await queryClient.invalidateQueries({ queryKey: ['entitlements'] })
    await queryClient.invalidateQueries({ queryKey: ['model-apis'] })
    await queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] })
  }

  // The table never applies a plan. Re-plan opens a fresh plan in the publish dialog, and only its
  // Apply plan applies it, so the administrator sees every plan before it runs. Unpublish works
  // the same way: its dialog plans the unpublish, and only its confirm button runs that plan.
  const reviewPlan = useMutation({
    mutationFn: async (publication: Publication) => ({
      publication,
      plan: await api.createPublishPlan(publication.id),
    }),
    onSuccess: ({ publication, plan }) => {
      setReview({ publication, plan })
      void queryClient.invalidateQueries({ queryKey: ['publications'] })
    },
  })
  const remove = useMutation({
    mutationFn: (publicationId: string) => api.deletePublication(publicationId),
    onSuccess: async () => {
      setRemoving(null)
      await refresh()
      onMessage('Removed the publication record. Nothing changed in API Management.')
    },
    onError: async () => {
      await refresh()
    },
  })

  function confirmRemoval(publication: Publication) {
    remove.reset()
    setRemoving(publication)
  }

  return (
    <>
    <Card className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <Title3 as="h2">Published models</Title3>
          <Text>Model deployments MOSAIC has planned or published into API Management.</Text>
        </div>
      </div>
      {reviewPlan.isError && <ErrorState error={reviewPlan.error} />}
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
                    <TableCell>
                      <PublicationStatusBadge publication={publication} />
                      {publication.accessState === 'unknown' && (
                        <Text block size={200}>Runtime unknown — retained apply lock. Use the recovery instructions in Entitlements before retrying.</Text>
                      )}
                    </TableCell>
                    <TableCell>
                      <Link to={`/gateways/${publication.gatewayId}`}>
                        {gatewaysById.get(publication.gatewayId)?.name ?? publication.gatewayId}
                      </Link>
                    </TableCell>
                    <TableCell>/{publication.apiPath}</TableCell>
                    <TableCell>{lastAppliedLabel(publication)}</TableCell>
                    <TableCell>
                      <div className={styles.actionRow}>
                        <Button
                          appearance="secondary"
                          disabled={reviewPlan.isPending || publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown'}
                          onClick={() => reviewPlan.mutate(publication)}
                        >
                          {reviewPlan.isPending && reviewPlan.variables?.id === publication.id ? 'Planning…' : 'Re-plan'}
                        </Button>
                        <Button
                          appearance="secondary"
                          disabled={publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown'}
                          onClick={() => setUnpublishing(publication)}
                          {...restoreFocus}
                        >
                          Unpublish
                        </Button>
                        <Button appearance="subtle" disabled={remove.isPending} onClick={() => confirmRemoval(publication)} {...restoreFocus}>Remove</Button>
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
    <UnpublishDialog
      open={unpublishing !== null}
      target="model"
      publication={unpublishing}
      onClose={() => setUnpublishing(null)}
      onUnpublished={onMessage}
    />
    <RemovalDialog
      open={removing !== null}
      title={`Remove ${removing?.displayName ?? 'this publication'}?`}
      confirmLabel="Remove publication"
      refusalTitle="MOSAIC didn't remove this publication"
      pending={remove.isPending}
      error={remove.error}
      onConfirm={() => removing && remove.mutate(removing.id)}
      onCancel={() => setRemoving(null)}
    >
      <Text block>
        MOSAIC deletes its record of this publication. Nothing changes in API Management.
      </Text>
      <Text block>
        MOSAIC refuses while the publication still owns resources there. Unpublish it first so
        MOSAIC can remove them.
      </Text>
    </RemovalDialog>
    </>
  )
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

/** What an administrator can do about a subscription scan that cannot see everything. */
function ScanReaderExplanation() {
  return (
    <Text>
      Endpoints can still be registered by pasting a resource ID, and granting{' '}
      <strong>Reader</strong> at subscription scope lets MOSAIC suggest them.
    </Text>
  )
}

/** A role granted to MOSAIC is rarely in effect by the time the page is reloaded. */
function ScanRoleDelayNote() {
  return (
    <Text size={200} className={styles.muted}>
      Azure can take several minutes, and occasionally longer, to apply a new role. If the scan
      still reports this right after the grant, wait a few minutes and refresh this page.
    </Text>
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
  // A model API MOSAIC publishes is listed in the portal only while its publication holds its API.
  const publications = useQuery({
    queryKey: ['publications'],
    queryFn: () => api.listPublications(),
  })
  const catalog = useEnvironmentCatalog()

  const gatewaysById = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) {
      map.set(gateway.id, gateway)
    }
    return map
  }, [gateways.data])

  function hiddenFromPortal(record: { publicationId?: string | null }): boolean {
    if (!record.publicationId || !publications.isSuccess) return false
    const publication = publications.data.find((item) => item.id === record.publicationId)
    return !publication || !holdsApi(publication)
  }

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
                  <TableHeaderCell>Environment</TableHeaderCell>
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
                      {(() => {
                        const gateway = gatewaysById.get(record.gatewayId)
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
                      {hiddenFromPortal(record) && (
                        <Text block size={200} className={styles.muted}>
                          Hidden from the portal catalog while it isn&apos;t published.
                        </Text>
                      )}
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
  const api = useMosaicApi()
  const { access, runtimeAccess } = endpoint
  const keyed = usesBackendKey(endpoint)
  const catalog = useEnvironmentCatalog()
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })
  const gatewaysById = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) map.set(gateway.id, gateway)
    return map
  }, [gateways.data])
  // A key check MOSAIC couldn't finish is not a denial, so it isn't shown as one.
  const accessIntent = access.canRead
    ? 'success'
    : keyed && access.evaluation === 'notEvaluated'
      ? 'warning'
      : 'error'

  return (
    <Card className={styles.accessCard}>
      <Title3 as="h2">Access</Title3>

      <section className={styles.accessSection}>
        <MessageBar intent={accessIntent}>
          <MessageBarBody>
            <MessageBarTitle>
              {keyed
                ? access.canRead
                  ? 'MOSAIC read the key and the endpoint accepted it'
                  : "MOSAIC can't confirm the key"
                : access.canRead
                  ? 'MOSAIC can read this endpoint'
                  : 'MOSAIC cannot read this endpoint'}
            </MessageBarTitle>
            {access.message}
          </MessageBarBody>
        </MessageBar>
        <Text size={200} className={styles.muted}>
          {keyed
            ? 'Authentication: API key from Key Vault. MOSAIC reads the key only to check it, with a ' +
              'request that runs no model, and never keeps it. API Management reads the key from ' +
              'Key Vault itself.'
            : 'This is what lets MOSAIC list the models deployed here. It grants no ability to call them.'}
        </Text>
        {access.remediation && (
          <>
            <Text block>
              Grant the <strong>{access.remediation.roleName}</strong> role to MOSAIC
              {keyed ? ' on the vault' : ' at this scope'}. Someone with permission to assign roles
              must run:
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

      {endpoint.azureResourceId && <EndpointSettings capabilities={endpoint.capabilities} />}

      <section className={styles.accessSection}>
        <Text weight="semibold">Gateways calling this endpoint</Text>
        <Text size={200} className={styles.muted}>
          {keyed
            ? "At runtime the gateway sends the endpoint's API key, which it reads from Key Vault " +
              'with its own managed identity, so it needs a role on the vault that reads secrets. ' +
              'MOSAIC reports this and never assigns it.'
            : 'At runtime the gateway authenticates as itself, not as MOSAIC, so it needs its own ' +
              'role on this endpoint. MOSAIC reports this and never assigns it.'}
        </Text>
        {runtimeAccess.length === 0 ? (
          <Text size={200}>No gateways are registered yet.</Text>
        ) : (
          runtimeAccess.map((entry) => (
            <RuntimeAccessRow
              key={entry.gatewayId}
              access={entry}
              keyAccess={keyed}
              registeredScope={endpoint.azureResourceId}
              environmentCell={lookupCompatibility(catalog.data, gatewaysById.get(entry.gatewayId)?.environment, endpoint.environment)}
            />
          ))
        )}
      </section>
    </Card>
  )
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/**
 * What MOSAIC read about the resource itself. Network settings decide whether a gateway can reach
 * the endpoint at all, whatever roles it holds, so they sit beside the gateway verdicts.
 */
function EndpointSettings({ capabilities }: { capabilities: ModelEndpointCapabilities }) {
  const facts: Array<[string, string]> = [
    ['Resource kind', capabilities.kind ?? 'Not known yet. MOSAIC cannot read this resource.'],
  ]
  if (capabilities.publicNetworkAccess) {
    facts.push(['Public network access', capabilities.publicNetworkAccess])
  }
  if (capabilities.networkDefaultAction) {
    const addressRules = plural(capabilities.networkIpRules?.length ?? 0, 'address rule')
    const networkRules = plural(
      capabilities.networkVirtualNetworkRuleCount ?? 0,
      'virtual network rule',
    )
    facts.push([
      'Firewall',
      capabilities.networkDefaultAction.toLowerCase() === 'deny'
        ? `Admits only listed networks (${addressRules}, ${networkRules})`
        : 'Admits all networks',
    ])
  }
  if (capabilities.localAuthDisabled != null) {
    facts.push(['Key authentication', capabilities.localAuthDisabled ? 'Disabled' : 'Enabled'])
  }

  return (
    <section className={styles.accessSection} aria-label="Endpoint settings">
      <Text weight="semibold">Endpoint settings</Text>
      <Text size={200} className={styles.muted}>
        Read from the Azure resource. Network settings decide whether a gateway can reach it at
        all, whatever roles it holds.
      </Text>
      <dl className={styles.settingsList}>
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {capabilities.notes.map((note) => (
        <Text key={note} size={200}>
          {note}
        </Text>
      ))}
    </section>
  )
}

function ScopeName({ scope }: { scope: string }) {
  return <span title={scope}>{describeScope(scope)}</span>
}

function sameScope(left?: string | null, right?: string | null): boolean {
  const normalize = (scope?: string | null) => (scope ?? '').replace(/\/+$/, '').toLowerCase()
  return normalize(left) === normalize(right)
}

function RuntimeAccessRow({
  access,
  keyAccess = false,
  registeredScope,
  environmentCell,
}: {
  access: GatewayRuntimeAccess
  /** The row says whether the gateway can read a key from Key Vault, not call with its identity. */
  keyAccess?: boolean
  registeredScope?: string | null
  environmentCell?: EnvironmentCompatibilityCell
}) {
  const verdict = runtimeVerdict(access)
  const verdictLabel = keyAccess
    ? verdict === CAN_INVOKE
      ? 'can read the key'
      : verdict.intent === 'error'
        ? "can't read the key"
        : verdict.label
    : verdict.label
  const environmentVerdict = environmentRuntimeVerdict(environmentCell)
  const grantedRole = access.grantedRoleName ?? access.grantedRoleDefinitionId
  // Findings explain why nothing satisfied the check, so they are noise once something has.
  const findings = grantedRole ? [] : (access.roleFindings ?? [])
  const requiredDataActions = access.requiredDataActions ?? []
  // An access-policy vault is fixed with a policy, which is not a role assignment.
  const accessPolicy = keyAccess && access.remediation?.roleDefinitionId === ''

  return (
    <div className={styles.runtimeRow}>
      <MessageBar intent={verdict.intent}>
        <MessageBarBody>
          <MessageBarTitle>
            {access.gatewayName}: {verdictLabel}
          </MessageBarTitle>
          {access.message}
        </MessageBarBody>
      </MessageBar>
      <MessageBar intent={environmentVerdict.intent}>
        <MessageBarBody>
          <MessageBarTitle>
            Environment rules: {environmentVerdict.label}
          </MessageBarTitle>
          {environmentCell?.reason ?? 'MOSAIC has not evaluated the gateway and endpoint environments yet.'}
        </MessageBarBody>
      </MessageBar>
      {grantedRole && access.assignmentScope ? (
        <Text size={200}>
          {verdict === CAN_INVOKE ? 'Satisfied by' : 'The role requirement is met by'}{' '}
          <strong>{grantedRole}</strong>,{' '}
          {access.inherited ? (
            <>
              inherited from <ScopeName scope={access.assignmentScope} />. It works, but it is
              broader than an assignment made directly on the resource.
            </>
          ) : (
            <>
              assigned directly on <ScopeName scope={access.assignmentScope} />.
            </>
          )}
        </Text>
      ) : (
        access.inherited &&
        access.assignmentScope && (
          <Text size={200} className={styles.muted}>
            Inherited from {access.assignmentScope}. It works, but it is broader than an assignment
            made directly on this endpoint.
          </Text>
        )
      )}
      {registeredScope &&
        access.evaluatedScope &&
        !sameScope(access.evaluatedScope, registeredScope) && (
          <Text size={200} className={styles.muted}>
            Checked at <ScopeName scope={access.evaluatedScope} />, the resource the published API
            calls. A Foundry project&apos;s models are deployed on its parent resource.
          </Text>
        )}
      {findings.length > 0 && (
        <div className={styles.findings}>
          <Text size={200} weight="semibold">
            Role assignments MOSAIC found
          </Text>
          <ul className={styles.findingList}>
            {findings.map((finding, index) => (
              <li key={`${finding.roleDefinitionId ?? 'role'}-${finding.scope}-${index}`}>
                <Text size={200}>{findingSummary(finding)}</Text>
              </li>
            ))}
          </ul>
        </div>
      )}
      {access.remediation && (
        <>
          <Text size={200}>
            {accessPolicy ? (
              <>
                Recommended: add <strong>{access.remediation.roleName}</strong> for the gateway
                on {access.remediation.scope}. Someone with permission to change the vault&apos;s
                access policies must run:
              </>
            ) : (
              <>
                Recommended: grant <strong>{access.remediation.roleName}</strong> on{' '}
                <ScopeName scope={access.remediation.scope} />.{' '}
                {keyAccess
                  ? 'Any role that can read secrets is also accepted.'
                  : 'Any role that grants the data actions the published API needs is also accepted.'}{' '}
                Someone with permission to assign roles must run:
              </>
            )}
          </Text>
          <CommandBlock command={access.remediation.command} />
          {requiredDataActions.length > 0 && !accessPolicy && (
            <details className={styles.dataActions}>
              <summary>
                {keyAccess ? 'Data actions reading the key needs' : 'Data actions the published API needs'}
              </summary>
              <ul className={styles.findingList}>
                {requiredDataActions.map((action) => (
                  <li key={action}>
                    <code>{action}</code>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  )
}

function ModelEndpoints({ onMessage }: { onMessage: (message: string) => void }) {
  const api = useMosaicApi()
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const hasConsumedRegisterQueryRef = useRef(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  // Where the last registration came from, so a refusal is shown next to the button that asked.
  const [registerOrigin, setRegisterOrigin] = useState<'dialog' | 'suggestion'>('dialog')
  const [removing, setRemoving] = useState<ModelEndpoint | null>(null)
  const [mode, setMode] = useState<RegistrationMode>('azure')
  const [resourceId, setResourceId] = useState('')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [secretUri, setSecretUri] = useState('')
  const [declarations, setDeclarations] = useState<DeclarationDraft[]>(() => [
    blankDeclaration(null),
  ])
  const [name, setName] = useState('')
  const [environment, setEnvironment] = useState<string | null>(null)
  const [environmentTouched, setEnvironmentTouched] = useState(false)
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [changingEndpoint, setChangingEndpoint] = useState<ModelEndpoint | null>(null)
  const [suggestionEnvironment, setSuggestionEnvironment] = useState<
    { environment: string; evidence: string } | undefined
  >()

  const endpoints = useQuery({
    queryKey: ['model-endpoints'],
    queryFn: api.listModelEndpoints,
  })
  const catalog = useEnvironmentCatalog()
  const suggestions = useQuery({
    queryKey: ['model-endpoint-suggestions'],
    queryFn: api.listSuggestedModelEndpoints,
  })

  const selected =
    endpoints.data?.find((item) => item.id === selectedId) ?? endpoints.data?.[0] ?? null

  const deployments = useQuery({
    queryKey: ['model-deployments', selected?.id],
    queryFn: () => api.listModelDeployments(selected!.id),
    enabled: Boolean(selected) && !(selected && usesBackendKey(selected)),
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
      setDeclarations([blankDeclaration(null)])
      setName('')
      setEnvironment(null)
      setEnvironmentTouched(false)
      setSuggestionEnvironment(undefined)
      closeDialog()
      setSelectedId(endpoint.id)
      await refresh()
    },
    onError: async () => {
      // A refusal can mean the suggestions are out of date, such as an account now covered.
      await queryClient.invalidateQueries({ queryKey: ['model-endpoint-suggestions'] })
    },
  })

  const sync = useMutation({ mutationFn: api.syncModelEndpoint, onSuccess: refresh })
  // A refusal says why at the top of the dialog, and takes focus so a screen reader reads it.
  const registerErrorRef = useRef<HTMLDivElement>(null)
  const shownRegisterError = useRef<unknown>(null)
  useEffect(() => {
    const shown = shownRegisterError.current
    shownRegisterError.current = register.error
    if (!register.error || Object.is(register.error, shown) || registerOrigin !== 'dialog') return
    registerErrorRef.current?.focus()
  }, [register.error, registerOrigin])
  const recheck = useMutation({ mutationFn: api.preflightModelEndpoint, onSuccess: refresh })
  const remove = useMutation({
    mutationFn: (endpoint: ModelEndpoint) => api.deleteModelEndpoint(endpoint.id),
    onSuccess: async (_, endpoint) => {
      setRemoving(null)
      setSelectedId(null)
      onMessage(`Removed ${endpoint.name} from MOSAIC. Nothing changed in Azure.`)
      await refresh()
      await queryClient.invalidateQueries({ queryKey: ['publications'] })
    },
    onError: async () => {
      await queryClient.invalidateQueries({ queryKey: ['publications'] })
    },
  })

  function confirmRemoval(endpoint: ModelEndpoint) {
    remove.reset()
    setRemoving(endpoint)
  }

  function openDialog() {
    register.reset()
    setSuggestionEnvironment(undefined)
    setDialogOpen(true)
  }

  function registerSuggestion(item: ModelEndpointSuggestion) {
    setRegisterOrigin('suggestion')
    register.reset()
    setMode('azure')
    setResourceId(item.azureResourceId ?? '')
    setName(item.accountName ?? '')
    setEnvironment(item.suggestedEnvironment ?? null)
    setEnvironmentTouched(false)
    setSuggestionEnvironment(item.suggestedEnvironment && item.azureEnvironmentTag
      ? { environment: item.suggestedEnvironment, evidence: `Azure tag environment = "${item.azureEnvironmentTag}"` }
      : undefined)
    setDialogOpen(true)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (register.isPending) return
    setRegisterOrigin('dialog')
    setEnvironmentTouched(true)
    if (!environment) return
    if (mode === 'azure') {
      register.mutate({
        azureResourceId: resourceId.trim(),
        name: name.trim() || undefined,
        environment,
      })
      return
    }
    if (mode === 'key') {
      register.mutate({
        endpoint: endpointUrl.trim(),
        credentialSecretUri: secretUri.trim(),
        name: name.trim() || undefined,
        environment,
        deployments: toDeclarations(declarations, keyedProvider(endpointUrl)),
      })
      return
    }
    register.mutate({
      endpoint: endpointUrl.trim(),
      credentialSecretUri: secretUri.trim(),
      name: name.trim() || undefined,
      environment,
    })
  }

  const pending = (suggestions.data?.suggestions ?? []).filter(
    (item) => !item.alreadyRegistered,
  )
  const filteredEndpoints = (endpoints.data ?? []).filter((endpoint) => {
    if (environmentFilter === 'all') return true
    if (environmentFilter === '__unclassified__') return endpoint.environment == null
    return endpoint.environment === environmentFilter
  })
  const scanIssues = suggestions.data?.scanIssues ?? []
  const partialScans = suggestions.data?.partialScans ?? []
  const scanStatus = suggestions.data?.scanStatus
  const scanRemediation = suggestions.data?.scanRemediation ?? []
  const scanVisibilityTitle = scanStatus ? scanVisibilityTitles[scanStatus] : undefined
  const subscriptionsScanned = suggestions.data?.subscriptionsScanned ?? 0
  // A count of zero would only restate the subscriptions listed as unscannable below.
  const scanSummary =
    scanStatus === 'scanned' && subscriptionsScanned > 0
      ? `Scanned ${subscriptionsScanned} subscription${subscriptionsScanned === 1 ? '' : 's'}.`
      : null
  // Azure leaves out what MOSAIC cannot read and still answers, so a partly readable
  // subscription must never be summarised as having nothing new in it.
  const partlyReadIn =
    subscriptionsScanned <= 1
      ? 'it'
      : partialScans.length >= subscriptionsScanned
        ? 'each of them'
        : `${partialScans.length} of them`
  const scanOutcome =
    partialScans.length > 0
      ? `MOSAIC can read only some resources in ${partlyReadIn}, so any Azure AI resources it ` +
        "can't read are missing from this list."
      : pending.length > 0
        ? null
        : 'Nothing new to register.'

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
  useEffect(() => {
    if (dialogOpen && !environment && !suggestionEnvironment && catalog.data?.environments[0]) {
      setEnvironment(catalog.data.environments[0].key)
    }
  }, [dialogOpen, environment, suggestionEnvironment, catalog.data])

  function closeDialog() {
    setDialogOpen(false)
    setResourceId('')
    setEndpointUrl('')
    setSecretUri('')
    setDeclarations([blankDeclaration(null)])
    setName('')
    setEnvironment(null)
    setEnvironmentTouched(false)
    setSuggestionEnvironment(undefined)
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
            onClick={openDialog}
            {...restoreFocus}
          >
            Register endpoint
          </Button>
        </div>

        {endpoints.isPending && <Loading label="Loading model endpoints" />}
        {endpoints.isError && <ErrorState error={endpoints.error} />}
        {endpoints.data?.length === 0 && (
          <EmptyState title="No model endpoints yet">
            Register an Azure OpenAI or Azure AI Foundry resource to see the models deployed on it.
          </EmptyState>
        )}

        {endpoints.data && endpoints.data.length > 0 && (
          <>
            <div className={styles.filterBar}>
              <Field label="Environment filter">
                <Select
                  aria-label="Filter model endpoints by environment"
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
            <div className={styles.tableWrap}>
              <Table aria-label="Registered model endpoints">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Endpoint</TableHeaderCell>
                    <TableHeaderCell>Provider</TableHeaderCell>
                    <TableHeaderCell>Environment</TableHeaderCell>
                    <TableHeaderCell>Status</TableHeaderCell>
                    <TableHeaderCell>Models</TableHeaderCell>
                    <TableHeaderCell>Last synced</TableHeaderCell>
                    <TableHeaderCell>Actions</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredEndpoints.map((endpoint) => (
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
                      <TableCell>
                        <div className={styles.cellStack}>
                          <span>{providerLabels[endpoint.provider]}</span>
                          {usesBackendKey(endpoint) && (
                            <span className={styles.secondaryCell}>API key from Key Vault</span>
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
                        <EndpointStatusBadge status={endpoint.status} />
                      </TableCell>
                      <TableCell>
                        {usesBackendKey(endpoint)
                          ? `${endpoint.declaredDeployments?.length ?? 0} declared`
                          : endpoint.inventory.deployments}
                      </TableCell>
                      <TableCell>
                        {usesBackendKey(endpoint) ? '—' : formatTimestamp(endpoint.lastSyncedAt)}
                      </TableCell>
                      <TableCell>
                        <div className={styles.actionRow}>
                          <Button
                            appearance="secondary"
                            onClick={() => recheck.mutate(endpoint.id)}
                            disabled={recheck.isPending}
                          >
                            Check access
                          </Button>
                          {!usesBackendKey(endpoint) && (
                            <Button
                              appearance="secondary"
                              onClick={() => sync.mutate(endpoint.id)}
                              disabled={sync.isPending || !endpoint.access.canRead}
                            >
                              Sync models
                            </Button>
                          )}
                          <Button
                            appearance="subtle"
                            onClick={() => setChangingEndpoint(endpoint)}
                          >
                            Change environment
                          </Button>
                          <Button
                            appearance="subtle"
                            onClick={() => confirmRemoval(endpoint)}
                            disabled={remove.isPending}
                            {...restoreFocus}
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
              its deployments are never modified. MOSAIC refuses while a model from it is still
              published.
            </Text>
          </>
        )}
      </Card>

      {(pending.length > 0 || scanSummary) && (
        <Card className={styles.panel}>
          <Title3 as="h2">Endpoints MOSAIC found</Title3>
          {scanSummary && (
            <Text size={200} className={styles.muted}>
              {scanOutcome ? `${scanSummary} ${scanOutcome}` : scanSummary}
            </Text>
          )}
          {registerOrigin === 'suggestion' && register.isError && (
            <ErrorState title={REGISTRATION_REFUSED} error={register.error} />
          )}
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
                  onClick={() => registerSuggestion(item)}
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

      {partialScans.length > 0 && (
        <Card className={styles.panel}>
          <Title3 as="h2">
            {partialScans.length === 1
              ? 'MOSAIC can read only part of a subscription'
              : `MOSAIC can read only part of ${partialScans.length} subscriptions`}
          </Title3>
          <ScanReaderExplanation />
          {partialScans.map((scan) => (
            <div key={scan.subscriptionId} className={styles.scanIssue}>
              <Text size={200} weight="semibold">
                {scan.displayName ?? scan.subscriptionId}
              </Text>
              {scan.remediation && <CommandBlock command={scan.remediation.command} />}
            </div>
          ))}
          {partialScans.some((scan) => scan.remediation) && <ScanRoleDelayNote />}
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
          {scanIssues.some((issue) => issue.remediation) && <ScanRoleDelayNote />}
        </Card>
      )}

      {scanVisibilityTitle && (
        <Card className={styles.panel}>
          <Title3 as="h2">{scanVisibilityTitle}</Title3>
          {scanStatus === 'listFailed' && suggestions.data?.scanMessage && (
            <Text size={200} className={styles.muted}>
              {suggestions.data.scanMessage}
            </Text>
          )}
          <ScanReaderExplanation />
          {scanRemediation.map((remediation) => (
            <CommandBlock key={remediation.scope} command={remediation.command} />
          ))}
          {scanRemediation.length > 0 && <ScanRoleDelayNote />}
        </Card>
      )}

      {selected && <AccessPanel endpoint={selected} />}

      {selected && usesBackendKey(selected) && (
        <DeclaredDeploymentsCard endpoint={selected} className={styles.panel} />
      )}

      {selected && !usesBackendKey(selected) && (
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
                  onTabSelect={(_, data) => setMode(data.value as RegistrationMode)}
                >
                  <Tab value="azure">Azure AI</Tab>
                  <Tab value="key">Azure AI with an API key</Tab>
                  <Tab value="compatible">OpenAI compatible</Tab>
                </TabList>

                {mode === 'azure' && (
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
                )}
                {mode === 'key' && (
                  <>
                    <MessageBar intent="info">
                      <MessageBarBody>
                        <MessageBarTitle>Only when MOSAIC can&apos;t reach the resource</MessageBarTitle>
                        Register by resource ID whenever you can: MOSAIC then reads the deployments
                        with its managed identity, and no key is involved. Use an API key when that
                        isn&apos;t possible, for example when the resource is in another Microsoft
                        Entra tenant.
                      </MessageBarBody>
                    </MessageBar>
                    <Field
                      label="Endpoint URL"
                      required
                      hint="The resource endpoint, such as https://<resource>.services.ai.azure.com, or a Foundry project endpoint ending in /api/projects/<project>. MOSAIC publishes from the resource."
                    >
                      <Input
                        value={endpointUrl}
                        onChange={(_, data) => setEndpointUrl(data.value)}
                        placeholder="https://my-resource.services.ai.azure.com/api/projects/my-project"
                      />
                    </Field>
                    <Field
                      label="Key Vault secret URI"
                      required
                      hint="Store the resource's API key as a secret in Key Vault yourself, then paste the secret's URI. Never paste the key: MOSAIC keeps only this URI, and API Management reads the key from Key Vault. MOSAIC and each gateway need Key Vault Secrets User on the vault."
                    >
                      <Input
                        value={secretUri}
                        onChange={(_, data) => setSecretUri(data.value)}
                        placeholder="https://my-vault.vault.azure.net/secrets/foundry-key"
                      />
                    </Field>
                    <DeclarationFields
                      drafts={declarations}
                      provider={keyedProvider(endpointUrl)}
                      onChange={setDeclarations}
                    />
                  </>
                )}
                {mode === 'compatible' && (
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
                <EnvironmentPicker
                  catalog={catalog.data}
                  value={environment}
                  onChange={(value) => { setEnvironment(value); setEnvironmentTouched(true) }}
                  required
                  label="Environment"
                  suggestion={suggestionEnvironment}
                  validationMessage={environmentTouched && !environment ? 'Choose an environment.' : undefined}
                />

                {registerOrigin === 'dialog' && register.isError && (
                  <div ref={registerErrorRef} tabIndex={-1}>
                    <ErrorState title={REGISTRATION_REFUSED} error={register.error} />
                  </div>
                )}
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" onClick={closeDialog}>
                  Cancel
                </Button>
                <Button appearance="primary" type="submit" disabledFocusable={register.isPending}>
                  {register.isPending ? 'Registering…' : 'Register'}
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>

      <RemovalDialog
        open={removing !== null}
        title={`Remove ${removing?.name ?? 'this endpoint'}?`}
        confirmLabel="Remove endpoint"
        refusalTitle="MOSAIC didn't remove this endpoint"
        pending={remove.isPending}
        error={remove.error}
        statusLabel={(status) =>
          PUBLICATION_STATUS_LABELS[status as PublicationStatus] ?? status
        }
        onConfirm={() => removing && remove.mutate(removing)}
        onCancel={() => setRemoving(null)}
      >
        <Text block>
          MOSAIC deletes its record of this endpoint, its{' '}
          {removing ? plural(removing.inventory.deployments, 'synced model') : 'synced models'}{' '}
          and its sync history. Nothing changes in Azure: the resource and its deployments stay as
          they are.
        </Text>
        <Text block>
          Publication records from this endpoint that own nothing in API Management, such as
          drafts, are deleted with it. MOSAIC refuses while a model from it is still published.
        </Text>
      </RemovalDialog>
      {changingEndpoint && (
        <ChangeEnvironmentDialog
          resource={{
            resourceKind: 'modelEndpoint',
            resourceId: changingEndpoint.id,
            resourceName: changingEndpoint.name,
            environment: changingEndpoint.environment,
          }}
          open={changingEndpoint !== null}
          onClose={() => setChangingEndpoint(null)}
          onChanged={async () => {
            setChangingEndpoint(null)
            onMessage(`Updated ${changingEndpoint.name}'s environment.`)
            await refresh()
          }}
        />
      )}
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

      <ModelEndpoints onMessage={setLiveBanner} />

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
