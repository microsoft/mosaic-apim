import { type ComponentPropsWithoutRef, type ReactNode, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import sectionStyles from './SectionLayout.module.css'
import styles from './WorkspaceDialog.module.css'

export interface WorkspaceDialogProps {
  open: boolean
  onOpenChange: (event: unknown, data: { open: boolean }) => void
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode | ((requestClose: () => void) => ReactNode)
  children: ReactNode | ((requestClose: () => void) => ReactNode)
  dirty?: boolean
  pending?: boolean
}

/**
 * Incremental replacement for substantive dialogs. It takes over the nearest section's
 * content, keeping navigation available and the existing form/mutation body intact.
 */
export function WorkspaceDialog(props: WorkspaceDialogProps) {
  const anchor = useRef<HTMLSpanElement>(null)
  const [host, setHost] = useState<HTMLElement | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const [edited, setEdited] = useState(false)

  useLayoutEffect(() => {
    if (!props.open || !anchor.current?.parentElement) return
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const scroll = { left: window.scrollX, top: window.scrollY }
    const parent = anchor.current.parentElement
    let sectionContent = parent.closest<HTMLElement>(`.${sectionStyles.content}`)
    for (let outer = sectionContent?.parentElement?.closest<HTMLElement>(`.${sectionStyles.content}`);
      outer; outer = outer.parentElement?.closest<HTMLElement>(`.${sectionStyles.content}`)) {
      sectionContent = outer
    }
    const target = sectionContent ?? parent.querySelector<HTMLElement>(`.${sectionStyles.content}`)
      ?? parent.closest<HTMLElement>('[data-resource-workbench]')
      ?? parent.querySelector<HTMLElement>('[data-resource-workbench]') ?? parent
    const hostScroll = { left: target.scrollLeft, top: target.scrollTop }
    const saved = new Map<HTMLElement, {
      hidden: HTMLElement['hidden']; inert: HTMLElement['inert']; display: string; priority: string
    }>()
    const conceal = () => {
      for (const child of Array.from(target.children)) {
        if (!(child instanceof HTMLElement) || child.hasAttribute('data-workspace-editor') || saved.has(child)) continue
        saved.set(child, {
          hidden: child.hidden, inert: child.inert, display: child.style.display,
          priority: child.style.getPropertyPriority('display'),
        })
        child.hidden = true
        child.inert = true
        child.style.setProperty('display', 'none', 'important')
      }
    }
    conceal()
    const observer = new MutationObserver(conceal)
    observer.observe(target, { childList: true })
    setHost(target)
    setEdited(false)
    return () => {
      observer.disconnect()
      for (const [child, previous] of saved) {
        child.hidden = previous.hidden
        child.inert = previous.inert
        if (previous.display) child.style.setProperty('display', previous.display, previous.priority)
        else child.style.removeProperty('display')
      }
      setHost(null)
      if (opener.current?.isConnected) opener.current.focus({ preventScroll: true })
      queueMicrotask(() => {
        if (!target.isConnected) return
        target.scrollLeft = hostScroll.left
        target.scrollTop = hostScroll.top
        window.scrollTo(scroll)
      })
    }
  }, [props.open])

  const close = () => props.onOpenChange(undefined, { open: false })
  return <>
    <span ref={anchor} hidden />
    {props.open && host && createPortal(
      <WorkspaceEditorFrame title={props.title} description={props.description}
        actions={(requestLeave) => typeof props.actions === 'function' ? props.actions(() => requestLeave(close)) : props.actions}
        onBack={close} onCancel={close} dirty={props.dirty ?? edited} pending={props.pending}>
        {(requestLeave) => <div onChangeCapture={() => setEdited(true)}>
          {typeof props.children === 'function' ? props.children(() => requestLeave(close)) : props.children}
        </div>}
      </WorkspaceEditorFrame>, host,
    )}
  </>
}

/** In-flow adapters for retaining existing form bodies without Fluent dialog portals. */
export function WorkspaceDialogSurface({ style, ...props }: ComponentPropsWithoutRef<'div'>) {
  return <div {...props} style={{ ...style, width: '100%', maxWidth: 'none', maxHeight: 'none' }} />
}

export function WorkspaceDialogBody(props: ComponentPropsWithoutRef<'div'>) {
  return <div {...props} />
}

export function WorkspaceDialogTitle(props: ComponentPropsWithoutRef<'h3'>) {
  return <h3 {...props} />
}

export function WorkspaceDialogContent(props: ComponentPropsWithoutRef<'div'>) {
  return <div {...props} />
}

export function WorkspaceDialogActions({ className, ...props }: ComponentPropsWithoutRef<'div'>) {
  return <div {...props} className={[styles.actions, className].filter(Boolean).join(' ')} />
}
