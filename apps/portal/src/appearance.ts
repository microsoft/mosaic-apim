/**
 * MOSAIC color palettes.
 *
 * A palette is six colors. Every `--mosaic-*` token and the Fluent theme are derived from it,
 * so the default look, an organization's scheme, and a person's custom colors all take the same
 * path. Text placed on the accent and the accent used as text are computed, never chosen, so a
 * custom accent cannot make a button or link unreadable.
 */

export type ThemeBase = 'light' | 'dark'

export interface Palette {
  /** Primary buttons, the active navigation item, counters, and the backdrop wash. */
  accent: string
  /** The page behind every panel. */
  backdrop: string
  /** Cards, navigation panels, dialogs, and menus. */
  panel: string
  border: string
  text: string
  textMuted: string
}

export const PALETTE_FIELDS: Array<{ key: keyof Palette; label: string; hint: string }> = [
  { key: 'accent', label: 'Accent', hint: 'Primary buttons, active navigation, counters, and the backdrop wash' },
  { key: 'backdrop', label: 'Page background', hint: 'The page behind every panel' },
  { key: 'panel', label: 'Panels', hint: 'Cards, navigation, dialogs, and menus' },
  { key: 'border', label: 'Borders', hint: 'Outlines and dividers' },
  { key: 'text', label: 'Text', hint: 'Headings and body text' },
  { key: 'textMuted', label: 'Secondary text', hint: 'Descriptions and labels' },
]

export interface OrganizationScheme {
  light: Palette
  dark: Palette
}

export interface CustomTheme {
  /** Which built-in mode the custom colors replace. It decides shadows, status colors, and the browser's own controls. */
  base: ThemeBase
  colors: Palette
}

export const DEFAULT_PALETTES: Record<ThemeBase, Palette> = {
  light: { accent: '#3566e8', backdrop: '#eef1f8', panel: '#ffffff', border: '#cdd4e3', text: '#0f1629', textMuted: '#465069' },
  dark: { accent: '#3566e8', backdrop: '#0a0e19', panel: '#1b2233', border: '#3a4560', text: '#f3f5fb', textMuted: '#b6bfd4' },
}

/** The lowest text contrast MOSAIC accepts for a saved palette (WCAG AA for body text). */
export const MIN_TEXT_CONTRAST = 4.5

const HEX = /^#[0-9a-f]{6}$/

export function isHexColor(value: unknown): value is string {
  return typeof value === 'string' && HEX.test(value.toLowerCase())
}

type Rgb = [number, number, number]

function rgb(hex: string): Rgb {
  return [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16)) as Rgb
}

function hex(channels: Rgb): string {
  return `#${channels.map((channel) => Math.round(Math.min(255, Math.max(0, channel))).toString(16).padStart(2, '0')).join('')}`
}

function linear(channel: number): number {
  const value = channel / 255
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
}

function gamma(value: number): number {
  return 255 * (value <= 0.0031308 ? value * 12.92 : 1.055 * value ** (1 / 2.4) - 0.055)
}

function luminance(color: string): number {
  const [red, green, blue] = rgb(color).map(linear)
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

/** WCAG contrast ratio between two opaque colors, from 1 to 21. */
export function contrast(first: string, second: string): number {
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (light + 0.05) / (dark + 0.05)
}

/** `amount` of `to` mixed into `from`, in sRGB. */
export function mix(from: string, to: string, amount: number): string {
  const [a, b] = [rgb(from), rgb(to)]
  return hex(a.map((channel, index) => channel + (b[index] - channel) * amount) as Rgb)
}

function alpha(color: string, opacity: number): string {
  const [red, green, blue] = rgb(color)
  return `rgba(${red}, ${green}, ${blue}, ${opacity})`
}

/** Black or white, whichever reads better on `background`. */
export function readableOn(background: string): string {
  return contrast('#ffffff', background) >= contrast('#0b0d12', background) ? '#ffffff' : '#0b0d12'
}

/** `color`, moved toward black or white only as far as it takes to reach `ratio` on `background`. */
export function readableAs(color: string, background: string, ratio = MIN_TEXT_CONTRAST): string {
  const toward = luminance(background) > 0.4 ? '#000000' : '#ffffff'
  let result = color
  for (let step = 1; step <= 20 && contrast(result, background) < ratio; step += 1) {
    result = mix(color, toward, step * 0.05)
  }
  return result
}

// OKLCH keeps the backdrop's companion hues equally vivid whatever accent is chosen.
function hueOf(color: string): number {
  const [red, green, blue] = rgb(color).map(linear)
  const l = Math.cbrt(0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue)
  const m = Math.cbrt(0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue)
  const s = Math.cbrt(0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue)
  const a = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s
  const b = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s
  return ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360
}

function oklch(lightness: number, chroma: number, hue: number): Rgb {
  const a = chroma * Math.cos((hue * Math.PI) / 180)
  const b = chroma * Math.sin((hue * Math.PI) / 180)
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    gamma(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    gamma(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    gamma(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

function wash(lightness: number, chroma: number, hue: number, opacity: number): string {
  return alpha(hex(oklch(lightness, chroma, hue)), opacity)
}

export function normalizePalette(value: unknown): Palette | null {
  if (!value || typeof value !== 'object') return null
  const source = value as Record<string, unknown>
  const palette = {} as Palette
  for (const { key } of PALETTE_FIELDS) {
    const color = source[key]
    if (!isHexColor(color)) return null
    palette[key] = color.toLowerCase()
  }
  return palette
}

export function normalizeScheme(value: unknown): OrganizationScheme | null {
  if (!value || typeof value !== 'object') return null
  const source = value as Record<string, unknown>
  const light = normalizePalette(source.light)
  const dark = normalizePalette(source.dark)
  return light && dark ? { light, dark } : null
}

export function normalizeCustomTheme(value: unknown): CustomTheme | null {
  if (!value || typeof value !== 'object') return null
  const source = value as Record<string, unknown>
  const colors = normalizePalette(source.colors)
  return colors && (source.base === 'light' || source.base === 'dark') ? { base: source.base, colors } : null
}

export function samePalette(first: Palette, second: Palette): boolean {
  return PALETTE_FIELDS.every(({ key }) => first[key] === second[key])
}

export interface ContrastCheck {
  key: 'text' | 'textMuted' | 'textOnBackdrop' | 'mutedOnBackdrop'
  label: string
  ratio: number
  passes: boolean
}

/** The pairs a palette must keep readable. Accent pairs are computed, so they always pass. */
export function contrastChecks(palette: Palette): ContrastCheck[] {
  const check = (key: ContrastCheck['key'], label: string, foreground: string, background: string): ContrastCheck => {
    const ratio = contrast(foreground, background)
    // Judge the figure people see, so 4.46 shown as "4.5:1" is not then refused.
    return { key, label, ratio, passes: Math.round(ratio * 10) / 10 >= MIN_TEXT_CONTRAST }
  }
  return [
    check('text', 'Text on panels', palette.text, palette.panel),
    check('textMuted', 'Secondary text on panels', palette.textMuted, palette.panel),
    check('textOnBackdrop', 'Text on the page background', palette.text, palette.backdrop),
    check('mutedOnBackdrop', 'Secondary text on the page background', palette.textMuted, palette.backdrop),
  ]
}

export function paletteIsReadable(palette: Palette): boolean {
  return contrastChecks(palette).every((check) => check.passes)
}

const STATUS: Record<ThemeBase, { success: string; warning: string; danger: string }> = {
  light: { success: '#0e7a3b', warning: '#8f5400', danger: '#b3261e' },
  dark: { success: '#62d38e', warning: '#f2c063', danger: '#ff9a94' },
}

/** Every `--mosaic-*` custom property for a palette. */
export function paletteTokens(palette: Palette, base: ThemeBase): Record<string, string> {
  const dark = base === 'dark'
  const { accent, backdrop, panel, border, text, textMuted } = palette
  const accentInk = readableAs(accent, panel)
  const hue = hueOf(accent)
  const rail = dark ? mix(panel, text, 0.06) : mix(text, '#000000', 0.12)
  const railText = readableOn(rail)
  const status = STATUS[base]
  const success = readableAs(status.success, panel)
  const warning = readableAs(status.warning, panel)
  const danger = readableAs(status.danger, panel)
  const soft = dark ? 0.2 : 0.13
  return {
    '--mosaic-bg': backdrop,
    '--mosaic-aurora-1': alpha(accent, dark ? 0.5 : 0.3),
    '--mosaic-aurora-2': dark ? wash(0.5, 0.16, hue + 70, 0.42) : wash(0.84, 0.11, hue + 70, 0.55),
    '--mosaic-aurora-3': dark ? wash(0.46, 0.15, hue - 60, 0.4) : wash(0.86, 0.1, hue - 60, 0.5),
    '--mosaic-surface': alpha(panel, dark ? 0.6 : 0.68),
    '--mosaic-surface-solid': panel,
    '--mosaic-tray': alpha(panel, dark ? 0.4 : 0.38),
    '--mosaic-glass': alpha(panel, dark ? 0.66 : 0.7),
    '--mosaic-surface-subtle': alpha(text, dark ? 0.07 : 0.045),
    '--mosaic-surface-strong': alpha(text, dark ? 0.13 : 0.09),
    '--mosaic-border': border,
    '--mosaic-border-soft': alpha(border, dark ? 0.6 : 0.62),
    '--mosaic-edge': dark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(255, 255, 255, 0.86)',
    '--mosaic-text': text,
    '--mosaic-text-muted': textMuted,
    '--mosaic-text-faint': readableAs(mix(textMuted, panel, 0.3), panel, 3),
    '--mosaic-accent': accent,
    '--mosaic-on-accent': readableOn(accent),
    '--mosaic-accent-glow': alpha(accent, dark ? 0.55 : 0.45),
    '--mosaic-primary': accentInk,
    '--mosaic-primary-hover': mix(accentInk, dark ? '#ffffff' : '#000000', 0.16),
    '--mosaic-primary-soft': alpha(accent, soft),
    '--mosaic-primary-text': readableOn(accentInk),
    '--mosaic-strong': text,
    '--mosaic-on-strong': panel,
    '--mosaic-rail': rail,
    '--mosaic-rail-text': railText,
    '--mosaic-rail-muted': alpha(railText, 0.64),
    '--mosaic-rail-hover': alpha(railText, 0.1),
    '--mosaic-rail-active': alpha(railText, 0.16),
    '--mosaic-success': success,
    '--mosaic-success-soft': alpha(success, soft),
    '--mosaic-warning': warning,
    '--mosaic-warning-soft': alpha(warning, soft),
    '--mosaic-danger': danger,
    '--mosaic-danger-soft': alpha(danger, soft),
    '--mosaic-code-bg': dark ? '#0d1017' : '#151b29',
    '--mosaic-code-text': '#e8eef4',
    '--mosaic-shadow': dark
      ? 'inset 0 1px 0 rgba(255, 255, 255, 0.04), 0 18px 44px -18px rgba(0, 0, 0, 0.6)'
      : 'inset 0 1px 0 rgba(255, 255, 255, 0.6), 0 18px 44px -18px rgba(30, 45, 90, 0.22)',
  }
}

/** Fluent's sixteen brand shades with the accent at 80, where Fluent's light theme reads its primary. */
export function brandRamp(accent: string): Record<10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 110 | 120 | 130 | 140 | 150 | 160, string> {
  const darker = [0.86, 0.78, 0.68, 0.56, 0.44, 0.3, 0.14]
  const lighter = [0.14, 0.28, 0.4, 0.52, 0.62, 0.72, 0.8, 0.88]
  const shades = [...darker.map((amount) => mix(accent, '#000000', amount)), accent, ...lighter.map((amount) => mix(accent, '#ffffff', amount))]
  return Object.fromEntries(shades.map((shade, index) => [(index + 1) * 10, shade])) as ReturnType<typeof brandRamp>
}

/** The Fluent tokens MOSAIC replaces so Fluent components match the palette. */
export function fluentOverrides(palette: Palette, base: ThemeBase): Record<string, string> {
  const dark = base === 'dark'
  const { accent, panel, border, text, textMuted } = palette
  const accentInk = readableAs(accent, panel)
  const shade = dark ? '#ffffff' : '#000000'
  return {
    colorNeutralBackground1: panel,
    colorNeutralBackground1Hover: mix(panel, text, 0.05),
    colorNeutralBackground1Pressed: mix(panel, text, 0.1),
    colorNeutralBackground2: mix(panel, text, 0.04),
    colorNeutralBackground3: mix(panel, text, 0.09),
    colorNeutralForeground1: text,
    colorNeutralForeground2: textMuted,
    colorNeutralForeground3: readableAs(mix(textMuted, panel, 0.2), panel),
    colorNeutralStroke1: border,
    colorNeutralStroke2: mix(border, panel, 0.4),
    colorNeutralStrokeAccessible: readableAs(border, panel, 3),
    colorBrandBackground: accent,
    colorBrandBackgroundHover: mix(accent, shade, 0.1),
    colorBrandBackgroundPressed: mix(accent, shade, 0.2),
    colorBrandBackgroundSelected: mix(accent, shade, 0.14),
    colorNeutralForegroundOnBrand: readableOn(accent),
    colorBrandForeground1: accentInk,
    colorBrandForeground2: accentInk,
    colorBrandForegroundLink: accentInk,
    colorBrandForegroundLinkHover: mix(accentInk, shade, 0.16),
    colorBrandForegroundLinkPressed: mix(accentInk, shade, 0.28),
    colorCompoundBrandForeground1: accentInk,
    colorCompoundBrandForeground1Hover: mix(accentInk, shade, 0.14),
    colorCompoundBrandForeground1Pressed: mix(accentInk, shade, 0.26),
    colorCompoundBrandBackground: accentInk,
    colorCompoundBrandBackgroundHover: mix(accentInk, shade, 0.14),
    colorCompoundBrandBackgroundPressed: mix(accentInk, shade, 0.26),
    colorCompoundBrandStroke: accentInk,
    colorCompoundBrandStrokeHover: mix(accentInk, shade, 0.14),
    colorCompoundBrandStrokePressed: mix(accentInk, shade, 0.26),
    colorBrandStroke1: accentInk,
    colorStrokeFocus2: text,
    colorNeutralForegroundInverted: readableOn(accentInk),
    borderRadiusMedium: '10px',
    borderRadiusLarge: '14px',
    borderRadiusXLarge: '20px',
    fontFamilyBase: '"Outfit Variable", "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif',
    fontFamilyMonospace: '"JetBrains Mono Variable", "Cascadia Mono", Consolas, "Courier New", monospace',
  }
}

/** The palette a mode shows, given what the organization and the person have saved. */
export function resolvePalette(
  preference: 'light' | 'dark' | 'system' | 'custom',
  systemTheme: ThemeBase,
  scheme: OrganizationScheme | null,
  custom: CustomTheme | null,
): { base: ThemeBase; palette: Palette } {
  if (preference === 'custom' && custom) return { base: custom.base, palette: custom.colors }
  const base = preference === 'light' || preference === 'dark' ? preference : systemTheme
  return { base, palette: (scheme ?? DEFAULT_PALETTES)[base] }
}
