import { Badge, Button, Checkbox, Field, Popover, PopoverSurface, PopoverTrigger, Select, Text, Title3 } from '@fluentui/react-components'
import { AlertRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { NOTIFICATION_CATEGORIES } from '../admin-types'
import { notificationTarget } from '../admin-governance'
import { ErrorState, Loading } from './AsyncState'
import { PageHeader } from './PageHeader'
import { ResourceWorkbench } from './ResourceWorkbench'
import { useWorkbenchLocation } from './useWorkbenchLocation'
import styles from './AdminGovernance.module.css'

export function NotificationBell() {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const inbox = useQuery({ queryKey: ['admin-notification-inbox', 'bell'], queryFn: () => api.listNotificationInbox({ unreadOnly: true }), retry: false, refetchInterval: 60000, refetchIntervalInBackground: false })
  return <Popover open={open} onOpenChange={(_, d) => setOpen(d.open)} positioning="below-end" trapFocus>
    <PopoverTrigger disableButtonEnhancement><Button appearance="subtle" icon={<AlertRegular />} aria-label={inbox.data ? `Notification inbox, ${inbox.data.unreadCount} unread` : 'Notification inbox, unread count unavailable'}>
      {inbox.data && inbox.data.unreadCount > 0 && <Badge color="important">{inbox.data.unreadCount}</Badge>}
    </Button></PopoverTrigger>
    <PopoverSurface className={styles.bell}>
      <div className={styles.stack}><Title3 as="h2">Notification inbox</Title3>
        <Text>Unread notifications are separate from pending approval requests.</Text>
        {inbox.isPending && <Loading label="Loading unread notifications" />}
        {inbox.isError && <><ErrorState error={inbox.error} /><Button onClick={() => void inbox.refetch()}>Retry inbox</Button></>}
        {inbox.data?.items.slice(0, 5).map(item => <Button key={item.id} appearance="subtle" onClick={() => { setOpen(false); navigate(`/notifications?selected=${encodeURIComponent(item.id)}`) }}>{item.title}</Button>)}
        {inbox.data && !inbox.data.items.length && <Text>No unread notifications.</Text>}
        <Button onClick={() => { setOpen(false); navigate('/notifications') }}>Open notification inbox</Button>
      </div>
    </PopoverSurface>
  </Popover>
}
export function NotificationInboxPage() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const workbench = useWorkbenchLocation({ section: 'inbox' })
  const [category, setCategory] = useState('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [tokens, setTokens] = useState<string[]>([])
  const token = tokens.at(-1)
  const inbox = useQuery({ queryKey: ['admin-notification-inbox', category, unreadOnly, token], queryFn: () => api.listNotificationInbox({ category: category || undefined, unreadOnly, continuationToken: token }), retry: false })
  const selected = useQuery({
    queryKey: ['admin-notification-inbox-item', workbench.selectedId],
    queryFn: () => api.getNotificationInboxItem(workbench.selectedId!),
    enabled: Boolean(workbench.selectedId) && !inbox.data?.items.some(item => item.id === workbench.selectedId),
    retry: false,
  })
  const items = selected.data && selected.data.id === workbench.selectedId && !inbox.data?.items.some(item => item.id === selected.data.id)
    ? [...(inbox.data?.items ?? []), selected.data] : inbox.data?.items ?? []
  const read = useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) => api.setNotificationRead(id, read),
    onSuccess: value => { client.setQueryData(['admin-notification-inbox-item', value.id], value); void client.invalidateQueries({ queryKey: ['admin-notification-inbox'] }); if (unreadOnly) workbench.select(null) },
  })
  return <div className={styles.stack}><PageHeader title="Notification inbox" description="Persistent administrator notifications. Read/unread status does not decide a request or resolve an operational issue." actions={<RouterLink to="/profile">Manage personal notifications</RouterLink>} />
    {inbox.isError && <><ErrorState error={inbox.error} /><Button onClick={() => void inbox.refetch()}>Retry inbox</Button></>}
    {read.isError && <ErrorState error={read.error} />}
    <ResourceWorkbench title="Administrator notifications" description={inbox.data ? `${inbox.data.unreadCount} unread notifications` : 'Unread count unavailable'}
      items={items} getId={i => i.id} selectedId={workbench.selectedId} onSelect={workbench.select} listLabel="Notifications" detailLabel="Selected notification"
      missingSelection={selected.isPending ? <Loading label="Loading exact notification" /> : selected.isError ? <><ErrorState error={selected.error} /><Button onClick={() => void selected.refetch()}>Retry selected notification</Button></> : 'This exact notification is no longer available.'}
      emptyList={inbox.isPending ? <Loading label="Loading notifications" /> : inbox.isError ? 'Inbox unavailable. Retry loading notifications.' : 'No notifications match these filters.'}
      toolbar={<><Field label="Notification category"><Select value={category} onChange={(_, d) => { setCategory(d.value); setTokens([]); workbench.select(null) }}><option value="">All categories</option>{NOTIFICATION_CATEGORIES.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}</Select></Field>
        <Checkbox label="Unread only" checked={unreadOnly} onChange={(_, d) => { setUnreadOnly(Boolean(d.checked)); setTokens([]); workbench.select(null) }} /></>}
      renderListItem={i => <div className={styles.stack}><strong>{i.title}</strong><Text>{new Date(i.occurredAt).toLocaleString()}</Text>{!i.readAt && <Badge appearance="outline">Unread</Badge>}</div>}
      renderDetail={i => <div className={styles.stack}><Title3 as="h3">{i.title}</Title3><Text>{i.summary}</Text><Text>{new Date(i.occurredAt).toLocaleString()} · {NOTIFICATION_CATEGORIES.find(c => c.value === i.category)?.label}</Text>
        {i.subject.runId && <Text>Recorded operation run: {i.subject.runId}</Text>}
        {i.subject.endpointId && !i.subject.endpointKind && <Text>Endpoint: {i.subject.endpointId}. This historical notification does not identify the endpoint kind; open the affected model or MCP endpoint and review this exact sync run.</Text>}
        {i.subject.integration === 'gatewayApply' && i.subject.runId && (!i.subject.publicationId || !i.subject.publicationTarget) && <Text>This historical notification does not identify its publication and target. The gateway link opens related inventory, not the exact publication run.</Text>}
        <div className={styles.toolbar}><Button disabled={read.isPending} onClick={() => read.mutate({ id: i.id, read: !i.readAt })}>{i.readAt ? 'Mark unread' : 'Mark read'}</Button>
          {notificationTarget(i.subject) && <RouterLink to={notificationTarget(i.subject)!}>Open related record</RouterLink>}</div></div>}
      pager={<div className={styles.toolbar}><Button disabled={!tokens.length || inbox.isFetching} onClick={() => { setTokens(values => values.slice(0, -1)); workbench.select(null) }}>Previous page</Button>
        <Button disabled={!inbox.data?.continuationToken || inbox.isFetching} onClick={() => { setTokens(values => [...values, inbox.data!.continuationToken!]); workbench.select(null) }}>Next page</Button></div>} />
  </div>
}
