import {
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
} from '@fluentui/react-components'
import type { ReactNode } from 'react'

export function Loading({ label }: { label: string }) {
  return <Spinner label={label} />
}

export type ErrorStateProps = {
  error: unknown
  announcementKey?: string | number
} & ({ context?: 'load'; title?: string } | { context: 'action'; title: string })

export function ErrorState({ error, title = 'Unable to load data', announcementKey }: ErrorStateProps) {
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

export function EmptyState({
  title,
  children,
}: {
  title: string
  children: ReactNode
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  )
}
