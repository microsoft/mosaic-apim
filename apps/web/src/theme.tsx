import {
  FluentProvider,
  AriaLiveAnnouncer,
  AnnounceProvider,
  type AnnounceContextValue,
  useAnnounce,
  webDarkTheme,
  webLightTheme,
} from '@fluentui/react-components'
import {
  type PropsWithChildren,
  useEffect,
  useMemo,
  useRef,
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

// Mount consumers after Fluent's fallback live region exists.
function AnnouncedContent({ children }: PropsWithChildren) {
  const [ready, setReady] = useState(false)
  const { announce } = useAnnounce()
  const delivered = useRef(new Set<string>())
  const announcement = useMemo<AnnounceContextValue>(() => ({
    announce: (message, options) => {
      const key = JSON.stringify([message, options?.polite, options?.alert, options?.priority])
      // StrictMode can replay Fluent's MessageBar effect within the same turn.
      if (delivered.current.has(key)) return
      delivered.current.add(key)
      queueMicrotask(() => delivered.current.delete(key))
      announce(message, options)
    },
  }), [announce])
  useEffect(() => {
    let active = true
    queueMicrotask(() => { if (active) setReady(true) })
    return () => { active = false }
  }, [])
  return ready ? <AnnounceProvider value={announcement}>{children}</AnnounceProvider> : null
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
      <FluentProvider theme={resolvedTheme === 'dark' ? {
        ...webDarkTheme,
        colorNeutralBackground1: '#1c2531',
        colorNeutralBackground2: '#222e3b',
        colorNeutralBackground3: '#303e4e',
        colorNeutralForeground1: '#f5f5f5',
        colorNeutralForeground2: '#c8c6c4',
        colorNeutralStroke1: '#536479',
        colorNeutralStroke2: '#354354',
        colorBrandBackground: '#005faa',
        borderRadiusMedium: '8px',
        borderRadiusLarge: '12px',
      } : {
        ...webLightTheme,
        colorNeutralBackground2: '#f5f7fa',
        colorNeutralBackground3: '#e8edf4',
        colorNeutralStroke1: '#c6cfdb',
        colorNeutralStroke2: '#dce5ef',
        colorBrandBackground: '#005faa',
        borderRadiusMedium: '8px',
        borderRadiusLarge: '12px',
      }}>
        <AriaLiveAnnouncer><AnnouncedContent>{children}</AnnouncedContent></AriaLiveAnnouncer>
      </FluentProvider>
    </ThemeContext.Provider>
  )
}
