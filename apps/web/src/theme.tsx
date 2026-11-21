import {
  FluentProvider,
  AriaLiveAnnouncer,
  AnnounceProvider,
  type AnnounceContextValue,
  useAnnounce,
  createDarkTheme,
  createLightTheme,
  type Theme,
} from '@fluentui/react-components'
import {
  type PropsWithChildren,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  brandRamp,
  type CustomTheme,
  fluentOverrides,
  normalizeCustomTheme,
  normalizeScheme,
  paletteTokens,
  resolvePalette,
} from './appearance'
import {
  type OrganizationAppearance,
  ThemeContext,
  type ResolvedTheme,
  type ThemePreference,
} from './theme-context'

const STORAGE_KEY = 'mosaic-theme'
const CUSTOM_KEY = 'mosaic-custom-theme'
const ORGANIZATION_KEY = 'mosaic-organization-appearance'
/** The tokens last applied, so index.html can paint them before React loads. */
const PAINT_KEY = 'mosaic-theme-paint'

function storedPreference(): ThemePreference {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' || stored === 'system' || stored === 'custom' ? stored : 'system'
}

function storedJson(key: string): unknown {
  try {
    return JSON.parse(window.localStorage.getItem(key) ?? 'null')
  } catch {
    return null
  }
}

function storedOrganization(): OrganizationAppearance {
  const stored = storedJson(ORGANIZATION_KEY) as { scheme?: unknown; allowCustomThemes?: unknown } | null
  return { scheme: normalizeScheme(stored?.scheme), allowCustomThemes: stored?.allowCustomThemes !== false }
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
  const [customTheme, setCustomThemeState] = useState<CustomTheme | null>(() => normalizeCustomTheme(storedJson(CUSTOM_KEY)))
  const [organization, setOrganizationState] = useState<OrganizationAppearance>(storedOrganization)
  // An administrator can turn custom colours off. The stored choice is kept, so it returns if they turn it back on.
  const effectivePreference = preference === 'custom' && !organization.allowCustomThemes ? 'system' : preference
  const { base: resolvedTheme, palette } = useMemo(
    () => resolvePalette(effectivePreference, preferredSystemTheme, organization.scheme, customTheme),
    [effectivePreference, preferredSystemTheme, organization.scheme, customTheme],
  )

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
  }, [preference])

  // Before paint, so a palette change never shows one frame of the old colours.
  useLayoutEffect(() => {
    const root = document.documentElement
    const tokens = paletteTokens(palette, resolvedTheme)
    root.dataset.theme = resolvedTheme
    for (const [name, value] of Object.entries(tokens)) root.style.setProperty(name, value)
    window.localStorage.setItem(PAINT_KEY, JSON.stringify({ theme: resolvedTheme, tokens }))
  }, [palette, resolvedTheme])

  const setCustomTheme = useCallback((theme: CustomTheme | null) => {
    if (theme) window.localStorage.setItem(CUSTOM_KEY, JSON.stringify(theme))
    else window.localStorage.removeItem(CUSTOM_KEY)
    setCustomThemeState(theme)
  }, [])

  const setOrganization = useCallback((appearance: OrganizationAppearance) => {
    window.localStorage.setItem(ORGANIZATION_KEY, JSON.stringify(appearance))
    setOrganizationState(appearance)
  }, [])

  const value = useMemo(
    () => ({ preference, resolvedTheme, setPreference, palette, customTheme, setCustomTheme, organization, setOrganization }),
    [preference, resolvedTheme, palette, customTheme, setCustomTheme, organization, setOrganization],
  )

  const fluentTheme = useMemo<Theme>(() => {
    const brand = brandRamp(palette.accent)
    return { ...(resolvedTheme === 'dark' ? createDarkTheme(brand) : createLightTheme(brand)), ...fluentOverrides(palette, resolvedTheme) }
  }, [palette, resolvedTheme])

  return (
    <ThemeContext.Provider value={value}>
      <FluentProvider theme={fluentTheme}>
        <AriaLiveAnnouncer><AnnouncedContent>{children}</AnnouncedContent></AriaLiveAnnouncer>
      </FluentProvider>
    </ThemeContext.Provider>
  )
}
