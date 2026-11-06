import { Badge, Button, Popover, PopoverSurface, PopoverTrigger, Spinner, Text } from '@fluentui/react-components'
import { PersonClockRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { adminRequestKey, adminRequestLink, combineAdminRequests } from '../request-queue'
import type { AdminRequest, Principal } from '../types'
import styles from './RequestNotifications.module.css'

function RequestItem({ item, principals, principalsReady, onSelect }: {
  item: AdminRequest
  principals: Principal[] | undefined
  principalsReady: boolean
  onSelect: () => void
}) {
  const api = useMosaicApi()
  const request = item.request
  const requesterPrincipalId = item.requestKind === 'resource' ? item.request.requesterPrincipalId : undefined
  const principal = principals?.find((item) =>
    item.id === requesterPrincipalId || item.objectId?.toLowerCase() === request.requesterObjectId.toLowerCase())
  const directory = useQuery({
    queryKey: ['directory-object', request.requesterObjectId],
    queryFn: () => api.getDirectoryObject(request.requesterObjectId),
    enabled: principalsReady && !principal?.label?.trim(),
    retry: false,
    throwOnError: false,
    staleTime: 300_000,
  })
  const name = principal?.label?.trim() || directory.data?.displayName?.trim()
  const resource = item.requestKind === 'costCenter' ? item.request.name : item.requestKind === 'costCenterMembership' ? item.request.costCenter.name
    : item.request.resourceSummary?.displayName ?? item.request.resourceSnapshot?.displayName ?? 'Resource details unavailable'
  return (
    <li>
      <Button appearance="subtle" className={styles.request} onClick={onSelect}>
        <span className={styles.requestName}>{name || (!principalsReady || directory.isPending ? 'Loading requester name...' : 'Requester name unavailable')}</span>
        <span className={styles.resource}>{item.requestKind === 'costCenter' ? 'Requests cost center' : item.requestKind === 'costCenterMembership' ? 'Requests to join' : 'Requests access to'} {resource}</span>
        <span className={styles.context}>{item.requestKind === 'costCenter' ? 'Cost-center creation' : item.requestKind === 'costCenterMembership' ? 'Cost-center membership' : 'Resource access'}</span>
        <span className={styles.context}>
          {item.requestKind === 'resource' && item.request.costCenter?.name && <span>{item.request.costCenter.name} · </span>}
          <time dateTime={request.createdAt}>{new Date(request.createdAt).toLocaleString()}</time>
        </span>
      </Button>
      {directory.isError && !principal?.label && (
        <div className={styles.lookupError}>
          <Text size={200}>Requester lookup unavailable.</Text>
          <Button size="small" appearance="subtle" onClick={() => void directory.refetch()}>Retry name lookup</Button>
        </div>
      )}
    </li>
  )
}

export function RequestNotifications() {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const headingId = useId()
  const counts = useQuery({
    queryKey: ['request-counts'],
    queryFn: () => api.getRequestCounts(),
    throwOnError: false,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  })
  const requests = useQuery({
    queryKey: ['access-requests', 'pending'],
    queryFn: () => api.listAccessRequests('pending'),
    throwOnError: false,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    enabled: open,
  })
  const centers = useQuery({
    queryKey: ['cost-center-requests', 'pending'],
    queryFn: () => api.listCostCenterRequests('pending'),
    throwOnError: false,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    enabled: open,
  })
  const memberships = useQuery({
    queryKey: ['cost-center-membership-requests', 'notifications'],
    queryFn: () => api.listCostCenterMembershipRequests(),
    throwOnError: false, refetchInterval: 60_000, refetchIntervalInBackground: false, enabled: open,
  })
  const queue = combineAdminRequests(requests.data ?? [], centers.data ?? [],
    (memberships.data ?? []).filter((item) => item.state === 'pending'
      || (item.state === 'approved' && (item.needsAdministratorAttention || item.membershipState !== 'succeeded'))))
  const principals = useQuery({
    queryKey: ['principals'],
    queryFn: api.listPrincipals,
    enabled: open && queue.length > 0,
    throwOnError: false,
  })
  function openQueue(id?: string, kind: AdminRequest['requestKind'] = 'resource') {
    setOpen(false)
    navigate(adminRequestLink(kind, id))
  }
  const notificationLabel = counts.isSuccess
    ? `Pending approvals, ${counts.data.pendingCount} pending ${counts.data.pendingCount === 1 ? 'request' : 'requests'}`
    : 'Pending approvals'
  return (
    <Popover open={open} onOpenChange={(_, data) => setOpen(data.open)} positioning="below-end" trapFocus>
      <PopoverTrigger disableButtonEnhancement>
        <Button appearance="subtle" icon={<PersonClockRegular />} aria-label={notificationLabel}>
          {counts.isSuccess && counts.data.pendingCount > 0 && <Badge color="important" appearance="filled">{counts.data.pendingCount}</Badge>}
        </Button>
      </PopoverTrigger>
      <PopoverSurface className={styles.popover} aria-labelledby={headingId}>
        <h2 id={headingId}>Requests</h2>
        {counts.isSuccess && <Text>{counts.data.pendingResourceCount} resource · {counts.data.pendingCostCenterCount} cost-center creation · {counts.data.pendingMembershipCount ?? 0} membership</Text>}
        {counts.isError && <Button onClick={() => void counts.refetch()}>Pending count unavailable — retry</Button>}
        {(requests.isPending || centers.isPending || memberships.isPending) && <Spinner size="tiny" label="Checking pending requests" />}
        {(requests.isError || centers.isError || memberships.isError) && <Button onClick={() => { void requests.refetch(); void centers.refetch(); void memberships.refetch() }}>Requests unavailable — retry</Button>}
        {requests.isSuccess && centers.isSuccess && memberships.isSuccess && queue.length === 0 && <Text>No pending requests</Text>}
        {principals.isError && <Text className={styles.lookupError}>Registered names unavailable; checking the directory for visible requests.</Text>}
        {queue.length > 0 && <ul className={styles.list}>
          {queue.slice(0, 5).map((item) => (
            <RequestItem key={adminRequestKey(item.requestKind, item.request.id)} item={item} principals={principals.data} principalsReady={!principals.isPending}
              onSelect={() => openQueue(item.request.id, item.requestKind)} />
          ))}
        </ul>}
        <Button appearance="subtle" onClick={() => openQueue()}>Review requests</Button>
      </PopoverSurface>
    </Popover>
  )
}
