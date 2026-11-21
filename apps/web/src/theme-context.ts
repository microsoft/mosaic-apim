import { createContext, useContext } from 'react'
import type { CustomTheme, OrganizationScheme, Palette } from './appearance'

export type ThemePreference = 'light' | 'dark' | 'system' | 'custom'
export type ResolvedTheme = 'light' | 'dark'

/** What an administrator has saved for everyone: optional light and dark palettes, and whether people may use their own. */
export interface OrganizationAppearance {
  scheme: OrganizationScheme | null
  allowCustomThemes: boolean
}

export interface ThemeContextValue {
  preference: ThemePreference
  resolvedTheme: ResolvedTheme
  setPreference: (preference: ThemePreference) => void
  /** The colors on screen now. */
  palette: Palette
  /** This person's saved custom colors, whether or not Custom is the active mode. */
  customTheme: CustomTheme | null
  /** Saves the custom colors in this browser. `null` forgets them. */
  setCustomTheme: (theme: CustomTheme | null) => void
  organization: OrganizationAppearance
  /** Applies what the API returned, or what an administrator just saved. */
  setOrganization: (appearance: OrganizationAppearance) => void
}

export const ThemeContext = createContext<ThemeContextValue | undefined>(undefined)

export function useMosaicTheme(): ThemeContextValue {
  const context = useContext(ThemeContext)
  if (!context) {
    throw new Error('useMosaicTheme must be used within MosaicThemeProvider')
  }
  return context
}
