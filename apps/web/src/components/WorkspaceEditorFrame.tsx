import { AnnounceProvider, Button, MessageBar, MessageBarBody, Spinner, useAnnounce } from '@fluentui/react-components'
import { ArrowLeftRegular } from '@fluentui/react-icons'
import { type ReactNode, useEffect, useId, useLayoutEffect, useRef } from 'react'
import { type RequestWorkspaceLeave, WorkspaceLeaveGuard } from './WorkspaceLeaveGuard'
import { useFocusAwareAnnounce } from './useFocusAwareAnnounce'
import styles from './WorkspaceEditorFrame.module.css'

export interface WorkspaceEditorFrameProps {
  title: ReactNode
  description?: ReactNode
  children: ReactNode | ((requestLeave: RequestWorkspaceLeave) => ReactNode)
  onBack: () => void
  onCancel?: () => void
  backLabel?: string
  cancelLabel?: string
  actions?: ReactNode | ((requestLeave: RequestWorkspaceLeave) => ReactNode)
  primaryAction?: { label: string; onClick: () => void; disabled?: boolean }
  dirty?: boolean
  pending?: boolean
  pendingLabel?: string
  error?: ReactNode
  errorKey?: string | number
  validationSummary?: ReactNode
  className?: string
}

export function WorkspaceEditorFrame({
  title, description, children, onBack, onCancel, backLabel = 'Back', cancelLabel = 'Cancel',
  actions, primaryAction, dirty = false, pending = false, pendingLabel = 'Saving…',
  error, errorKey, validationSummary, className,
}: WorkspaceEditorFrameProps) {
  const titleId = useId()
  const heading = useRef<HTMLHeadingElement>(null)
  const validation = useRef<HTMLDivElement>(null)
  const { announce } = useAnnounce()
  const focusedAnnouncement = useFocusAwareAnnounce(validation)
  const hasSummary = Boolean(validationSummary || error)
  const shownSummary = useRef({ active: false, key: errorKey })
  const wasPending = useRef(false)
  useLayoutEffect(() => { heading.current?.focus() }, [])
  useLayoutEffect(() => {
    const previous = shownSummary.current
    shownSummary.current = { active: hasSummary, key: errorKey }
    if (hasSummary && (!previous.active || previous.key !== errorKey)) {
      validation.current?.focus({ preventScroll: true })
    }
  }, [hasSummary, errorKey])
  useEffect(() => {
    if (pending && !wasPending.current) announce(pendingLabel, { polite: true })
    wasPending.current = pending
  }, [pending, pendingLabel, announce])

  return (
    <WorkspaceLeaveGuard dirty={dirty} pending={pending}>
      {(requestLeave) => (
        <section className={[styles.frame, className].filter(Boolean).join(' ')}
          aria-labelledby={titleId} aria-busy={pending || undefined} data-workspace-editor>
          <header className={styles.header}>
            <Button className={styles.back} appearance="subtle" icon={<ArrowLeftRegular />}
              onClick={() => requestLeave(onBack)}>{backLabel}</Button>
            <div className={styles.heading}>
              <h2 id={titleId} ref={heading} tabIndex={-1}>{title}</h2>
              {description && <div className={styles.description}>{description}</div>}
            </div>
            <div className={styles.actions}>
              {onCancel && <Button onClick={() => requestLeave(onCancel)}>{cancelLabel}</Button>}
              {typeof actions === 'function' ? actions(requestLeave) : actions}
              {primaryAction && <Button appearance="primary" disabled={primaryAction.disabled} disabledFocusable={pending}
                onClick={() => { if (!pending && !primaryAction.disabled) primaryAction.onClick() }}>{primaryAction.label}</Button>}
            </div>
          </header>
          {hasSummary && <AnnounceProvider value={focusedAnnouncement}>
            <div ref={validation} tabIndex={-1} className={styles.validation}>
              <MessageBar key={errorKey} intent="error"><MessageBarBody>{validationSummary}{error}</MessageBarBody></MessageBar>
            </div>
          </AnnounceProvider>}
          {pending && <div className={styles.pending}><Spinner size="tiny" />{pendingLabel}</div>}
          <div className={styles.content}>{typeof children === 'function' ? children(requestLeave) : children}</div>
        </section>
      )}
    </WorkspaceLeaveGuard>
  )
}
