import { Button, MessageBar, MessageBarBody, Spinner } from '@fluentui/react-components'
import { ArrowLeftRegular } from '@fluentui/react-icons'
import { type ReactNode, useEffect, useId, useRef } from 'react'
import { type RequestWorkspaceLeave, WorkspaceLeaveGuard } from './WorkspaceLeaveGuard'
import styles from './WorkspaceEditorFrame.module.css'

export interface WorkspaceEditorFrameProps {
  title: ReactNode
  description?: ReactNode
  children: ReactNode | ((requestLeave: RequestWorkspaceLeave) => ReactNode)
  onBack: () => void
  onCancel?: () => void
  backLabel?: string
  actions?: ReactNode | ((requestLeave: RequestWorkspaceLeave) => ReactNode)
  primaryAction?: { label: string; onClick: () => void; disabled?: boolean }
  dirty?: boolean
  pending?: boolean
  pendingLabel?: string
  error?: ReactNode
  validationSummary?: ReactNode
  className?: string
}

export function WorkspaceEditorFrame({
  title, description, children, onBack, onCancel, backLabel = 'Back',
  actions, primaryAction, dirty = false, pending = false, pendingLabel = 'Saving…',
  error, validationSummary, className,
}: WorkspaceEditorFrameProps) {
  const titleId = useId()
  const heading = useRef<HTMLHeadingElement>(null)
  const validation = useRef<HTMLDivElement>(null)
  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => { if (validationSummary || error) validation.current?.focus({ preventScroll: true }) }, [validationSummary, error])

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
              {onCancel && <Button onClick={() => requestLeave(onCancel)}>Cancel</Button>}
              {typeof actions === 'function' ? actions(requestLeave) : actions}
              {primaryAction && <Button appearance="primary" disabled={pending || primaryAction.disabled}
                onClick={primaryAction.onClick}>{primaryAction.label}</Button>}
            </div>
          </header>
          {(validationSummary || error) && <div ref={validation} tabIndex={-1} className={styles.validation}>
            <MessageBar intent="error"><MessageBarBody>{validationSummary}{error}</MessageBarBody></MessageBar>
          </div>}
          {pending && <div className={styles.pending} role="status"><Spinner size="tiny" />{pendingLabel}</div>}
          <div className={styles.content}>{typeof children === 'function' ? children(requestLeave) : children}</div>
        </section>
      )}
    </WorkspaceLeaveGuard>
  )
}
