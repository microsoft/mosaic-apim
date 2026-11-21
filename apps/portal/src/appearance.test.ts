import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  brandRamp,
  contrast,
  contrastChecks,
  DEFAULT_PALETTES,
  fluentOverrides,
  MIN_TEXT_CONTRAST,
  normalizeCustomTheme,
  normalizePalette,
  normalizeScheme,
  paletteIsReadable,
  paletteTokens,
  readableAs,
  readableOn,
  resolvePalette,
  type Palette,
} from './appearance'

const stylesheet = readFileSync(`${import.meta.dirname}/prism.css`, 'utf8')

const coral: Palette = { accent: '#f0603f', backdrop: '#160b08', panel: '#2a1611', border: '#5a3329', text: '#fdf3ef', textMuted: '#e0b9ad' }

function declared(block: string): Record<string, string> {
  return Object.fromEntries([...block.matchAll(/^\s*(--mosaic-[\w-]+):\s*(.+);$/gm)].map((match) => [match[1], match[2]]))
}

describe('colour arithmetic', () => {
  it('measures contrast the way WCAG does', () => {
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21, 5)
    expect(contrast('#ffffff', '#000000')).toBeCloseTo(21, 5)
    expect(contrast('#3566e8', '#3566e8')).toBeCloseTo(1, 5)
    // The usual reference pair: #767676 is the lightest grey that still passes on white.
    expect(contrast('#767676', '#ffffff')).toBeGreaterThanOrEqual(4.5)
    expect(contrast('#777777', '#ffffff')).toBeLessThan(4.5)
  })

  it('puts readable text on any accent', () => {
    for (const accent of ['#3566e8', '#c8f24a', '#f0603f', '#0f9d8a', '#ffffff', '#000000', '#808080']) {
      expect(contrast(readableOn(accent), accent)).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    }
  })

  it('moves an accent only as far as it takes to read as text', () => {
    expect(readableAs('#3566e8', '#ffffff')).toBe('#3566e8')
    const lime = readableAs('#c8f24a', '#ffffff')
    expect(lime).not.toBe('#c8f24a')
    expect(contrast(lime, '#ffffff')).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    expect(contrast(readableAs('#3566e8', '#1b2233'), '#1b2233')).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
  })
})

describe('palettes', () => {
  it('keeps the stylesheet defaults and the built-in palettes in step', () => {
    const dark = stylesheet.indexOf(':root[data-theme="dark"]')
    const lightBlock = stylesheet.slice(stylesheet.indexOf(':root {'), dark)
    const darkBlock = stylesheet.slice(dark, stylesheet.indexOf('}', dark))
    // The light block also holds the fixed tokens (fonts, radii, sizes), which no palette changes.
    expect(declared(lightBlock)).toMatchObject(paletteTokens(DEFAULT_PALETTES.light, 'light'))
    expect(declared(darkBlock)).toEqual(paletteTokens(DEFAULT_PALETTES.dark, 'dark'))
  })

  it('ships built-in palettes that pass their own checks', () => {
    expect(paletteIsReadable(DEFAULT_PALETTES.light)).toBe(true)
    expect(paletteIsReadable(DEFAULT_PALETTES.dark)).toBe(true)
    expect(paletteIsReadable(coral)).toBe(true)
  })

  it('names the pair that fails', () => {
    const faint = { ...DEFAULT_PALETTES.light, textMuted: '#b8bfcc' }
    const failing = contrastChecks(faint).filter((check) => !check.passes).map((check) => check.key)
    expect(failing).toEqual(['textMuted', 'mutedOnBackdrop'])
    expect(paletteIsReadable(faint)).toBe(false)
  })

  it.each([
    ['a short hex', { ...coral, accent: '#f63' }],
    ['a CSS function', { ...coral, accent: 'rgb(240, 96, 63)' }],
    ['a variable reference', { ...coral, accent: 'var(--mosaic-accent)' }],
    ['an injected declaration', { ...coral, accent: '#f0603f; background: url(https://example.test)' }],
    ['a missing colour', { ...coral, textMuted: undefined }],
    ['a non-object', 'coral'],
  ])('refuses %s', (_, value) => {
    expect(normalizePalette(value)).toBeNull()
    expect(normalizeScheme({ light: value, dark: coral })).toBeNull()
    expect(normalizeCustomTheme({ base: 'dark', colors: value })).toBeNull()
  })

  it('normalizes case and ignores anything that is not a palette colour', () => {
    expect(normalizePalette({ ...coral, accent: '#F0603F', extra: 'ignored' })).toEqual(coral)
    expect(normalizeCustomTheme({ base: 'sepia', colors: coral })).toBeNull()
  })

  it('derives readable accent pairs for a custom palette', () => {
    const tokens = paletteTokens(coral, 'dark')
    expect(tokens['--mosaic-accent']).toBe('#f0603f')
    expect(contrast(tokens['--mosaic-on-accent'], coral.accent)).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    expect(contrast(tokens['--mosaic-primary'], coral.panel)).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    expect(contrast(tokens['--mosaic-primary-text'], tokens['--mosaic-primary'])).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    for (const status of ['--mosaic-success', '--mosaic-warning', '--mosaic-danger']) {
      expect(contrast(tokens[status], coral.panel)).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    }
  })

  it('gives Fluent the accent at shade 80 and readable brand text', () => {
    expect(brandRamp('#f0603f')[80]).toBe('#f0603f')
    expect(Object.keys(brandRamp('#f0603f'))).toHaveLength(16)
    const overrides = fluentOverrides(coral, 'dark')
    expect(overrides.colorBrandBackground).toBe('#f0603f')
    expect(overrides.colorNeutralBackground1).toBe(coral.panel)
    expect(contrast(overrides.colorNeutralForegroundOnBrand, coral.accent)).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
    expect(contrast(overrides.colorBrandForegroundLink, coral.panel)).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST)
  })
})

describe('resolvePalette', () => {
  const scheme = { light: { ...DEFAULT_PALETTES.light, accent: '#0f9d8a' }, dark: { ...DEFAULT_PALETTES.dark, accent: '#0f9d8a' } }
  const custom = { base: 'dark' as const, colors: coral }

  it('uses the built-in palettes until the organization saves its own', () => {
    expect(resolvePalette('light', 'dark', null, null)).toEqual({ base: 'light', palette: DEFAULT_PALETTES.light })
    expect(resolvePalette('dark', 'light', scheme, custom)).toEqual({ base: 'dark', palette: scheme.dark })
  })

  it('follows the device in System', () => {
    expect(resolvePalette('system', 'dark', scheme, custom)).toEqual({ base: 'dark', palette: scheme.dark })
    expect(resolvePalette('system', 'light', null, null).base).toBe('light')
  })

  it('uses a person’s colours and their base in Custom, and the device when none are saved', () => {
    expect(resolvePalette('custom', 'light', scheme, custom)).toEqual({ base: 'dark', palette: coral })
    expect(resolvePalette('custom', 'light', scheme, null)).toEqual({ base: 'light', palette: scheme.light })
  })
})
