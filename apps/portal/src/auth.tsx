import { InteractionStatus } from '@azure/msal-browser'
import { useIsAuthenticated, useMsal } from '@azure/msal-react'
import { Button, Card, Spinner, Text, Title2, makeStyles } from '@fluentui/react-components'
import type { PropsWithChildren } from 'react'
import { runtimeConfig } from './runtime-config'

const useStyles = makeStyles({
  page: {
    minHeight: '100vh',
    display: 'grid',
    placeItems: 'center',
    padding: '24px',
    backgroundColor: 'var(--mosaic-bg)',
  },
  card: {
    width: 'min(460px, 100%)',
    gap: '16px',
    padding: '32px',
    border: '1px solid var(--mosaic-border)',
    backgroundColor: 'var(--mosaic-surface)',
  },
})

export function AuthGate({ children }: PropsWithChildren) {
  const styles = useStyles()
  const authenticated = useIsAuthenticated()
  const { instance, inProgress } = useMsal()

  if (runtimeConfig.authMode === 'local') {
    return children
  }
  if (inProgress !== InteractionStatus.None) {
    return (
      <main className={styles.page}>
        <Spinner label="Completing Microsoft Entra sign-in" />
      </main>
    )
  }
  if (!authenticated) {
    return (
      <main className={styles.page}>
        <Card className={styles.card}>
          <Title2>MOSAIC portal access</Title2>
          <Text>Sign in with an account assigned the MOSAIC User application role.</Text>
          <Button
            appearance="primary"
            onClick={() => void instance.loginRedirect({ scopes: [runtimeConfig.entraApiScope] })}
          >
            Sign in with Microsoft
          </Button>
        </Card>
      </main>
    )
  }
  return children
}
