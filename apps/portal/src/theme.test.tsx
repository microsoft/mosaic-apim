import { Button } from '@fluentui/react-components'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useMosaicTheme } from './theme-context'
import { MosaicThemeProvider } from './theme'

function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()
  return (
    <>
      <span>{`${preference}:${resolvedTheme}`}</span>
      <Button onClick={() => setPreference('dark')}>Use dark</Button>
      <Button onClick={() => setPreference('system')}>Use system</Button>
    </>
  )
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
  })

  it.each([
    { preference: 'light', surface: '#ffffff', subtle: '#f5f7fa', border: '#c6cfdb' },
    { preference: 'dark', surface: '#1c2531', subtle: '#222e3b', border: '#536479' },
  ])('uses the console palette for stored $preference appearance', ({ preference, surface, subtle, border }) => {
    window.localStorage.setItem('mosaic-theme', preference)
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(screen.getByText(`${preference}:${preference}`)).toBeVisible()
    expect(themeValue('colorNeutralBackground1')).toBe(surface)
    expect(themeValue('colorNeutralBackground2')).toBe(subtle)
    expect(themeValue('colorNeutralStroke1')).toBe(border)
    expect(themeValue('colorBrandBackground')).toBe('#005faa')
    expect(document.documentElement.dataset.theme).toBe(preference)
  })

  it('persists an explicit theme preference', async () => {
    const user = userEvent.setup()
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(screen.getByText('system:light')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Use dark' }))

    expect(screen.getByText('dark:dark')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(themeValue('colorNeutralBackground1')).toBe('#1c2531')
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
    expect(screen.getByText('system:dark')).toBeVisible()
    expect(themeValue('colorNeutralBackground1')).toBe('#1c2531')

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

  it('falls back to system appearance for an invalid stored preference', () => {
    window.localStorage.setItem('mosaic-theme', 'invalid')
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(screen.getByText('system:light')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('system')
  })
})
