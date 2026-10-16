import { useMsal } from '@azure/msal-react'
import { useState } from 'react'
import { Button, Card, Text, Title3 } from '@fluentui/react-components'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import { IdentityAvatar } from '../components/IdentityAvatar'
import { NotificationPreferences } from '../components/NotificationPreferences'
import { runtimeConfig } from '../runtime-config'
import styles from './AdminProfilePage.module.css'

export function AdminProfilePage() {
  const [editingNotifications, setEditingNotifications] = useState(false)
  const { instance, accounts } = useMsal()
  const account = accounts[0]
  const displayName = account?.name ?? (runtimeConfig.authMode === 'local' ? 'Local administrator' : 'No signed-in administrator')
  const username = account?.username ?? 'No Entra account connected'
  const unavailable = 'Not available without a signed-in account'

  return (
    <section className={styles.page}>
      {!editingNotifications && <>
      <PageHeader
        title="Admin Profile"
        description="Review the identity and authentication context of your administrator session."
        actions={
          runtimeConfig.authMode === 'entra' ? (
            <Button onClick={() => void instance.logoutRedirect()}>
              Sign out
            </Button>
          ) : undefined
        }
      />
      {runtimeConfig.authMode === 'local' && (
        <PreviewNotice kind="local">
          Local development authentication is enabled. No Entra account is connected.
        </PreviewNotice>
      )}

      <div className={styles.grid}>
        <Card className={styles.card}>
          <div className={styles.identityHeader}>
            <IdentityAvatar name={displayName} objectId={account?.localAccountId} size={56} />
            <div>
              <Title3 as="h2">Administrator identity</Title3>
              <Text className={styles.cardDescription}>
                Account details come from your signed-in Entra account. This page does not generate
                credentials or change role assignments.
              </Text>
            </div>
          </div>

          <dl className={styles.definitionList}>
            <div className={styles.definitionRow}>
              <dt>Display name</dt>
              <dd>{displayName}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Sign-in name</dt>
              <dd>{username}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Authentication mode</dt>
              <dd>{runtimeConfig.authMode}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Tenant ID</dt>
              <dd>{account?.tenantId ?? unavailable}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Environment</dt>
              <dd>{account?.environment ?? unavailable}</dd>
            </div>
            <div className={styles.definitionRow}>
              <dt>Home account ID</dt>
              <dd>{account?.homeAccountId ?? unavailable}</dd>
            </div>
          </dl>
        </Card>

      </div>
      </>}
      <NotificationPreferences onEditingChange={setEditingNotifications} />
    </section>
  )
}
