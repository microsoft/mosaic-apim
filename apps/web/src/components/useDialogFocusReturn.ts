import type { DialogProps } from '@fluentui/react-components'
import { type RefObject, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'

function usable(target: HTMLElement | null | undefined): target is HTMLElement {
  if (!target?.isConnected || target.matches(':disabled') || target.closest('[hidden], [inert]')) return false
  for (let node: HTMLElement | null = target; node; node = node.parentElement) {
    const style = target.ownerDocument.defaultView?.getComputedStyle(node)
    if (style?.display === 'none' || style?.visibility === 'hidden') return false
  }
  return true
}

export function restoreFocusTo(
  opener: HTMLElement | null | undefined,
  fallback: HTMLElement | null | undefined,
  closingSurface?: HTMLElement | null,
) {
  const target = usable(opener) ? opener : usable(fallback) ? fallback : null
  if (!target) return
  const active = target.ownerDocument.activeElement
  if (active instanceof HTMLElement) {
    const modal = active.closest('[role="dialog"], [role="alertdialog"]')
    if (modal && modal !== closingSurface && !modal.contains(target)) return
    const workspace = active.closest('[data-workspace-editor]')
    if (workspace && workspace !== closingSurface && !workspace.contains(target)) return
  }
  if (active !== target) target.focus({ preventScroll: true })
}

interface FocusReturnOptions {
  open: boolean
  surfaceRef: RefObject<HTMLElement | null>
  openerRef?: RefObject<HTMLElement | null>
  fallbackRef?: RefObject<HTMLElement | null>
}

function focusedElement() {
  return document.activeElement instanceof HTMLElement && document.activeElement !== document.body
    ? document.activeElement : null
}

export function useDialogFocusReturn({
  open, surfaceRef, openerRef, fallbackRef,
}: FocusReturnOptions): DialogProps['surfaceMotion'] {
  // Capture the opening element before Fluent moves focus into its portal.
  const [session, setSession] = useState(() => ({ open, opener: open ? focusedElement() : null }))
  if (session.open !== open) setSession({ open, opener: open ? focusedElement() : session.opener })
  const openRef = useRef(open)
  const surface = useRef<HTMLElement | null>(null)
  const generation = useRef(0)
  const restore = useCallback(() => {
    restoreFocusTo(openerRef?.current ?? session.opener, fallbackRef?.current, surface.current)
  }, [openerRef, session.opener, fallbackRef])

  useLayoutEffect(() => {
    openRef.current = open
    if (open) generation.current += 1
    if (surfaceRef.current) surface.current = surfaceRef.current
  }, [open, surfaceRef])
  useLayoutEffect(() => {
    generation.current += 1
    return () => {
      const ticket = ++generation.current
      queueMicrotask(() => { if (generation.current === ticket) restore() })
    }
  }, [restore])

  return useMemo(() => ({
    onMotionStart: () => { if (surfaceRef.current) surface.current = surfaceRef.current },
    onMotionFinish: (_, { direction }) => {
      if (direction !== 'exit') {
        if (surfaceRef.current) surface.current = surfaceRef.current
        return
      }
      const ticket = ++generation.current
      queueMicrotask(() => {
        if (generation.current === ticket && !openRef.current) restore()
      })
    },
  }), [restore, surfaceRef])
}
