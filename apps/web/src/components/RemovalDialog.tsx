import {
  Button,
  AnnounceProvider,
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
import { type ReactNode, type RefObject, useLayoutEffect, useRef } from 'react'
import { ApiError } from '../api'
import { useDialogFocusReturn } from './useDialogFocusReturn'
import { useFocusAwareAnnounce } from './useFocusAwareAnnounce'
import styles from './RemovalDialog.module.css'

interface BlockingRecord {
  id: string
  displayName: string
  status: string
}

/** The records of one kind a server refusal names, when it names any. */
function blockingRecords(error: unknown, kind: 'publications' | 'modelPools'): BlockingRecord[] {
  if (!(error instanceof ApiError)) return []
  const listed = error.body?.details?.[kind]
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
  fallbackFocusRef,
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
  fallbackFocusRef?: RefObject<HTMLElement | null>
}) {
  const textId = useId('removal-dialog-')
  const blocking = blockingRecords(error, 'publications')
  const blockingPools = blockingRecords(error, 'modelPools')
  const surfaceRef = useRef<HTMLDivElement>(null)
  const outcomeRef = useRef<HTMLDivElement>(null)
  const motion = useDialogFocusReturn({ open, surfaceRef, fallbackRef: fallbackFocusRef })
  const announcement = useFocusAwareAnnounce(outcomeRef)
  useLayoutEffect(() => { if (open && error != null) outcomeRef.current?.focus() }, [open, error])

  return (
    <Dialog
      modalType="alert"
      open={open}
      surfaceMotion={motion}
      onOpenChange={(_, data) => {
        if (!data.open && !pending) onCancel()
      }}
    >
      <DialogSurface ref={surfaceRef} aria-describedby={textId}>
        <DialogBody>
          <DialogTitle>{title}</DialogTitle>
          <DialogContent className={styles.content}>
            <div id={textId} className={styles.content}>
              {children}
            </div>
            {error != null && (
              <AnnounceProvider value={announcement}><div ref={outcomeRef} tabIndex={-1}><MessageBar intent="error">
                <MessageBarBody>
                  <MessageBarTitle>{refusalTitle}</MessageBarTitle>
                  {error instanceof Error ? error.message : 'An unexpected error occurred.'}
                </MessageBarBody>
              </MessageBar></div></AnnounceProvider>
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
            {blockingPools.length > 0 && (
              <div className={styles.content}>
                <Text weight="semibold">Take it out of these model pools first</Text>
                <ul className={styles.list} aria-label="Model pools blocking removal">
                  {blockingPools.map((pool) => (
                    <li key={pool.id}>
                      {pool.displayName} ({statusLabel(pool.status)})
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" disabledFocusable={pending} onClick={onCancel}>
              Cancel
            </Button>
            <Button appearance="primary" disabledFocusable={pending} onClick={() => { if (!pending) onConfirm() }}>
              {pending ? 'Removing…' : confirmLabel}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
