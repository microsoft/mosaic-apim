import { Button, Menu, MenuDivider, MenuItem, MenuList, MenuPopover, MenuTrigger } from '@fluentui/react-components'
import { ColorRegular, DarkThemeRegular, DesktopRegular, PaintBrushRegular, WeatherSunnyRegular } from '@fluentui/react-icons'
import { useMosaicTheme, type ThemePreference } from '../theme-context'
import type { ReactNode } from 'react'

const options = [
  { value: 'light', label: 'Light', icon: <WeatherSunnyRegular /> },
  { value: 'dark', label: 'Dark', icon: <DarkThemeRegular /> },
  { value: 'system', label: 'System', icon: <DesktopRegular /> },
  { value: 'custom', label: 'Custom', icon: <ColorRegular /> },
] satisfies Array<{ value: ThemePreference; label: string; icon: ReactNode }>

/**
 * Light, Dark, System, and the person's own Custom colors. `onCustomize` opens wherever this app
 * edits those colors; Custom leads there until the person has saved some.
 */
export function AppearanceControl({ onCustomize }: { onCustomize?: () => void }) {
  const { preference, resolvedTheme, setPreference, customTheme, organization } = useMosaicTheme()
  const shown = options.filter((option) => option.value !== 'custom' || (organization.allowCustomThemes && (customTheme || onCustomize)))
  // With custom colors turned off, a stored Custom choice shows as System, which is what applies.
  const active = preference === 'custom' && !(organization.allowCustomThemes && customTheme) ? 'system' : preference
  const choose = (value: ThemePreference) => {
    if (value === 'custom' && !customTheme) onCustomize?.()
    else setPreference(value)
  }
  return (
    <div className="appearance-control">
      <div className="appearance-options" role="group" aria-label="Appearance">
        {shown.map((option) => (
          <Button key={option.value} size="small" appearance={active === option.value ? 'secondary' : 'subtle'}
            icon={option.icon} aria-pressed={active === option.value} aria-label={option.label} title={option.label}
            onClick={() => choose(option.value)} />
        ))}
      </div>
      <div className="appearance-compact">
        <Menu>
          <MenuTrigger disableButtonEnhancement>
            <Button appearance="subtle" icon={active === 'custom' ? <ColorRegular /> : resolvedTheme === 'dark' ? <DarkThemeRegular /> : <WeatherSunnyRegular />}
              aria-label={`Appearance: ${active}`} title={`Appearance: ${active}`} />
          </MenuTrigger>
          <MenuPopover><MenuList>
            {shown.map((option) => (
              <MenuItem key={option.value} icon={option.icon} onClick={() => choose(option.value)}>
                {option.label}{active === option.value ? ' (selected)' : ''}
              </MenuItem>
            ))}
            {onCustomize && organization.allowCustomThemes && <>
              <MenuDivider />
              <MenuItem icon={<PaintBrushRegular />} onClick={onCustomize}>Customize colors</MenuItem>
            </>}
          </MenuList></MenuPopover>
        </Menu>
      </div>
    </div>
  )
}
