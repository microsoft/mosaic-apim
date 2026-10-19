import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Link,
  MessageBar,
  MessageBarBody,
  Select,
  Switch,
  Text,
  Title3,
} from '@fluentui/react-components'
import { AddRegular, KeyRegular, PersonClockRegular, ShieldCheckmarkRegular, SettingsRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { grantReviewValues, rememberEdit } from '../change-review'
import {
  ApproveAccessRequestDialog,
  type ApprovalRequester,
} from '../components/ApproveAccessRequestDialog'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { RowActions } from '../components/RowActions'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { EntitlementAccessState } from '../components/EntitlementAccessState'
import { EntitlementConnectionDialog } from '../components/EntitlementConnectionDialog'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PrincipalKindBadge } from '../components/PrincipalKindBadge'
import { IdentityAvatar } from '../components/IdentityAvatar'
import { ModelAccessSettingsPanel } from '../components/ModelAccessSettingsPanel'
import { ApprovalPolicies } from '../components/ApprovalPolicies'
import { BulkApprovalWorkspace } from '../components/BulkApprovalWorkspace'
import { BulkRequestSelection } from '../components/BulkRequestSelection'
import { PublishModelDialog } from '../components/PublishModelDialog'
import { ResourceWorkbench } from '../components/ResourceWorkbench'
import { useWorkbenchLocation } from '../components/useWorkbenchLocation'
import { completeWorkspaceNavigation } from '../components/WorkspaceLeaveGuard'
import {
  WorkspaceDialog as Dialog,
  WorkspaceDialogActions as DialogActions,
  WorkspaceDialogBody as DialogBody,
  WorkspaceDialogContent as DialogContent,
  WorkspaceDialogSurface as DialogSurface,
} from '../components/WorkspaceDialog'
import {
  QUOTA_PERIODS,
  buildEnforcement,
  callRateError,
  describeLimits,
  describePoolSafeguard,
  emptyLimitForm,
  limitFormFrom,
  type LimitForm,
} from '../entitlement-limits'
import { entitlementResourceCloud, entitlementResourceEnvironment } from '../entitlement-environment'
import { environmentLabel, useEnvironmentCatalog } from '../environments'
import { holdsApi } from '../publication-state'
import { runtimeConfig } from '../runtime-config'
import {
  ENTITLEMENT_SUBJECT_KIND_LABELS,
  GRANT_OVERLAP_KIND_LABELS,
  PRINCIPAL_KIND_LABELS,
  subjectKindForPrincipal,
} from '../labels'
import type {
  AccessRequest,
  AccessRequestApproval,
  Entitlement,
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementResourceKind,
  EntitlementSubject,
  EntitlementSubjectKind,
  CostCenter,
  ModelPool,
  QuotaPeriod,
  Publication,
  PublishPlan,
  Principal,
  PrincipalKind,
  ResolvedEntitlement,
} from '../types'
import styles from './EntitlementsPage.module.css'

interface SubjectOption {
  id: string
  kind: EntitlementSubjectKind
  label: string
  group: 'People' | 'Agents' | 'Security groups' | 'Applications' | 'MOSAIC groups'
  principalKind?: PrincipalKind
}

interface ResourceOption {
  /** The option's form value: the resource's ID, or for a pool model its pool and model IDs. */
  value: string
  id: string
  kind: EntitlementResourceKind
  label: string
  /** A pool model's pool. */
  scopeId?: string
}

/** Pool model IDs are unique only within their pool, so a pool model's key names its pool too. */
function resourceKey(resource: Pick<EntitlementResource, 'kind' | 'id' | 'scopeId'>): string {
  return resource.kind === 'poolModel' ? `poolModel:${resource.scopeId ?? ''}:${resource.id}` : resource.id
}

interface GrantForm extends LimitForm {
  subject: string
  resource: string
  costCenter: string
  notes: string
}

const emptyForm: GrantForm = {
  ...emptyLimitForm,
  subject: '',
  resource: '',
  costCenter: '',
  notes: '',
}

function formForGrant(grant: Entitlement): GrantForm {
  return {
    ...limitFormFrom(grant.enforcement?.tokens),
    calls: grant.enforcement?.requests?.calls ? String(grant.enforcement.requests.calls) : '',
    renewalPeriodSeconds: String(grant.enforcement?.requests?.renewalPeriodSeconds ?? 60),
    subject: grant.subject.id,
    resource: resourceKey(grant.resource),
    costCenter: grant.costCenterId ?? '',
    notes: grant.notes ?? '',
  }
}

interface Banner {
  text: string
  /** A published model whose plan must be reviewed and applied before the change takes effect. */
  review?: { publicationId: string; displayName: string }
  /** A pool whose plan, on its own page, must be reviewed and applied before the change takes effect. */
  pool?: { id: string; displayName: string }
}

/** What saving a grant on a pool model means, which depends on whether the pool governs access yet. */
function poolGrantSaved(lead: string, pool: Pick<ModelPool, 'id' | 'displayName' | 'governedAccess'> | undefined): Banner {
  if (!pool) {
    return { text: `${lead} API Management is unchanged until the pool's plan is reviewed and applied.` }
  }
  return {
    text: pool.governedAccess
      ? `${lead} API Management is unchanged until the ${pool.displayName} plan is reviewed and applied.`
      : `${lead} ${pool.displayName} doesn't use governed access yet, so its callers still share one subscription. Turn on governed access for the pool, then review and apply its plan.`,
    pool: { id: pool.id, displayName: pool.displayName },
  }
}

/**
 * The registered principal who made a request: the one the request recorded, or the one with the
 * requester's Entra object ID in any letter case. Undefined for a requester MOSAIC doesn't know,
 * and while principals are loading or failed to load.
 */
function requestingPrincipal(accessRequest: AccessRequest, principals: Principal[] = []) {
  const objectId = accessRequest.requesterObjectId.toLowerCase()
  return principals.find(
    (item) => item.id === accessRequest.requesterPrincipalId || item.objectId.toLowerCase() === objectId,
  )
}

function requestResourceLabel(accessRequest: AccessRequest, fallback?: string) {
  // A pool model's summary names only the model. The console's own label names its pool too.
  if (accessRequest.resource.kind === 'poolModel' && fallback) return fallback
  return (
    accessRequest.resourceSummary?.displayName ??
    accessRequest.resourceSnapshot?.displayName ??
    fallback ??
    `Unavailable ${accessRequest.resource.kind}`
  )
}

/** Everything the approval dialog and its outcome banner need, resolved when it opens. */
interface ApprovalContext {
  accessRequest: AccessRequest
  requester: ApprovalRequester
  resourceLabel: string
  publication?: Publication
  reviewable?: Publication
  /** The pool of a requested pool model. */
  pool?: ModelPool
  governed: boolean
  existingGrant: boolean
}

function approvalBanner({ accessRequest, requester, publication, reviewable, pool, governed }: ApprovalContext): Banner {
  const lead = requester.registered
    ? `Approved the request and created grant intent for ${requester.label}.`
    : `Approved the request, registered ${requester.label} as a user principal, and created their grant intent.`
  if (accessRequest.resource.kind === 'poolModel') {
    return poolGrantSaved(lead, pool)
  }
  if (governed && accessRequest.resource.kind === 'mcpServer') {
    return {
      text: `${lead} API Management is unchanged; use Plan and apply on the MCPs page to activate it.`,
    }
  }
  if (governed && reviewable) {
    return {
      text: `${lead} API Management is unchanged; review and apply the ${reviewable.displayName} model plan to activate it.`,
      review: { publicationId: reviewable.id, displayName: reviewable.displayName },
    }
  }
  if (governed) {
    return {
      text: `${lead} API Management is unchanged until the ${publication?.displayName ?? 'model'} plan is reviewed and applied.`,
    }
  }
  return {
    text: `${lead} The grant is desired state only; MOSAIC does not apply grants for this resource to API Management.`,
  }
}

const grantRowId = (entitlementId: string) => `grant-${entitlementId}`

const ACCESS_SECTIONS = [
  { value: 'grants', label: 'Grants', icon: <KeyRegular />, description: 'Saved grants describe desired access. Saving, disabling, or revoking a grant does not by itself apply gateway changes.' },
  { value: 'effective', label: 'Effective access & overlaps', icon: <ShieldCheckmarkRegular />, description: 'Inspect recorded access paths and precedence. This is desired state, not a live runtime authorization test.' },
  { value: 'requests', label: 'Access requests', icon: <PersonClockRegular />, description: 'Decide portal requests, then review and apply the affected model, pool, or MCP plan where runtime access is governed.' },
  { value: 'approvalPolicies', label: 'Approval policies', icon: <ShieldCheckmarkRegular />, description: 'Explicit, scheduled automatic approvals create bounded grant intent. Gateway plan review and apply remain separate.' },
  { value: 'settings', label: 'Model access settings', icon: <SettingsRegular />, description: 'Choose a published model to configure its authentication methods and review all saved grants and settings together.' },
] as const

function grantPathLabel(item: ResolvedEntitlement): string {
  const group = item.viaGroupName ?? item.viaGroupId ?? ''
  if (item.via === 'direct') return 'Direct'
  return item.via === 'securityGroup' ? `Security group ${group}` : `MOSAIC group ${group}`
}

function overriddenByLabel(winner: ResolvedEntitlement | undefined): string {
  if (!winner) return 'Overridden by another grant'
  if (winner.via === 'direct') return 'Overridden by the direct grant'
  const group = winner.viaGroupName ?? winner.viaGroupId ?? ''
  return winner.via === 'securityGroup'
    ? `Overridden by security group ${group}`
    : `Overridden by MOSAIC group ${group}`
}

/**
 * A pool grant's key. A subject has one per pool and cost center, serving every pool model it holds
 * there, and a security group has none: its members sign in with Entra tokens.
 */
function PoolGrantKey({ entitlement }: { entitlement: Entitlement }) {
  const runtime = entitlement.runtime
  if (!runtime) {
    return (
      <Text className={styles.secondaryCell}>
        {entitlement.subject.kind === 'group' ? 'Desired state only' : 'Waits for the pool to govern access'}
      </Text>
    )
  }
  if (!runtime.subscriptionName) {
    return (
      <div className={styles.cellStack}>
        <Badge appearance="tint" shape="rounded" className={styles.bindingBadge}>Entra token</Badge>
        <Text className={styles.secondaryCell}>No key</Text>
      </div>
    )
  }
  return (
    <div className={styles.cellStack}>
      <Badge
        appearance="tint"
        shape="rounded"
        className={`${runtime.keyExists ? styles.statusReady : styles.statusAttention} ${styles.bindingBadge}`}
      >
        Subscription key
      </Badge>
      <Text className={styles.secondaryCell}>
        {runtime.keyExists ? 'Pool key' : 'Pool key, created when the plan applies'}
      </Text>
      <details className={styles.technicalDetails}>
        <summary>Subscription details</summary>
        <Text block className={styles.secondaryCell}>{runtime.subscriptionName}</Text>
      </details>
    </div>
  )
}

function GrantBinding({ entitlement }: { entitlement: Entitlement }) {
  if (entitlement.resource.kind === 'poolModel') return <PoolGrantKey entitlement={entitlement} />
  if (!entitlement.binding) {
    return <Badge appearance="tint" className={styles.statusAttention}>Not bound</Badge>
  }
  return (
    <div className={styles.cellStack}>
      <Badge
        appearance="tint"
        shape="rounded"
        className={`${styles.statusReady} ${styles.bindingBadge}`}
      >
        {entitlement.binding.apimSubscriptionName ? 'APIM subscription' :
          (entitlement.binding.attributionKey ? 'Gateway log' : 'Recorded')}
      </Badge>
      <details className={styles.technicalDetails}>
        <summary>Binding details</summary>
        {entitlement.binding.apimSubscriptionName && <Text block className={styles.secondaryCell}>{entitlement.binding.apimSubscriptionName}</Text>}
        <Text block className={styles.secondaryCell}>{entitlement.binding.source}</Text>
      </details>
    </div>
  )
}

export function EntitlementsPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const [activeSection] = usePageSection(ACCESS_SECTIONS)
  const workbench = useWorkbenchLocation({ section: activeSection })
  const requestedStatus = searchParams.get('requestStatus') ?? 'pending'
  const requestStatus = ['pending', 'approved', 'denied', 'withdrawn', 'all'].includes(requestedStatus)
    ? requestedStatus : 'pending'
  const costCenterFilter = searchParams.get('costCenter') ?? 'all'
  const [banner, setBanner] = useState<Banner | null>(null)
  const announce = useCallback((text: string) => setBanner({ text }), [])
  const [inspectedPrincipal, setInspectedPrincipal] = useState('')
  const [form, setForm] = useState<GrantForm>(emptyForm)
  const editBase = useRef<Entitlement | undefined>(undefined)
  const { selectedId, mode } = workbench
  const selectedPublicationId = activeSection === 'settings' ? selectedId ?? '' : ''
  const environmentFilter = searchParams.get('environment') ?? 'all'
  const directModelId = mode === 'new' ? searchParams.get('model') : null
  const dialogOpen = activeSection === 'grants' && (mode === 'new' || mode === 'edit')
  const [connectionGrantId, setConnectionGrantId] = useState<string | null>(null)
  const [review, setReview] = useState<{ publication: Publication; plan: PublishPlan } | null>(null)
  const [approving, setApproving] = useState<ApprovalContext | null>(null)
  const [bulkStage, setBulkStage] = useState<'select' | 'review' | null>(null)
  const [bulkIds, setBulkIds] = useState<string[]>([])
  const publishedModelSelectRef = useRef<HTMLSelectElement>(null)

  function updateLocation(changes: Record<string, string | null>, replace = false) {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key)
      else next.set(key, value)
    }
    if (next.has('selected') || next.has('mode')) next.set('workbench', `${location.pathname}:${next.get('tab') ?? activeSection}`)
    else next.delete('workbench')
    setSearchParams(next, { replace, preventScrollReset: true })
  }

  function selectSection(section: string) {
    updateLocation({ tab: section, selected: null, mode: null, model: null })
  }

  function selectRecord(id: string | null) {
    workbench.select(id)
  }

  function closeEditor(id = selectedId) {
    updateLocation({ mode: null, model: null, selected: id }, true)
  }

  useEffect(() => {
    setConnectionGrantId(null)
  }, [activeSection, selectedId, mode])

  const entitlements = useQuery({
    queryKey: ['entitlements', { costCenter: costCenterFilter }],
    queryFn: () => api.listEntitlements(costCenterFilter === 'all' ? undefined : { costCenter: costCenterFilter }),
  })
  const principals = useQuery({ queryKey: ['principals'], queryFn: () => api.listPrincipals() })
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api.listGroups() })
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: () => api.listModelApis() })
  const mcpServers = useQuery({ queryKey: ['mcp-servers'], queryFn: () => api.listMcpServers() })
  const modelPools = useQuery({ queryKey: ['model-pools', 'list'], queryFn: () => api.listModelPools() })
  const costCenters = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters })
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })
  const modelEndpoints = useQuery({
    queryKey: ['model-endpoints'],
    queryFn: () => api.listModelEndpoints(),
  })
  const environmentCatalog = useEnvironmentCatalog()
  const publications = useQuery({ queryKey: ['publications'], queryFn: () => api.listPublications() })
  const mcpPublications = useQuery({ queryKey: ['mcp-publications'], queryFn: () => api.listMcpPublications() })
  const pendingRequests = useQuery({
    queryKey: ['access-requests', 'pending'],
    queryFn: () => api.listAccessRequests('pending'),
  })
  const accessRequests = useQuery({
    queryKey: ['access-requests', requestStatus],
    queryFn: () => api.listAccessRequests(requestStatus === 'all' ? undefined : requestStatus),
    enabled: activeSection === 'requests',
  })
  const requestHistory = useQuery({
    queryKey: ['access-requests', 'all'],
    queryFn: () => api.listAccessRequests(),
    enabled: activeSection === 'requests' && Boolean(selectedId),
  })
  const overlaps = useQuery({
    queryKey: ['entitlements', 'overlaps'],
    queryFn: () => api.getGrantOverlaps(),
    enabled: activeSection === 'effective',
  })

  const subjectOptions = useMemo<SubjectOption[]>(
    () => [
      ...(principals.data ?? []).map<SubjectOption>((principal) => {
        const kind = subjectKindForPrincipal(principal.kind)
        const principalLabel = principal.label ?? principal.objectId
        const group: SubjectOption['group'] =
          principal.kind === 'user'
            ? 'People'
            : principal.kind === 'agentIdentity' || principal.kind === 'agentUser'
              ? 'Agents'
              : principal.kind === 'securityGroup'
                ? 'Security groups'
                : 'Applications'
        return {
          id: principal.id,
          kind,
          label: `${principalLabel} (${PRINCIPAL_KIND_LABELS[principal.kind]})`,
          group,
          principalKind: principal.kind,
        }
      }),
      ...(groups.data ?? []).map<SubjectOption>((group) => ({
        id: group.id,
        kind: 'group',
        label: `${group.name} (MOSAIC group)`,
        group: 'MOSAIC groups',
      })),
    ].sort((left, right) => left.group.localeCompare(right.group) || left.label.localeCompare(right.label)),
    [groups.data, principals.data],
  )

  const resourceOptions = useMemo<ResourceOption[]>(
    () => [
      ...(modelApis.data ?? []).map<ResourceOption>((item) => ({
        value: item.id,
        id: item.id,
        kind: 'modelApi',
        label: `${item.displayName} (model API)`,
      })),
      ...(modelPools.data ?? []).flatMap((pool) =>
        pool.models.map<ResourceOption>((model) => ({
          value: resourceKey({ kind: 'poolModel', id: model.id, scopeId: pool.id }),
          id: model.id,
          kind: 'poolModel',
          scopeId: pool.id,
          label: `${model.displayName} in ${pool.displayName} (pool model)`,
        })),
      ),
      ...(mcpServers.data ?? []).map<ResourceOption>((item) => ({
        value: item.id,
        id: item.id,
        kind: 'mcpServer',
        label: `${item.displayName} (MCP server)`,
      })),
    ],
    [mcpServers.data, modelApis.data, modelPools.data],
  )

  const labels = useMemo(() => {
    const map = new Map<string, string>()
    for (const option of subjectOptions) map.set(option.id, option.label)
    for (const option of resourceOptions) map.set(option.value, option.label)
    return map
  }, [resourceOptions, subjectOptions])
  const resourceLabel = (resource: Pick<EntitlementResource, 'kind' | 'id' | 'scopeId'>) =>
    labels.get(resourceKey(resource))

  const costCenterById = useMemo(
    () => new Map((costCenters.data ?? []).map((costCenter: CostCenter) => [costCenter.id, costCenter])),
    [costCenters.data],
  )

  const resolved = useQuery({
    queryKey: ['entitlements', 'resolve', inspectedPrincipal],
    queryFn: () => api.resolveEntitlements(inspectedPrincipal),
    enabled: activeSection === 'effective' && Boolean(inspectedPrincipal),
  })

  const createMutation = useMutation({
    mutationFn: (payload: {
      subject: EntitlementSubject
      resource: EntitlementResource
      costCenterId?: string
      enforcement: EntitlementEnforcement | null
      notes: string | null
    }) => api.createEntitlement(payload),
    onSuccess: async (saved, variables) => {
      rememberGrantEdit(saved)
      await queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      completeWorkspaceNavigation(() => updateLocation({ tab: 'grants', selected: saved.id, mode: null, model: null }))
      setForm(emptyForm)
      await queryClient.invalidateQueries({ queryKey: ['publications'] })
      await queryClient.invalidateQueries({ queryKey: ['mcp-publications'] })
      if (variables.resource.kind === 'poolModel') {
        await queryClient.invalidateQueries({ queryKey: ['model-pools'] })
        setBanner(poolGrantSaved('Saved grant intent.', poolFor(variables.resource)))
        return
      }
      announce(variables.resource.kind === 'mcpServer'
        ? 'Saved grant intent. API Management is unchanged; use Plan and apply on the MCPs page to activate it on a MOSAIC-published MCP server.'
        : 'Saved grant intent. API Management is unchanged; review and apply the model plan to activate a supported direct grant.')
    },
  })

  const editMutation = useMutation({
    mutationFn: ({ id, enforcement, notes }: { id: string; enforcement: EntitlementEnforcement | null; notes: string | null; before: Entitlement }) =>
      api.updateEntitlement(id, { enforcement, notes }),
    onSuccess: async (saved, variables) => {
      rememberGrantEdit(saved, variables.before)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: ['publications'] }),
        queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
        queryClient.invalidateQueries({ queryKey: ['model-pools'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] }),
      ])
      completeWorkspaceNavigation(() => closeEditor(saved.id))
      announce('Saved grant intent. API Management is unchanged; review and apply the affected resource plan separately.')
    },
  })

  const toggleMutation = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => {
      const before = entitlements.data?.find((item) => item.id === id)
      const saved = await api.updateEntitlement(id, { enabled })
      return { saved, before }
    },
    onSuccess: async ({ saved, before: grant }, variables) => {
      if (grant) rememberGrantEdit(saved, grant)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: ['publications'] }),
        queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] }),
        queryClient.invalidateQueries({ queryKey: ['model-pools'] }),
      ])
      if (grant?.resource.kind === 'poolModel') {
        setBanner(poolGrantSaved(
          variables.enabled ? 'Saved enabled intent.' : 'Saved disabled intent.',
          poolFor(grant.resource),
        ))
        return
      }
      announce(variables.enabled
        ? 'Saved enabled intent. Runtime access changes only after a supported model plan is reviewed and applied.'
        : 'Saved disabled intent. Managed revocation is pending until the model plan is reviewed, applied, and propagated; API Management is unchanged by this save.')
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (id: string) => api.deleteEntitlement(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      announce('Removed the desired-state grant. API Management is unchanged.')
    },
  })

  const approveMutation = useMutation({
    mutationFn: ({ context, approval }: { context: ApprovalContext; approval: AccessRequestApproval }) =>
      api.approveAccessRequest(context.accessRequest.id, approval),
    onMutate: () => setBanner(null),
    onSuccess: async (_, { context }) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['access-requests'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: ['principals'] }),
        queryClient.invalidateQueries({ queryKey: ['publications'] }),
        queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
        queryClient.invalidateQueries({ queryKey: ['model-pools'] }),
      ])
      completeWorkspaceNavigation(() => closeEditor(context.accessRequest.id))
      setBanner(approvalBanner(context))
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) {
        await queryClient.invalidateQueries({ queryKey: ['access-requests'] })
      }
    },
  })

  const denyMutation = useMutation({
    mutationFn: (id: string) => api.denyAccessRequest(id),
    onMutate: () => setBanner(null),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['access-requests'] })
      announce('Denied the access request. No grant was created, and API Management is unchanged.')
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) {
        await queryClient.invalidateQueries({ queryKey: ['access-requests'] })
      }
    },
  })

  const reviewMutation = useMutation({
    mutationFn: async (publicationId: string) => {
      const plan = await api.createPublishPlan(publicationId)
      const publication = await api.getPublication(publicationId)
      if (!plan.accessSnapshot) throw new Error('The service did not return a governed-access snapshot. No model access changes can be applied from this review.')
      return { publication, plan }
    },
    onSuccess: (result) => {
      setReview(result)
      updateLocation({ tab: 'settings', selected: result.publication.id, mode: 'review' })
    },
  })

  // Models whose API is in API Management, even if their latest apply failed. An unpublished one
  // isn't offered: reviewing its access would publish it again.
  const publishedModels = (publications.data ?? []).filter(
    (publication) =>
      publication.status === 'published' || (Boolean(publication.lastAppliedAt) && holdsApi(publication)),
  )
  const selectedPublication = publishedModels.find((publication) => publication.id === selectedPublicationId)
  useEffect(() => {
    if (activeSection === 'settings' && mode === 'review' && selectedPublicationId
      && review?.publication.id !== selectedPublicationId && !reviewMutation.isPending && !reviewMutation.isError) {
      reviewMutation.mutate(selectedPublicationId)
    }
    // A directly linked review reconstructs a plan, never applies it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSection, mode, selectedPublicationId])

  function publicationFor(entitlement: Pick<Entitlement, 'resource'>) {
    if (entitlement.resource.kind !== 'modelApi') return undefined
    const modelApi = modelApis.data?.find((model) => model.id === entitlement.resource.id)
    return publications.data?.find(
      (publication) => publication.modelApiId === entitlement.resource.id || publication.id === modelApi?.publicationId,
    )
  }

  function mcpPublicationFor(entitlement: Pick<Entitlement, 'resource'>) {
    if (entitlement.resource.kind !== 'mcpServer') return undefined
    const server = mcpServers.data?.find((item) => item.id === entitlement.resource.id)
    return mcpPublications.data?.find(
      (publication) => publication.mcpServerId === entitlement.resource.id || publication.id === server?.publicationId,
    )
  }

  function poolFor(resource: Pick<EntitlementResource, 'kind' | 'scopeId'>) {
    if (resource.kind !== 'poolModel') return undefined
    return modelPools.data?.find((pool) => pool.id === resource.scopeId)
  }

  function rememberGrantEdit(saved: Entitlement, before?: Entitlement) {
    const target = saved.resource.kind === 'poolModel' ? poolFor(saved.resource)
      : saved.resource.kind === 'mcpServer' ? mcpPublicationFor(saved) : publicationFor(saved)
    if (target) rememberEdit(target.id, before ? grantReviewValues(before) : {}, grantReviewValues(saved))
  }

  function managedGrant(entitlement: Pick<Entitlement, 'resource' | 'subject' | 'runtime'>) {
    // A pool applies every direct grant on its models once it governs access. Until then they wait.
    if (entitlement.resource.kind === 'poolModel') return entitlement.subject.kind !== 'group'
    const publication = publicationFor(entitlement)
    if (entitlement.resource.kind === 'mcpServer') {
      return entitlement.subject.kind !== 'group' && Boolean(entitlement.runtime || mcpPublicationFor(entitlement)?.appliedAccess)
    }

    return entitlement.resource.kind === 'modelApi' && entitlement.subject.kind !== 'group'
      && Boolean(entitlement.runtime || publication?.governedAccess || publication?.appliedAccess)
  }

  function grantLocked(entitlement: Pick<Entitlement, 'resource' | 'runtime'>) {
    const publication = publicationFor(entitlement)
    const pool = poolFor(entitlement.resource)
    const mcp = mcpPublicationFor(entitlement)
    return ['applying', 'unknown'].includes(entitlement.runtime?.status ?? '')
      || [publication?.accessState, pool?.accessState, mcp?.accessState].some((state) => state === 'applying' || state === 'unknown')
      || [publication?.status, pool?.status, mcp?.status].some((status) => status === 'applying')
  }

  function approvalContext(accessRequest: AccessRequest): ApprovalContext {
    const principal = requestingPrincipal(accessRequest, principals.data)
    const subject: EntitlementSubject = {
      kind: principal ? subjectKindForPrincipal(principal.kind) : 'user',
      id: principal?.id ?? accessRequest.requesterObjectId,
    }
    const { resource } = accessRequest
    const publication = publicationFor(accessRequest)
    return {
      accessRequest,
      requester: {
        label: principal?.label || accessRequest.requesterObjectId,
        objectId: accessRequest.requesterObjectId,
        registered: Boolean(principal),
      },
      resourceLabel: requestResourceLabel(accessRequest, resourceLabel(resource)),
      publication,
      reviewable: publishedModels.find((item) => item.id === publication?.id),
      pool: poolFor(resource),
      governed: managedGrant({ resource, subject }),
      existingGrant: Boolean(principal) && (entitlements.data ?? []).some(
        (item) => item.subject.kind === subject.kind && item.subject.id === subject.id
          && item.resource.kind === resource.kind && item.resource.id === resource.id
          && (item.resource.scopeId ?? '') === (resource.scopeId ?? '')
          && (item.costCenterId ?? '') === (accessRequest.costCenterId ?? ''),
      ),
    }
  }

  function openApproval(accessRequest: AccessRequest) {
    approveMutation.reset()
    setApproving(approvalContext(accessRequest))
    updateLocation({ tab: 'requests', selected: accessRequest.id, mode: 'review' })
  }

  function openReview(publicationId: string) {
    reviewMutation.reset()
    setConnectionGrantId(null)
    updateLocation({ tab: 'settings', selected: publicationId, mode: null, model: null })
  }

  /** Overlap diagnostics and decided requests open the exact grant in one URL update. */
  function showGrant(entitlementId: string) {
    updateLocation({ tab: 'grants', selected: entitlementId, mode: null, model: null })
  }

  useEffect(() => {
    if (activeSection === 'settings' && selectedPublicationId) publishedModelSelectRef.current?.focus({ preventScroll: true })
  }, [activeSection, selectedPublicationId])

  function addDirectGrant(modelApiId: string) {
    setForm({ ...emptyForm, resource: modelApiId })
    createMutation.reset()
    updateLocation({ tab: 'grants', mode: 'new', model: modelApiId, selected: null })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (mode === 'edit' && selectedId) {
      const grant = entitlements.data?.find((item) => item.id === selectedId)
      if (!grant || grantLocked(grant) || callRateError(form) || editMutation.isPending) return
      let enforcement = buildEnforcement(form, managedGrant(grant))
      const original = formForGrant(grant)
      const limitsUnchanged = (['tokensPerMinute', 'tokenQuota', 'tokenQuotaPeriod', 'calls', 'renewalPeriodSeconds'] as const)
        .every((key) => original[key] === form[key])
      if (limitsUnchanged) enforcement = grant.enforcement ?? null
      if (grant.enforcement?.requests?.callQuota) {
        enforcement = {
          ...enforcement,
          requests: {
            counterKeyExpression: grant.enforcement.requests.counterKeyExpression,
            ...enforcement?.requests,
            callQuota: grant.enforcement.requests.callQuota,
            callQuotaPeriod: grant.enforcement.requests.callQuotaPeriod,
          },
        }
      }
      if (!editBase.current || editBase.current.id !== grant.id) return
      editMutation.mutate({ id: grant.id, enforcement, notes: form.notes.trim() || null, before: editBase.current })
      return
    }
    const subject = subjectOptions.find((option) => option.id === form.subject)
    const resource = resourceOptions.find((option) => option.value === form.resource)
    if (createMutation.isPending || !subject || !resource || callRateError(form) || (directModelId && subject.kind === 'group')) {
      return
    }
    const target = {
      subject: { kind: subject.kind, id: subject.id },
      resource: { kind: resource.kind, id: resource.id, ...(resource.scopeId ? { scopeId: resource.scopeId } : {}) },
    }
    if (grantLocked(target)) return
    createMutation.mutate({
      ...target,
      ...(form.costCenter ? { costCenterId: form.costCenter } : {}),
      enforcement: buildEnforcement(form, managedGrant(target)),
      notes: form.notes.trim() || null,
    })
  }

  const rows = entitlements.data ?? []
  const editingGrant = mode === 'edit' ? rows.find((item) => item.id === selectedId) : undefined
  useEffect(() => {
    if (!dialogOpen) return
    createMutation.reset()
    editMutation.reset()
    editBase.current = editingGrant
    setForm(editingGrant ? formForGrant(editingGrant) : { ...emptyForm, resource: directModelId ?? '' })
    // Re-entering an editor starts a fresh draft, including browser Back/Forward.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dialogOpen, mode, selectedId, editingGrant?.id, directModelId])
  const approvalReady = principals.isSuccess && entitlements.isSuccess && publications.isSuccess
    && modelApis.isSuccess && mcpServers.isSuccess && modelPools.isSuccess && mcpPublications.isSuccess
  const selectedRequest = (requestHistory.data ?? accessRequests.data ?? []).find((item) => item.id === selectedId)
  useEffect(() => {
    if (activeSection !== 'requests' || mode !== 'review') {
      setApproving(null)
      return
    }
    if (selectedRequest && approvalReady) {
      setApproving((current) => current?.accessRequest.id === selectedRequest.id
        ? { ...current, accessRequest: selectedRequest } : approvalContext(selectedRequest))
    }
    // Preserve entered limits when refreshed data changes; the request ID owns the draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSection, mode, selectedRequest, approvalReady])
  const grantRows = rows
    .map((entitlement) => ({
      entitlement,
      environment: entitlementResourceEnvironment(
        entitlement.resource,
        {
          gateways: gateways.data,
          modelApis: modelApis.data,
          mcpServers: mcpServers.data,
          modelEndpoints: modelEndpoints.data,
          modelPools: modelPools.data,
        },
        entitlement,
      ),
    }))
    .filter((row) => {
      if (environmentFilter === 'all') return true
      if (environmentFilter === 'unclassified') return row.environment == null
      return row.environment === environmentFilter
    })
  // A pool grant is never bound to its own subscription: the pool's policy checks it on every call.
  const unbound = rows.filter((item) => !item.binding && item.resource.kind !== 'poolModel').length
  const rateError = callRateError(form)
  const selectedResource = resourceOptions.find((option) => option.value === form.resource)
  const newGrantLocked = Boolean(!editingGrant && selectedResource && grantLocked({ resource: selectedResource }))
  const mcpSelected = (selectedResource ?? editingGrant?.resource)?.kind === 'mcpServer'
  const poolSelected = (selectedResource ?? editingGrant?.resource)?.kind === 'poolModel'
  const selectedPool = selectedResource ? poolFor(selectedResource) : editingGrant ? poolFor(editingGrant.resource) : undefined
  const connectionGrant = rows.find((entitlement) => entitlement.id === connectionGrantId)

  if (activeSection === 'requests' && bulkStage === 'select') return <BulkRequestSelection requests={pendingRequests.data ?? []} principals={principals.data ?? []}
    onBack={() => setBulkStage(null)} onReview={ids => { setBulkIds(ids); setBulkStage('review') }} />
  if (activeSection === 'requests' && bulkStage === 'review') return <BulkApprovalWorkspace requestIds={bulkIds} principals={principals.data ?? []} costCenters={costCenters.data ?? []}
    onBack={() => setBulkStage(null)} onComplete={() => {
      void queryClient.invalidateQueries({ queryKey: ['access-requests'] })
      void queryClient.invalidateQueries({ queryKey: ['entitlements'] })
    }} />

  return (
    <section className={styles.page}>
      <PageHeader
        title="Entitlements"
        description="Manage grants, inspect access paths, and decide requests. Saving intent never changes API Management; review and apply the affected resource's plan separately."
      />

      <div className={styles.rowActions}>
        <Button onClick={() => {
          const next = new URLSearchParams(searchParams)
          next.set('tab', 'requests')
          next.delete('requestStatus')
          next.delete('selected')
          next.delete('mode')
          next.delete('workbench')
          setSearchParams(next)
        }}>
          {pendingRequests.isSuccess ? `${pendingRequests.data.length} pending access requests` : 'Open access requests'}
        </Button>
        {pendingRequests.isPending && <Text role="status">Checking pending requests…</Text>}
        {pendingRequests.isError && <Text role="status">Pending count unavailable. Open requests to retry.</Text>}
      </div>
      {banner && (
        <MessageBar intent="success" layout="multiline">
          <MessageBarBody>
            {banner.text}
            {banner.review && (
              <>
                {' '}
                <Link
                  inline
                  href="#governed-model-access"
                  onClick={(event) => {
                    event.preventDefault()
                    if (banner.review) openReview(banner.review.publicationId)
                  }}
                >
                  Go to review and apply for {banner.review.displayName}
                </Link>
              </>
            )}
            {banner.pool && (
              <>
                {' '}
                <RouterLink to={`/pools/${banner.pool.id}`}>Go to {banner.pool.displayName}</RouterLink>
              </>
            )}
          </MessageBarBody>
        </MessageBar>
      )}
      {revokeMutation.isError && <ErrorState error={revokeMutation.error} />}
      {toggleMutation.isError && <ErrorState error={toggleMutation.error} />}
      {denyMutation.isError && <ErrorState error={denyMutation.error} />}
      {activeSection === 'grants' && entitlements.isError && rows.length > 0 && <ErrorState error={entitlements.error} />}
      {activeSection === 'requests' && accessRequests.isError && Boolean(accessRequests.data?.length) && <ErrorState error={accessRequests.error} />}
      {costCenters.isError && <ErrorState error={costCenters.error} />}
      {principals.isError && <ErrorState error={principals.error} />}
      {groups.isError && <ErrorState error={groups.error} />}
      {modelApis.isError && <ErrorState error={modelApis.error} />}
      {mcpServers.isError && <ErrorState error={mcpServers.error} />}
      {modelPools.isError && <ErrorState error={modelPools.error} />}
      {gateways.isError && <ErrorState error={gateways.error} />}
      {modelEndpoints.isError && <ErrorState error={modelEndpoints.error} />}
      {runtimeConfig.authMode === 'local' && (
        <MessageBar intent="warning" layout="multiline">
          <MessageBarBody>Local development mode: service responses may be simulated and do not verify live APIM enforcement.</MessageBarBody>
        </MessageBar>
      )}

      <SectionLayout label="Entitlements" sections={ACCESS_SECTIONS} active={activeSection} onSelect={selectSection}>
      {activeSection === 'approvalPolicies' && <ApprovalPolicies />}
      {activeSection === 'settings' && <Card className={styles.detailCard} id="governed-model-access">
        <Title3 as="h2">Model access settings</Title3>
        <Text>Choose a successfully published MOSAIC model. Direct grants to people, agents, and applications, and Entra security-group grants, are applied; MOSAIC groups and imported-only APIs are not orchestrated.</Text>
        {publications.isPending && <Loading label="Loading published models..." />}
        {publications.isError && <ErrorState error={publications.error} />}
        {mcpPublications.isError && <ErrorState error={mcpPublications.error} />}
        <Field label="Published model">
          <Select
            ref={publishedModelSelectRef}
            value={selectedPublicationId}
            onChange={(_, data) => {
              selectRecord(data.value || null)
              reviewMutation.reset()
              setConnectionGrantId(null)
            }}
          >
            <option value="">Select a published model</option>
            {publishedModels.map((publication) => (
              <option key={publication.id} value={publication.id}>
                {publication.displayName} · {publication.deploymentName} · {publication.gatewayId}
              </option>
            ))}
          </Select>
        </Field>
        {publications.isSuccess && publishedModels.length === 0 && <Text>No successfully published models are available. Publish a model before configuring runtime grants.</Text>}
        {selectedPublication && (
          <ModelAccessSettingsPanel
            key={`${selectedPublication.id}:${JSON.stringify(selectedPublication.governedAccess)}`}
            publication={selectedPublication}
            reviewing={reviewMutation.isPending}
            onReview={() => reviewMutation.mutate(selectedPublication.id)}
            onAddGrant={addDirectGrant}
            onMessage={announce}
          />
        )}
        {reviewMutation.isError && <ErrorState error={reviewMutation.error} />}
      </Card>}

      {activeSection === 'grants' && <dl className={styles.summaryStrip}>
        <div>
          <dt>Grants</dt>
          <dd>{entitlements.isSuccess ? rows.length : '—'}</dd>
        </div>
        <div>
          <dt>Desired enabled</dt>
          <dd>{entitlements.isSuccess ? rows.filter((item) => item.enabled).length : '—'}</dd>
        </div>
        <div>
          <dt>Without a binding</dt>
          <dd>{entitlements.isSuccess ? unbound : '—'}</dd>
          <dd className={styles.summaryNote}>Consumption cannot be attributed until one is recorded.</dd>
        </div>
      </dl>}

      {activeSection === 'grants' && <ResourceWorkbench
        title="Grants"
        actions={<Button
          appearance="primary"
          icon={<AddRegular />}
          disabled={Boolean(mode) || subjectOptions.length === 0 || resourceOptions.length === 0}
          onClick={() => {
            setForm(emptyForm)
            createMutation.reset()
            updateLocation({ tab: 'grants', selected: null, mode: 'new', model: null })
          }}
        >New grant</Button>}
        description="Desired intent and last recorded runtime state are separate. Applied metadata is not a live invocation test."
        items={grantRows}
        getId={({ entitlement }) => entitlement.id}
        selectedId={selectedId}
        onSelect={selectRecord}
        listLabel="Grants"
        detailLabel="Selected grant"
        emptyList={entitlements.isPending ? <Loading label="Loading entitlements..." /> : entitlements.isError
          ? <ErrorState error={entitlements.error} />
          : <EmptyState title={rows.length ? 'No grants match these filters' : 'Nothing has been granted yet'}>Change the filters or create a grant.</EmptyState>}
        missingSelection={<EmptyState title="Grant unavailable">This grant was removed or is outside the current filters. <Button onClick={() => selectRecord(null)}>Back to list</Button></EmptyState>}
        renderListItem={({ entitlement, environment }) => <div className={styles.listSummary}>
          <Text weight="semibold">{labels.get(entitlement.subject.id) ?? entitlement.subject.id}</Text>
          <Text className={styles.secondaryCell}>{resourceLabel(entitlement.resource) ?? entitlement.resource.id}</Text>
          <div className={styles.rowActions}>
            <EnvironmentBadge azureCloud={entitlementResourceCloud(entitlement.resource, {
              gateways: gateways.data, modelApis: modelApis.data, mcpServers: mcpServers.data,
              modelEndpoints: modelEndpoints.data, modelPools: modelPools.data,
            }, entitlement)} environment={environment} catalog={environmentCatalog.data} />
            <Badge appearance="outline">{entitlement.enabled ? 'Desired enabled' : 'Desired disabled'}</Badge>
          </div>
        </div>}
        toolbar={<div className={styles.rowActions}>
          <Field label="Environment">
            <Select
              value={environmentFilter}
              onChange={(_, data) => updateLocation({ environment: data.value === 'all' ? null : data.value })}
              aria-label="Filter grants by environment"
            >
              <option value="all">All environments</option>
              {(environmentCatalog.data?.environments ?? []).map((environment) => (
                <option key={environment.key} value={environment.key}>
                  {environment.displayName}
                </option>
              ))}
              <option value="unclassified">Unclassified</option>
            </Select>
          </Field>
          <Field label="Cost center">
            <Select
              value={costCenterFilter}
              onChange={(_, data) => {
                const next = new URLSearchParams(searchParams)
                if (data.value === 'all') next.delete('costCenter')
                else next.set('costCenter', data.value)
                setSearchParams(next)
              }}
              aria-label="Filter grants by cost center"
            >
              <option value="all">All cost centers</option>
              {(costCenters.data ?? []).map((costCenter) => (
                <option key={costCenter.id} value={costCenter.id}>
                  {costCenter.name} ({costCenter.code})
                </option>
              ))}
            </Select>
          </Field>
        </div>}
        renderDetail={({ entitlement, environment }) => {
                    const relatedPublication = publicationFor(entitlement)
                    const relatedMcpPublication = mcpPublicationFor(entitlement)
                    const appliedGrant = relatedPublication?.appliedAccess?.grants.find(
                      (grant) => grant.entitlementId === entitlement.id,
                    )
                    const appliedMcpGrant = relatedMcpPublication?.appliedAccess?.grants.find(
                      (grant) => grant.entitlementId === entitlement.id,
                    )
                    const relatedPool = poolFor(entitlement.resource)
                    const appliedPoolGrant = relatedPool?.appliedAccess?.grants.find(
                      (grant) => grant.entitlementId === entitlement.id,
                    )
                    const managed = managedGrant(entitlement)
                    const locked = grantLocked(entitlement)
                    const subjectPrincipal = principals.data?.find((item) => item.id === entitlement.subject.id)
                    const connectionAvailable = (managed && Boolean(entitlement.runtime)) || entitlement.resource.kind === 'mcpServer'
                    const primaryAction = !managed ? 'toggle'
                      : !entitlement.enabled ? 'enable'
                      : connectionAvailable ? 'connection'
                      : relatedPublication ? 'model'
                      : entitlement.resource.kind === 'poolModel' && relatedPool ? 'pool'
                      : null
                    return (
                      <div className={styles.grantDetail}
                        id={grantRowId(entitlement.id)}
                        tabIndex={-1}
                      >
                      <section aria-label="Subject">
                        <div className={styles.identityCell}>
                          {subjectPrincipal && <IdentityAvatar
                            name={labels.get(entitlement.subject.id) ?? entitlement.subject.id}
                            kind={subjectPrincipal.kind}
                            objectId={subjectPrincipal.objectId}
                            size={32}
                          />}
                          <div className={styles.cellStack}>
                            <Text className={styles.primaryCell}>
                              {labels.get(entitlement.subject.id) ?? entitlement.subject.id}
                            </Text>
                            <div className={styles.rowActions}>
                              {subjectPrincipal ? (
                                <PrincipalKindBadge kind={subjectPrincipal.kind} />
                              ) : (
                                <Badge appearance="tint">
                                  {ENTITLEMENT_SUBJECT_KIND_LABELS[entitlement.subject.kind]}
                                </Badge>
                              )}
                            </div>
                            {entitlement.subject.kind === 'securityGroup' && (
                              <Text className={styles.secondaryCell}>
                                Entra tokens only · limits apply to each member
                              </Text>
                            )}
                          </div>
                        </div>
                      </section>
                      <section aria-label="Resource">
                        <div className={styles.cellStack}>
                          <Text className={styles.primaryCell}>
                            {resourceLabel(entitlement.resource) ?? entitlement.resource.id}
                          </Text>
                          <Text className={styles.secondaryCell}>{entitlement.resource.kind}</Text>
                          <div role="group" aria-label="Environment">
                            <EnvironmentBadge azureCloud={entitlementResourceCloud(entitlement.resource, {
                              gateways: gateways.data, modelApis: modelApis.data, mcpServers: mcpServers.data,
                              modelEndpoints: modelEndpoints.data, modelPools: modelPools.data,
                            }, entitlement)} environment={environment} catalog={environmentCatalog.data} />
                          </div>
                          <div className={styles.resourceMetadata} role="group" aria-label="Cost center">
                            <Text size={200} weight="semibold">Cost center</Text>
                            {(() => {
                              const costCenter = entitlement.costCenter ?? (entitlement.costCenterId ? costCenterById.get(entitlement.costCenterId) : undefined)
                              return costCenter ? (
                                <div className={styles.cellStack}>
                                  <Text className={styles.secondaryCell}>{costCenter.name}</Text>
                                  <Text className={styles.secondaryCell}>{costCenter.code}</Text>
                                </div>
                              ) : '—'
                            })()}
                          </div>
                        </div>
                      </section>
                      <section aria-label="Limits and binding">
                        <div className={styles.cellStack} role="group" aria-label="Limits">
                          <Text size={200} weight="semibold">Desired limits</Text>
                          {describeLimits(entitlement, relatedPublication?.enforcement).map((sentence) => (
                            <Text key={sentence} className={styles.secondaryCell}>
                              {sentence}
                            </Text>
                          ))}
                          {relatedPool && describePoolSafeguard(
                            relatedPool.safeguard,
                            relatedPool.appliedAccess?.tokenMetering !== false,
                          ).map((sentence) => (
                            <Text key={`pool:${sentence}`} className={styles.secondaryCell}>
                              Pool: {sentence}
                            </Text>
                          ))}
                          {(appliedGrant || appliedMcpGrant || appliedPoolGrant) && <details className={styles.technicalDetails}>
                            <summary>Last applied limit details</summary>
                            <div className={styles.cellStack}>
                              {appliedGrant && (
                                <>
                                  <Text size={200} weight="semibold">Last applied limits {appliedGrant.enabled ? '' : '(grant disabled)'}</Text>
                                  {describeLimits(appliedGrant, relatedPublication?.appliedAccess?.publicationEnforcement).map((sentence) => (
                                    <Text key={`applied:${sentence}`} className={styles.secondaryCell}>{sentence}</Text>
                                  ))}
                                </>
                              )}
                              {appliedMcpGrant && (
                                <>
                                  <Text size={200} weight="semibold">Last applied MCP limits {appliedMcpGrant.enabled ? '' : '(grant disabled)'}</Text>
                                  {describeLimits(appliedMcpGrant).map((sentence) => (
                                    <Text key={`mcp-applied:${sentence}`} className={styles.secondaryCell}>{sentence}</Text>
                                  ))}
                                </>
                              )}
                              {appliedPoolGrant && (
                                <>
                                  <Text size={200} weight="semibold">Last applied limits {appliedPoolGrant.enabled ? '' : '(grant disabled)'}</Text>
                                  {describeLimits(appliedPoolGrant).map((sentence) => (
                                    <Text key={`pool-applied:${sentence}`} className={styles.secondaryCell}>{sentence}</Text>
                                  ))}
                                </>
                              )}
                            </div>
                          </details>}
                          {appliedPoolGrant?.keysAllowed === false && (
                            <Text className={styles.secondaryCell}>
                              Its cost center turned keys off, so only Entra tokens work.
                            </Text>
                          )}
                        </div>
                        <div className={styles.bindingGroup} role="group" aria-label="Binding">
                          <Text size={200} weight="semibold">Binding</Text>
                          <GrantBinding entitlement={entitlement} />
                        </div>
                      </section>
                      <section aria-label="Desired and applied state">
                        <div className={styles.cellStack}>
                          <EntitlementAccessState entitlement={entitlement} />
                          {entitlement.revocation?.reason === 'costCenterMembership' && (
                            <Text className={styles.secondaryCell}>
                              Revoked when its subject left the cost center.
                            </Text>
                          )}
                        </div>
                      </section>
                      <section aria-label="Grant actions">
                        {locked && <MessageBar intent="warning"><MessageBarBody>Access is applying or its state is unknown. Recover the resource's access state before editing or revoking this grant.</MessageBarBody></MessageBar>}
                        <Button disabled={locked || toggleMutation.isPending} onClick={() => workbench.open('edit', entitlement.id)}>Edit grant</Button>
                        <RowActions
                          label={`More actions for ${labels.get(entitlement.subject.id) ?? entitlement.subject.id} on ${resourceLabel(entitlement.resource) ?? entitlement.resource.id}`}
                          primary={primaryAction === 'toggle' ? <Switch
                            checked={entitlement.enabled}
                            label={entitlement.enabled ? 'Enabled' : 'Disabled'}
                            disabled={toggleMutation.isPending || locked}
                            onChange={(_, data) =>
                              toggleMutation.mutate({
                                id: entitlement.id,
                                enabled: data.checked,
                              })
                            }
                          /> : primaryAction === 'enable' ? (
                            <Button disabled={toggleMutation.isPending || locked} onClick={() => toggleMutation.mutate({ id: entitlement.id, enabled: true })}>
                              Re-enable
                            </Button>
                          ) : primaryAction === 'connection' ? (
                            <Button onClick={() => setConnectionGrantId(entitlement.id)}>Connection info</Button>
                          ) : primaryAction === 'model' && relatedPublication ? (
                            <Button onClick={() => openReview(relatedPublication.id)}>Manage model</Button>
                          ) : primaryAction === 'pool' && relatedPool ? (
                            <Button onClick={() => navigate(`/pools/${relatedPool.id}`)}>Manage pool</Button>
                          ) : null}
                          actions={[
                            ...(managed && relatedPublication && primaryAction !== 'model' ? [{
                              label: 'Manage model',
                              onClick: () => openReview(relatedPublication.id),
                            }] : []),
                            ...(entitlement.resource.kind === 'poolModel' && relatedPool && primaryAction !== 'pool' ? [{
                              label: 'Manage pool',
                              onClick: () => navigate(`/pools/${relatedPool.id}`),
                            }] : []),
                            ...(relatedMcpPublication ? [{
                              label: 'Manage MCP',
                              onClick: () => navigate(`/mcps?tab=published&selected=${encodeURIComponent(relatedMcpPublication.id)}`),
                            }] : []),
                            ...(connectionAvailable && primaryAction !== 'connection' ? [{
                              label: 'Connection info',
                              onClick: () => setConnectionGrantId(entitlement.id),
                            }] : []),
                          ]}
                          destructiveActions={[{
                            label: 'Revoke',
                            disabled: locked || revokeMutation.isPending || toggleMutation.isPending || (managed && !entitlement.enabled),
                            onClick: () => {
                              setConnectionGrantId(null)
                              if (managed) toggleMutation.mutate({ id: entitlement.id, enabled: false })
                              else revokeMutation.mutate(entitlement.id)
                            },
                          }]}
                        />
                      </section>
                      <dl className={styles.detailList}>
                        <div><dt>Notes</dt><dd>{entitlement.notes || 'No notes'}</dd></div>
                        <div><dt>Created</dt><dd>{entitlement.createdAt}</dd></div>
                        <div><dt>Updated</dt><dd>{entitlement.updatedAt}</dd></div>
                        <div><dt>Grant ID</dt><dd>{entitlement.id}</dd></div>
                      </dl>
                      </div>
                    )
                  }}
      />}

      {activeSection === 'effective' && <Card className={styles.tableCard}>
        <div className={styles.tableHeader}>
          <div className={styles.tableHeading}>
            <Title3 as="h2">Overlapping grants</Title3>
            <Text size={200}>
              When more than one grant can reach the same caller on the same resource, MOSAIC shows
              which grant applies and which grants are shadowed.
            </Text>
          </div>
        </div>
        <div className={styles.tableWrap}>
          {overlaps.isPending && <Loading label="Loading overlapping grants..." />}
          {overlaps.isError && <ErrorState error={overlaps.error} />}
          {overlaps.data && !overlaps.data.membershipChecked && (
            <MessageBar intent="warning">
              <MessageBarBody>
                Microsoft Graph membership was not checked. Group-only overlaps are shown, but
                principal-specific overlaps may be missing.
                {overlaps.data.skipped.length > 0 && (
                  <> Skipped: {overlaps.data.skipped.join(', ')}.</>
                )}
              </MessageBarBody>
            </MessageBar>
          )}
          {overlaps.data && overlaps.data.overlaps.length === 0 && (
            <EmptyState title="No overlapping grants.">
              No saved grants currently overlap for the selected scope.
            </EmptyState>
          )}
          {overlaps.data && overlaps.data.overlaps.length > 0 && (
            <div className={styles.accessPanel}>
              {overlaps.data.overlaps.map((overlap) => (
                <Card key={`${overlap.kind}:${overlap.resource.kind}:${overlap.resource.id}:${overlap.winner.entitlementId}`} className={styles.noteCard}>
                  <div className={styles.cellStack}>
                    <div className={styles.rowActions}>
                      <Badge appearance="tint">{GRANT_OVERLAP_KIND_LABELS[overlap.kind]}</Badge>
                      <Badge appearance="outline">{overlap.resourceLabel}</Badge>
                    </div>
                    {overlap.principalLabel && (
                      <Text>
                        Affected principal: <strong>{overlap.principalLabel}</strong>
                      </Text>
                    )}
                    <Text>{overlap.reason}</Text>
                    {overlap.costCenterId && (
                      <Text className={styles.secondaryCell}>
                        Cost center: {costCenterById.get(overlap.costCenterId)?.name ?? overlap.costCenterId}
                      </Text>
                    )}
                    <dl className={`${styles.detailList} ${styles.overlapGrants}`}>
                      {[overlap.winner, ...overlap.shadowed].map((grant, index) => (
                        <div key={grant.entitlementId}>
                          <dt>{index === 0 ? 'Applies' : "Doesn't apply"}</dt>
                          <dd>{grant.subjectLabel}</dd>
                          {describeLimits({ enforcement: grant.enforcement }).map((sentence) => (
                            <dd key={sentence} className={styles.secondaryCell}>
                              {sentence}
                            </dd>
                          ))}
                          {rows.some((row) => row.id === grant.entitlementId) && (
                            <dd>
                              <Link
                                href={`#${grantRowId(grant.entitlementId)}`}
                                aria-label={`Go to the ${grant.subjectLabel} grant on ${overlap.resourceLabel}`}
                                onClick={(event) => {
                                  event.preventDefault()
                                  showGrant(grant.entitlementId)
                                }}
                              >
                                Go to grant
                              </Link>
                            </dd>
                          )}
                        </div>
                      ))}
                    </dl>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>
      </Card>}

      {activeSection === 'requests' &&
        <ResourceWorkbench
          title="Access requests"
          description="Approving creates grant intent with the limits you confirm. A request can be decided once; a decision is final."
          items={selectedRequest && !accessRequests.data?.some((item) => item.id === selectedRequest.id)
            ? [...(accessRequests.data ?? []), selectedRequest] : accessRequests.data ?? []}
          getId={(item) => item.id}
          selectedId={selectedId}
          onSelect={selectRecord}
          listLabel="Access requests"
          detailLabel="Selected access request"
          emptyList={accessRequests.isPending ? <Loading label="Loading access requests..." /> : accessRequests.isError
            ? <><ErrorState error={accessRequests.error} /><Button onClick={() => accessRequests.refetch()}>Retry requests</Button></>
            : <EmptyState title={requestStatus === 'pending' ? 'No pending requests' : 'No requests with this status'}>Requests appear when a portal user asks for a resource they are not entitled to.</EmptyState>}
          missingSelection={requestHistory.isPending ? <Loading label="Loading selected request..." /> : requestHistory.isError
            ? <><ErrorState error={requestHistory.error} /><Button onClick={() => requestHistory.refetch()}>Retry selected request</Button></>
            : <EmptyState title="Request unavailable">This exact request was removed or is inaccessible. <Button onClick={() => selectRecord(null)}>Back to list</Button></EmptyState>}
          renderListItem={(accessRequest) => <div className={styles.listSummary}>
            <Text weight="semibold">{requestingPrincipal(accessRequest, principals.data)?.label ?? 'Requester name unavailable'}</Text>
            <Text className={styles.secondaryCell}>{requestResourceLabel(accessRequest, resourceLabel(accessRequest.resource))}</Text>
            <Badge appearance="outline">{accessRequest.state}</Badge>
          </div>}
          toolbar={
            <>
            <Button disabled={!pendingRequests.isSuccess || !pendingRequests.data.length} onClick={() => setBulkStage('select')}>Bulk approval review</Button>
            <Field label="Request status">
              <Select value={requestStatus} onChange={(_, data) => {
                const next = new URLSearchParams(searchParams)
                if (data.value === 'pending') next.delete('requestStatus')
                else next.set('requestStatus', data.value)
                next.delete('selected')
                next.delete('mode')
                setSearchParams(next)
              }}>
                <option value="pending">Pending</option>
                <option value="approved">Approved</option>
                <option value="denied">Denied</option>
                <option value="withdrawn">Withdrawn</option>
                <option value="all">All statuses</option>
              </Select>
            </Field>
            </>
          }
          renderDetail={(accessRequest) => <div className={styles.grantDetail}>
            <Title3 as="h3">{requestResourceLabel(accessRequest, resourceLabel(accessRequest.resource))}</Title3>
            <dl className={styles.detailList}>
              <div><dt>Requester</dt><dd className={styles.cellStack}>
                {requestingPrincipal(accessRequest, principals.data)?.label && <Text className={styles.primaryCell}>{requestingPrincipal(accessRequest, principals.data)?.label}</Text>}
                <Text className={styles.codeValue}>{accessRequest.requesterObjectId}</Text>
              </dd></div>
              <div><dt>Cost center</dt><dd>{accessRequest.costCenter ? `${accessRequest.costCenter.name} (${accessRequest.costCenter.code})` : 'Subject default'}</dd></div>
              <div><dt>Requested environment</dt><dd><EnvironmentBadge azureCloud={accessRequest.resourceSnapshot?.azureCloud ?? 'commercial'} environment={accessRequest.requestedEnvironment ?? null} catalog={environmentCatalog.data} /></dd></div>
              <div><dt>Current environment</dt><dd><EnvironmentBadge azureCloud={accessRequest.resourceSummary?.azureCloud ?? 'commercial'} environment={accessRequest.resourceSummary?.environment ?? null} catalog={environmentCatalog.data} />
                {(accessRequest.requestedEnvironment ?? null) !== (accessRequest.resourceSummary?.environment ?? null) && <Text block>Now {environmentLabel(environmentCatalog.data, accessRequest.resourceSummary?.environment ?? null)}</Text>}
              </dd></div>
              <div><dt>Justification</dt><dd>{accessRequest.justification ?? 'No justification given'}</dd></div>
              <div><dt>Status</dt><dd><Badge appearance="tint">{accessRequest.state}</Badge></dd></div>
              <div><dt>Requested</dt><dd>{accessRequest.createdAt}</dd></div>
              <div><dt>Updated</dt><dd>{accessRequest.updatedAt}</dd></div>
              <div><dt>Request ID</dt><dd>{accessRequest.id}</dd></div>
              {accessRequest.decidedAt && <div><dt>Decided</dt><dd>{accessRequest.decidedAt}</dd></div>}
              {accessRequest.decidedByObjectId && <div><dt>Decided by</dt><dd>{accessRequest.decidedByObjectId}</dd></div>}
              {accessRequest.decisionNote && <div><dt>Decision note</dt><dd>{accessRequest.decisionNote}</dd></div>}
            </dl>
            {accessRequest.resourceSummary?.available === false && <Text>No longer available</Text>}
            {accessRequest.grantedEntitlementId && <Button onClick={() => showGrant(accessRequest.grantedEntitlementId!)}>Go to grant</Button>}
                          <RowActions
                            label={`More actions for request from ${requestingPrincipal(accessRequest, principals.data)?.label ?? accessRequest.requesterObjectId}`}
                            primary={<Button
                              appearance="primary"
                              size="small"
                              disabled={accessRequest.state !== 'pending' || accessRequest.resourceSummary?.available === false || !approvalReady || approveMutation.isPending || denyMutation.isPending}
                              onClick={() => openApproval(accessRequest)}
                            >
                              Approve
                            </Button>}
                            destructiveActions={[{
                              label: 'Deny',
                              disabled: accessRequest.state !== 'pending' || approveMutation.isPending || denyMutation.isPending,
                              onClick: () => denyMutation.mutate(accessRequest.id),
                            }]}
                          />
          </div>}
        />}

        {activeSection === 'effective' && <Card className={styles.detailCard}>
          <div className={styles.detailHeader}>
            <Title3 as="h2">Resolved desired access</Title3>
          </div>
          <Text size={200}>
            Recorded direct, MOSAIC group, and security-group intent for a principal. Access through
            security groups is found with Microsoft Graph.
          </Text>
          <Field label="Principal">
            <Select
              value={inspectedPrincipal}
              onChange={(_, data) => setInspectedPrincipal(data.value)}
            >
              <option value="">Select a principal</option>
              {(principals.data ?? []).map((principal) => (
                <option key={principal.id} value={principal.id}>
                  {principal.label ?? principal.objectId}
                </option>
              ))}
            </Select>
          </Field>
          {resolved.isError && <ErrorState error={resolved.error} />}
          {inspectedPrincipal && resolved.isPending && <Loading label="Resolving desired access" />}
          {resolved.isSuccess &&
            (resolved.data.length === 0 ? (
              <Text size={200}>Nothing has been granted to this principal.</Text>
            ) : (
              <dl className={styles.detailList}>
                {resolved.data.map((item) => (
                  <div key={item.entitlement.id}>
                    <dt>
                      {resourceLabel(item.entitlement.resource) ?? item.entitlement.resource.id}
                    </dt>
                    <dd>
                      {grantPathLabel(item)}
                      {item.costCenter && ` · ${item.costCenter.name} (${item.costCenter.code})`}
                      {' · '}
                      {!item.entitlement.enabled
                        ? 'Disabled'
                        : item.effective
                          ? 'Applies'
                          : overriddenByLabel(
                            resolved.data.find((other) => other.entitlement.id === item.shadowedBy),
                          )}
                    </dd>
                  </div>
                ))}
              </dl>
            ))}
        </Card>}
      </SectionLayout>

      <Dialog open={dialogOpen && (mode !== 'edit' || Boolean(editingGrant))}
        title={mode === 'edit' ? 'Edit grant' : 'New grant'}
        description="Save desired access intent. Nothing changes in API Management until the affected resource plan is reviewed and applied."
        pending={createMutation.isPending || editMutation.isPending}
        onOpenChange={(_, data) => { if (!data.open) closeEditor() }}>
        <DialogSurface>
          <form onSubmit={submit}>
            <DialogBody>
              <DialogContent className={styles.dialogForm}>
                {createMutation.isError && <ErrorState error={createMutation.error} />}
                {editMutation.isError && <ErrorState error={editMutation.error} />}
                {newGrantLocked && <MessageBar intent="warning"><MessageBarBody>Access is applying or unknown. Recover the resource before saving grant intent.</MessageBarBody></MessageBar>}
                {editingGrant?.enforcement?.requests?.callQuota && <Text>Existing call quota is retained when saving: {describeLimits(editingGrant).filter((sentence) => sentence.startsWith('Allows')).join(' ')}</Text>}
                {editingGrant && grantLocked(editingGrant) && <ErrorState error={new Error('Access is applying or unknown. Recover the resource before saving grant intent.')} />}
                {directModelId && <Text>Direct model grant: saving does not activate access. Review and apply the complete model plan afterward.</Text>}
                <Field label="Subject" required>
                  <Select
                    value={form.subject}
                    disabled={mode === 'edit'}
                    onChange={(_, data) => {
                      setForm({
                        ...form,
                        subject: data.value,
                        costCenter: '',
                      })
                    }}
                  >
                    <option value="">Select a user, group, or application</option>
                    {editingGrant && !subjectOptions.some((item) => item.id === editingGrant.subject.id) && <option value={editingGrant.subject.id}>{editingGrant.subject.id}</option>}
                    {(['People', 'Agents', 'Security groups', 'Applications', 'MOSAIC groups'] as const).map((group) => {
                    const grouped = subjectOptions.filter(
                      (option) => option.group === group && (!directModelId || option.kind !== 'group'),
                    )
                    return grouped.length ? (
                      <optgroup key={group} label={group}>
                        {grouped.map((option) => (
                          <option key={option.id} value={option.id}>
                            {option.label}
                          </option>
                        ))}
                      </optgroup>
                    ) : null
                    })}
                  </Select>
                </Field>
                <Field label="Resource" required>
                  <Select
                    value={form.resource}
                    disabled={Boolean(directModelId) || mode === 'edit'}
                    onChange={(_, data) => {
                      const mcp = resourceOptions.find((option) => option.value === data.value)?.kind === 'mcpServer'
                      // MCP servers are limited by calls, never tokens. Clear token limits the
                      // form no longer shows, so none are sent or silently dropped.
                      setForm(mcp
                        ? { ...form, resource: data.value, tokensPerMinute: '', tokenQuota: '' }
                        : { ...form, resource: data.value })
                    }}
                  >
                    <option value="">Select a model API, pool model, or MCP server</option>
                    {editingGrant && !resourceOptions.some((item) => item.value === resourceKey(editingGrant.resource)) && <option value={resourceKey(editingGrant.resource)}>{editingGrant.resource.id}</option>}
                    {resourceOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Cost center" hint="Defaults to the subject's default cost center when MOSAIC knows it. The service validates membership.">
                  <Select
                    value={form.costCenter}
                    disabled={mode === 'edit'}
                    onChange={(_, data) => setForm({ ...form, costCenter: data.value })}
                  >
                    <option value="">Subject default cost center</option>
                    {(costCenters.data ?? []).map((costCenter) => (
                      <option key={costCenter.id} value={costCenter.id}>
                        {costCenter.name} ({costCenter.code})
                      </option>
                    ))}
                  </Select>
                </Field>
                {mcpSelected ? (
                  <Text size={200}>
                    MCP servers are limited by calls, not tokens. Leave the call rate empty to add
                    no grant-specific limit.
                  </Text>
                ) : (
                  <>
                    {poolSelected ? (
                      <Text size={200}>
                        Leave a limit empty to add no grant-specific restriction. The pool&apos;s
                        shared token limit still applies; this does not mean unrestricted gateway
                        access.
                        {selectedPool && !selectedPool.governedAccess
                          && ` ${selectedPool.displayName} doesn't use governed access yet, so this grant waits until it does.`}
                      </Text>
                    ) : (
                      <Text size={200}>
                        Leave a limit empty to add no grant-specific restriction. Inherited publication
                        safeguards still apply; this does not mean unrestricted gateway access.
                      </Text>
                    )}
                    <div className={styles.dialogGrid}>
                      <Field label="Tokens per minute">
                        <Input
                          type="number"
                          min={1}
                          value={form.tokensPerMinute}
                          onChange={(_, data) => setForm({ ...form, tokensPerMinute: data.value })}
                        />
                      </Field>
                      <Field label="Token quota">
                        <Input
                          type="number"
                          min={1}
                          value={form.tokenQuota}
                          onChange={(_, data) => setForm({ ...form, tokenQuota: data.value })}
                        />
                      </Field>
                      <Field label="Quota period">
                        <Select
                          value={form.tokenQuotaPeriod}
                          onChange={(_, data) =>
                            setForm({ ...form, tokenQuotaPeriod: data.value as QuotaPeriod })
                          }
                        >
                          {QUOTA_PERIODS.map((period) => (
                            <option key={period} value={period}>
                              {period}
                            </option>
                          ))}
                        </Select>
                      </Field>
                    </div>
                  </>
                )}
                <div className={styles.switchGrid}>
                  <Field label="Calls" validationMessage={rateError ?? undefined}>
                    <Input
                      type="number"
                      min={1}
                      value={form.calls}
                      onChange={(_, data) => setForm({ ...form, calls: data.value })}
                    />
                  </Field>
                  <Field label="Per how many seconds">
                    <Input
                      type="number"
                      min={1}
                      value={form.renewalPeriodSeconds}
                      onChange={(_, data) =>
                        setForm({ ...form, renewalPeriodSeconds: data.value })
                      }
                    />
                  </Field>
                </div>
                <Field label="Notes">
                  <Input
                    value={form.notes}
                    onChange={(_, data) => setForm({ ...form, notes: data.value })}
                  />
                </Field>
              </DialogContent>
              <DialogActions>
                <Button
                  appearance="primary"
                  type="submit"
                  disabled={
                    !form.subject ||
                    !form.resource ||
                    Boolean(rateError) ||
                    createMutation.isPending || editMutation.isPending ||
                    Boolean(editingGrant && grantLocked(editingGrant)) || newGrantLocked
                  }
                >
                  {mode === 'edit' ? 'Save grant' : 'Grant access'}
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>
      {dialogOpen && mode === 'edit' && !editingGrant && <EmptyState title="Grant unavailable"><Button onClick={() => closeEditor()}>Back to list</Button></EmptyState>}
      <PublishModelDialog
        open={activeSection === 'settings' && mode === 'review' && review?.publication.id === selectedPublicationId}
        initialReview={review}
        onClose={() => { setReview(null); closeEditor() }}
        onPublished={announce}
      />
      {activeSection === 'requests' && mode === 'review' && approving && approving.accessRequest.id === selectedId && (
        <ApproveAccessRequestDialog
          key={approving.accessRequest.id}
          accessRequest={approving.accessRequest}
          requester={approving.requester}
          resourceLabel={approving.resourceLabel}
          costCenters={costCenters.data ?? []}
          environmentCatalog={environmentCatalog.data}
          publication={approving.publication}
          pool={approving.pool}
          governed={approving.governed}
          existingGrant={approving.existingGrant}
          locked={grantLocked(approving.accessRequest)}
          pending={approveMutation.isPending}
          error={approveMutation.error}
          onCancel={() => closeEditor()}
          onApprove={(approval) => approveMutation.mutate({ context: approving, approval })}
        />
      )}
      {activeSection === 'grants' && !mode && connectionGrant && connectionGrant.id === selectedId && (
        <EntitlementConnectionDialog
          open
          entitlement={connectionGrant}
          onClose={() => setConnectionGrantId(null)}
        />
      )}
    </section>
  )
}
