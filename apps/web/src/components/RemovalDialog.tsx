import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Text,
  useId,
} from '@fluentui/react-components'
import type { ReactNode } from 'react'
import { ApiError } from '../api'
import styles from './RemovalDialog.module.css'

interface BlockingPublication {
  id: string
  displayName: string
  status: string
}

/** The publications a server refusal names, when it names any. */
function blockingPublications(error: unknown): BlockingPublication[] {
  if (!(error instanceof ApiError)) return []
  const listed = error.body?.details?.publications
  if (!Array.isArray(listed)) return []
  return listed.flatMap((item: unknown) => {
    if (typeof item !== 'object' || item === null) return []
    const { id, displayName, status } = item as Record<string, unknown>
    return typeof id === 'string' && typeof displayName === 'string'
      ? [{ id, displayName, status: typeof status === 'string' ? status : 'unknown' }]
      : []
  })
}

/**
 * Confirms a removal, then keeps the dialog open to show the server's refusal if it refuses. A
 * refusal explains what to do first, so it belongs next to the action it refused rather than under
 * a page-level error.
 */
export function RemovalDialog({
  open,
  title,
  children,
  confirmLabel,
  refusalTitle,
  pending,
  error,
  statusLabel = (status) => status,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  children: ReactNode
  confirmLabel: string
  refusalTitle: string
  pending: boolean
  error: unknown
  statusLabel?: (status: string) => string
  onConfirm: () => void
  onCancel: () => void
}) {
  const textId = useId('removal-dialog-')
  const blocking = blockingPublications(error)

  return (
    <Dialog
      modalType="alert"
      open={open}
      onOpenChange={(_, data) => {
        if (!data.open && !pending) onCancel()
      }}
    >
      <DialogSurface aria-describedby={textId}>
        <DialogBody>
          <DialogTitle>{title}</DialogTitle>
          <DialogContent className={styles.content}>
            <div id={textId} className={styles.content}>
              {children}
            </div>
            {error != null && (
              <MessageBar intent="error" role="alert">
                <MessageBarBody>
                  <MessageBarTitle>{refusalTitle}</MessageBarTitle>
                  {error instanceof Error ? error.message : 'An unexpected error occurred.'}
                </MessageBarBody>
              </MessageBar>
            )}
            {blocking.length > 0 && (
              <div className={styles.content}>
                <Text weight="semibold">Unpublish or recover these first</Text>
                <ul className={styles.list} aria-label="Publications blocking removal">
                  {blocking.map((publication) => (
                    <li key={publication.id}>
                      {publication.displayName} ({statusLabel(publication.status)})
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" disabled={pending} onClick={onCancel}>
              Cancel
            </Button>
            <Button appearance="primary" disabledFocusable={pending} onClick={onConfirm}>
              {pending ? 'Removing…' : confirmLabel}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
