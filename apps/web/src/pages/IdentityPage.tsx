import {
  Badge,
  Button,
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
  Text,
  Textarea,
  Title3,
} from '@fluentui/react-components'
import { BotRegular, PeopleRegular, PersonRegular, ShieldRegular, AppsRegular } from '@fluentui/react-icons'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { DirectoryGroupVerification } from '../components/DirectoryGroupVerification'
import { DirectoryPrincipalPicker } from '../components/DirectoryPrincipalPicker'
import { IdentityAvatar } from '../components/IdentityAvatar'
import { PageHeader } from '../components/PageHeader'
import { PrincipalKindBadge } from '../components/PrincipalKindBadge'
import { SectionLayout } from '../components/SectionLayout'
import { ResourceWorkbench, type ResourceWorkbenchProps } from '../components/ResourceWorkbench'
import { WorkspaceEditorFrame } from '../components/WorkspaceEditorFrame'
import {
  WorkspaceDialog,
  WorkspaceDialogSurface,
  WorkspaceDialogBody,
  WorkspaceDialogContent,
  WorkspaceDialogActions,
} from '../components/WorkspaceDialog'
import { useWorkbenchLocation } from '../components/useWorkbenchLocation'
import { completeWorkspaceNavigation } from '../components/WorkspaceLeaveGuard'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import { invalidateCostCenterMembership } from '../cost-centers'
import type { DirectorySearchKind, Principal, PrincipalKind } from '../types'
import styles from './IdentityPage.module.css'

type PrincipalTab = 'users' | 'agents' | 'applications' | 'security-groups'
type IdentityTab = PrincipalTab | 'groups'
type PrincipalDialogView = 'search' | 'manual'

const IDENTITY_SECTIONS = [
  { value: 'users', label: 'People', icon: <PersonRegular />, description: 'People referenced from Microsoft Entra. Labels and defaults are stored in MOSAIC; adding a reference does not create an Entra account.' },
  { value: 'agents', label: 'Agents', icon: <BotRegular />, description: 'Microsoft Entra agent identities and their paired agent users. MOSAIC records references, not new agents.' },
  { value: 'applications', label: 'Applications & managed identities', icon: <AppsRegular />, description: 'Application service principals and Azure managed identities from Microsoft Entra. Registration in MOSAIC does not grant an Entra application role.' },
  { value: 'security-groups', label: 'Entra security groups', icon: <ShieldRegular />, description: 'Security-enabled groups whose membership is managed in Microsoft Entra. Group grants use validated token claims, not MOSAIC-local membership.' },
  { value: 'groups', label: 'MOSAIC groups', icon: <PeopleRegular />, description: 'Local collections and managed cost-center rosters. Creating a MOSAIC group does not create or change an Entra group or grant model access.' },
] as const

function principalTabForKind(kind: PrincipalKind): PrincipalTab {
  if (kind === 'user') return 'users'
  if (kind === 'agentIdentity' || kind === 'agentUser') return 'agents'
  return kind === 'securityGroup' ? 'security-groups' : 'applications'
}

const PRINCIPAL_TAB_TEXT: Record<
  PrincipalTab,
  {
    title: string
    add: string
    emptyTitle: string
    emptyHint: string
    selectPrompt: string
    filter: string
  }
> = {
  users: {
    title: 'People',
    add: 'Add person',
    emptyTitle: 'No people registered',
    emptyHint: 'Add a person to begin.',
    selectPrompt: 'Select a person',
    filter: 'Filter by label or object ID',
  },
  agents: {
    title: 'Agents',
    add: 'Add agent',
    emptyTitle: 'No agents registered',
    emptyHint: 'Add an agent identity or agent user to begin.',
    selectPrompt: 'Select an agent',
    filter: 'Filter by label, detail, object ID, or kind',
  },
  applications: {
    title: 'Applications & managed identities',
    add: 'Add application or managed identity',
    emptyTitle: 'No applications or managed identities registered',
    emptyHint: 'Add an application or managed identity to begin.',
    selectPrompt: 'Select a record',
    filter: 'Filter by label, detail, object ID, or kind',
  },
  'security-groups': {
    title: 'Entra security groups',
    add: 'Add security group',
    emptyTitle: 'No Entra security groups registered',
    emptyHint: 'Reference a security-enabled Entra group to begin.',
    selectPrompt: 'Select a record',
    filter: 'Filter by label, detail, object ID, or kind',
  },
}

/** Directory search and deliberate manual fallback follow the current identity section. */
const ADD_DIALOG_DEFAULTS: Record<IdentityTab, { search: DirectorySearchKind; manual: PrincipalKind }> = {
  users: { search: 'user', manual: 'user' },
  agents: { search: 'agent', manual: 'agentIdentity' },
  applications: { search: 'application', manual: 'servicePrincipal' },
  'security-groups': { search: 'group', manual: 'securityGroup' },
  groups: { search: 'user', manual: 'user' },
}

/** Manual entry picks up the kind the administrator was searching for. */
const MANUAL_KIND_FOR_SEARCH: Record<DirectorySearchKind, PrincipalKind> = {
  user: 'user',
  agent: 'agentIdentity',
  group: 'securityGroup',
  application: 'servicePrincipal',
}

const SEARCH_DIALOG_TITLES: Record<DirectorySearchKind, string> = {
  user: 'Add person',
  agent: 'Add agent',
  group: 'Add security group',
  application: 'Add application or managed identity',
}

type ConfirmationState =
  | {
      type: 'principal'
      principalId: string
      principalName: string
    }
  | {
      type: 'group'
      groupId: string
      groupName: string
    }
  | {
      type: 'membership'
      groupId: string
      groupName: string
      principalId: string
      principalName: string
      managedCostCenter?: boolean
    }
  | null

function isIdentityTab(value: string | null): value is IdentityTab {
  return IDENTITY_SECTIONS.some((section) => section.value === value)
}

function matchesSearch(search: string, ...values: Array<string | undefined>) {
  if (!search) {
    return true
  }
  return values.some((value) => value?.toLowerCase().includes(search))
}

function principalMatchesSearch(principal: Principal, tab: PrincipalTab, query: string) {
  const search = query.trim().toLowerCase()
  return tab === 'users'
    ? matchesSearch(search, principal.label, principal.objectId)
    : matchesSearch(
        search,
        principal.label,
        principal.objectId,
        principal.detail ?? undefined,
        PRINCIPAL_KIND_LABELS[principal.kind],
      )
}

function getPrincipalName(principal: Principal) {
  return principal.label?.trim() || principal.objectId
}

function formatTimestamp(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString()
}

function IdentityWorkbench<T>({
  editing, dirty, pending, onBack, editorTitle, editorCancelLabel, children, ...workbench
}: Omit<ResourceWorkbenchProps<T>, 'renderDetail'> & {
  editing: boolean
  dirty: boolean
  pending: boolean
  onBack: () => void
  editorTitle: string
  editorCancelLabel?: string
  children: (requestCancel: () => void) => ReactNode
}) {
  return editing
    ? <WorkspaceEditorFrame title={editorTitle} onBack={onBack} onCancel={onBack} cancelLabel={editorCancelLabel}
        dirty={dirty} pending={pending}>
        {(requestLeave) => children(() => requestLeave(onBack))}
      </WorkspaceEditorFrame>
    : <ResourceWorkbench {...workbench} renderDetail={() => children(onBack)} />
}

export function IdentityPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const searchParams = useMemo(() => new URLSearchParams(location.search), [location.search])
  const rawTab = searchParams.get('tab')
  const activeTab: IdentityTab = rawTab === 'workloads' ? 'applications' : isIdentityTab(rawTab) ? rawTab : 'users'
  const workbench = useWorkbenchLocation({ section: activeTab })
  const activeMutations = useIsMutating()

  const [searchQuery, setSearchQuery] = useState('')
  const requestedPrincipalId = activeTab === 'groups' ? '' : workbench.selectedId ?? ''
  const selectedGroupId = activeTab === 'groups' ? workbench.selectedId ?? '' : ''
  const principalDialogOpen = activeTab !== 'groups' && workbench.mode === 'new'
  const createGroupOpen = activeTab === 'groups' && workbench.mode === 'new'
  const detailEditing = workbench.mode === 'edit'
  const [principalDialogView, setPrincipalDialogView] = useState<PrincipalDialogView>('search')
  const [directoryKind, setDirectoryKind] = useState<DirectorySearchKind>('user')
  const [createPrincipalObjectId, setCreatePrincipalObjectId] = useState('')
  const [createPrincipalLabel, setCreatePrincipalLabel] = useState('')
  const [createPrincipalKind, setCreatePrincipalKind] = useState<PrincipalKind>('user')
  const [createPrincipalDefaultCostCenterId, setCreatePrincipalDefaultCostCenterId] = useState('')
  const [createPrincipalParentId, setCreatePrincipalParentId] = useState('')
  const [createGroupName, setCreateGroupName] = useState('')
  const [createGroupDescription, setCreateGroupDescription] = useState('')
  const [principalDraftLabel, setPrincipalDraftLabel] = useState('')
  const [principalDraftKind, setPrincipalDraftKind] = useState<PrincipalKind>('user')
  const [principalDraftDefaultCostCenterId, setPrincipalDraftDefaultCostCenterId] = useState('')
  const [groupDraftDescription, setGroupDraftDescription] = useState('')
  const [principalToAdd, setPrincipalToAdd] = useState('')
  const [confirmation, setConfirmation] = useState<ConfirmationState>(null)
  const [saveCommitted, setSaveCommitted] = useState(false)
  const editButtonRef = useRef<HTMLButtonElement>(null)
  const editFormRef = useRef<HTMLFormElement>(null)
  const previousEditing = useRef(false)
  const draftedPrincipalId = useRef<string | undefined>(undefined)
  const draftedGroupId = useRef<string | undefined>(undefined)
  const principalWorkspaceTab = useRef<IdentityTab | null>(null)

  useEffect(() => {
    if (workbench.selectedId) {
      if (detailEditing) {
        editFormRef.current?.querySelector<HTMLInputElement | HTMLTextAreaElement>('input, textarea')?.focus()
      } else if (previousEditing.current) {
        editButtonRef.current?.focus()
      }
    }
    previousEditing.current = detailEditing
  }, [detailEditing, workbench.selectedId])

  const principals = useQuery({ queryKey: ['principals'], queryFn: api.listPrincipals })
  const costCenters = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters })
  const costCenterSettings = useQuery({ queryKey: ['cost-center-settings'], queryFn: api.getCostCenterSettings })
  const groups = useQuery({ queryKey: ['groups'], queryFn: api.listGroups })
  const mcpPublications = useQuery({
    queryKey: ['mcp-publications'],
    queryFn: () => api.listMcpPublications(),
  })
  const directoryStatus = useQuery({
    queryKey: ['directory', 'status'],
    queryFn: () => api.getDirectoryStatus(),
  })
  const memberships = useQuery({
    queryKey: ['memberships', selectedGroupId],
    queryFn: () => api.listMemberships(selectedGroupId),
    enabled: activeTab === 'groups' && Boolean(selectedGroupId),
  })

  const updateTab = useCallback(
    (nextTab: IdentityTab, replace = false) => {
      const nextParams = new URLSearchParams(location.search)
      nextParams.set('tab', nextTab)
      nextParams.delete('selected')
      nextParams.delete('workbench')
      nextParams.delete('mode')
      navigate(
        {
          pathname: location.pathname,
          search: `?${nextParams.toString()}`,
        },
        { replace },
      )
    },
    [location.pathname, location.search, navigate],
  )

  useEffect(() => {
    if (!isIdentityTab(rawTab)) {
      const nextParams = new URLSearchParams(location.search)
      nextParams.set('tab', activeTab)
      navigate({ pathname: location.pathname, search: `?${nextParams}` }, { replace: true, preventScrollReset: true })
    }
  }, [activeTab, rawTab, location.pathname, location.search, navigate])

  // Open the tab that lists the principal, and select it there.
  const showPrincipal = useCallback(
    (principal: Principal) => {
      const nextTab = principalTabForKind(principal.kind)
      setSearchQuery((current) => (principalMatchesSearch(principal, nextTab, current) ? current : ''))
      setSaveCommitted(true)
      const nextParams = new URLSearchParams(location.search)
      nextParams.set('tab', nextTab)
      nextParams.set('selected', principal.id)
      nextParams.delete('workbench')
      nextParams.delete('mode')
      completeWorkspaceNavigation(() => navigate({ pathname: location.pathname, search: `?${nextParams}` }, { replace: true }))
    },
    [location.pathname, location.search, navigate],
  )

  const createPrincipal = useMutation({
    mutationFn: api.createPrincipal,
    onSuccess: async (principal) => {
      setCreatePrincipalObjectId('')
      setCreatePrincipalLabel('')
      setCreatePrincipalParentId('')
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      showPrincipal(principal)
    },
  })

  const updatePrincipal = useMutation({
    mutationFn: ({
      principalId,
      payload,
    }: {
      principalId: string
      payload: { kind?: PrincipalKind; label?: string | null; defaultCostCenterId?: string | null }
    }) => api.updatePrincipal(principalId, payload),
    onSuccess: async (principal) => {
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      showPrincipal(principal)
    },
  })

  const deletePrincipal = useMutation({
    mutationFn: api.deletePrincipal,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      completeWorkspaceNavigation(() => workbench.select(null))
    },
  })

  const createGroup = useMutation({
    mutationFn: api.createGroup,
    onSuccess: async (group) => {
      setCreateGroupName('')
      setCreateGroupDescription('')
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      setSaveCommitted(true)
      completeWorkspaceNavigation(() => workbench.close(group.id))
    },
  })
  const resetCreatePrincipal = createPrincipal.reset
  const resetCreateGroup = createGroup.reset

  useEffect(() => {
    if (!principalDialogOpen) {
      principalWorkspaceTab.current = null
      return
    }
    if (principalWorkspaceTab.current === activeTab) return
    principalWorkspaceTab.current = activeTab
    const defaults = ADD_DIALOG_DEFAULTS[activeTab]
    setDirectoryKind(defaults.search)
    setCreatePrincipalKind(defaults.manual)
    setPrincipalDialogView('search')
    setCreatePrincipalObjectId('')
    setCreatePrincipalLabel('')
    setCreatePrincipalParentId('')
    setCreatePrincipalDefaultCostCenterId(costCenterSettings.data?.defaultCostCenterId ?? '')
    setSaveCommitted(false)
    resetCreatePrincipal()
  }, [principalDialogOpen, activeTab, costCenterSettings.data?.defaultCostCenterId, resetCreatePrincipal])

  useEffect(() => {
    if (!createGroupOpen) return
    setCreateGroupName('')
    setCreateGroupDescription('')
    setSaveCommitted(false)
    resetCreateGroup()
  }, [createGroupOpen, resetCreateGroup])

  const updateGroup = useMutation({
    mutationFn: ({
      groupId,
      payload,
    }: {
      groupId: string
      payload: { description?: string | null }
    }) => api.updateGroup(groupId, payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      setSaveCommitted(true)
      completeWorkspaceNavigation(() => workbench.close())
    },
  })

  const deleteGroup = useMutation({
    mutationFn: api.deleteGroup,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      if (selectedGroupId) {
        await queryClient.removeQueries({ queryKey: ['memberships', selectedGroupId] })
      }
      completeWorkspaceNavigation(() => workbench.select(null))
    },
  })

  const addMembership = useMutation({
    mutationFn: ({ groupId, principalId }: { groupId: string; principalId: string }) =>
      api.addMembership(groupId, principalId),
    onSuccess: async (_, { groupId }) => {
      setPrincipalToAdd('')
      await refreshMembership(groupId)
    },
  })

  const removeMembership = useMutation({
    mutationFn: ({ groupId, principalId }: { groupId: string; principalId: string }) =>
      api.removeMembership(groupId, principalId),
    onSuccess: async (_, { groupId }) => {
      await refreshMembership(groupId)
    },
  })

  async function refreshMembership(groupId: string) {
    const group = groups.data?.find((item) => item.id === groupId)
    if (group?.purpose === 'costCenter') {
      await invalidateCostCenterMembership(queryClient, groupId)
    } else {
      await queryClient.invalidateQueries({ queryKey: ['memberships', groupId] })
    }
  }

  const normalizedSearch = searchQuery.trim().toLowerCase()
  const principalsByTab = useMemo(() => {
    const grouped: Record<PrincipalTab, Principal[]> = { users: [], agents: [], applications: [], 'security-groups': [] }
    for (const principal of principals.data ?? []) {
      grouped[principalTabForKind(principal.kind)].push(principal)
    }
    return grouped
  }, [principals.data])
  // The MOSAIC groups tab doesn't show principals, so the lookup only needs a valid key there.
  const principalTab: PrincipalTab = activeTab === 'groups' ? 'users' : activeTab
  const principalTabText = PRINCIPAL_TAB_TEXT[principalTab]
  const tabPrincipals = principalsByTab[principalTab]
  const visiblePrincipals = useMemo(
    () => tabPrincipals.filter((principal) => principalMatchesSearch(principal, principalTab, normalizedSearch)),
    [normalizedSearch, principalTab, tabPrincipals],
  )
  const filteredGroups = useMemo(
    () =>
      groups.data?.filter((group) =>
        matchesSearch(normalizedSearch, group.name, group.description),
      ) ?? [],
    [groups.data, normalizedSearch],
  )
  const principalsById = useMemo(
    () => new Map((principals.data ?? []).map((principal) => [principal.id, principal])),
    [principals.data],
  )
  const costCenterById = useMemo(
    () => new Map((costCenters.data ?? []).map((costCenter) => [costCenter.id, costCenter])),
    [costCenters.data],
  )

  const selectedPrincipalId = requestedPrincipalId

  const selectedPrincipal = principals.data?.find((principal) =>
    principal.id === selectedPrincipalId && principalTabForKind(principal.kind) === principalTab,
  )
  const selectedGroup = groups.data?.find((group) => group.id === selectedGroupId)
  const managedGroup = selectedGroup?.purpose === 'costCenter'
  const owningCostCenterId = managedGroup ? selectedGroup?.costCenterId : null
  const groupEditing = detailEditing && (!managedGroup || Boolean(owningCostCenterId))
  const selectedPrincipalMembers = useQuery({
    queryKey: ['principals', selectedPrincipal?.id, 'members'],
    queryFn: () => api.listPrincipalMembers(selectedPrincipal?.id ?? ''),
    enabled: Boolean(selectedPrincipal && selectedPrincipal.kind === 'securityGroup'),
  })

  useEffect(() => {
    if (detailEditing && draftedPrincipalId.current === selectedPrincipal?.id) return
    draftedPrincipalId.current = selectedPrincipal?.id
    setPrincipalDraftLabel(selectedPrincipal?.label ?? '')
    setPrincipalDraftKind(selectedPrincipal?.kind ?? 'user')
    setPrincipalDraftDefaultCostCenterId(selectedPrincipal?.defaultCostCenterId ?? '')
  }, [selectedPrincipal, detailEditing])

  useEffect(() => {
    if (detailEditing && draftedGroupId.current === selectedGroup?.id) return
    draftedGroupId.current = selectedGroup?.id
    setGroupDraftDescription(selectedGroup?.description ?? '')
    setPrincipalToAdd('')
  }, [selectedGroup, detailEditing])

  const memberIds = useMemo(
    () => new Set(memberships.data?.map((membership) => membership.principalId) ?? []),
    [memberships.data],
  )
  const availablePrincipals = useMemo(
    () =>
      [...(principals.data ?? [])]
        .filter((principal) => (managedGroup || principal.kind !== 'securityGroup') && !memberIds.has(principal.id))
        .sort((left, right) =>
          getPrincipalName(left).localeCompare(getPrincipalName(right), undefined, {
            sensitivity: 'base',
          }),
        ),
    [managedGroup, memberIds, principals.data],
  )
  const mcpServersByModelCaller = useMemo(() => {
    const map = new Map<string, string[]>()
    if (!mcpPublications.isSuccess) return map
    for (const publication of mcpPublications.data) {
      if (!publication.modelCallerId) continue
      const names = map.get(publication.modelCallerId) ?? []
      names.push(publication.displayName)
      map.set(publication.modelCallerId, names)
    }
    // The same order on every refresh, whatever order the publications arrive in.
    for (const [principalId, names] of map) {
      map.set(
        principalId,
        [...new Set(names)].sort((left, right) => left.localeCompare(right, undefined, { sensitivity: 'base' })),
      )
    }
    return map
  }, [mcpPublications.data, mcpPublications.isSuccess])
  const filteredMemberships = useMemo(
    () =>
      (memberships.data ?? []).filter((membership) => {
        const principal = principalsById.get(membership.principalId)
        return matchesSearch(
          normalizedSearch,
          principal?.label,
          principal?.objectId,
          principal ? PRINCIPAL_KIND_LABELS[principal.kind] : undefined,
        )
      }),
    [memberships.data, normalizedSearch, principalsById],
  )

  const directoryLookupEnabled = directoryStatus.data?.lookupEnabled === true
  const effectivePrincipalDialogView = principalDialogView
  // The title names what the dialog will add, so it follows the search or type choice.
  const principalDialogTitle =
    effectivePrincipalDialogView === 'search'
      ? SEARCH_DIALOG_TITLES[directoryKind]
      : `Add ${PRINCIPAL_KIND_LABELS[createPrincipalKind].toLowerCase()}`
  const principalCreateHint =
    effectivePrincipalDialogView === 'search'
      ? 'Search Microsoft Entra and add a reference to MOSAIC. Nothing is created or changed in Entra.'
      : directoryLookupEnabled
        ? 'Enter the Entra object ID and type. Manual entry does not bypass required directory verification while lookup is enabled. Application and managed-identity references remain unverified.'
        : directoryStatus.isError
          ? 'Directory status could not be loaded. Manual entry is available; retry to check lookup availability.'
          : 'Directory lookup is off. Enter the Entra object ID and kind manually.'

  const groupSearchPlaceholder = 'Filter groups or visible members'

  const principalDetailHasChanges =
    selectedPrincipal !== undefined &&
    (principalDraftKind !== selectedPrincipal.kind ||
      principalDraftLabel.trim() !== (selectedPrincipal.label ?? '') ||
      principalDraftDefaultCostCenterId !== (selectedPrincipal.defaultCostCenterId ?? ''))

  const groupDetailHasChanges =
    !managedGroup &&
    selectedGroup !== undefined &&
    groupDraftDescription.trim() !== (selectedGroup.description ?? '')

  function cancelEditing() {
    setPrincipalDraftLabel(selectedPrincipal?.label ?? '')
    setPrincipalDraftKind(selectedPrincipal?.kind ?? 'user')
    setPrincipalDraftDefaultCostCenterId(selectedPrincipal?.defaultCostCenterId ?? '')
    setGroupDraftDescription(selectedGroup?.description ?? '')
    workbench.close()
  }

  const principalDetailError = updatePrincipal.error ?? deletePrincipal.error
  const groupDetailError =
    updateGroup.error ?? deleteGroup.error ?? addMembership.error ?? removeMembership.error

  function openPrincipalDialog() {
    const defaults = ADD_DIALOG_DEFAULTS[activeTab]
    createPrincipal.reset()
    setPrincipalDialogView('search')
    setSaveCommitted(false)
    workbench.open('new')
    setDirectoryKind(defaults.search)
    setCreatePrincipalObjectId('')
    setCreatePrincipalLabel('')
    setCreatePrincipalParentId('')
    setCreatePrincipalKind(defaults.manual)
    setCreatePrincipalDefaultCostCenterId(costCenterSettings.data?.defaultCostCenterId ?? '')
  }

  function closePrincipalDialog() {
    createPrincipal.reset()
    workbench.close()
  }

  function closeGroupDialog() {
    createGroup.reset()
    workbench.close()
  }

  function submitCreatePrincipal(event: FormEvent) {
    event.preventDefault()
    if (!createPrincipalObjectId.trim() || createPrincipal.isPending) return
    createPrincipal.mutate({
      objectId: createPrincipalObjectId.trim(),
      kind: createPrincipalKind,
      label: createPrincipalLabel.trim() || undefined,
      identityParentId:
        createPrincipalKind === 'agentUser' && createPrincipalParentId.trim()
          ? createPrincipalParentId.trim()
          : undefined,
      ...(createPrincipalKind !== 'securityGroup' && createPrincipalDefaultCostCenterId
          ? { defaultCostCenterId: createPrincipalDefaultCostCenterId }
          : {}),
    })
  }

  function submitPrincipalDetails(event: FormEvent) {
    event.preventDefault()
    if (!selectedPrincipal || !principalDetailHasChanges) {
      return
    }
    updatePrincipal.mutate({
      principalId: selectedPrincipal.id,
      payload: {
        kind: principalDraftKind,
        label: principalDraftLabel.trim() || null,
        defaultCostCenterId: principalDraftKind === 'securityGroup' ? null : (principalDraftDefaultCostCenterId || null),
      },
    })
  }

  function submitCreateGroup(event: FormEvent) {
    event.preventDefault()
    if (!createGroupName.trim() || createGroup.isPending) return
    createGroup.mutate({
      name: createGroupName.trim(),
      description: createGroupDescription.trim() || undefined,
    })
  }

  function submitGroupDetails(event: FormEvent) {
    event.preventDefault()
    if (!selectedGroup || !groupDetailHasChanges) {
      return
    }
    updateGroup.mutate({
      groupId: selectedGroup.id,
      payload: {
        description: groupDraftDescription.trim() || null,
      },
    })
  }

  function submitAddMembership(event: FormEvent) {
    event.preventDefault()
    if (!selectedGroup || !principalToAdd) {
      return
    }
    addMembership.mutate({ groupId: selectedGroup.id, principalId: principalToAdd })
  }

  function confirmDestructiveAction() {
    if (!confirmation) {
      return
    }
    if (confirmation.type === 'principal') {
      deletePrincipal.mutate(confirmation.principalId)
    } else if (confirmation.type === 'group') {
      deleteGroup.mutate(confirmation.groupId)
    } else {
      removeMembership.mutate({
        groupId: confirmation.groupId,
        principalId: confirmation.principalId,
      })
    }
    setConfirmation(null)
  }

  const identityFilter = <Input
    className={styles.searchInput}
    value={searchQuery}
    placeholder={activeTab === 'groups' ? groupSearchPlaceholder : principalTabText.filter}
    aria-label={activeTab === 'groups' ? groupSearchPlaceholder : principalTabText.filter}
    onChange={(_, data) => setSearchQuery(data.value)}
  />
  const identityActions = activeTab === 'groups'
    ? <Button appearance="primary" onClick={() => {
        createGroup.reset()
        setCreateGroupName('')
        setCreateGroupDescription('')
        setSaveCommitted(false)
        workbench.open('new')
      }}>Create MOSAIC group</Button>
    : <Button appearance="primary" aria-label={principalTabText.add} title={principalTabText.add}
        onClick={openPrincipalDialog}>
        {activeTab === 'applications' ? <>
          <span className={styles.applicationAction}>{principalTabText.add}</span>
          <span className={styles.compactApplicationAction}>Add application</span>
        </> : principalTabText.add}
      </Button>

  return (
    <section className={styles.page}>
      <PageHeader
        title="Identity"
        description="Manage registered identity references and local groups. Microsoft Entra remains authoritative for directory identities and security-group membership."
      />

      {directoryStatus.data?.groupClaimsEnabled === false && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Security-group grants are not enforceable yet</MessageBarTitle>
            MOSAIC can record security-group grants, but the gateway cannot enforce them until
            Entra group claims are configured for the model-runtime app.
          </MessageBarBody>
        </MessageBar>
      )}

      <SectionLayout label="Identity" sections={IDENTITY_SECTIONS} active={activeTab}
        onSelect={(value) => isIdentityTab(value) && value !== activeTab && updateTab(value)}>

      {activeTab === 'groups' ? (
        groups.isLoading || principals.isLoading ? (
          <Loading label="Loading groups and principals" />
        ) : groups.error || principals.error ? (
          <ErrorState error={groups.error ?? principals.error} />
        ) : (
          <IdentityWorkbench
            title="MOSAIC groups"
            description={`${filteredGroups.length} of ${groups.data?.length ?? 0} groups`}
            actions={identityActions}
            toolbar={identityFilter}
            items={filteredGroups}
            getId={(group) => group.id}
            selectedId={workbench.selectedId}
            onSelect={workbench.select}
            listLabel="MOSAIC groups"
            detailLabel={selectedGroup?.name ?? 'Group details'}
            renderListItem={(group) => <span className={styles.resourceRow}>
              <IdentityAvatar name={group.name} kind="securityGroup" />
              <span className={styles.rowName}>
                <span className={styles.rowPrimary}>{group.name}</span>
                <span className={styles.rowSecondary}>{group.description || 'No description'}</span>
              </span>
            </span>}
            emptyList={<EmptyState title={groups.data?.length ? 'No matching groups' : 'No groups configured'}>
              {groups.data?.length ? 'Refine the filter or create a new group.' : 'Create the first MOSAIC group to manage memberships.'}
            </EmptyState>}
            emptySelection={<EmptyState title="Select a group">Choose a group to inspect its description and memberships.</EmptyState>}
            missingSelection={<EmptyState title="Group not in this view">
              This group may be filtered out or no longer available.
              <Button onClick={() => { setSearchQuery(''); workbench.select(null) }}>Clear selection and filter</Button>
            </EmptyState>}
            editing={groupEditing}
            editorTitle={`${managedGroup ? 'Manage members for' : 'Edit'} ${selectedGroup?.name ?? 'group'}`}
            editorCancelLabel={managedGroup ? 'Done' : undefined}
            dirty={!saveCommitted && (groupDetailHasChanges || Boolean(principalToAdd))}
            pending={!saveCommitted && (updateGroup.isPending || deleteGroup.isPending || addMembership.isPending || removeMembership.isPending)}
            onBack={cancelEditing}>
            {(requestCancel) =>
            <div className={styles.detailCard}>
              {selectedGroup ? (
                <>
                  <div className={styles.detailHeader}>
                    <div className={styles.detailTitleBlock}>
                      <IdentityAvatar name={selectedGroup.name} kind="securityGroup" size={48} />
                      <Title3 as="h3">{selectedGroup.name}</Title3>
                      <Text className={styles.muted}>
                        {managedGroup
                          ? "This group shares its cost center's explicit roster. Membership permits charging that cost center; it does not grant model access."
                          : <>Organize identities in MOSAIC. The gateway doesn&apos;t enforce these
                            groups; to control access for a group, add an Entra security group and grant it.</>}
                      </Text>
                      <div className={styles.badgeRow}>
                        {managedGroup && <Badge appearance="tint" color="brand">Managed cost-center group</Badge>}
                        {memberships.data && <Badge appearance="tint" className={styles.groupBadge}>
                          {memberships.data.length} {managedGroup ? 'explicit reference' : 'member'}{memberships.data.length === 1 ? '' : 's'}
                        </Badge>}
                      </div>
                      {owningCostCenterId && <Button appearance="secondary"
                        onClick={() => navigate(`/cost-centers/${encodeURIComponent(owningCostCenterId)}?tab=members`)}>
                        Open owning cost center
                      </Button>}
                      {!detailEditing && (!managedGroup || selectedGroup.costCenterId) && <Button ref={editButtonRef} appearance="secondary" onClick={() => { setSaveCommitted(false); workbench.open('edit') }}>{managedGroup ? 'Edit members' : 'Edit group'}</Button>}
                    </div>
                    {!managedGroup && <Button
                      appearance="subtle"
                      className={styles.dangerButton}
                      disabled={
                        deleteGroup.isPending ||
                        updateGroup.isPending ||
                        addMembership.isPending ||
                        removeMembership.isPending ||
                        memberships.isLoading ||
                        Boolean(memberships.data?.length)
                      }
                      onClick={() =>
                        setConfirmation({
                          type: 'group',
                          groupId: selectedGroup.id,
                          groupName: selectedGroup.name,
                        })
                      }
                    >
                      Delete group
                    </Button>}
                  </div>

                  {managedGroup && !selectedGroup.costCenterId && <MessageBar intent="error">
                    <MessageBarBody>
                      <MessageBarTitle>Cost-center ownership unavailable</MessageBarTitle>
                      Refresh the group before editing its members.
                      <Button onClick={() => void groups.refetch()} disabled={groups.isFetching}>Refresh groups</Button>
                    </MessageBarBody>
                  </MessageBar>}

                  {groupDetailError && (
                    <MessageBar intent="error">
                      <MessageBarBody>
                        <MessageBarTitle>Unable to update group or memberships</MessageBarTitle>
                        {groupDetailError.message}
                      </MessageBarBody>
                    </MessageBar>
                  )}

                  <form ref={editFormRef} className={styles.detailSection} onSubmit={submitGroupDetails}>
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Authoritative source</Text>
                      <Text block className={styles.muted}>
                        {managedGroup
                          ? 'The owning cost center is authoritative for the name and description. Both membership views share one explicit roster; personal and tenant defaults are not group members.'
                          : 'MOSAIC stores desired-state access groups only. Entra groups remain unchanged.'}
                      </Text>
                    </div>

                    {groupEditing && !managedGroup ? <Field label="Description">
                      <Textarea
                        value={groupDraftDescription}
                        onChange={(_, data) => setGroupDraftDescription(data.value)}
                      />
                    </Field> : <Text>{selectedGroup.description || 'No description'}</Text>}

                    {groupEditing && !managedGroup && <div className={styles.formActions}>
                      <Text className={styles.helperText}>
                        Group names are set at creation time. Delete is disabled while members remain.
                      </Text>
                      <Button
                        appearance="primary"
                        type="submit"
                        disabled={!groupDetailHasChanges || updateGroup.isPending}
                      >
                        Save changes
                      </Button>
                      <Button onClick={requestCancel}>Cancel editing</Button>
                    </div>}
                  </form>

                  <div className={styles.detailSection}>
                    <Title3 as="h3">Memberships</Title3>

                    {groupEditing && managedGroup && <Text className={styles.helperText}>
                      Member changes are saved as you add or remove them.
                    </Text>}
                    {groupEditing && <form className={styles.inlineForm} onSubmit={submitAddMembership}>
                      <Field className={styles.memberField} label="Add principal">
                        <Select
                          value={principalToAdd}
                          disabled={!availablePrincipals.length || addMembership.isPending || removeMembership.isPending}
                          onChange={(event) => setPrincipalToAdd(event.target.value)}
                        >
                          <option value="">Select a principal</option>
                          {availablePrincipals.map((principal) => (
                            <option key={principal.id} value={principal.id}>
                              {getPrincipalName(principal)} — {PRINCIPAL_KIND_LABELS[principal.kind]}
                            </option>
                          ))}
                        </Select>
                      </Field>
                      <Button
                        className={styles.addMemberButton}
                        appearance="primary"
                        type="submit"
                        disabled={!principalToAdd || addMembership.isPending || removeMembership.isPending}
                      >
                        Add member
                      </Button>
                    </form>}
                    {!(principals.data?.length ?? 0) ? (
                      <Text className={styles.helperText}>
                        Register a person, agent, application, or managed identity before adding group members.
                      </Text>
                    ) : !availablePrincipals.length && (
                      <Text className={styles.helperText}>
                        All registered principals are already members of this group.
                      </Text>
                    )}

                    {memberships.isLoading ? (
                      <Loading label="Loading memberships" />
                    ) : memberships.error ? (
                      <ErrorState error={memberships.error} />
                    ) : !memberships.data?.length ? (
                      <EmptyState title="No members">
                        Add a registered principal to this group.
                      </EmptyState>
                    ) : filteredMemberships.length ? (
                      <div className={styles.memberList}>
                        {filteredMemberships.map((membership) => {
                          const principal = principalsById.get(membership.principalId)
                          const principalName = principal
                            ? getPrincipalName(principal)
                            : membership.principalId
                          return (
                            <div key={membership.id} className={styles.memberRow}>
                              <div className={styles.memberMeta}>
                                <Text className={styles.memberName}>{principalName}</Text>
                                {principal && (
                                  <div className={styles.badgeRow}>
                                    <PrincipalKindBadge kind={principal.kind} />
                                  </div>
                                )}
                                <code className={styles.monospace}>
                                  {principal?.objectId ?? membership.principalId}
                                </code>
                              </div>
                              {groupEditing && <Button
                                appearance="subtle"
                                className={styles.dangerButton}
                                disabled={addMembership.isPending || removeMembership.isPending}
                                onClick={() =>
                                  setConfirmation({
                                    type: 'membership',
                                    groupId: selectedGroup.id,
                                    groupName: selectedGroup.name,
                                    principalId: membership.principalId,
                                    principalName,
                                    managedCostCenter: managedGroup,
                                  })
                                }
                              >
                                Remove
                              </Button>}
                            </div>
                          )
                        })}
                      </div>
                    ) : (
                      <EmptyState title="No matching members">
                        Refine the filter or clear it to see all memberships.
                      </EmptyState>
                    )}
                  </div>

                  {groupEditing && managedGroup && <div className={styles.actionRow}>
                    <Button onClick={requestCancel} disabled={addMembership.isPending || removeMembership.isPending}>Done</Button>
                  </div>}

                  <div className={styles.metadataGrid}>
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Created</Text>
                      <Text block>{formatTimestamp(selectedGroup.createdAt)}</Text>
                    </div>
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Updated</Text>
                      <Text block>{formatTimestamp(selectedGroup.updatedAt)}</Text>
                    </div>
                  </div>
                </>
              ) : (
                <EmptyState title="Select a group">
                  Choose a group to edit its description and memberships.
                </EmptyState>
              )}
            </div>}
          </IdentityWorkbench>
        )
      ) : principals.isLoading ? (
        <Loading label={`Loading ${principalTabText.title.toLowerCase()}`} />
      ) : principals.error ? (
        <ErrorState error={principals.error} />
      ) : (
        <IdentityWorkbench
          title={principalTabText.title}
          description={`${visiblePrincipals.length} of ${tabPrincipals.length} shown`}
          actions={identityActions}
          toolbar={identityFilter}
          items={visiblePrincipals}
          getId={(principal) => principal.id}
          selectedId={workbench.selectedId}
          onSelect={workbench.select}
          listLabel={principalTabText.title}
          detailLabel={selectedPrincipal ? getPrincipalName(selectedPrincipal) : 'Identity details'}
          renderListItem={(principal) => <span className={styles.resourceRow}>
            <IdentityAvatar name={getPrincipalName(principal)} kind={principal.kind} objectId={principal.objectId} />
            <span className={styles.rowName}>
              <span className={styles.rowPrimary}>{getPrincipalName(principal)}</span>
              <span className={styles.rowSecondary}>{principal.detail ?? principal.objectId}</span>
              <span className={styles.badgeRow}>
                <PrincipalKindBadge kind={principal.kind} />
                <Badge appearance="outline">{principal.directoryVerifiedAt ? 'Verified in Entra' : 'Not directory-verified'}</Badge>
              </span>
              {principal.kind === 'agentUser' && <span className={styles.rowSecondary}>
                Parent agent {principals.data?.find((item) => item.objectId === principal.identityParentId)?.label ?? principal.identityParentId ?? 'not recorded'}
              </span>}
              {principal.kind === 'agentIdentity' && principal.blueprintId && <span className={styles.rowSecondary}>Blueprint {principal.blueprintId}</span>}
              {(mcpServersByModelCaller.get(principal.id) ?? []).length > 0 && <span className={styles.rowSecondary}>
                Calls models for {(mcpServersByModelCaller.get(principal.id) ?? []).join(', ')}
              </span>}
            </span>
          </span>}
          emptyList={<EmptyState title={tabPrincipals.length ? `No matching ${principalTabText.title.toLowerCase()}` : principalTabText.emptyTitle}>
            {tabPrincipals.length ? 'Refine the filter or add a new record.' : principalTabText.emptyHint}
          </EmptyState>}
          emptySelection={<EmptyState title={principalTabText.selectPrompt}>Choose a record to inspect its local metadata.</EmptyState>}
          missingSelection={<EmptyState title="Identity not in this view">
            This reference may be filtered out, unavailable, or listed in another identity section.
            <Button onClick={() => { setSearchQuery(''); workbench.select(null) }}>Clear selection and filter</Button>
          </EmptyState>}
          editing={detailEditing}
          editorTitle={`Edit ${selectedPrincipal ? getPrincipalName(selectedPrincipal) : 'identity'}`}
          dirty={!saveCommitted && principalDetailHasChanges}
          pending={!saveCommitted && (updatePrincipal.isPending || deletePrincipal.isPending)}
          onBack={cancelEditing}>
          {(requestCancel) =>
          <div className={styles.detailCard}>
            {selectedPrincipal ? (
              <>
                <div className={styles.detailHeader}>
                  <div className={styles.detailTitleBlock}>
                    <IdentityAvatar name={getPrincipalName(selectedPrincipal)} kind={selectedPrincipal.kind} objectId={selectedPrincipal.objectId} size={56} />
                    <Title3 as="h3">{getPrincipalName(selectedPrincipal)}</Title3>
                    <div className={styles.badgeRow}>
                      <PrincipalKindBadge kind={selectedPrincipal.kind} />
                      {(mcpServersByModelCaller.get(selectedPrincipal.id) ?? []).length > 0 && (
                        <Text className={styles.muted}>
                          Calls models for {(mcpServersByModelCaller.get(selectedPrincipal.id) ?? []).join(', ')}
                        </Text>
                      )}
                    </div>
                    {!detailEditing && <Button ref={editButtonRef} appearance="secondary" onClick={() => { setSaveCommitted(false); workbench.open('edit') }}>Edit local metadata</Button>}
                  </div>
                  <Button
                    appearance="subtle"
                    className={styles.dangerButton}
                    disabled={deletePrincipal.isPending || updatePrincipal.isPending}
                    onClick={() =>
                      setConfirmation({
                        type: 'principal',
                        principalId: selectedPrincipal.id,
                        principalName: getPrincipalName(selectedPrincipal),
                      })
                    }
                  >
                    Remove MOSAIC record
                  </Button>
                </div>

                {principalDetailError && (
                  <MessageBar intent="error">
                    <MessageBarBody>
                      <MessageBarTitle>Unable to save principal changes</MessageBarTitle>
                      {principalDetailError.message}
                    </MessageBarBody>
                  </MessageBar>
                )}

                <form ref={editFormRef} className={styles.detailSection} onSubmit={submitPrincipalDetails}>
                  <div className={styles.readOnlyPanel}>
                    <Text className={styles.readOnlyLabel}>Authoritative source</Text>
                    <Text>Microsoft Entra. MOSAIC stores only the reference, local label, kind mapping, and charge default.</Text>
                    <Text className={styles.muted}>{selectedPrincipal.directoryVerifiedAt
                      ? `Verified in Entra ${formatTimestamp(selectedPrincipal.directoryVerifiedAt)}`
                      : 'Not directory-verified'}</Text>
                  </div>
                  <details className={styles.technicalDetails}>
                  <summary>Technical details</summary>
                  <div className={styles.readOnlyPanel}>
                    <Text className={styles.readOnlyLabel}>Entra object ID</Text>
                    <code className={styles.monospace}>{selectedPrincipal.objectId}</code>
                  </div>
                  {selectedPrincipal.detail && (
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Directory detail</Text>
                      <Text block>{selectedPrincipal.detail}</Text>
                    </div>
                  )}
                  {selectedPrincipal.kind === 'agentUser' && (
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Parent agent</Text>
                      <Text block>
                        {principals.data?.find((item) => item.objectId === selectedPrincipal.identityParentId)?.label
                          ?? selectedPrincipal.identityParentId
                          ?? 'Not recorded'}
                      </Text>
                    </div>
                  )}
                  {selectedPrincipal.kind === 'agentIdentity' && selectedPrincipal.blueprintId && (
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Blueprint ID</Text>
                      <code className={styles.monospace}>{selectedPrincipal.blueprintId}</code>
                    </div>
                  )}
                  </details>
                  {selectedPrincipal.kind !== 'securityGroup' && (
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Default cost center</Text>
                      <Text block>
                        {selectedPrincipal.defaultCostCenterId
                          ? `${costCenterById.get(selectedPrincipal.defaultCostCenterId)?.name ?? selectedPrincipal.defaultCostCenterId}${costCenterById.get(selectedPrincipal.defaultCostCenterId)?.code ? ` (${costCenterById.get(selectedPrincipal.defaultCostCenterId)?.code})` : ''}`
                          : 'Tenant default'}
                      </Text>
                    </div>
                  )}

                  {detailEditing && <>
                  <Field label="Local label">
                    <Input
                      value={principalDraftLabel}
                      onChange={(_, data) => setPrincipalDraftLabel(data.value)}
                    />
                  </Field>

                  <Field label="Principal type">
                    <Select
                      value={principalDraftKind}
                      onChange={(event) => setPrincipalDraftKind(event.target.value as PrincipalKind)}
                    >
                      <option value="user">User</option>
                      <option value="agentUser">Agent user</option>
                      <option value="agentIdentity">Agent</option>
                      <option value="securityGroup">Security group</option>
                      <option value="servicePrincipal">Application</option>
                      <option value="managedIdentity">Managed identity</option>
                    </Select>
                  </Field>

                  {principalDraftKind !== 'securityGroup' && (
                    <Field
                      label="Default cost center"
                      hint="Changing it revokes this principal's grants under the old default unless it's listed there, directly or through a security group."
                    >
                      <Select
                        value={principalDraftDefaultCostCenterId}
                        onChange={(_, data) => setPrincipalDraftDefaultCostCenterId(data.value)}
                      >
                        <option value="">Tenant default</option>
                        {(costCenters.data ?? []).map((costCenter) => (
                          <option key={costCenter.id} value={costCenter.id}>
                            {costCenter.name} ({costCenter.code})
                          </option>
                        ))}
                      </Select>
                    </Field>
                  )}

                  <div className={styles.formActions}>
                    <Text className={styles.helperText}>
                      Entra remains the source of truth. Update only the local label or kind mapping.
                    </Text>
                    <Button
                      appearance="primary"
                      type="submit"
                      disabled={!principalDetailHasChanges || updatePrincipal.isPending}
                    >
                      Save changes
                    </Button>
                    <Button onClick={requestCancel}>Cancel editing</Button>
                  </div>
                  </>}
                </form>

                <div className={styles.metadataGrid}>
                  <div className={styles.readOnlyPanel}>
                    <Text className={styles.readOnlyLabel}>Created</Text>
                    <Text block>{formatTimestamp(selectedPrincipal.createdAt)}</Text>
                  </div>
                  <div className={styles.readOnlyPanel}>
                    <Text className={styles.readOnlyLabel}>Updated</Text>
                    <Text block>{formatTimestamp(selectedPrincipal.updatedAt)}</Text>
                  </div>
                </div>

                {selectedPrincipal.kind === 'securityGroup' && (
                  <DirectoryGroupVerification
                    key={selectedPrincipal.id}
                    groupObjectId={selectedPrincipal.objectId}
                  />
                )}
                {selectedPrincipal.kind === 'securityGroup' && (
                  <div className={styles.detailSection}>
                    <Title3 as="h3">Security group members</Title3>
                    <Text className={styles.helperText}>
                      Loaded from Microsoft Graph when this group is selected. Recorded members can
                      already be granted directly in MOSAIC.
                    </Text>
                    {selectedPrincipalMembers.isLoading ? (
                      <Loading label="Loading security group members" />
                    ) : selectedPrincipalMembers.error ? (
                      <ErrorState
                        title="Unable to load security group members"
                        error={selectedPrincipalMembers.error}
                      />
                    ) : selectedPrincipalMembers.data ? (
                      <>
                        {selectedPrincipalMembers.data.truncated && (
                          <MessageBar intent="warning">
                            <MessageBarBody>
                              This bounded member preview is truncated, not complete membership
                              evidence. Use the explicit complete-read check above to verify
                              Graph readability.
                            </MessageBarBody>
                          </MessageBar>
                        )}
                        {!selectedPrincipalMembers.data.members.length ? (
                          <EmptyState title="No members returned">
                            Microsoft Graph did not return members for this security group.
                          </EmptyState>
                        ) : (
                          <div className={styles.memberList}>
                            {selectedPrincipalMembers.data.members.map((member) => (
                              <div key={`${member.kind}:${member.objectId}`} className={styles.memberRow}>
                                <IdentityAvatar name={member.displayName ?? 'Directory member'} kind={member.kind} objectId={member.objectId} />
                                <div className={styles.memberMeta}>
                                  <Text className={styles.memberName}>
                                    {member.displayName ?? member.objectId}
                                  </Text>
                                  <div className={styles.badgeRow}>
                                    <PrincipalKindBadge kind={member.kind} />
                                    {member.principalId && <Badge appearance="tint">Recorded in MOSAIC</Badge>}
                                  </div>
                                  {member.detail && <Text className={styles.rowSecondary}>{member.detail}</Text>}
                                  <code className={styles.monospace}>{member.objectId}</code>
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </>
                    ) : null}
                  </div>
                )}
              </>
            ) : (
              <EmptyState title={principalTabText.selectPrompt}>
                Choose a record to inspect and edit its local metadata.
              </EmptyState>
            )}
          </div>}
        </IdentityWorkbench>
      )}

      </SectionLayout>

      <WorkspaceDialog open={principalDialogOpen} title={principalDialogTitle}
        pending={!saveCommitted && activeMutations > 0}
        onOpenChange={(_, data) => !data.open && closePrincipalDialog()}>
        {(requestClose) => <WorkspaceDialogSurface>
          {effectivePrincipalDialogView === 'search' ? (
            <WorkspaceDialogBody>
              <WorkspaceDialogContent className={styles.dialogForm}>
                <Text className={styles.muted}>{principalCreateHint}</Text>
                {!directoryStatus.isPending && !directoryLookupEnabled && <MessageBar intent={directoryStatus.isError ? 'error' : 'warning'}>
                  <MessageBarBody>
                    {directoryStatus.isError ? 'Could not check directory availability.' : 'Directory lookup requires Entra authentication. Manual entries are recorded without directory verification.'}
                    {' '}<a href="/settings?tab=runtime">Check directory configuration</a>
                    <Button onClick={() => directoryStatus.refetch()} disabled={directoryStatus.isFetching}>Retry directory availability</Button>
                  </MessageBarBody>
                </MessageBar>}
                {directoryKind !== 'group' && (
                  <Field label="Default cost center">
                    <Select
                      value={createPrincipalDefaultCostCenterId}
                      onChange={(_, data) => setCreatePrincipalDefaultCostCenterId(data.value)}
                    >
                      <option value="">Tenant default</option>
                      {(costCenters.data ?? []).map((costCenter) => (
                        <option key={costCenter.id} value={costCenter.id}>
                          {costCenter.name} ({costCenter.code})
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
                {directoryStatus.isPending ? <Loading label="Checking directory lookup availability" /> : directoryLookupEnabled ? <DirectoryPrincipalPicker
                  kind={directoryKind}
                  onKindChange={setDirectoryKind}
                  defaultCostCenterId={directoryKind === 'group' ? null : createPrincipalDefaultCostCenterId}
                  onCreated={(principal) => {
                    showPrincipal(principal)
                  }}
                  onManualFallback={() => {
                    setCreatePrincipalKind(MANUAL_KIND_FOR_SEARCH[directoryKind])
                    setPrincipalDialogView('manual')
                  }}
                /> : <Button appearance="subtle" onClick={() => {
                  setCreatePrincipalKind(MANUAL_KIND_FOR_SEARCH[directoryKind])
                  setPrincipalDialogView('manual')
                }}>Use manual entry</Button>}
              </WorkspaceDialogContent>
            </WorkspaceDialogBody>
          ) : (
            <form onSubmit={submitCreatePrincipal}>
              <WorkspaceDialogBody>
                <WorkspaceDialogContent className={styles.dialogForm}>
                  <Text className={styles.muted}>{principalCreateHint}</Text>

                  {createPrincipal.error && (
                    <MessageBar intent="error">
                      <MessageBarBody>
                        <MessageBarTitle>Unable to add principal</MessageBarTitle>
                        {createPrincipal.error.message}
                      </MessageBarBody>
                    </MessageBar>
                  )}

                  <Field label="Entra object ID" required>
                    <Input
                      required
                      value={createPrincipalObjectId}
                      onChange={(_, data) => setCreatePrincipalObjectId(data.value)}
                    />
                  </Field>

                  <Field label="Principal type">
                    <Select
                      value={createPrincipalKind}
                      onChange={(event) => setCreatePrincipalKind(event.target.value as PrincipalKind)}
                    >
                      <option value="user">Person</option>
                      <option value="agentIdentity">Agent</option>
                      <option value="agentUser">Agent user</option>
                      <option value="securityGroup">Security group</option>
                      <option value="servicePrincipal">Application</option>
                      <option value="managedIdentity">Managed identity</option>
                    </Select>
                  </Field>

                  {createPrincipalKind === 'agentUser' && directoryStatus.data?.lookupEnabled === false && (
                    <Field label="Parent agent object ID">
                      <Input
                        value={createPrincipalParentId}
                        onChange={(_, data) => setCreatePrincipalParentId(data.value)}
                      />
                    </Field>
                  )}

                  {createPrincipalKind !== 'securityGroup' && (
                    <Field label="Default cost center">
                      <Select
                        value={createPrincipalDefaultCostCenterId}
                        onChange={(_, data) => setCreatePrincipalDefaultCostCenterId(data.value)}
                      >
                        <option value="">Tenant default</option>
                        {(costCenters.data ?? []).map((costCenter) => (
                          <option key={costCenter.id} value={costCenter.id}>
                            {costCenter.name} ({costCenter.code})
                          </option>
                        ))}
                      </Select>
                    </Field>
                  )}

                  <Field label="Local label">
                    <Input
                      value={createPrincipalLabel}
                      onChange={(_, data) => setCreatePrincipalLabel(data.value)}
                    />
                  </Field>
                </WorkspaceDialogContent>
                {
                  <WorkspaceDialogActions>
                    <Button appearance="subtle" type="button" onClick={() => setPrincipalDialogView('search')}>
                      Search the directory
                    </Button>
                  </WorkspaceDialogActions>
                }
                <WorkspaceDialogActions>
                  <Button appearance="secondary" type="button" onClick={requestClose}>Cancel entry</Button>
                  <Button
                    appearance="primary"
                    type="submit"
                    disabled={!createPrincipalObjectId.trim() || createPrincipal.isPending}
                  >
                    Save
                  </Button>
                </WorkspaceDialogActions>
              </WorkspaceDialogBody>
            </form>
          )}
        </WorkspaceDialogSurface>}
      </WorkspaceDialog>

      <WorkspaceDialog open={createGroupOpen} title="Create group"
        pending={!saveCommitted && createGroup.isPending}
        onOpenChange={(_, data) => !data.open && closeGroupDialog()}>
        <WorkspaceDialogSurface>
          <form onSubmit={submitCreateGroup}>
            <WorkspaceDialogBody>
              <WorkspaceDialogContent className={styles.dialogForm}>
                <Text className={styles.muted}>
                  Create a MOSAIC access group without mirroring Entra group profiles.
                </Text>

                {createGroup.error && (
                  <MessageBar intent="error">
                    <MessageBarBody>
                      <MessageBarTitle>Unable to create group</MessageBarTitle>
                      {createGroup.error.message}
                    </MessageBarBody>
                  </MessageBar>
                )}

                <Field label="Group name" required>
                  <Input
                    required
                    value={createGroupName}
                    onChange={(_, data) => setCreateGroupName(data.value)}
                  />
                </Field>

                <Field label="Description">
                  <Textarea
                    value={createGroupDescription}
                    onChange={(_, data) => setCreateGroupDescription(data.value)}
                  />
                </Field>
              </WorkspaceDialogContent>
              <WorkspaceDialogActions>
                <Button
                  appearance="primary"
                  type="submit"
                  disabled={!createGroupName.trim() || createGroup.isPending}
                >
                  Save
                </Button>
              </WorkspaceDialogActions>
            </WorkspaceDialogBody>
          </form>
        </WorkspaceDialogSurface>
      </WorkspaceDialog>

      <Dialog open={confirmation !== null} onOpenChange={(_, data) => !data.open && setConfirmation(null)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Confirm action</DialogTitle>
            <DialogContent>
              {confirmation?.type === 'principal' && (
                <Text>
                  Remove <strong>{confirmation.principalName}</strong>&apos;s reference from MOSAIC?
                  This does not delete its Microsoft Entra account or directory object.
                </Text>
              )}
              {confirmation?.type === 'group' && (
                <Text>
                  Delete the group <strong>{confirmation.groupName}</strong>?
                </Text>
              )}
              {confirmation?.type === 'membership' && (
                <Text>
                  Remove <strong>{confirmation.principalName}</strong> from{' '}
                  <strong>{confirmation.groupName}</strong>?
                </Text>
              )}
              {confirmation?.type === 'membership' && confirmation.managedCostCenter && <Text block>
                This also removes the reference from the owning cost center. Grants that depended
                on it are revoked unless their subjects can still charge that center another way.
                The next apply deletes affected keys; personal defaults are rechecked.
              </Text>}
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setConfirmation(null)}>
                Cancel
              </Button>
              <Button appearance="primary" onClick={confirmDestructiveAction}>
                Confirm
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </section>
  )
}
