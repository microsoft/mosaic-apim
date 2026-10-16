import { Badge } from '@fluentui/react-components'
import { AlertRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { NavLink } from 'react-router-dom'
import { usePortalApi } from '../api'
import { useNotificationKeys } from '../notification-queries'

export function NotificationBell({ enabled }: { enabled: boolean }) {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const inbox = useQuery({
    queryKey: [...keys.inbox, 'count'],
    queryFn: () => api.listNotifications({ limit: 1 }),
    enabled,
    refetchInterval: 60_000,
  })
  const label = inbox.isError ? 'Notifications — unread count unavailable'
    : inbox.isSuccess ? `Notifications — ${inbox.data.unreadCount} unread`
      : 'Notifications — checking unread count'
  return (
    <NavLink className="notification-bell" to="/notifications" aria-label={label} title={label}>
      <AlertRegular aria-hidden="true" />
      {inbox.isSuccess && inbox.data.unreadCount > 0 &&
        <Badge appearance="filled" aria-hidden="true">{inbox.data.unreadCount > 99 ? '99+' : inbox.data.unreadCount}</Badge>}
    </NavLink>
  )
}
