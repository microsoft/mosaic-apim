import { Badge, Button, Popover, PopoverSurface, PopoverTrigger, Spinner, Text } from '@fluentui/react-components'
import { PersonClockRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import type { AccessRequest, Principal } from '../types'
import styles from './RequestNotifications.module.css'

function RequestItem({ request, principals, principalsReady, onSelect }: {
  request: AccessRequest
  principals: Principal[] | undefined
  principalsReady: boolean
  onSelect: () => void
}) {
  const api = useMosaicApi()
  const principal = principals?.find((item) =>
    item.id === request.requesterPrincipalId || item.objectId?.toLowerCase() === request.requesterObjectId.toLowerCase())
  const directory = useQuery({
    queryKey: ['directory-object', request.requesterObjectId],
    queryFn: () => api.getDirectoryObject(request.requesterObjectId),
    enabled: principalsReady && !principal?.label?.trim(),
    retry: false,
    throwOnError: false,
    staleTime: 300_000,
  })
  const name = principal?.label?.trim() || directory.data?.displayName?.trim()
  const resource = request.resourceSummary?.displayName ?? request.resourceSnapshot?.displayName ?? 'Resource details unavailable'
  return (
    <li>
      <Button appearance="subtle" className={styles.request} onClick={onSelect}>
        <span className={styles.requestName}>{name || (!principalsReady || directory.isPending ? 'Loading requester name...' : 'Requester name unavailable')}</span>
        <span className={styles.resource}>Requests access to {resource}</span>
        <span className={styles.context}>
          {request.costCenter?.name && <span>{request.costCenter.name} · </span>}
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
  const requests = useQuery({
    queryKey: ['access-requests', 'pending'],
    queryFn: () => api.listAccessRequests('pending'),
    throwOnError: false,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  })
  const principals = useQuery({
    queryKey: ['principals'],
    queryFn: api.listPrincipals,
    enabled: open && requests.isSuccess && requests.data.length > 0,
    throwOnError: false,
  })
  function openQueue(id?: string) {
    setOpen(false)
    const params = new URLSearchParams({ tab: 'requests' })
    if (id) params.set('selected', id)
    navigate(`/entitlements?${params}`)
  }
  const notificationLabel = requests.isSuccess
    ? `Pending approvals, ${requests.data.length} pending ${requests.data.length === 1 ? 'request' : 'requests'}`
    : 'Pending approvals'
  return (
    <Popover open={open} onOpenChange={(_, data) => setOpen(data.open)} positioning="below-end" trapFocus>
      <PopoverTrigger disableButtonEnhancement>
        <Button appearance="subtle" icon={<PersonClockRegular />} aria-label={notificationLabel}>
          {requests.isSuccess && requests.data.length > 0 && <Badge color="important" appearance="filled">{requests.data.length}</Badge>}
        </Button>
      </PopoverTrigger>
      <PopoverSurface className={styles.popover} aria-labelledby={headingId}>
        <h2 id={headingId}>Access requests</h2>
        {requests.isPending && <Spinner size="tiny" label="Checking pending requests" />}
        {requests.isError && <Button onClick={() => void requests.refetch()}>Requests unavailable — retry</Button>}
        {requests.isSuccess && requests.data.length === 0 && <Text>No pending access requests</Text>}
        {principals.isError && <Text className={styles.lookupError}>Registered names unavailable; checking the directory for visible requests.</Text>}
        {requests.isSuccess && <ul className={styles.list}>
          {requests.data.slice(0, 5).map((request) => (
            <RequestItem key={request.id} request={request} principals={principals.data} principalsReady={!principals.isPending} onSelect={() => openQueue(request.id)} />
          ))}
        </ul>}
        <Button appearance="subtle" onClick={() => openQueue()}>Review access requests</Button>
      </PopoverSurface>
    </Popover>
  )
}
