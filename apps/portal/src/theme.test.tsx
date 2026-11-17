import { Button } from '@fluentui/react-components'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode, useState } from 'react'
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

  it.each([
    { preference: 'light', surface: '#ffffff', subtle: '#f5f7fa', border: '#c6cfdb' },
    { preference: 'dark', surface: '#1c2531', subtle: '#222e3b', border: '#536479' },
  ])('uses the console palette for stored $preference appearance', async ({ preference, surface, subtle, border }) => {
    window.localStorage.setItem('mosaic-theme', preference)
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText(`${preference}:${preference}`)).toBeVisible()
    expect(themeValue('colorNeutralBackground1')).toBe(surface)
    expect(themeValue('colorNeutralBackground2')).toBe(subtle)
    expect(themeValue('colorNeutralStroke1')).toBe(border)
    expect(themeValue('colorBrandBackground')).toBe('#005faa')
    expect(themeValue('colorNeutralStroke2')).toBe(preference === 'light' ? '#dce5ef' : '#354354')
    expect(themeValue('borderRadiusMedium')).toBe('8px')
    expect(themeValue('borderRadiusLarge')).toBe('12px')
    expect(document.documentElement.dataset.theme).toBe(preference)
  })

  it('persists an explicit theme preference', async () => {
    const user = userEvent.setup()
    render(<MosaicThemeProvider><ThemeProbe /></MosaicThemeProvider>)

    expect(await screen.findByText('system:light')).toBeVisible()
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
    expect(await screen.findByText('system:dark')).toBeVisible()
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
