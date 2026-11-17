import { Button } from '@fluentui/react-components'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode, useState } from 'react'
import { useMosaicTheme } from './theme-context'
import { MosaicThemeProvider } from './theme'
import { ErrorState } from './components/AsyncState'

const originalAriaNotify = Object.getOwnPropertyDescriptor(document, 'ariaNotify')

afterEach(() => {
  if (originalAriaNotify) Object.defineProperty(document, 'ariaNotify', originalAriaNotify)
  else Reflect.deleteProperty(document, 'ariaNotify')
})

function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()
  return (
    <>
      <span>{`${preference}:${resolvedTheme}`}</span>
      <Button onClick={() => setPreference('dark')}>Use dark</Button>
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

describe('MosaicThemeProvider', () => {
  beforeEach(() => {
    window.localStorage.clear()
    delete document.documentElement.dataset.theme
  })

  it('persists an explicit theme preference', async () => {
    const user = userEvent.setup()
    render(
      <MosaicThemeProvider>
        <ThemeProbe />
      </MosaicThemeProvider>,
    )

    expect(await screen.findByText('system:light')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Use dark' }))

    expect(screen.getByText('dark:dark')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('delivers an initial action error through the real root announcer', async () => {
    const announce = vi.fn()
    Object.defineProperty(document, 'ariaNotify', { configurable: true, value: announce })
    render(<MosaicThemeProvider>
      <ErrorState context="action" title="Unable to import APIs"
        error={new Error('Sync the gateway, then import again.')} />
    </MosaicThemeProvider>)
    await waitFor(() => expect(announce).toHaveBeenCalledOnce())
    expect(announce.mock.calls[0][0]).toContain('Unable to import APIs')
    expect(announce.mock.calls[0][0]).toContain('Sync the gateway, then import again.')
    expect(announce.mock.calls[0][1]).toEqual({ priority: 'high' })
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

  it('does not lose an initial load error before the fallback live region mounts', async () => {
    Object.defineProperty(document, 'ariaNotify', { configurable: true, value: undefined })
    render(<MosaicThemeProvider>
      <ErrorState error={new Error('The model catalog could not be loaded. Retry the request.')} />
    </MosaicThemeProvider>)
    await waitFor(() => {
      const region = document.querySelector<HTMLElement>('[aria-live="assertive"]')
      expect(region?.innerText).toContain('The model catalog could not be loaded. Retry the request.')
    })
  })
})
