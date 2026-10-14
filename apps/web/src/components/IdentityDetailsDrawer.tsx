import { Button, DrawerBody, DrawerHeader, DrawerHeaderTitle, OverlayDrawer, useRestoreFocusSource } from '@fluentui/react-components'
import { DismissRegular } from '@fluentui/react-icons'
import type { ReactNode } from 'react'

export function IdentityDetailsDrawer({ open, title, onClose, children }: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
}) {
  const restoreFocus = useRestoreFocusSource()
  return (
    <OverlayDrawer open={open} position="end" size="medium" className="identity-drawer"
      {...restoreFocus}
      onOpenChange={(_, data) => { if (!data.open) onClose() }}>
      <DrawerHeader>
        <DrawerHeaderTitle action={<Button appearance="subtle" icon={<DismissRegular />}
          aria-label="Close identity details" onClick={onClose} />}>{title}</DrawerHeaderTitle>
      </DrawerHeader>
      <DrawerBody>{children}</DrawerBody>
    </OverlayDrawer>
  )
}
