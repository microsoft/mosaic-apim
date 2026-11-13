import { useMsal } from '@azure/msal-react'
import {
  Button,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
} from '@fluentui/react-components'
import { AppGenericRegular, ArrowRightRegular, PersonCircleRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import type { PropsWithChildren, ReactNode } from 'react'
import { useMosaicApi } from './api'
import { MosaicMark } from './components/MosaicMark'
import { resolvePortalDestination, runtimeConfig } from './runtime-config'

const roleChangeHint = 'If an administrator has just granted you a role, sign out and sign in again.'

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
      <p className="access-denied-hint">{roleChangeHint}</p>
      <div className="access-denied-actions">
        <Button onClick={onSignOut}>Sign out</Button>
      </div>
    </AccessPage>
  )
}

function UserOnlyAccess({ account, onSignOut }: { account?: string; onSignOut: () => void }) {
  const portal = resolvePortalDestination(runtimeConfig.portalUrl, window.location.origin)
  return (
    <main className="centered-page portal-handoff-page">
      <section className="portal-handoff-card" aria-labelledby="console-user-only-title">
        <header className="portal-handoff-brand">
          <span className="brand-mark" aria-hidden="true"><MosaicMark /></span>
          <span className="portal-handoff-wordmark">MOSAIC</span>
          <span className="portal-handoff-app">Administrator console</span>
        </header>
        <div className="portal-handoff-grid">
          <div className="portal-handoff-explanation">
            <span className="portal-handoff-role">User role</span>
            <h1 id="console-user-only-title">This console is for MOSAIC administrators</h1>
            <p>To use this console, an administrator must grant you the Admin role.</p>
          </div>
          <div className="portal-handoff-destination">
            <section className="portal-handoff-panel" aria-labelledby="portal-handoff-title">
              <AppGenericRegular className="portal-handoff-icon" aria-hidden="true" />
              <h2 id="portal-handoff-title">MOSAIC end-user portal</h2>
              <p>Browse the catalog, request access, and see your own usage.</p>
              {portal.status === 'ready' ? (
                <Button as="a" appearance="primary" href={portal.href}
                  icon={<ArrowRightRegular />} iconPosition="after">
                  <span className="portal-handoff-action-label">Open end-user portal</span>
                </Button>
              ) : (
                <MessageBar intent="warning" layout="multiline">
                  <MessageBarBody>
                    <MessageBarTitle>Portal link unavailable</MessageBarTitle>
                    {portal.status === 'missing'
                      ? 'The portal address is not configured. '
                      : "This deployment's portal address is invalid. "}
                    Ask your MOSAIC administrator for the portal address.
                  </MessageBarBody>
                </MessageBar>
              )}
            </section>
            <p className="portal-handoff-note">
              Portal access is checked separately. You may need to sign in.
            </p>
          </div>
          <div className="portal-handoff-account">
            {account && (
              <div className="portal-handoff-account-label">
                <PersonCircleRegular aria-hidden="true" />
                <p>Signed in as {account}</p>
              </div>
            )}
            <p>{roleChangeHint}</p>
            <Button onClick={onSignOut}>Sign out</Button>
          </div>
        </div>
      </section>
    </main>
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
      <UserOnlyAccess account={accountLabel} onSignOut={signOut} />
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
