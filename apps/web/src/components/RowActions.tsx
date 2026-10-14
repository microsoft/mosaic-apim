import {
  Button,
  Menu,
  MenuDivider,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  makeStyles,
  useRestoreFocusTarget,
} from '@fluentui/react-components'
import { MoreHorizontalRegular } from '@fluentui/react-icons'
import { type ReactNode, useRef, useState } from 'react'
import { flushSync } from 'react-dom'

export interface RowAction {
  label: string
  onClick: () => void
  disabled?: boolean
  disabledFocusable?: boolean
  title?: string
  'aria-expanded'?: boolean
}

export interface RowActionsProps {
  /** A row-specific accessible name, such as "More actions for Chat API". */
  label: string
  /** Keep the frequent action's existing Button and its permission/focus props intact. */
  primary?: ReactNode
  actions?: readonly RowAction[]
  destructiveActions?: readonly RowAction[]
}

const useStyles = makeStyles({
  root: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '10px',
    whiteSpace: 'nowrap',
  },
  destructive: {
    color: 'var(--mosaic-danger)',
  },
})

/** Secondary actions restore dialog focus to the persistent overflow trigger, not a menu item. */
export function RowActions({ label, primary, actions = [], destructiveActions = [] }: RowActionsProps) {
  const styles = useStyles()
  const restoreFocus = useRestoreFocusTarget()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const hasMenu = actions.length > 0 || destructiveActions.length > 0
  function renderAction({ label: actionLabel, onClick, disabled, disabledFocusable, ...props }: RowAction, destructive = false) {
    const blocked = Boolean(disabled || disabledFocusable)
    return (
      <MenuItem
        key={actionLabel}
        {...props}
        disabled={blocked}
        className={destructive && !blocked ? styles.destructive : undefined}
        onClick={() => {
          if (blocked) return
          // Close the popover before a callback can open a modal and make the trigger inert.
          flushSync(() => setOpen(false))
          triggerRef.current?.focus()
          onClick()
        }}
      >
        {actionLabel}
      </MenuItem>
    )
  }
  return (
    <div className={styles.root}>
      {primary}
      {hasMenu && (
        <Menu open={open} onOpenChange={(_, data) => setOpen(data.open)}>
          <MenuTrigger disableButtonEnhancement>
            <Button
              ref={triggerRef}
              appearance="subtle"
              size="small"
              icon={<MoreHorizontalRegular />}
              aria-label={label}
              title={label}
              {...restoreFocus}
            />
          </MenuTrigger>
          <MenuPopover>
            <MenuList>
              {actions.map((action) => renderAction(action))}
              {actions.length > 0 && destructiveActions.length > 0 && <MenuDivider />}
              {destructiveActions.map((action) => renderAction(action, true))}
            </MenuList>
          </MenuPopover>
        </Menu>
      )}
    </div>
  )
}
