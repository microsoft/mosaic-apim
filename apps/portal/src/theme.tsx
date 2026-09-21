import {
  FluentProvider,
  webDarkTheme,
  webLightTheme,
} from '@fluentui/react-components'
import {
  type PropsWithChildren,
  useEffect,
  useMemo,
  useState,
} from 'react'
import {
  ThemeContext,
  type ResolvedTheme,
  type ThemePreference,
} from './theme-context'

const STORAGE_KEY = 'mosaic-theme'

function storedPreference(): ThemePreference {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function MosaicThemeProvider({ children }: PropsWithChildren) {
  const [preference, setPreference] = useState<ThemePreference>(storedPreference)
  const [preferredSystemTheme, setPreferredSystemTheme] = useState<ResolvedTheme>(systemTheme)
  const resolvedTheme = preference === 'system' ? preferredSystemTheme : preference

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = (event: MediaQueryListEvent) => {
      setPreferredSystemTheme(event.matches ? 'dark' : 'light')
    }
    media.addEventListener('change', handleChange)
    return () => media.removeEventListener('change', handleChange)
  }, [])

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, preference)
    document.documentElement.dataset.theme = resolvedTheme
  }, [preference, resolvedTheme])

  const value = useMemo(
    () => ({ preference, resolvedTheme, setPreference }),
    [preference, resolvedTheme],
  )

  return (
    <ThemeContext.Provider value={value}>
      <FluentProvider theme={resolvedTheme === 'dark' ? webDarkTheme : webLightTheme}>
        {children}
      </FluentProvider>
    </ThemeContext.Provider>
  )
}
