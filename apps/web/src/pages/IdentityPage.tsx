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
  Tab,
  TabList,
  Text,
  Textarea,
  Title3,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { DirectoryPrincipalPicker } from '../components/DirectoryPrincipalPicker'
import { PageHeader } from '../components/PageHeader'
import { PrincipalKindBadge } from '../components/PrincipalKindBadge'
import { PRINCIPAL_KIND_LABELS, type PrincipalTab, principalTabForKind } from '../labels'
import type { DirectorySearchKind, Principal, PrincipalKind } from '../types'
import styles from './IdentityPage.module.css'

type IdentityTab = PrincipalTab | 'groups'
type PrincipalDialogView = 'search' | 'manual'

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
  workloads: {
    title: 'Applications and security groups',
    add: 'Add identity',
    emptyTitle: 'No applications or security groups registered',
    emptyHint: 'Add an application, managed identity, or security group to begin.',
    selectPrompt: 'Select a record',
    filter: 'Filter by label, detail, object ID, or kind',
  },
}

/** What the add dialog searches for, or enters by hand, when it opens from each tab. */
const ADD_DIALOG_DEFAULTS: Record<IdentityTab, { search: DirectorySearchKind; manual: PrincipalKind }> = {
  users: { search: 'user', manual: 'user' },
  agents: { search: 'agent', manual: 'agentIdentity' },
  workloads: { search: 'group', manual: 'servicePrincipal' },
  groups: { search: 'user', manual: 'user' },
}

/** Manual entry picks up the kind the administrator was searching for. */
const MANUAL_KIND_FOR_SEARCH: Record<DirectorySearchKind, PrincipalKind> = {
  user: 'user',
  agent: 'agentIdentity',
  group: 'securityGroup',
}

const SEARCH_DIALOG_TITLES: Record<DirectorySearchKind, string> = {
  user: 'Add person',
  agent: 'Add agent',
  group: 'Add security group',
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
  return value === 'users' || value === 'agents' || value === 'workloads' || value === 'groups'
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
    <Badge appearance="tint" className={styles.liveBadge}>
      Live
    </Badge>
  )
}

export function IdentityPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const searchParams = useMemo(() => new URLSearchParams(location.search), [location.search])
  const rawTab = searchParams.get('tab')
  const activeTab: IdentityTab = isIdentityTab(rawTab) ? rawTab : 'users'

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
    },
  })

  const createGroup = useMutation({
    mutationFn: api.createGroup,
    onSuccess: async (group) => {
      setCreateGroupName('')
      setCreateGroupDescription('')
      setCreateGroupOpen(false)
      setSelectedGroupId(group.id)
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      updateTab('groups', true)
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
    },
  })

  const deleteGroup = useMutation({
    mutationFn: api.deleteGroup,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['groups'] })
      if (selectedGroupId) {
        await queryClient.removeQueries({ queryKey: ['memberships', selectedGroupId] })
      }
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
    const grouped: Record<PrincipalTab, Principal[]> = { users: [], agents: [], workloads: [] }
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

  // Show the requested record while it's on this tab and passes the filter, and the first row
  // otherwise. Deriving it, rather than resetting the request, keeps a record that was just added
  // or moved selected even when its tab opens in a later render than the request.
  const selectedPrincipalId = visiblePrincipals.some((principal) => principal.id === requestedPrincipalId)
    ? requestedPrincipalId
    : (visiblePrincipals[0]?.id ?? '')

  useEffect(() => {
    if (!filteredGroups.length) {
      setSelectedGroupId('')
      return
    }
    if (!filteredGroups.some((group) => group.id === selectedGroupId)) {
      setSelectedGroupId(filteredGroups[0].id)
    }
  }, [filteredGroups, selectedGroupId])

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
  const effectivePrincipalDialogView =
    principalDialogView === 'search' && directoryLookupEnabled ? 'search' : 'manual'
  // The title names what the dialog will add, so it follows the search or type choice.
  const principalDialogTitle =
    effectivePrincipalDialogView === 'search'
      ? SEARCH_DIALOG_TITLES[directoryKind]
      : `Add ${PRINCIPAL_KIND_LABELS[createPrincipalKind].toLowerCase()}`
  const principalCreateHint =
    effectivePrincipalDialogView === 'search'
      ? 'Search Microsoft Entra for people, agents, and security groups. Applications and managed identities need manual entry.'
      : directoryLookupEnabled
        ? 'Enter the Entra object ID and type for an application, a managed identity, or anyone the directory search does not find.'
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
        description="MOSAIC stores live references to Entra object IDs for access control. Entra IDs remain the authoritative source for identity details."
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

      <div className={styles.toolbar}>
        <TabList
          className={styles.tabs}
          selectedValue={activeTab}
          onTabSelect={(_, data) => {
            if (typeof data.value === 'string' && isIdentityTab(data.value) && data.value !== activeTab) {
              updateTab(data.value)
            }
          }}
        >
          <Tab value="users">People</Tab>
          <Tab value="agents">Agents</Tab>
          <Tab value="workloads">Applications and security groups</Tab>
          <Tab value="groups">MOSAIC groups</Tab>
        </TabList>

        <div className={styles.toolbarControls}>
          <Input
            className={styles.searchInput}
            value={searchQuery}
            placeholder={activeTab === 'groups' ? groupSearchPlaceholder : principalTabText.filter}
            aria-label={activeTab === 'groups' ? groupSearchPlaceholder : principalTabText.filter}
            onChange={(_, data) => setSearchQuery(data.value)}
          />

          <div className={styles.actionRow}>
            <Button
              appearance={activeTab !== 'groups' ? 'primary' : 'secondary'}
              onClick={() => openPrincipalDialog()}
            >
              {activeTab === 'groups' ? 'Add identity' : principalTabText.add}
            </Button>
            <Button
              appearance={activeTab === 'groups' ? 'primary' : 'secondary'}
              onClick={() => {
                createGroup.reset()
                setCreateGroupName('')
                setCreateGroupDescription('')
                setCreateGroupOpen(true)
              }}
            >
              Create group
            </Button>
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
                        key={group.id}
                        type="button"
                        className={
                          isSelected
                            ? `${styles.groupListItem} ${styles.groupListItemSelected}`
                            : styles.groupListItem
                        }
                        onClick={() => setSelectedGroupId(group.id)}
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

            <Card className={styles.detailCard}>
              {selectedGroup ? (
                <>
                  <div className={styles.detailHeader}>
                    <div className={styles.detailTitleBlock}>
                      <Text className={styles.eyebrow}>Group</Text>
                      <Title3 as="h2">{selectedGroup.name}</Title3>
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

                  <form className={styles.detailSection} onSubmit={submitGroupDetails}>
                    <div className={styles.readOnlyPanel}>
                      <Text className={styles.readOnlyLabel}>Authoritative source</Text>
                      <Text block className={styles.muted}>
                        MOSAIC stores desired-state access groups only. Entra groups remain unchanged.
                      </Text>
                    </div>

                    <Field label="Description">
                      <Textarea
                        value={groupDraftDescription}
                        onChange={(_, data) => setGroupDraftDescription(data.value)}
                      />
                    </Field>

                    <div className={styles.formActions}>
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
                    </div>
                  </form>

                  <div className={styles.detailSection}>
                    <Title3 as="h3">Memberships</Title3>

                    <form className={styles.inlineForm} onSubmit={submitAddMembership}>
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
                    </form>
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
                              <Button
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
                              </Button>
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
            </Card>
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
                      <th scope="col">Details</th>
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
                              type="button"
                              className={styles.rowButton}
                              aria-pressed={isSelected}
                              onClick={() => setSelectedPrincipalId(principal.id)}
                            >
                              <span className={styles.rowPrimary}>{getPrincipalName(principal)}</span>
                              <span className={styles.rowSecondary}>
                                {principal.detail ?? principal.objectId}
                              </span>
                            </button>
                          </td>
                          <td>
                            <PrincipalKindBadge kind={principal.kind} />
                          </td>
                          <td>
                            <div className={styles.memberMeta}>
                              <code className={styles.monospace}>{principal.objectId}</code>
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
                                {principal.directoryVerifiedAt ? 'Verified in Entra' : 'Entered by hand'}
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

          <Card className={styles.detailCard}>
            {selectedPrincipal ? (
              <>
                <div className={styles.detailHeader}>
                  <div className={styles.detailTitleBlock}>
                    <Text className={styles.eyebrow}>
                      {PRINCIPAL_KIND_LABELS[selectedPrincipal.kind]}
                    </Text>
                    <Title3 as="h2">{getPrincipalName(selectedPrincipal)}</Title3>
                    <div className={styles.badgeRow}>
                      <LiveBadge />
                      <PrincipalKindBadge kind={selectedPrincipal.kind} />
                      {(mcpServersByModelCaller.get(selectedPrincipal.id) ?? []).length > 0 && (
                        <Badge appearance="tint" className={styles.groupBadge}>
                          Calls models for {(mcpServersByModelCaller.get(selectedPrincipal.id) ?? []).join(', ')}
                        </Badge>
                      )}
                    </div>
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

                <form className={styles.detailSection} onSubmit={submitPrincipalDetails}>
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
                  </div>
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
          </Card>
        </div>
      )}

      <Dialog open={principalDialogOpen} onOpenChange={(_, data) => !data.open && closePrincipalDialog()}>
        <DialogSurface>
          {effectivePrincipalDialogView === 'search' ? (
            <DialogBody>
              <DialogTitle>{principalDialogTitle}</DialogTitle>
              <DialogContent className={styles.dialogForm}>
                <Text className={styles.muted}>{principalCreateHint}</Text>
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
                <DirectoryPrincipalPicker
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
                />
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
                {directoryLookupEnabled && (
                  <DialogActions position="start">
                    <Button appearance="subtle" type="button" onClick={() => setPrincipalDialogView('search')}>
                      Search the directory
                    </Button>
                  </DialogActions>
                )}
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
