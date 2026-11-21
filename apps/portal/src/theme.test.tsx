import { Button } from '@fluentui/react-components'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode, useState } from 'react'
import { DEFAULT_PALETTES, fluentOverrides } from './appearance'
import { useMosaicTheme } from './theme-context'
import { MosaicThemeProvider } from './theme'
import { ErrorState } from './components/AsyncState'

const originalAriaNotify = Object.getOwnPropertyDescriptor(document, 'ariaNotify')

function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()
  return (
    <>
      <span>{`${preference}:${resolvedTheme}`}</span>
      <Button onClick={() => setPreference('dark')}>Use dark</Button>
      <Button onClick={() => setPreference('system')}>Use system</Button>
      <Button onClick={() => setPreference('custom')}>Use custom</Button>
    </>
  )
}

function RepeatedFailure() {
  const [attempt, setAttempt] = useState(0)
  return <>
    <Button onClick={() => setAttempt((value) => value + 1)}>Retry fixture action</Button>
    <ErrorState context="action" title="Unable to import APIs"
      error={new Error('Sync the gateway, then import again.')} announcementKey={attempt} />
  </>
}

function themeValue(token: string) {
  const provider = document.querySelector('.fui-FluentProvider')
  if (!provider) throw new Error('Fluent theme provider is missing')
  return getComputedStyle(provider).getPropertyValue(`--${token}`).trim()
}

describe('MosaicThemeProvider', () => {
  beforeEach(() => {
    window.localStorage.clear()
    delete document.documentElement.dataset.theme
  })

  afterEach(() => {
    vi.restoreAllMocks()
    if (originalAriaNotify) Object.defineProperty(document, 'ariaNotify', originalAriaNotify)
    else Reflect.deleteProperty(document, 'ariaNotify')
  })

  it.each(['light', 'dark'] as const)('uses the built-in palette for stored %s appearance', async (preference) => {
    window.localStorage.setItem('mosaic-theme', preference)
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText(`${preference}:${preference}`)).toBeVisible()
    const palette = DEFAULT_PALETTES[preference]
    expect(themeValue('colorNeutralBackground1')).toBe(palette.panel)
    expect(themeValue('colorNeutralStroke1')).toBe(palette.border)
    expect(themeValue('colorNeutralForeground1')).toBe(palette.text)
    expect(themeValue('colorBrandBackground')).toBe(palette.accent)
    expect(themeValue('borderRadiusMedium')).toBe('10px')
    for (const [token, value] of Object.entries(fluentOverrides(palette, preference))) {
      expect(themeValue(token)).toBe(value)
    }
    expect(document.documentElement.dataset.theme).toBe(preference)
    expect(document.documentElement.style.getPropertyValue('--mosaic-bg')).toBe(palette.backdrop)
  })

  it('paints the organization\u2019s colours in light and dark, and a person\u2019s own in custom', async () => {
    const scheme = {
      light: { ...DEFAULT_PALETTES.light, accent: '#0f9d8a' },
      dark: { ...DEFAULT_PALETTES.dark, accent: '#0f9d8a', panel: '#15241f' },
    }
    const custom = { base: 'dark', colors: { accent: '#f0603f', backdrop: '#160b08', panel: '#2a1611', border: '#5a3329', text: '#fdf3ef', textMuted: '#e0b9ad' } }
    window.localStorage.setItem('mosaic-organization-appearance', JSON.stringify({ scheme, allowCustomThemes: true }))
    window.localStorage.setItem('mosaic-custom-theme', JSON.stringify(custom))
    const user = userEvent.setup()
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText('system:light')).toBeVisible()
    expect(themeValue('colorBrandBackground')).toBe('#0f9d8a')
    await user.click(screen.getByRole('button', { name: 'Use dark' }))
    expect(themeValue('colorNeutralBackground1')).toBe('#15241f')

    await user.click(screen.getByRole('button', { name: 'Use custom' }))
    expect(screen.getByText('custom:dark')).toBeVisible()
    expect(themeValue('colorBrandBackground')).toBe('#f0603f')
    expect(document.documentElement.style.getPropertyValue('--mosaic-surface-solid')).toBe('#2a1611')
    // What index.html paints before the app loads on the next visit.
    const paint = JSON.parse(window.localStorage.getItem('mosaic-theme-paint')!)
    expect(paint.theme).toBe('dark')
    expect(paint.tokens['--mosaic-accent']).toBe('#f0603f')
  })

  it('ignores stored colours that are not colours', async () => {
    window.localStorage.setItem('mosaic-theme', 'custom')
    window.localStorage.setItem('mosaic-custom-theme', JSON.stringify({ base: 'dark', colors: { ...DEFAULT_PALETTES.dark, accent: 'red; background: url(https://example.test)' } }))
    window.localStorage.setItem('mosaic-organization-appearance', '{not json')
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText('custom:light')).toBeVisible()
    expect(themeValue('colorBrandBackground')).toBe(DEFAULT_PALETTES.light.accent)
    expect(document.documentElement.getAttribute('style')).not.toContain('example.test')
  })

  it('persists an explicit theme preference', async () => {
    const user = userEvent.setup()
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText('system:light')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Use dark' }))

    expect(screen.getByText('dark:dark')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(themeValue('colorNeutralBackground1')).toBe(DEFAULT_PALETTES.dark.panel)
  })

  it('follows system changes only when system appearance is selected', async () => {
    const media = Object.assign(new EventTarget(), {
      matches: false,
      media: '(prefers-color-scheme: dark)',
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })
    vi.spyOn(window, 'matchMedia').mockReturnValue(media)
    const user = userEvent.setup()
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    act(() => {
      media.matches = true
      media.dispatchEvent(new MediaQueryListEvent('change', { matches: true }))
    })
    expect(await screen.findByText('system:dark')).toBeVisible()
    expect(themeValue('colorNeutralBackground1')).toBe(DEFAULT_PALETTES.dark.panel)

    await user.click(screen.getByRole('button', { name: 'Use dark' }))
    act(() => {
      media.matches = false
      media.dispatchEvent(new MediaQueryListEvent('change', { matches: false }))
    })
    expect(screen.getByText('dark:dark')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Use system' }))
    expect(screen.getByText('system:light')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('system')
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('falls back to system appearance for an invalid stored preference', async () => {
    window.localStorage.setItem('mosaic-theme', 'invalid')
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText('system:light')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('system')
  })

  it('reports an action refusal without mistaking it for missing portal access', async () => {
    const announce = vi.fn()
    Object.defineProperty(document, 'ariaNotify', { configurable: true, value: announce })
    const failure = Object.assign(new Error('This grant no longer qualifies. Refresh My access.'), { status: 403 })
    render(<MosaicThemeProvider>
      <ErrorState context="action" title="Unable to update the grant" error={failure} />
    </MosaicThemeProvider>)
    await waitFor(() => expect(announce).toHaveBeenCalledOnce())
    expect(screen.queryByText('You do not have access to the portal yet')).not.toBeInTheDocument()
    expect(announce.mock.calls[0][0]).toContain('Unable to update the grant')
    expect(announce.mock.calls[0][0]).toContain(failure.message)
  })

  it('delivers an initial load error through the fallback live region', async () => {
    Object.defineProperty(document, 'ariaNotify', { configurable: true, value: undefined })
    render(<MosaicThemeProvider><ErrorState error={new Error('Retry the catalog request.')} /></MosaicThemeProvider>)
    await waitFor(() => {
      const region = document.querySelector<HTMLElement>('[aria-live="assertive"]')
      expect(region?.innerText).toContain('Retry the catalog request.')
    })
  })

  it('delivers each explicit attempt once when mount effects replay', async () => {
    const user = userEvent.setup()
    const announce = vi.fn()
    Object.defineProperty(document, 'ariaNotify', { configurable: true, value: announce })
    render(<StrictMode><MosaicThemeProvider><RepeatedFailure /></MosaicThemeProvider></StrictMode>)
    await waitFor(() => expect(announce).toHaveBeenCalledOnce())
    await user.click(screen.getByRole('button', { name: 'Retry fixture action' }))
    await waitFor(() => expect(announce).toHaveBeenCalledTimes(2))
    expect(announce.mock.calls.every(([message]) => message.includes('Sync the gateway, then import again.'))).toBe(true)
  })
})
