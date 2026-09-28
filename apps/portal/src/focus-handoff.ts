import { useLayoutEffect, useState, type RefObject } from 'react'

/**
 * Carries keyboard focus across a re-render that replaces the element holding it, such as a
 * panel that remounts when its data changes. Without it, focus falls back to the page body and
 * keyboard users must start again from the top of the page.
 */
export interface FocusHandoff {
  give: () => void
  take: () => boolean
}

export function useFocusHandoffSource(): FocusHandoff {
  const [handoff] = useState<FocusHandoff>(() => {
    let given = false
    return {
      give: () => {
        given = true
      },
      take: () => {
        const taken = given
        given = false
        return taken
      },
    }
  })
  return handoff
}

function focusIsLost() {
  const active = document.activeElement
  return !active || active === document.body || !active.isConnected
}

/**
 * Gives focus away when `container` unmounts while holding it. On mount, takes a pending handoff
 * by focusing `target`, or `fallback` when `target` is not rendered.
 */
export function useFocusHandoff(
  handoff: FocusHandoff | undefined,
  container: RefObject<HTMLElement | null>,
  target: RefObject<HTMLElement | null>,
  fallback: RefObject<HTMLElement | null> = target,
) {
  useLayoutEffect(() => {
    const node = container.current
    if (handoff?.take() && focusIsLost()) (target.current ?? fallback.current)?.focus()
    return () => {
      // React runs layout cleanup before it removes this subtree, so focus is still inside it.
      if (node?.contains(document.activeElement)) handoff?.give()
    }
  }, [handoff, container, target, fallback])
}
