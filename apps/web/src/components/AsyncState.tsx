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

export function ErrorState({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : 'An unexpected error occurred.'
  return (
    <MessageBar intent="error">
      <MessageBarBody>
        <MessageBarTitle>Unable to load data</MessageBarTitle>
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
