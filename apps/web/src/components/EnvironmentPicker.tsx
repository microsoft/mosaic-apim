import { Combobox, Field, Option, Text } from '@fluentui/react-components'
import { useEffect, useMemo, useState } from 'react'
import { environmentLabel } from '../environments'
import type { EnvironmentCatalogView } from '../types'
import { EnvironmentBadge } from './EnvironmentBadge'
import styles from './EnvironmentComponents.module.css'

export interface EnvironmentPickerSuggestion {
  environment: string
  evidence: string
}

export function EnvironmentPicker({
  catalog,
  value,
  onChange,
  allowUnclassified = false,
  suggestion,
  required,
  label = 'Environment',
  disabled,
  validationMessage,
}: {
  catalog?: EnvironmentCatalogView
  value?: string | null
  onChange?: (environment: string | null) => void
  allowUnclassified?: boolean
  suggestion?: EnvironmentPickerSuggestion
  required?: boolean
  label?: string
  disabled?: boolean
  validationMessage?: string
}) {
  const initial =
    value ??
    suggestion?.environment ??
    (allowUnclassified ? null : (catalog?.environments[0]?.key ?? null))
  const [selected, setSelected] = useState<string | null>(initial)
  useEffect(
    () =>
      setSelected(
        value ??
          suggestion?.environment ??
          (allowUnclassified ? null : (catalog?.environments[0]?.key ?? null)),
      ),
    [value, suggestion?.environment, allowUnclassified, catalog],
  )
  const options = useMemo(
    () => [
      ...(allowUnclassified
        ? [{ key: '__unclassified__', environment: null as string | null, label: 'Unclassified' }]
        : []),
      ...(catalog?.environments ?? []).map((environment) => ({
        key: environment.key,
        environment: environment.key,
        label: environment.displayName,
      })),
    ],
    [allowUnclassified, catalog],
  )
  const selectedKey = selected ?? '__unclassified__'
  return (
    <Field label={label} required={required} validationMessage={validationMessage}>
      <Combobox
        disabled={disabled}
        selectedOptions={[selectedKey]}
        value={environmentLabel(catalog, selected)}
        aria-label={label}
        onOptionSelect={(_, data) => {
          const next = data.optionValue === '__unclassified__' ? null : (data.optionValue ?? null)
          setSelected(next)
          onChange?.(next)
        }}
      >
        {options.map((option) => (
          <Option key={option.key} value={option.key} text={option.label}>
            <span className={styles.pickerOption}>
              <EnvironmentBadge environment={option.environment} catalog={catalog} />
            </span>
          </Option>
        ))}
      </Combobox>
      {suggestion && (
        <Text className={styles.hint} size={200}>
          Suggested: {environmentLabel(catalog, suggestion.environment)} — {suggestion.evidence}
        </Text>
      )}
    </Field>
  )
}
