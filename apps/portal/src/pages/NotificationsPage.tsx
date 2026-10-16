import { Badge, Button, Checkbox, Field, Select } from '@fluentui/react-components'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useState } from 'react'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { notificationCategories, notificationLink } from '../notification-format'
import { useNotificationKeys } from '../notification-queries'
import type { NotificationCategory, NotificationInboxItem } from '../notification-types'

function NotificationInbox() {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const queryClient = useQueryClient()
  const [category, setCategory] = useState<NotificationCategory | ''>('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const inbox = useInfiniteQuery({
    queryKey: [...keys.inbox, 'list', category, unreadOnly],
    queryFn: ({ pageParam }) => api.listNotifications({
      category: category || undefined, unreadOnly, limit: 50, continuationToken: pageParam,
    }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.continuationToken ?? undefined,
  })
  const updateRead = useMutation({
    mutationFn: (item: NotificationInboxItem) => api.setNotificationRead(item.id, !item.readAt),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: keys.inbox }) },
  })
  const items = [...new Map(inbox.data?.pages.flatMap((page) => page.items).map((item) => [item.id, item]) ?? []).values()]
  return (
    <>
      <PageHeader title="Notifications" description="Your persistent inbox for access, usage, and delivery updates."
        actions={<Link to="/notification-preferences">Notification preferences</Link>} />
      <div className="notification-toolbar">
        <Field label="Category">
          <Select value={category} onChange={(_, data) => setCategory(data.value as NotificationCategory | '')}>
            <option value="">All categories</option>
            {notificationCategories.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </Select>
        </Field>
        <Checkbox label="Unread only" checked={unreadOnly} onChange={(_, data) => setUnreadOnly(data.checked === true)} />
        <Button onClick={() => void inbox.refetch()} disabled={inbox.isFetching}>Refresh inbox</Button>
      </div>
      <p className="notification-context">Approval creates a grant; it does not mean gateway access is ready.
        Usage updates show only your attributed activity and authorized shared budget totals, never other members’ activity.</p>
      {inbox.isPending && <Loading label="Loading your notifications" />}
      {inbox.isError && <ErrorState error={inbox.error} />}
      {inbox.isSuccess && items.length === 0 && <EmptyState title={unreadOnly ? 'No unread notifications' : 'No notifications match'}>
        {unreadOnly || category ? 'Change the filters to see more of your inbox.' : 'Updates for your account will appear here when available.'}
      </EmptyState>}
      {items.length > 0 && <ul className="notification-list" aria-label="Your notifications">
        {items.map((item) => {
          const link = notificationLink(item)
          const saving = updateRead.isPending && updateRead.variables?.id === item.id
          return <li key={item.id} className={item.readAt ? 'notification-item' : 'notification-item unread'}>
            <div className="notification-item-heading">
              <h2>{item.title}</h2>
              {!item.readAt && <Badge appearance="tint">Unread</Badge>}
              {item.severity !== 'information' && <Badge appearance="outline" color={item.severity === 'error' ? 'danger' : 'warning'}>{item.severity === 'error' ? 'Error' : 'Warning'}</Badge>}
            </div>
            <p>{item.summary}</p>
            <p className="notification-meta">{notificationCategories.find((category) => category.value === item.category)?.label}
              {' · '}<time dateTime={item.occurredAt}>{new Date(item.occurredAt).toLocaleString()}</time></p>
            <div className="notification-actions">
              {link && <Link to={link.to}>{link.label}</Link>}
              <Button size="small" disabled={updateRead.isPending} onClick={() => updateRead.mutate(item)}>
                {saving ? 'Saving…' : item.readAt ? 'Mark unread' : 'Mark read'}
              </Button>
            </div>
            {updateRead.isError && updateRead.variables?.id === item.id &&
              <p role="alert" className="form-error">Read status was not saved. {updateRead.error.message} Try again.</p>}
          </li>
        })}
      </ul>}
      {inbox.hasNextPage && <Button className="notification-load-more" disabled={inbox.isFetchingNextPage}
        onClick={() => void inbox.fetchNextPage()}>{inbox.isFetchingNextPage ? 'Loading more…' : 'Load more notifications'}</Button>}
    </>
  )
}

export function NotificationsPage() {
  const { owner } = useNotificationKeys()
  return <NotificationInbox key={owner} />
}
