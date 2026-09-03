import { Button } from '@fluentui/react-components'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { useMosaicTheme } from './theme-context'
import { MosaicThemeProvider } from './theme'

function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useMosaicTheme()
  return (
    <>
      <span>{`${preference}:${resolvedTheme}`}</span>
      <Button onClick={() => setPreference('dark')}>Use dark</Button>
    </>
  )
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

    expect(screen.getByText('system:light')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Use dark' }))

    expect(screen.getByText('dark:dark')).toBeVisible()
    expect(window.localStorage.getItem('mosaic-theme')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })
})
