import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
} from '@fluentui/react-components'
import type { ReactNode } from 'react'
import { ApiError } from '../api'

export function Loading({ label }: { label: string }) {
  return <Spinner label={label} />
}

export function NoPortalAccess({ onSignOut }: { onSignOut?: () => void }) {
  return (
    <MessageBar intent="warning">
      <MessageBarBody>
        <MessageBarTitle>You do not have access to the portal yet</MessageBarTitle>
        An administrator must grant you the MOSAIC User role before catalog or entitlement data can be shown.
      </MessageBarBody>
      {onSignOut && (
        <MessageBarActions>
          <Button onClick={onSignOut}>Sign out</Button>
        </MessageBarActions>
      )}
    </MessageBar>
  )
}

function errorStatus(error: unknown) {
  if (error instanceof ApiError) {
    return error.status
  }
  if (typeof error === 'object' && error && 'status' in error) {
    return (error as { status?: unknown }).status
  }
  return undefined
}

export type ErrorStateProps = {
  error: unknown
  announcementKey?: string | number
} & ({ context?: 'load'; title?: string } | { context: 'action'; title: string })

export function ErrorState({
  error, context = 'load', title = 'Unable to load data', announcementKey,
}: ErrorStateProps) {
  if (context === 'load' && errorStatus(error) === 403) {
    return <NoPortalAccess />
  }
  const message = error instanceof Error ? error.message : 'An unexpected error occurred.'
  return (
    <MessageBar key={announcementKey} intent="error">
      <MessageBarBody>
        <MessageBarTitle>{title}</MessageBarTitle>
        {message}
      </MessageBarBody>
    </MessageBar>
  )
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  )
}
