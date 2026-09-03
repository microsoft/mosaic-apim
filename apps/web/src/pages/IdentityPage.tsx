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
import { PageHeader } from '../components/PageHeader'
import type { Principal, PrincipalKind } from '../types'
import styles from './IdentityPage.module.css'

type IdentityTab = 'users' | 'workloads' | 'groups'
type PrincipalDialogMode = 'user' | 'workload' | null

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

const principalKindLabels: Record<PrincipalKind, string> = {
  user: 'User',
  servicePrincipal: 'Service principal',
  managedIdentity: 'Managed identity',
}

function isIdentityTab(value: string | null): value is IdentityTab {
  return value === 'users' || value === 'workloads' || value === 'groups'
}

function matchesSearch(search: string, ...values: Array<string | undefined>) {
  if (!search) {
    return true
  }
  return values.some((value) => value?.toLowerCase().includes(search))
}

function getPrincipalName(principal: Principal) {
  return principal.label?.trim() || principal.objectId
}

function formatTimestamp(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString()
}

function PrincipalKindBadge({ kind }: { kind: PrincipalKind }) {
  const className =
    kind === 'user'
      ? styles.userBadge
      : kind === 'servicePrincipal'
        ? styles.servicePrincipalBadge
        : styles.managedIdentityBadge

  return (
    <Badge appearance="tint" className={className}>
      {principalKindLabels[kind]}
    </Badge>
  )
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
  const [selectedPrincipalId, setSelectedPrincipalId] = useState('')
  const [selectedGroupId, setSelectedGroupId] = useState('')
  const [principalDialogMode, setPrincipalDialogMode] = useState<PrincipalDialogMode>(null)
  const [createPrincipalObjectId, setCreatePrincipalObjectId] = useState('')
  const [createPrincipalLabel, setCreatePrincipalLabel] = useState('')
  const [createPrincipalKind, setCreatePrincipalKind] = useState<PrincipalKind>('user')
  const [createGroupOpen, setCreateGroupOpen] = useState(false)
  const [createGroupName, setCreateGroupName] = useState('')
  const [createGroupDescription, setCreateGroupDescription] = useState('')
  const [principalDraftLabel, setPrincipalDraftLabel] = useState('')
  const [principalDraftKind, setPrincipalDraftKind] = useState<PrincipalKind>('user')
  const [groupDraftDescription, setGroupDraftDescription] = useState('')
  const [principalToAdd, setPrincipalToAdd] = useState('')
  const [confirmation, setConfirmation] = useState<ConfirmationState>(null)

  const principals = useQuery({ queryKey: ['principals'], queryFn: api.listPrincipals })
  const groups = useQuery({ queryKey: ['groups'], queryFn: api.listGroups })
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

  const createPrincipal = useMutation({
    mutationFn: api.createPrincipal,
    onSuccess: async (principal) => {
      setCreatePrincipalObjectId('')
      setCreatePrincipalLabel('')
      setCreatePrincipalKind(principal.kind)
      setPrincipalDialogMode(null)
      setSelectedPrincipalId(principal.id)
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      updateTab(principal.kind === 'user' ? 'users' : 'workloads', true)
    },
  })

  const updatePrincipal = useMutation({
    mutationFn: ({
      principalId,
      payload,
    }: {
      principalId: string
      payload: { kind?: PrincipalKind; label?: string | null }
    }) => api.updatePrincipal(principalId, payload),
    onSuccess: async (principal) => {
      setSelectedPrincipalId(principal.id)
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      const nextTab = principal.kind === 'user' ? 'users' : 'workloads'
      if (activeTab !== nextTab) {
        updateTab(nextTab, true)
      }
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
  const users = useMemo(
    () => principals.data?.filter((principal) => principal.kind === 'user') ?? [],
    [principals.data],
  )
  const workloads = useMemo(
    () => principals.data?.filter((principal) => principal.kind !== 'user') ?? [],
    [principals.data],
  )
  const filteredUsers = useMemo(
    () =>
      users.filter((principal) =>
        matchesSearch(normalizedSearch, principal.label, principal.objectId),
      ),
    [normalizedSearch, users],
  )
  const filteredWorkloads = useMemo(
    () =>
      workloads.filter((principal) =>
        matchesSearch(
          normalizedSearch,
          principal.label,
          principal.objectId,
          principalKindLabels[principal.kind],
        ),
      ),
    [normalizedSearch, workloads],
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

  const visiblePrincipals = activeTab === 'users' ? filteredUsers : filteredWorkloads

  useEffect(() => {
    if (activeTab === 'groups') {
      return
    }
    if (!visiblePrincipals.length) {
      setSelectedPrincipalId('')
      return
    }
    if (!visiblePrincipals.some((principal) => principal.id === selectedPrincipalId)) {
      setSelectedPrincipalId(visiblePrincipals[0].id)
    }
  }, [activeTab, selectedPrincipalId, visiblePrincipals])

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

  useEffect(() => {
    setPrincipalDraftLabel(selectedPrincipal?.label ?? '')
    setPrincipalDraftKind(selectedPrincipal?.kind ?? 'user')
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
        .filter((principal) => !memberIds.has(principal.id))
        .sort((left, right) =>
          getPrincipalName(left).localeCompare(getPrincipalName(right), undefined, {
            sensitivity: 'base',
          }),
        ),
    [memberIds, principals.data],
  )
  const filteredMemberships = useMemo(
    () =>
      (memberships.data ?? []).filter((membership) => {
        const principal = principalsById.get(membership.principalId)
        return matchesSearch(
          normalizedSearch,
          principal?.label,
          principal?.objectId,
          principal ? principalKindLabels[principal.kind] : undefined,
        )
      }),
    [memberships.data, normalizedSearch, principalsById],
  )

  const principalCreateLabel =
    principalDialogMode === 'workload' ? 'Add workload identity' : 'Add user'
  const principalCreateHint =
    principalDialogMode === 'workload'
      ? 'Register a service principal or managed identity by its Entra object ID.'
      : 'Register an Entra user object ID without copying profile details into MOSAIC.'

  const principalListTitle = activeTab === 'workloads' ? 'Workload identities' : 'Users'
  const principalEmptyTitle =
    activeTab === 'workloads' ? 'No workload identities registered' : 'No users registered'
  const principalSearchPlaceholder =
    activeTab === 'workloads'
      ? 'Filter by label, object ID, or workload type'
      : 'Filter by label or object ID'
  const groupSearchPlaceholder = 'Filter groups or visible members'

  const principalDetailHasChanges =
    selectedPrincipal !== undefined &&
    (principalDraftKind !== selectedPrincipal.kind ||
      principalDraftLabel.trim() !== (selectedPrincipal.label ?? ''))

  const groupDetailHasChanges =
    selectedGroup !== undefined &&
    groupDraftDescription.trim() !== (selectedGroup.description ?? '')

  const principalDetailError = updatePrincipal.error ?? deletePrincipal.error
  const groupDetailError =
    updateGroup.error ?? deleteGroup.error ?? addMembership.error ?? removeMembership.error

  function openPrincipalDialog(mode: NonNullable<PrincipalDialogMode>) {
    createPrincipal.reset()
    setPrincipalDialogMode(mode)
    setCreatePrincipalObjectId('')
    setCreatePrincipalLabel('')
    setCreatePrincipalKind(mode === 'user' ? 'user' : 'servicePrincipal')
  }

  function closePrincipalDialog() {
    createPrincipal.reset()
    setPrincipalDialogMode(null)
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
          <Tab value="users">Users</Tab>
          <Tab value="workloads">Workload identities</Tab>
          <Tab value="groups">Groups</Tab>
        </TabList>

        <div className={styles.toolbarControls}>
          <Input
            className={styles.searchInput}
            value={searchQuery}
            placeholder={activeTab === 'groups' ? groupSearchPlaceholder : principalSearchPlaceholder}
            aria-label={activeTab === 'groups' ? groupSearchPlaceholder : principalSearchPlaceholder}
            onChange={(_, data) => setSearchQuery(data.value)}
          />

          <div className={styles.actionRow}>
            <Button
              appearance={activeTab === 'users' ? 'primary' : 'secondary'}
              onClick={() => openPrincipalDialog('user')}
            >
              Add user
            </Button>
            <Button
              appearance={activeTab === 'workloads' ? 'primary' : 'secondary'}
              onClick={() => openPrincipalDialog('workload')}
            >
              Add workload identity
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
                  <Title3 as="h2">Groups</Title3>
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
                              {getPrincipalName(principal)} — {principalKindLabels[principal.kind]}
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
                        Register a user or workload identity before adding group members.
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
        <Loading label={`Loading ${principalListTitle.toLowerCase()}`} />
      ) : principals.error ? (
        <ErrorState error={principals.error} />
      ) : !(activeTab === 'users' ? users.length : workloads.length) ? (
        <EmptyState title={principalEmptyTitle}>
          {activeTab === 'workloads'
            ? 'Add a service principal or managed identity to begin.'
            : 'Add an Entra user object ID to begin.'}
        </EmptyState>
      ) : (
        <div className={styles.contentGrid}>
          <Card className={styles.listCard}>
            <div className={styles.cardHeader}>
              <div className={styles.cardHeaderText}>
                <Title3 as="h2">{principalListTitle}</Title3>
                <Text className={styles.muted}>
                  {visiblePrincipals.length} of {(activeTab === 'users' ? users : workloads).length}{' '}
                  shown
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
                      <th scope="col">Object ID</th>
                      <th scope="col">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visiblePrincipals.map((principal) => {
                      const isSelected = principal.id === selectedPrincipalId
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
                                {principal.label ? principal.objectId : 'No local label'}
                              </span>
                            </button>
                          </td>
                          <td>
                            <PrincipalKindBadge kind={principal.kind} />
                          </td>
                          <td>
                            <code className={styles.monospace}>{principal.objectId}</code>
                          </td>
                          <td>
                            <LiveBadge />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className={styles.emptyPanel}>
                <EmptyState title={`No matching ${principalListTitle.toLowerCase()}`}>
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
                      {selectedPrincipal.kind === 'user' ? 'User' : 'Workload identity'}
                    </Text>
                    <Title3 as="h2">{getPrincipalName(selectedPrincipal)}</Title3>
                    <div className={styles.badgeRow}>
                      <LiveBadge />
                      <PrincipalKindBadge kind={selectedPrincipal.kind} />
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
                      <option value="servicePrincipal">Service principal</option>
                      <option value="managedIdentity">Managed identity</option>
                    </Select>
                  </Field>

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
              </>
            ) : (
              <EmptyState title={`Select a ${activeTab === 'users' ? 'user' : 'workload identity'}`}>
                Choose a record to inspect and edit its local metadata.
              </EmptyState>
            )}
          </Card>
        </div>
      )}

      <Dialog open={principalDialogMode !== null} onOpenChange={(_, data) => !data.open && closePrincipalDialog()}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>{principalCreateLabel}</DialogTitle>
            <DialogContent>
              <form className={styles.dialogForm} onSubmit={submitCreatePrincipal}>
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

                {principalDialogMode === 'workload' ? (
                  <Field label="Workload type">
                    <Select
                      value={createPrincipalKind}
                      onChange={(event) => setCreatePrincipalKind(event.target.value as PrincipalKind)}
                    >
                      <option value="servicePrincipal">Service principal</option>
                      <option value="managedIdentity">Managed identity</option>
                    </Select>
                  </Field>
                ) : (
                  <div className={styles.readOnlyPanel}>
                    <Text className={styles.readOnlyLabel}>Principal type</Text>
                    <div className={styles.badgeRow}>
                      <PrincipalKindBadge kind="user" />
                    </div>
                  </div>
                )}

                <Field label="Local label">
                  <Input
                    value={createPrincipalLabel}
                    onChange={(_, data) => setCreatePrincipalLabel(data.value)}
                  />
                </Field>

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
              </form>
            </DialogContent>
          </DialogBody>
        </DialogSurface>
      </Dialog>

      <Dialog open={createGroupOpen} onOpenChange={(_, data) => !data.open && closeGroupDialog()}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Create group</DialogTitle>
            <DialogContent>
              <form className={styles.dialogForm} onSubmit={submitCreateGroup}>
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
              </form>
            </DialogContent>
          </DialogBody>
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
