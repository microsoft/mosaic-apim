import { useEffect, useRef } from 'react'

export function useNavigationDrawer(open: boolean, close: () => void) {
  const ref = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!open || !ref.current) return
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const drawer = ref.current
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const controls = () => Array.from(drawer.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input, select, [tabindex="0"]'))
      .filter((element) => getComputedStyle(element).display !== 'none' && getComputedStyle(element).visibility !== 'hidden' && element.getClientRects().length > 0)
    controls()[0]?.focus()
    function trap(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        close()
      }
      if (event.key !== 'Tab') return
      const items = controls()
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last?.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first?.focus()
      }
    }
    drawer.addEventListener('keydown', trap)
    return () => {
      drawer.removeEventListener('keydown', trap)
      document.body.style.overflow = previousOverflow
      opener?.focus()
    }
  }, [open, close])
  return ref
}
