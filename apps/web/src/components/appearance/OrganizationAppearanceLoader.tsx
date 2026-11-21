import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { normalizeScheme } from '../../appearance'
import { useMosaicTheme } from '../../theme-context'

export const appearanceQueryKey = ['appearance'] as const

export interface AppearanceResponse {
  scheme: unknown
  allowCustomThemes: boolean
}

/**
 * Fetches the organization's colours once the person is signed in and hands them to the theme.
 * A failure is deliberately quiet: the app stays on the colours it last knew, or MOSAIC's own.
 */
export function OrganizationAppearanceLoader({ load }: { load: () => Promise<AppearanceResponse> }) {
  const { setOrganization } = useMosaicTheme()
  const appearance = useQuery({ queryKey: appearanceQueryKey, queryFn: load, staleTime: 5 * 60_000, retry: false })
  useEffect(() => {
    if (!appearance.data) return
    // Colours from the API are validated again here, so a malformed scheme can never reach a style rule.
    setOrganization({ scheme: normalizeScheme(appearance.data.scheme), allowCustomThemes: appearance.data.allowCustomThemes !== false })
  }, [appearance.data, setOrganization])
  return null
}
