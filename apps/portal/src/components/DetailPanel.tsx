import { Button, DrawerBody, DrawerHeader, DrawerHeaderTitle, OverlayDrawer } from '@fluentui/react-components'
import { DismissRegular } from '@fluentui/react-icons'
import { type ReactNode, useEffect, useState } from 'react'

export function DetailPanel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const [wide, setWide] = useState(() => window.innerWidth >= 1200)
  useEffect(() => {
    const query = window.matchMedia('(min-width: 1200px)')
    const update = () => setWide(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  if (wide) {
    return (
      <section className="detail-panel" aria-label={title}>
        <div className="detail-panel-header">
          <h2>{title}</h2>
          <Button appearance="subtle" icon={<DismissRegular />} aria-label="Close details" onClick={onClose} />
        </div>
        {children}
      </section>
    )
  }
  return (
    <OverlayDrawer open position="end" size="medium" onOpenChange={(_, data) => { if (!data.open) onClose() }}>
      <DrawerHeader>
        <DrawerHeaderTitle action={<Button appearance="subtle" icon={<DismissRegular />} aria-label="Close details" onClick={onClose} />}>
          {title}
        </DrawerHeaderTitle>
      </DrawerHeader>
      <DrawerBody>{children}</DrawerBody>
    </OverlayDrawer>
  )
}
