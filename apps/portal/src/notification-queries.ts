import { useMsal } from '@azure/msal-react'
import { notificationHealthKey, notificationInboxKey, notificationPreferencesKey } from './notification-format'

export function useNotificationKeys() {
  const { accounts } = useMsal()
  const account = accounts[0]
  const owner = account ? `${account.tenantId ?? ''}:${account.homeAccountId ?? account.localAccountId ?? account.username}` : 'local'
  return {
    owner,
    inbox: [...notificationInboxKey, owner],
    preferences: [...notificationPreferencesKey, owner],
    health: [...notificationHealthKey, owner],
  }
}
