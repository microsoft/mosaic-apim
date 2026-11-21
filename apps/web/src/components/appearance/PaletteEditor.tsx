import { Badge, Field, Input } from '@fluentui/react-components'
import { CheckmarkRegular, WarningRegular } from '@fluentui/react-icons'
import { type CSSProperties, useId, useState } from 'react'
import {
  contrastChecks,
  isHexColor,
  MIN_TEXT_CONTRAST,
  type Palette,
  PALETTE_FIELDS,
  paletteTokens,
  type ThemeBase,
} from '../../appearance'
import styles from './appearance.module.css'

function ColorField({ label, hint, value, onChange }: { label: string; hint: string; value: string; onChange: (value: string) => void }) {
  const picker = useId()
  // What is typed may be half a color. Keep it as typed and only pass on a complete one.
  const [typed, setTyped] = useState<string | null>(null)
  const shown = typed ?? value
  const invalid = typed !== null && !isHexColor(typed)
  return (
    <Field label={label} hint={hint} validationState={invalid ? 'error' : 'none'}
      validationMessage={invalid ? 'Enter six hex digits after #, such as #3566e8.' : undefined}>
      {(control) => (
        <div className={styles.colorField}>
          <input id={picker} className={styles.swatch} type="color" value={value} aria-label={`${label} color picker`}
            onChange={(event) => { setTyped(null); onChange(event.target.value.toLowerCase()) }} />
          <Input {...control} className={styles.hex} value={shown} maxLength={7} spellCheck={false}
            onChange={(_, data) => {
              const next = data.value.trim().toLowerCase()
              if (isHexColor(next)) { setTyped(null); onChange(next) } else setTyped(data.value)
            }}
            onBlur={() => setTyped(null)} />
        </div>
      )}
    </Field>
  )
}

/** A small stand-in for the app, painted with the palette being edited, so a change shows before it is saved. */
export function PalettePreview({ palette, base }: { palette: Palette; base: ThemeBase }) {
  return (
    <div className={styles.preview} style={paletteTokens(palette, base) as CSSProperties} aria-hidden="true">
      <div className={styles.previewRail}><i /><i className={styles.previewActive} /><i /><i /></div>
      <div className={styles.previewTray}>
        <div className={styles.previewPanel}>
          <strong>Panel heading</strong>
          <span>Secondary text on a panel</span>
          <div className={styles.previewRow}>
            <b className={styles.previewButton}>Primary</b>
            <u className={styles.previewLink}>A link</u>
            <em className={styles.previewChip}>7</em>
          </div>
        </div>
        <span className={styles.previewCaption}>Secondary text on the page</span>
      </div>
    </div>
  )
}

/** The six colors of one palette, a live preview, and the contrast each text pair reaches. */
export function PaletteEditor({ palette, base, onChange }: { palette: Palette; base: ThemeBase; onChange: (palette: Palette) => void }) {
  const checks = contrastChecks(palette)
  return (
    <div className={styles.editor}>
      <div className={styles.fields}>
        {PALETTE_FIELDS.map((field) => (
          <ColorField key={field.key} label={field.label} hint={field.hint} value={palette[field.key]}
            onChange={(value) => onChange({ ...palette, [field.key]: value })} />
        ))}
      </div>
      <div className={styles.side}>
        <PalettePreview palette={palette} base={base} />
        <ul className={styles.checks} aria-label="Text contrast">
          {checks.map((check) => (
            <li key={check.key}>
              <span>{check.label}</span>
              <Badge appearance="tint" color={check.passes ? 'success' : 'danger'} icon={check.passes ? <CheckmarkRegular /> : <WarningRegular />}>
                {check.ratio.toFixed(1)}:1 {check.passes ? 'Readable' : `Needs ${MIN_TEXT_CONTRAST}:1`}
              </Badge>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
