import { Button, Menu, MenuItem, MenuList, MenuPopover, MenuTrigger } from '@fluentui/react-components'
import { DarkThemeRegular, DesktopRegular, WeatherSunnyRegular } from '@fluentui/react-icons'
import { useMosaicTheme, type ThemePreference } from '../theme-context'
import type { ReactNode } from 'react'

const options = [
  { value: 'light', label: 'Light', icon: <WeatherSunnyRegular /> },
  { value: 'dark', label: 'Dark', icon: <DarkThemeRegular /> },
  { value: 'system', label: 'System', icon: <DesktopRegular /> },
] satisfies Array<{ value: ThemePreference; label: string; icon: ReactNode }>

export function AppearanceControl() {
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()
  return (
    <div className="appearance-control">
      <div className="appearance-options" role="group" aria-label="Appearance">
        {options.map((option) => (
          <Button key={option.value} size="small" appearance={preference === option.value ? 'secondary' : 'subtle'}
            icon={option.icon} aria-pressed={preference === option.value}
            onClick={() => setPreference(option.value)}>{option.label}</Button>
        ))}
      </div>
      <div className="appearance-compact">
        <Menu>
          <MenuTrigger disableButtonEnhancement>
            <Button appearance="subtle" icon={resolvedTheme === 'dark' ? <DarkThemeRegular /> : <WeatherSunnyRegular />}
              aria-label={`Appearance: ${preference}`} title={`Appearance: ${preference}`} />
          </MenuTrigger>
          <MenuPopover><MenuList>
            {options.map((option) => (
              <MenuItem key={option.value} icon={option.icon} onClick={() => setPreference(option.value)}>
                {option.label}{preference === option.value ? ' (selected)' : ''}
              </MenuItem>
            ))}
          </MenuList></MenuPopover>
        </Menu>
      </div>
    </div>
  )
}
