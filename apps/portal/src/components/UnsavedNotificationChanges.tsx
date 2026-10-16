import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle } from '@fluentui/react-components'
import { useCallback, useEffect, useRef } from 'react'
import { useBlocker } from 'react-router-dom'

export function UnsavedNotificationChanges({ dirty }: { dirty: boolean }) {
  const opener = useRef<HTMLElement | null>(null)
  const blocker = useBlocker(useCallback(() => {
    if (dirty) opener.current = document.activeElement as HTMLElement
    return dirty
  }, [dirty]))
  useEffect(() => {
    if (!dirty) return
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', protect)
    return () => window.removeEventListener('beforeunload', protect)
  }, [dirty])
  useEffect(() => {
    if (blocker.state === 'unblocked' && opener.current) {
      opener.current.focus()
      opener.current = null
    }
  }, [blocker.state])
  const keepEditing = () => {
    blocker.reset?.()
  }
  if (blocker.state !== 'blocked') return null
  return <Dialog open onOpenChange={(_, data) => { if (!data.open) keepEditing() }}>
    <DialogSurface>
      <DialogBody>
        <DialogTitle>Discard unsaved notification preferences?</DialogTitle>
        <DialogContent>Your changes have not been saved. Keep editing to preserve this draft.</DialogContent>
        <DialogActions>
          <Button onClick={keepEditing} appearance="primary">Keep editing</Button>
          <Button onClick={() => blocker.proceed?.()}>Discard and leave</Button>
        </DialogActions>
      </DialogBody>
    </DialogSurface>
  </Dialog>
}
