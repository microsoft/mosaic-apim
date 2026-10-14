import {
  Button,
  Dialog,
  DialogBody,
  DialogSurface,
  DialogTitle,
  FluentProvider,
  webLightTheme,
} from '@fluentui/react-components'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { RowActions } from './RowActions'

describe('RowActions', () => {
  it('keeps the frequent action visible and exposes secondary and destructive actions by keyboard', async () => {
    const user = userEvent.setup()
    const primary = vi.fn()
    const secondary = vi.fn()
    const remove = vi.fn()
    render(<FluentProvider theme={webLightTheme}>
      <RowActions
        label="More actions for Chat API"
        primary={<Button onClick={primary}>Review changes</Button>}
        actions={[{ label: 'Change environment', onClick: secondary }]}
        destructiveActions={[{ label: 'Remove MOSAIC record', onClick: remove }]}
      />
    </FluentProvider>)
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Review changes' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(primary).toHaveBeenCalledOnce()
    await user.tab()
    const trigger = screen.getByRole('button', { name: 'More actions for Chat API' })
    expect(trigger).toHaveFocus()
    await user.keyboard('{Enter}')
    const change = await screen.findByRole('menuitem', { name: 'Change environment' })
    await waitFor(() => expect(change).toHaveFocus())
    await user.keyboard('{Enter}')
    expect(secondary).toHaveBeenCalledOnce()
    await waitFor(() => expect(trigger).toHaveFocus())
    await user.keyboard('{Enter}{ArrowDown}')
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Remove MOSAIC record' })).toHaveFocus())
    await user.keyboard('{Escape}')
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(remove).not.toHaveBeenCalled()
  })

  it('preserves disabled and focusable-pending predicates without disabling unrelated actions', async () => {
    const user = userEvent.setup()
    const blocked = vi.fn()
    const pending = vi.fn()
    const available = vi.fn()
    render(<FluentProvider theme={webLightTheme}>
      <RowActions
        label="More actions for Endpoint"
        primary={<Button disabled>Check access</Button>}
        actions={[
          { label: 'Sync models', onClick: blocked, disabled: true },
          { label: 'Planning…', onClick: pending, disabledFocusable: true },
          { label: 'Change environment', onClick: available },
        ]}
      />
    </FluentProvider>)
    expect(screen.getByRole('button', { name: 'Check access' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'More actions for Endpoint' }))
    const sync = await screen.findByRole('menuitem', { name: 'Sync models' })
    expect(sync).toHaveAttribute('aria-disabled', 'true')
    await user.click(sync)
    const planning = screen.getByRole('menuitem', { name: 'Planning…' })
    expect(planning).toHaveAttribute('aria-disabled', 'true')
    await user.click(planning)
    expect(planning).toHaveFocus()
    await user.click(screen.getByRole('menuitem', { name: 'Change environment' }))
    expect(blocked).not.toHaveBeenCalled()
    expect(pending).not.toHaveBeenCalled()
    expect(available).toHaveBeenCalledOnce()
  })

  it('returns keyboard-dismissed dialog focus to the persistent menu trigger', async () => {
    const user = userEvent.setup()
    function Surface() {
      const [open, setOpen] = useState(false)
      return <>
        <RowActions label="More actions for Chat API" destructiveActions={[{ label: 'Remove record', onClick: () => setOpen(true) }]} />
        <Dialog open={open} onOpenChange={(_, data) => setOpen(data.open)}>
          <DialogSurface>
            <DialogBody>
              <DialogTitle>Remove Chat API?</DialogTitle>
              <Button onClick={() => setOpen(false)}>Cancel</Button>
            </DialogBody>
          </DialogSurface>
        </Dialog>
      </>
    }
    render(<Surface />)
    const trigger = screen.getByRole('button', { name: 'More actions for Chat API' })
    await user.click(trigger)
    await user.click(await screen.findByRole('menuitem', { name: 'Remove record' }))
    const dialog = await screen.findByRole('dialog', { name: 'Remove Chat API?' })
    expect(dialog).toBeVisible()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    await waitFor(() => expect(cancel).toHaveFocus())
    await user.keyboard('{Escape}')
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('does not add an empty overflow menu to single-action rows', () => {
    render(<FluentProvider theme={webLightTheme}>
      <RowActions label="More actions for Endpoint" primary={<Button>Set facts</Button>} />
    </FluentProvider>)
    expect(screen.getByRole('button', { name: 'Set facts' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'More actions for Endpoint' })).not.toBeInTheDocument()
  })
})
