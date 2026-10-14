import { Button, DrawerBody, DrawerHeader, DrawerHeaderTitle, OverlayDrawer } from '@fluentui/react-components'
import { DismissRegular } from '@fluentui/react-icons'
import { type ReactNode, useEffect, useId, useRef, useState } from 'react'

export function DetailPanel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const titleId = useId()
  const [opener] = useState(() =>
    document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null)
  const restorationFrame = useRef<number | null>(null)
  useEffect(() => {
    if (restorationFrame.current !== null) {
      cancelAnimationFrame(restorationFrame.current)
      restorationFrame.current = null
    }
    return () => {
      restorationFrame.current = requestAnimationFrame(() => {
        restorationFrame.current = null
        const target = opener?.isConnected ? opener : document.querySelector<HTMLElement>('.resource-search input')
        target?.focus()
      })
    }
  }, [opener])
  return (
    <OverlayDrawer open position="end" size="large" className="resource-drawer" aria-labelledby={titleId}
      style={{ width: 'min(680px, 100%)', maxWidth: '100%' }}
      onOpenChange={(_, data) => { if (!data.open) onClose() }}>
      <DrawerHeader>
        <DrawerHeaderTitle id={titleId} action={<Button appearance="subtle" icon={<DismissRegular />} aria-label="Close details" onClick={onClose} />}>
          {title}
        </DrawerHeaderTitle>
      </DrawerHeader>
      <DrawerBody className="resource-drawer-body">{children}</DrawerBody>
    </OverlayDrawer>
  )
}
