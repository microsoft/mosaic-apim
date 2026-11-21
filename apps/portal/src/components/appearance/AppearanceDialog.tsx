import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, DialogTrigger } from '@fluentui/react-components'
import { CustomThemeEditor } from './CustomThemeEditor'

/** Where a portal user chooses their own colors. The portal has no settings page, so this opens over the current one. */
export function AppearanceDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(_, data) => { if (!data.open) onClose() }}>
      <DialogSurface className="appearance-dialog">
        <DialogBody>
          <DialogTitle>Your custom colors</DialogTitle>
          <DialogContent>
            <p className="appearance-dialog-intro">
              Choose six colors and MOSAIC works out the rest, including readable text on your accent.
              Light, Dark, and System keep your organization&apos;s colors.
            </p>
            {open && <CustomThemeEditor onSaved={onClose} />}
          </DialogContent>
          <DialogActions>
            <DialogTrigger disableButtonEnhancement><Button>Close</Button></DialogTrigger>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
