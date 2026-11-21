import { Button, MessageBar, MessageBarBody, Text } from '@fluentui/react-components'
import { useState } from 'react'
import { type CustomTheme, DEFAULT_PALETTES, paletteIsReadable, samePalette, type ThemeBase } from '../../appearance'
import { useMosaicTheme } from '../../theme-context'
import { PaletteEditor } from './PaletteEditor'
import styles from './appearance.module.css'

const BASES: Array<{ value: ThemeBase; label: string }> = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

/**
 * A person's own colours. They are kept in this browser only, start from the organization's
 * colours, and cannot be saved until every text pair is readable.
 */
export function CustomThemeEditor({ onSaved }: { onSaved?: () => void }) {
  const { customTheme, setCustomTheme, setPreference, preference, organization, resolvedTheme, palette } = useMosaicTheme()
  const starting = (base: ThemeBase) => (organization.scheme ?? DEFAULT_PALETTES)[base]
  const [draft, setDraft] = useState<CustomTheme>(() => customTheme ?? { base: resolvedTheme, colors: palette })
  const readable = paletteIsReadable(draft.colors)
  const saved = customTheme !== null && customTheme.base === draft.base && samePalette(customTheme.colors, draft.colors)

  if (!organization.allowCustomThemes) {
    return (
      <MessageBar intent="info">
        <MessageBarBody>Your administrator has turned off custom colours. Light, Dark, and System use your organization&apos;s colours.</MessageBarBody>
      </MessageBar>
    )
  }

  return (
    <div className={styles.stack}>
      <div className={styles.row}>
        <Text weight="semibold" id="custom-theme-base">Start from</Text>
        <div className={styles.modeTabs} role="group" aria-labelledby="custom-theme-base">
          {BASES.map((base) => (
            <Button key={base.value} size="small" appearance={draft.base === base.value ? 'primary' : 'subtle'} aria-pressed={draft.base === base.value}
              onClick={() => setDraft({ base: base.value, colors: starting(base.value) })}>{base.label}</Button>
          ))}
        </div>
        <Text className={styles.note}>Choosing one replaces the colours below with your organization&apos;s {draft.base} colours.</Text>
      </div>
      <PaletteEditor palette={draft.colors} base={draft.base} onChange={(colors) => setDraft({ ...draft, colors })} />
      {!readable && (
        <MessageBar intent="warning">
          <MessageBarBody>Some text would be hard to read. Adjust the colours marked above before saving.</MessageBarBody>
        </MessageBar>
      )}
      <div className={styles.row}>
        <Button appearance="primary" disabled={!readable || (saved && preference === 'custom')}
          onClick={() => { setCustomTheme(draft); setPreference('custom'); onSaved?.() }}>
          {saved && preference !== 'custom' ? 'Use my colours' : 'Save and use my colours'}
        </Button>
        {customTheme && (
          <Button onClick={() => { setCustomTheme(null); if (preference === 'custom') setPreference('system'); setDraft({ base: resolvedTheme, colors: starting(resolvedTheme) }) }}>
            Remove my colours
          </Button>
        )}
        <Text className={styles.note}>Saved in this browser only.</Text>
      </div>
    </div>
  )
}
