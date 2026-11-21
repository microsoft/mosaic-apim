import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_PALETTES } from '../../appearance'
import { MosaicThemeProvider } from '../../theme'
import { useMosaicTheme } from '../../theme-context'
import { AppearanceControl } from '../AppearanceControl'
import { CustomThemeEditor } from './CustomThemeEditor'

const coral = { accent: '#f0603f', backdrop: '#160b08', panel: '#2a1611', border: '#5a3329', text: '#fdf3ef', textMuted: '#e0b9ad' }

function Probe() {
  const { preference, resolvedTheme, palette } = useMosaicTheme()
  return <output>{`${preference}:${resolvedTheme}:${palette.accent}`}</output>
}

function renderEditor() {
  return render(<MosaicThemeProvider><Probe /><CustomThemeEditor /></MosaicThemeProvider>)
}

describe('custom colors', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('style')
  })

  it('starts from the current colors and saves a readable palette in this browser', async () => {
    const user = userEvent.setup()
    renderEditor()

    const accent = await screen.findByRole('textbox', { name: 'Accent' })
    expect(accent).toHaveValue(DEFAULT_PALETTES.light.accent)
    await user.clear(accent)
    await user.type(accent, '#0F9D8A')
    expect(screen.getByText('system:light:#3566e8')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Save and use my colors' }))

    expect(screen.getByText('custom:light:#0f9d8a')).toBeVisible()
    expect(localStorage.getItem('mosaic-theme')).toBe('custom')
    expect(JSON.parse(localStorage.getItem('mosaic-custom-theme')!)).toEqual({ base: 'light', colors: { ...DEFAULT_PALETTES.light, accent: '#0f9d8a' } })
    expect(document.documentElement.style.getPropertyValue('--mosaic-accent')).toBe('#0f9d8a')
    // White on this teal would be hard to read, so MOSAIC picks the text color itself.
    expect(document.documentElement.style.getPropertyValue('--mosaic-on-accent')).toBe('#0b0d12')
  })

  it('will not save colors that leave text hard to read', async () => {
    const user = userEvent.setup()
    renderEditor()

    const muted = await screen.findByRole('textbox', { name: 'Secondary text' })
    await user.clear(muted)
    await user.type(muted, '#d0d4dc')

    const checks = within(screen.getByRole('list', { name: 'Text contrast' }))
    expect(checks.getAllByText(/Needs 4.5:1/)).toHaveLength(2)
    // The warning is also announced, so it appears once on screen and once in the live region.
    expect(screen.getAllByText(/Some text would be hard to read/)[0]).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save and use my colors' })).toBeDisabled()
    expect(localStorage.getItem('mosaic-custom-theme')).toBeNull()
  })

  it('keeps a half-typed color out of the palette', async () => {
    const user = userEvent.setup()
    renderEditor()

    const accent = await screen.findByRole('textbox', { name: 'Accent' })
    await user.clear(accent)
    await user.type(accent, '#0f9')

    expect(screen.getByText('Enter six hex digits after #, such as #3566e8.')).toBeVisible()
    expect(screen.getByLabelText('Accent color picker')).toHaveValue(DEFAULT_PALETTES.light.accent)
    await user.tab()
    expect(accent).toHaveValue(DEFAULT_PALETTES.light.accent)
  })

  it('starts again from the dark colors, then removes saved colors', async () => {
    localStorage.setItem('mosaic-theme', 'custom')
    localStorage.setItem('mosaic-custom-theme', JSON.stringify({ base: 'dark', colors: coral }))
    const user = userEvent.setup()
    renderEditor()

    expect(await screen.findByText('custom:dark:#f0603f')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Light' }))
    expect(screen.getByRole('textbox', { name: 'Panels' })).toHaveValue(DEFAULT_PALETTES.light.panel)

    await user.click(screen.getByRole('button', { name: 'Remove my colors' }))

    expect(screen.getByText('system:light:#3566e8')).toBeVisible()
    expect(localStorage.getItem('mosaic-custom-theme')).toBeNull()
    expect(localStorage.getItem('mosaic-theme')).toBe('system')
  })

  it('explains when an administrator has turned custom colors off', async () => {
    localStorage.setItem('mosaic-organization-appearance', JSON.stringify({ scheme: null, allowCustomThemes: false }))
    renderEditor()

    expect(await screen.findByText(/Your administrator has turned off custom colors/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save and use my colors' })).not.toBeInTheDocument()
  })
})

describe('AppearanceControl with custom colors', () => {
  beforeEach(() => localStorage.clear())

  it('switches to saved custom colors from the navigation', async () => {
    localStorage.setItem('mosaic-custom-theme', JSON.stringify({ base: 'dark', colors: coral }))
    const user = userEvent.setup()
    render(<MosaicThemeProvider><Probe /><AppearanceControl /></MosaicThemeProvider>)

    const group = within(await screen.findByRole('group', { name: 'Appearance' }))
    await user.click(group.getByRole('button', { name: 'Custom' }))

    expect(screen.getByText('custom:dark:#f0603f')).toBeVisible()
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(screen.getByRole('button', { name: 'Appearance: custom' })).toBeVisible()
  })

  it('leads to the editor until colors are saved, and hides Custom where there is none', async () => {
    const onCustomize = vi.fn()
    const user = userEvent.setup()
    const { unmount } = render(<MosaicThemeProvider><Probe /><AppearanceControl onCustomize={onCustomize} /></MosaicThemeProvider>)

    await user.click(within(await screen.findByRole('group', { name: 'Appearance' })).getByRole('button', { name: 'Custom' }))
    expect(onCustomize).toHaveBeenCalledTimes(1)
    expect(screen.getByText('system:light:#3566e8')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Appearance: system' }))
    await user.click(screen.getByRole('menuitem', { name: 'Customize colors' }))
    expect(onCustomize).toHaveBeenCalledTimes(2)

    unmount()
    render(<MosaicThemeProvider><AppearanceControl /></MosaicThemeProvider>)
    expect(within(await screen.findByRole('group', { name: 'Appearance' })).queryByRole('button', { name: 'Custom' })).not.toBeInTheDocument()
  })

  it('shows System while custom colors are turned off, and keeps the choice for later', async () => {
    localStorage.setItem('mosaic-theme', 'custom')
    localStorage.setItem('mosaic-custom-theme', JSON.stringify({ base: 'dark', colors: coral }))
    localStorage.setItem('mosaic-organization-appearance', JSON.stringify({ scheme: null, allowCustomThemes: false }))
    render(<MosaicThemeProvider><Probe /><AppearanceControl onCustomize={vi.fn()} /></MosaicThemeProvider>)

    expect(await screen.findByText('custom:light:#3566e8')).toBeVisible()
    const group = within(screen.getByRole('group', { name: 'Appearance' }))
    expect(group.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true')
    expect(group.queryByRole('button', { name: 'Custom' })).not.toBeInTheDocument()
    expect(localStorage.getItem('mosaic-theme')).toBe('custom')
  })
})
