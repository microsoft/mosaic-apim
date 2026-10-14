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
  Text,
  Textarea,
  Title3,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { DirectoryPrincipalPicker } from '../components/DirectoryPrincipalPicker'
import { IdentityAvatar } from '../components/IdentityAvatar'
import { IdentityDetailsDrawer } from '../components/IdentityDetailsDrawer'
import { PageHeader } from '../components/PageHeader'
import { PrincipalKindBadge } from '../components/PrincipalKindBadge'
import { SectionLayout } from '../components/SectionLayout'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { DirectorySearchKind, Principal, PrincipalKind } from '../types'
import styles from './IdentityPage.module.css'

type PrincipalTab = 'users' | 'agents' | 'applications' | 'security-groups'
type IdentityTab = PrincipalTab | 'groups'
type PrincipalDialogView = 'search' | 'manual'

const IDENTITY_SECTIONS = [
  { value: 'users', label: 'People', description: 'People referenced from Microsoft Entra. Labels and defaults are stored in MOSAIC; adding a reference does not create an Entra account.' },
  { value: 'agents', label: 'Agents', description: 'Microsoft Entra agent identities and their paired agent users. MOSAIC records references, not new agents.' },
  { value: 'applications', label: 'Applications & managed identities', description: 'Application service principals and Azure managed identities from Microsoft Entra. Registration in MOSAIC does not grant an Entra application role.' },
  { value: 'security-groups', label: 'Entra security groups', description: 'Security-enabled groups whose membership is managed in Microsoft Entra. Group grants use validated token claims, not MOSAIC-local membership.' },
  { value: 'groups', label: 'MOSAIC groups', description: 'Local collections of registered principals. Creating a MOSAIC group does not create or change an Entra group; these grants remain desired state only.' },
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

/** What the add dialog searches for, or enters by hand, when it opens from each tab. */
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

function LiveBadge() {
  return (
    <Badge appearance="outline">
      MOSAIC record
    </Badge>
  )
}

export function IdentityPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const restoreFocus = useRestoreFocusTarget()
  const searchParams = useMemo(() => new URLSearchParams(location.search), [location.search])
  const rawTab = searchParams.get('tab')
  const activeTab: IdentityTab = rawTab === 'workloads' ? 'applications' : isIdentityTab(rawTab) ? rawTab : 'users'

  const [searchQuery, setSearchQuery] = useState('')
  const [requestedPrincipalId, setSelectedPrincipalId] = useState('')
  const [selectedGroupId, setSelectedGroupId] = useState('')
  const [principalDialogOpen, setPrincipalDialogOpen] = useState(false)
  const [principalDialogView, setPrincipalDialogView] = useState<PrincipalDialogView>('search')
  const [directoryKind, setDirectoryKind] = useState<DirectorySearchKind>('user')
  const [createPrincipalObjectId, setCreatePrincipalObjectId] = useState('')
  const [createPrincipalLabel, setCreatePrincipalLabel] = useState('')
  const [createPrincipalKind, setCreatePrincipalKind] = useState<PrincipalKind>('user')
  const [createPrincipalDefaultCostCenterId, setCreatePrincipalDefaultCostCenterId] = useState('')
  const [createPrincipalParentId, setCreatePrincipalParentId] = useState('')
  const [createGroupOpen, setCreateGroupOpen] = useState(false)
  const [createGroupName, setCreateGroupName] = useState('')
  const [createGroupDescription, setCreateGroupDescription] = useState('')
  const [principalDraftLabel, setPrincipalDraftLabel] = useState('')
  const [principalDraftKind, setPrincipalDraftKind] = useState<PrincipalKind>('user')
  const [principalDraftDefaultCostCenterId, setPrincipalDraftDefaultCostCenterId] = useState('')
  const [groupDraftDescription, setGroupDraftDescription] = useState('')
  const [principalToAdd, setPrincipalToAdd] = useState('')
  const [confirmation, setConfirmation] = useState<ConfirmationState>(null)
  const [detailEditing, setDetailEditing] = useState(false)
  const [discardOpen, setDiscardOpen] = useState(false)
  const [pendingTab, setPendingTab] = useState<IdentityTab | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const editButtonRef = useRef<HTMLButtonElement>(null)
  const editFormRef = useRef<HTMLFormElement>(null)
  const previousEditing = useRef(false)

  useLayoutEffect(() => {
    if (detailsOpen && !discardOpen) {
      if (detailEditing) {
        editFormRef.current?.querySelector<HTMLInputElement | HTMLTextAreaElement>('input, textarea')?.focus()
      } else if (previousEditing.current) {
        editButtonRef.current?.focus()
      }
    }
    previousEditing.current = detailEditing
  }, [detailEditing, detailsOpen, discardOpen])

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
    queryFn: api.getDirectoryStatus,
  })
  const memberships = useQuery({
    queryKey: ['memberships', selectedGroupId],
    queryFn: () => api.listMemberships(selectedGroupId),
    enabled: activeTab === 'groups' && Boolean(selectedGroupId),
  })

  const updateTab = useCallback(
    (nextTab: IdentityTab, replace = false) => {
      setSelectedPrincipalId('')
      setSelectedGroupId('')
      setDetailEditing(false)
      setDetailsOpen(false)
      const nextParams = new URLSearchParams(location.search)
      nextParams.set('tab', nextTab)
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
      updateTab(activeTab, true)
    }
  }, [activeTab, rawTab, updateTab])

  // Open the tab that lists the principal, and select it there.
  const showPrincipal = useCallback(
    (principal: Principal) => {
      const nextTab = principalTabForKind(principal.kind)
      setSearchQuery((current) => (principalMatchesSearch(principal, nextTab, current) ? current : ''))
      if (nextTab !== activeTab) {
        updateTab(nextTab, true)
      }
      setSelectedPrincipalId(principal.id)
      setDetailEditing(false)
    },
    [activeTab, updateTab],
  )

  const createPrincipal = useMutation({
    mutationFn: api.createPrincipal,
    onSuccess: async (principal) => {
      setCreatePrincipalObjectId('')
      setCreatePrincipalLabel('')
      setCreatePrincipalParentId('')
      setPrincipalDialogOpen(false)
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
      setSelectedPrincipalId('')
      setDetailsOpen(false)
    },
  })

  const createGroup = useMutation({
    mutationFn: api.createGroup,
    onSuccess: async (group) => {
      setCreateGroupName('')
      setCreateGroupDescription('')
      setCreateGroupOpen(false)
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      updateTab('groups', true)
      setSelectedGroupId(group.id)
    },
  })

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
      setDetailEditing(false)
    },
  })

  const deleteGroup = useMutation({
    mutationFn: api.deleteGroup,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      if (selectedGroupId) {
        await queryClient.removeQueries({ queryKey: ['memberships', selectedGroupId] })
      }
      setSelectedGroupId('')
      setDetailsOpen(false)
    },
  })

  const addMembership = useMutation({
    mutationFn: ({ groupId, principalId }: { groupId: string; principalId: string }) =>
      api.addMembership(groupId, principalId),
    onSuccess: async () => {
      setPrincipalToAdd('')
      await queryClient.invalidateQueries({ queryKey: ['memberships', selectedGroupId] })
    },
  })

  const removeMembership = useMutation({
    mutationFn: ({ groupId, principalId }: { groupId: string; principalId: string }) =>
      api.removeMembership(groupId, principalId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['memberships', selectedGroupId] })
    },
  })

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

  const selectedPrincipal = principals.data?.find((principal) => principal.id === selectedPrincipalId)
  const selectedGroup = groups.data?.find((group) => group.id === selectedGroupId)
  const selectedPrincipalMembers = useQuery({
    queryKey: ['principals', selectedPrincipal?.id, 'members'],
    queryFn: () => api.listPrincipalMembers(selectedPrincipal?.id ?? ''),
    enabled: Boolean(selectedPrincipal && selectedPrincipal.kind === 'securityGroup'),
  })

  useEffect(() => {
    setPrincipalDraftLabel(selectedPrincipal?.label ?? '')
    setPrincipalDraftKind(selectedPrincipal?.kind ?? 'user')
    setPrincipalDraftDefaultCostCenterId(selectedPrincipal?.defaultCostCenterId ?? '')
  }, [selectedPrincipal])

  useEffect(() => {
    setGroupDraftDescription(selectedGroup?.description ?? '')
    setPrincipalToAdd('')
  }, [selectedGroup])

  const memberIds = useMemo(
    () => new Set(memberships.data?.map((membership) => membership.principalId) ?? []),
    [memberships.data],
  )
  const availablePrincipals = useMemo(
    () =>
      [...(principals.data ?? [])]
        .filter((principal) => principal.kind !== 'securityGroup' && !memberIds.has(principal.id))
        .sort((left, right) =>
          getPrincipalName(left).localeCompare(getPrincipalName(right), undefined, {
            sensitivity: 'base',
          }),
        ),
    [memberIds, principals.data],
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
    selectedGroup !== undefined &&
    groupDraftDescription.trim() !== (selectedGroup.description ?? '')

  function cancelEditing() {
    setPrincipalDraftLabel(selectedPrincipal?.label ?? '')
    setPrincipalDraftKind(selectedPrincipal?.kind ?? 'user')
    setPrincipalDraftDefaultCostCenterId(selectedPrincipal?.defaultCostCenterId ?? '')
    setGroupDraftDescription(selectedGroup?.description ?? '')
    setDetailEditing(false)
  }

  function keepEditing() {
    setDiscardOpen(false)
    setPendingTab(null)
  }

  function closeDetails(discard = false, nextTab: IdentityTab | null = null) {
    if (updatePrincipal.isPending || updateGroup.isPending) return
    if (!discard && detailEditing && (principalDetailHasChanges || groupDetailHasChanges)) {
      setPendingTab(nextTab)
      setDiscardOpen(true)
      return
    }
    cancelEditing()
    setDiscardOpen(false)
    setPendingTab(null)
    setDetailsOpen(false)
    setSelectedPrincipalId('')
    setSelectedGroupId('')
    if (nextTab) updateTab(nextTab)
  }

  const principalDetailError = updatePrincipal.error ?? deletePrincipal.error
  const groupDetailError =
    updateGroup.error ?? deleteGroup.error ?? addMembership.error ?? removeMembership.error

  function openPrincipalDialog() {
    const defaults = ADD_DIALOG_DEFAULTS[activeTab]
    createPrincipal.reset()
    setPrincipalDialogView('search')
    setPrincipalDialogOpen(true)
    setDirectoryKind(defaults.search)
    setCreatePrincipalObjectId('')
    setCreatePrincipalLabel('')
    setCreatePrincipalParentId('')
    setCreatePrincipalKind(defaults.manual)
    setCreatePrincipalDefaultCostCenterId(costCenterSettings.data?.defaultCostCenterId ?? '')
  }

  function closePrincipalDialog() {
    createPrincipal.reset()
    setPrincipalDialogOpen(false)
  }

  function closeGroupDialog() {
    createGroup.reset()
    setCreateGroupOpen(false)
  }

  function submitCreatePrincipal(event: FormEvent) {
    event.preventDefault()
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

  return (
    <section className={styles.page}>
      <PageHeader
        title="Identity"
        source="live"
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

      <SectionLayout label="Identity" orientation="horizontal" sections={IDENTITY_SECTIONS} active={activeTab}
        onSelect={(value) => isIdentityTab(value) && value !== activeTab && closeDetails(false, value)}>
      <div className={styles.toolbar}>
        <div className={styles.toolbarControls}>
          <Input
            className={styles.searchInput}
            value={searchQuery}
            placeholder={activeTab === 'groups' ? groupSearchPlaceholder : principalTabText.filter}
            aria-label={activeTab === 'groups' ? groupSearchPlaceholder : principalTabText.filter}
            onChange={(_, data) => setSearchQuery(data.value)}
          />

          <div className={styles.actionRow}>
            {activeTab !== 'groups' && <Button
              appearance="primary"
              onClick={() => openPrincipalDialog()}
            >
              {principalTabText.add}
            </Button>}
            {activeTab === 'groups' && <Button
              appearance="primary"
              onClick={() => {
                createGroup.reset()
                setCreateGroupName('')
                setCreateGroupDescription('')
                setCreateGroupOpen(true)
              }}
            >
              Create MOSAIC group
            </Button>}
          </div>
        </div>
      </div>

      {activeTab === 'groups' ? (
        groups.isLoading || principals.isLoading ? (
          <Loading label="Loading groups and principals" />
        ) : groups.error || principals.error ? (
          <ErrorState error={groups.error ?? principals.error} />
        ) : !groups.data?.length ? (
          <EmptyState title="No groups configured">
            Create the first MOSAIC group to manage memberships.
          </EmptyState>
        ) : (
          <div className={styles.contentGrid}>
            <Card className={styles.listCard}>
              <div className={styles.cardHeader}>
                <div className={styles.cardHeaderText}>
                  <Title3 as="h2">MOSAIC groups</Title3>
                  <Text className={styles.muted}>
                    {filteredGroups.length} of {groups.data.length} group
                    {groups.data.length === 1 ? '' : 's'}
                  </Text>
                </div>
              </div>

              {filteredGroups.length ? (
                <div className={styles.groupList} aria-label="MOSAIC groups">
                  {filteredGroups.map((group) => {
                    const isSelected = group.id === selectedGroupId
                    return (
                      <button
                        {...restoreFocus}
                        key={group.id}
                        type="button"
                        className={
                          isSelected
                            ? `${styles.groupListItem} ${styles.groupListItemSelected}`
                            : styles.groupListItem
                        }
                        onClick={() => { setSelectedGroupId(group.id); setDetailEditing(false); setDetailsOpen(true) }}
                      >
                        <span className={styles.groupListTitle}>{group.name}</span>
                        <span className={styles.groupListDescription}>
                          {group.description || 'No description'}
                        </span>
                      </button>
                    )
                  })}
                </div>
              ) : (
                <div className={styles.emptyPanel}>
                  <EmptyState title="No matching groups">
                    Refine the filter or create a new group.
                  </EmptyState>
                </div>
              )}
            </Card>

            <IdentityDetailsDrawer open={detailsOpen && Boolean(selectedGroup)} title={selectedGroup?.name ?? 'Group details'} onClose={() => closeDetails()}>
            <div className={styles.detailCard}>
              {selectedGroup ? (
                <>
                  <div className={styles.detailHeader}>
                    <div className={styles.detailTitleBlock}>
                      <IdentityAvatar name={selectedGroup.name} kind="securityGroup" size={48} />
                      <Text className={styles.muted}>
                        Organize identities in MOSAIC. The gateway doesn&apos;t enforce these
                        groups; to control access for a group, add an Entra security group and grant it.
                      </Text>
                      <div className={styles.badgeRow}>
                        <LiveBadge />
                        <Badge appearance="tint" className={styles.groupBadge}>
                          {memberships.data?.length ?? 0} member{memberships.data?.length === 1 ? '' : 's'}
                        </Badge>
                      </div>
                      {!detailEditing && <Button ref={editButtonRef} appearance="secondary" onClick={() => setDetailEditing(true)}>Edit group</Button>}
                    </div>
                    <Button
                      appearance="subtle"
                      className={styles.dangerButton}
                      disabled={
                        deleteGroup.isPending ||
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
                    </Button>
                  </div>

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
                        MOSAIC stores desired-state access groups only. Entra groups remain unchanged.
                      </Text>
                    </div>

                    {detailEditing ? <Field label="Description">
                      <Textarea
                        value={groupDraftDescription}
                        onChange={(_, data) => setGroupDraftDescription(data.value)}
                      />
                    </Field> : <Text>{selectedGroup.description || 'No description'}</Text>}

                    {detailEditing && <div className={styles.formActions}>
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
                      <Button onClick={cancelEditing}>Cancel editing</Button>
                    </div>}
                  </form>

                  <div className={styles.detailSection}>
                    <Title3 as="h3">Memberships</Title3>

                    {detailEditing && <form className={styles.inlineForm} onSubmit={submitAddMembership}>
                      <Field className={styles.memberField} label="Add principal">
                        <Select
                          value={principalToAdd}
                          disabled={!availablePrincipals.length}
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
                        disabled={!principalToAdd || addMembership.isPending}
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
                              {detailEditing && <Button
                                appearance="subtle"
                                className={styles.dangerButton}
                                onClick={() =>
                                  setConfirmation({
                                    type: 'membership',
                                    groupId: selectedGroup.id,
                                    groupName: selectedGroup.name,
                                    principalId: membership.principalId,
                                    principalName,
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
            </div>
            </IdentityDetailsDrawer>
          </div>
        )
      ) : principals.isLoading ? (
        <Loading label={`Loading ${principalTabText.title.toLowerCase()}`} />
      ) : principals.error ? (
        <ErrorState error={principals.error} />
      ) : !tabPrincipals.length ? (
        <EmptyState title={principalTabText.emptyTitle}>{principalTabText.emptyHint}</EmptyState>
      ) : (
        <div className={styles.contentGrid}>
          <Card className={styles.listCard}>
            <div className={styles.cardHeader}>
              <div className={styles.cardHeaderText}>
                <Title3 as="h2">{principalTabText.title}</Title3>
                <Text className={styles.muted}>
                  {visiblePrincipals.length} of {tabPrincipals.length} shown
                </Text>
              </div>
            </div>

            {visiblePrincipals.length ? (
              <div className={styles.tableWrapper}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th scope="col">Identity</th>
                      <th scope="col">Type</th>
                      <th scope="col">Default cost center</th>
                      <th scope="col">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visiblePrincipals.map((principal) => {
                      const isSelected = principal.id === selectedPrincipalId
                      const mcpServerNames = mcpServersByModelCaller.get(principal.id) ?? []
                      return (
                        <tr
                          key={principal.id}
                          className={
                            isSelected
                              ? `${styles.selectableRow} ${styles.selectedRow}`
                              : styles.selectableRow
                          }
                        >
                          <td>
                            <button
                              {...restoreFocus}
                              type="button"
                              className={styles.rowButton}
                              aria-pressed={isSelected}
                              onClick={() => { setSelectedPrincipalId(principal.id); setDetailEditing(false); setDetailsOpen(true) }}
                            >
                              <IdentityAvatar name={getPrincipalName(principal)} kind={principal.kind} objectId={principal.objectId} />
                              <span className={styles.rowName}>
                              <span className={styles.rowPrimary}>{getPrincipalName(principal)}</span>
                              <span className={styles.rowSecondary}>
                                {principal.detail ?? principal.objectId}
                              </span>
                              </span>
                            </button>
                          </td>
                          <td>
                            <PrincipalKindBadge kind={principal.kind} />
                          </td>
                          <td>
                            <div className={styles.memberMeta}>
                              <span>{principal.defaultCostCenterId
                                ? costCenterById.get(principal.defaultCostCenterId)?.name ?? 'Recorded cost center'
                                : principal.kind === 'securityGroup' ? 'Not applicable' : 'Tenant default'}</span>
                              {principal.kind === 'agentUser' && (
                                <span className={styles.rowSecondary}>
                                  Parent agent {principals.data?.find((item) => item.objectId === principal.identityParentId)?.label ?? principal.identityParentId ?? 'not recorded'}
                                </span>
                              )}
                              {principal.kind === 'agentIdentity' && principal.blueprintId && (
                                <span className={styles.rowSecondary}>Blueprint {principal.blueprintId}</span>
                              )}
                            </div>
                          </td>
                          <td>
                            <div className={styles.statusCell}>
                              <Badge
                                appearance="tint"
                                className={`${styles.statusBadge} ${principal.directoryVerifiedAt ? styles.liveBadge : styles.groupBadge}`}
                              >
                                {principal.directoryVerifiedAt ? 'Verified in Entra' : 'Not directory-verified'}
                              </Badge>
                              {principal.directoryVerifiedAt && (
                                <span className={styles.rowSecondary}>
                                  {formatTimestamp(principal.directoryVerifiedAt)}
                                </span>
                              )}
                              {mcpServerNames.length > 0 && (
                                <Badge appearance="tint" className={styles.groupBadge}>
                                  Calls models for {mcpServerNames.join(', ')}
                                </Badge>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className={styles.emptyPanel}>
                <EmptyState title={`No matching ${principalTabText.title.toLowerCase()}`}>
                  Refine the filter or add a new record.
                </EmptyState>
              </div>
            )}
          </Card>

          <IdentityDetailsDrawer open={detailsOpen && Boolean(selectedPrincipal)} title={selectedPrincipal ? getPrincipalName(selectedPrincipal) : 'Identity details'} onClose={() => closeDetails()}>
          <div className={styles.detailCard}>
            {selectedPrincipal ? (
              <>
                <div className={styles.detailHeader}>
                  <div className={styles.detailTitleBlock}>
                    <IdentityAvatar name={getPrincipalName(selectedPrincipal)} kind={selectedPrincipal.kind} objectId={selectedPrincipal.objectId} size={56} />
                    <div className={styles.badgeRow}>
                      <LiveBadge />
                      <PrincipalKindBadge kind={selectedPrincipal.kind} />
                      {(mcpServersByModelCaller.get(selectedPrincipal.id) ?? []).length > 0 && (
                        <Badge appearance="tint" className={styles.groupBadge}>
                          Calls models for {(mcpServersByModelCaller.get(selectedPrincipal.id) ?? []).join(', ')}
                        </Badge>
                      )}
                    </div>
                    {!detailEditing && <Button ref={editButtonRef} appearance="secondary" onClick={() => setDetailEditing(true)}>Edit local metadata</Button>}
                  </div>
                  <Button
                    appearance="subtle"
                    className={styles.dangerButton}
                    disabled={deletePrincipal.isPending}
                    onClick={() =>
                      setConfirmation({
                        type: 'principal',
                        principalId: selectedPrincipal.id,
                        principalName: getPrincipalName(selectedPrincipal),
                      })
                    }
                  >
                    Delete
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
                    <Button onClick={cancelEditing}>Cancel editing</Button>
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
                              The member list is truncated. Refine access by adding direct grants
                              where needed.
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
          </div>
          </IdentityDetailsDrawer>
        </div>
      )}

      </SectionLayout>

      <Dialog open={principalDialogOpen} onOpenChange={(_, data) => !data.open && closePrincipalDialog()}>
        <DialogSurface className={styles.addDialog}>
          {effectivePrincipalDialogView === 'search' ? (
            <DialogBody>
              <DialogTitle>{principalDialogTitle}</DialogTitle>
              <DialogContent className={styles.dialogForm}>
                <Text className={styles.muted}>{principalCreateHint}</Text>
                {!directoryLookupEnabled && <MessageBar intent={directoryStatus.isError ? 'error' : 'warning'}>
                  <MessageBarBody>
                    {directoryStatus.isError ? 'Could not check directory availability.' : 'Directory lookup is disabled; entries are recorded without directory verification.'}
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
                    setPrincipalDialogOpen(false)
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
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" type="button" onClick={closePrincipalDialog}>
                  Cancel
                </Button>
              </DialogActions>
            </DialogBody>
          ) : (
            <form onSubmit={submitCreatePrincipal}>
              <DialogBody>
                <DialogTitle>{principalDialogTitle}</DialogTitle>
                <DialogContent className={styles.dialogForm}>
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
                </DialogContent>
                {
                  <DialogActions position="start">
                    <Button appearance="subtle" type="button" onClick={() => setPrincipalDialogView('search')}>
                      Search the directory
                    </Button>
                  </DialogActions>
                }
                <DialogActions>
                  <Button appearance="secondary" type="button" onClick={closePrincipalDialog}>
                    Cancel
                  </Button>
                  <Button
                    appearance="primary"
                    type="submit"
                    disabled={!createPrincipalObjectId.trim() || createPrincipal.isPending}
                  >
                    Save
                  </Button>
                </DialogActions>
              </DialogBody>
            </form>
          )}
        </DialogSurface>
      </Dialog>

      <Dialog open={discardOpen} onOpenChange={(_, data) => !data.open && keepEditing()}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Discard unsaved changes?</DialogTitle>
            <DialogContent>Your local edits have not been saved. Keep editing or discard them to {pendingTab ? 'switch sections' : 'close the details'}.</DialogContent>
            <DialogActions>
              <Button onClick={keepEditing}>Keep editing</Button>
              <Button appearance="primary" onClick={() => closeDetails(true, pendingTab)}>Discard changes</Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>

      <Dialog open={createGroupOpen} onOpenChange={(_, data) => !data.open && closeGroupDialog()}>
        <DialogSurface>
          <form onSubmit={submitCreateGroup}>
            <DialogBody>
              <DialogTitle>Create group</DialogTitle>
              <DialogContent className={styles.dialogForm}>
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
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" type="button" onClick={closeGroupDialog}>
                  Cancel
                </Button>
                <Button
                  appearance="primary"
                  type="submit"
                  disabled={!createGroupName.trim() || createGroup.isPending}
                >
                  Save
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>

      <Dialog open={confirmation !== null} onOpenChange={(_, data) => !data.open && setConfirmation(null)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Confirm action</DialogTitle>
            <DialogContent>
              {confirmation?.type === 'principal' && (
                <Text>
                  Delete <strong>{confirmation.principalName}</strong> from MOSAIC?
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
