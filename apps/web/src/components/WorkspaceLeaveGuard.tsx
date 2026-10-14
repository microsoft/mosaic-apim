import {
  Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle,
} from '@fluentui/react-components'
import { type ReactNode, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { UNSAFE_DataRouterContext, useBlocker, useInRouterContext } from 'react-router-dom'

export type RequestWorkspaceLeave = (leave: () => void) => void

let completionDepth = 0

/** Call only after a successful mutation, wrapping its synchronous return navigation. */
export function completeWorkspaceNavigation<T>(navigate: () => T): T {
  completionDepth += 1
  try { return navigate() }
  finally { completionDepth -= 1 }
}

export interface WorkspaceLeaveGuardProps {
  dirty?: boolean
  pending?: boolean
  children: (requestLeave: RequestWorkspaceLeave) => ReactNode
}

interface LeaveRequest {
  proceed: () => void
  cancel?: () => void
  focus: HTMLElement | null
}

function NavigationBlocker({
  active, onBlocked,
}: { active: boolean; onBlocked: (proceed: () => void, cancel: () => void) => void }) {
  const shouldBlock = useCallback(() => active && completionDepth === 0, [active])
  const blocker = useBlocker(shouldBlock)
  useEffect(() => {
    if (blocker.state === 'blocked') onBlocked(blocker.proceed, blocker.reset)
  }, [blocker, onBlocked])
  return null
}

export function WorkspaceLeaveGuard({ dirty = false, pending = false, children }: WorkspaceLeaveGuardProps) {
  const dataRouter = useContext(UNSAFE_DataRouterContext)
  const inRouter = useInRouterContext()
  const [request, setRequest] = useState<LeaveRequest | null>(null)
  const requestRef = useRef<LeaveRequest | null>(null)
  const closingRequest = useRef<{ request: LeaveRequest; proceed: boolean } | null>(null)
  const draftFocus = useRef<HTMLElement | null>(null)
  const active = dirty || pending

  const ask = useCallback((proceed: () => void, cancel?: () => void) => {
    if (requestRef.current || closingRequest.current) return
    const currentFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const next = {
      proceed, cancel,
      focus: draftFocus.current?.isConnected ? draftFocus.current : currentFocus,
    }
    requestRef.current = next
    setRequest(next)
  }, [])

  const requestLeave = useCallback<RequestWorkspaceLeave>((leave) => {
    if (active) ask(leave)
    else leave()
  }, [active, ask])

  const cancel = useCallback(() => {
    const current = requestRef.current
    requestRef.current = null
    setRequest(null)
    if (current) closingRequest.current = { request: current, proceed: false }
    current?.cancel?.()
  }, [])

  function finishClose() {
    const closing = closingRequest.current
    if (!closing) return
    // Let the modal surface unmount before returning focus or replacing its workspace.
    queueMicrotask(() => {
      if (closingRequest.current !== closing) return
      closingRequest.current = null
      if (closing.request.focus?.isConnected) closing.request.focus.focus({ preventScroll: true })
      if (closing.proceed) completeWorkspaceNavigation(closing.request.proceed)
    })
  }

  useEffect(() => () => { closingRequest.current = null }, [])

  useEffect(() => {
    if (!active && requestRef.current) cancel()
  }, [active, cancel])

  function proceed() {
    if (pending) return
    const current = requestRef.current
    if (!current) return
    closingRequest.current = { request: current, proceed: true }
    requestRef.current = null
    setRequest(null)
  }

  useEffect(() => {
    if (!active) return
    const rememberFocus = (event?: FocusEvent) => {
      const target = event?.target ?? document.activeElement
      if (target instanceof HTMLElement && target.closest('[data-workspace-editor]')
        && target.matches('input, textarea, select, [contenteditable="true"], [role="combobox"]')) draftFocus.current = target
    }
    rememberFocus()
    document.addEventListener('focusin', rememberFocus)
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      document.removeEventListener('focusin', rememberFocus)
    }
  }, [active])

  return (
    <>
      {inRouter && dataRouter !== null && (active || request !== null) && <NavigationBlocker active={active} onBlocked={ask} />}
      {children(requestLeave)}
      <Dialog open={request !== null} surfaceMotion={{ onMotionFinish: (_, { direction }) => { if (direction === 'exit') finishClose() } }}
        onOpenChange={(_, data) => { if (!data.open) cancel() }}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>{pending ? 'Operation in progress' : 'Discard unsaved changes?'}</DialogTitle>
            <DialogContent>{pending
              ? 'Wait for the current operation to finish before leaving this workspace.'
              : 'Your changes have not been saved. Stay to continue editing, or discard them and leave.'}</DialogContent>
            <DialogActions>
              <Button appearance="primary" onClick={cancel}>Stay</Button>
              {!pending && <Button onClick={proceed}>Discard changes</Button>}
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </>
  )
}
