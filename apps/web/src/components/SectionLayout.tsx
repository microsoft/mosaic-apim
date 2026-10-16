import { Button, Tooltip } from '@fluentui/react-components'
import { ChevronLeftRegular, ChevronRightRegular, GridRegular } from '@fluentui/react-icons'
import { type ReactNode, useCallback, useId, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import styles from './SectionLayout.module.css'

export interface PageSection {
  value: string
  label: string
  description?: string
  icon?: ReactNode
}

export function usePageSection(sections: readonly PageSection[], defaultValue = sections[0].value, parameter = 'tab') {
  const [params, setParams] = useSearchParams()
  const requested = params.get(parameter)
  const active = sections.some((section) => section.value === requested) ? requested! : defaultValue
  const select = useCallback((value: string) => {
    const next = new URLSearchParams(params)
    next.set(parameter, value)
    if (value !== active) {
      next.delete('selected')
      next.delete('mode')
      next.delete('runId')
      next.delete('syncRun')
    }
    setParams(next)
  }, [params, parameter, setParams, active])
  return [active, select] as const
}

export function SectionLayout({
  label,
  sections,
  active,
  onSelect,
  children,
  orientation = 'vertical',
}: {
  label: string
  sections: readonly PageSection[]
  active: string
  onSelect: (value: string) => void
  children: ReactNode
  orientation?: 'vertical' | 'horizontal'
}) {
  const id = useId()
  const key = `mosaic.sections.${label}`
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(key) === 'collapsed')
  const selected = sections.find((section) => section.value === active)
  function toggle() {
    const next = !collapsed
    localStorage.setItem(key, next ? 'collapsed' : 'expanded')
    setCollapsed(next)
  }
  return (
    <div className={`${styles.layout} ${orientation === 'horizontal' ? styles.horizontal : collapsed ? styles.collapsed : ''}`}>
      <nav className={styles.navigation} aria-label={`${label} sections`}>
        {orientation === 'vertical' && <Button appearance="subtle" icon={collapsed ? <ChevronRightRegular /> : <ChevronLeftRegular />}
          aria-label={collapsed ? `Expand ${label} sections` : `Collapse ${label} sections`}
          aria-expanded={!collapsed} onClick={toggle} />}
        {sections.map((section) => (
          <Tooltip key={section.value} content={section.label} relationship="label">
          <button type="button"
            className={active === section.value ? styles.active : ''}
            aria-current={active === section.value ? 'page' : undefined}
            title={section.label} aria-label={section.label} onClick={() => onSelect(section.value)}>
            <span className={styles.icon} aria-hidden="true">{section.icon ?? <GridRegular />}</span>
            <span className={collapsed && orientation === 'vertical' ? 'sr-only' : styles.label}>{section.label}</span>
          </button>
          </Tooltip>
        ))}
      </nav>
      <div className={styles.compact}>
        <label htmlFor={id}>{label} section</label>
        <select id={id} value={active} onChange={(event) => onSelect(event.target.value)}>
          {sections.map((section) => <option key={section.value} value={section.value}>{section.label}</option>)}
        </select>
      </div>
      <div className={styles.content}>
        {selected?.description && <p className={styles.description}>{selected.description}</p>}
        {children}
      </div>
    </div>
  )
}
