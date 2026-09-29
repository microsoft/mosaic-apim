import { useMsal } from '@azure/msal-react'
import {
  Button,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import type { PropsWithChildren, ReactNode } from 'react'
import { useMosaicApi } from './api'
import { runtimeConfig } from './runtime-config'

function statusOf(error: unknown): number | undefined {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const { status } = error as { status?: unknown }
    return typeof status === 'number' ? status : undefined
  }
  return undefined
}

/** A 4xx is the API's answer, so asking again would not change it. */
function isClientError(error: unknown) {
  const status = statusOf(error)
  return status !== undefined && status >= 400 && status < 500
}

function AccessPage({ children }: { children: ReactNode }) {
  return (
    <main className="centered-page">
      <div className="access-denied-card">{children}</div>
    </main>
  )
}

function AccessDenied({
  title,
  account,
  onSignOut,
  children,
}: {
  title: string
  account?: string
  onSignOut: () => void
  children: ReactNode
}) {
  return (
    <AccessPage>
      <MessageBar intent="warning" layout="multiline">
        <MessageBarBody>
          <MessageBarTitle>{title}</MessageBarTitle>
          {children}
        </MessageBarBody>
      </MessageBar>
      {account && <p className="access-denied-hint">Signed in as {account}</p>}
      {/* MSAL keeps the access token it already holds, so a new role reaches the API only after a new sign-in. */}
      <p className="access-denied-hint">
        If an administrator has just granted you a role, sign out and sign in again.
      </p>
      <div className="access-denied-actions">
        <Button onClick={onSignOut}>Sign out</Button>
      </div>
    </AccessPage>
  )
}

function AccessCheckFailed({
  error,
  onRetry,
  onSignOut,
}: {
  error: unknown
  onRetry: () => void
  onSignOut: () => void
}) {
  const message = error instanceof Error ? error.message : 'An unexpected error occurred.'
  return (
    <AccessPage>
      <MessageBar intent="error" layout="multiline">
        <MessageBarBody>
          <MessageBarTitle>Unable to check your access</MessageBarTitle>
          The console could not confirm your MOSAIC role: {message}
        </MessageBarBody>
      </MessageBar>
      <div className="access-denied-actions">
        <Button appearance="primary" onClick={onRetry}>
          Try again
        </Button>
        <Button onClick={onSignOut}>Sign out</Button>
      </div>
    </AccessPage>
  )
}

function EntraConsoleAccessGate({ children }: PropsWithChildren) {
  const { instance, accounts } = useMsal()
  const api = useMosaicApi()
  const account = accounts[0]
  const access = useQuery({
    queryKey: ['console', 'access', account?.homeAccountId],
    queryFn: api.getConsoleAccess,
    // Roles arrive in the access token, so they cannot change until the next sign-in.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: (failureCount, error) => !isClientError(error) && failureCount < 1,
  })
  const signOut = () => void instance.logoutRedirect()
  const accountLabel = account?.username || account?.name

  if (access.data?.isAdmin) {
    return children
  }
  if (access.data) {
    // The API answers only callers holding Admin or User, so a non-admin answer means User alone.
    return (
      <AccessDenied
        title="This console is for MOSAIC administrators"
        account={accountLabel}
        onSignOut={signOut}
      >
        Your account has the User role, which opens the MOSAIC end-user portal. To use this
        console, an administrator must grant you the Admin role.
      </AccessDenied>
    )
  }
  if (access.isError && statusOf(access.error) === 403) {
    return (
      <AccessDenied
        title="You do not have access to MOSAIC yet"
        account={accountLabel}
        onSignOut={signOut}
      >
        An administrator must grant you a MOSAIC role. The Admin role opens this console; the
        User role opens the MOSAIC end-user portal.
      </AccessDenied>
    )
  }
  if (access.isError) {
    return (
      <AccessCheckFailed
        error={access.error}
        onRetry={() => void access.refetch()}
        onSignOut={signOut}
      />
    )
  }
  return (
    <main className="centered-page">
      <Spinner label="Checking your access to the MOSAIC console" />
    </main>
  )
}

/**
 * Renders the admin console only for callers the API confirms hold the MOSAIC Admin role, and a
 * single explanatory page for everyone else. Local mode has no Entra roles, so it is not gated.
 */
export function ConsoleAccessGate({ children }: PropsWithChildren) {
  if (runtimeConfig.authMode !== 'entra') {
    return children
  }
  return <EntraConsoleAccessGate>{children}</EntraConsoleAccessGate>
}
