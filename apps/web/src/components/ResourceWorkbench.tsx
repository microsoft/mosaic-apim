import { Button } from '@fluentui/react-components'
import { ArrowLeftRegular } from '@fluentui/react-icons'
import { type ReactNode, useContext, useEffect, useId, useLayoutEffect, useRef } from 'react'
import { UNSAFE_LocationContext } from 'react-router-dom'
import styles from './ResourceWorkbench.module.css'

interface ScrollPosition { left: number; top: number; listLeft: number; listTop: number }
const scrollPositions = new Map<string, ScrollPosition>()

export interface ResourceWorkbenchProps<T> {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  toolbar?: ReactNode
  items: readonly T[]
  getId: (item: T) => string
  selectedId: string | null
  onSelect: (id: string | null) => void
  /** Non-interactive row content; the workbench supplies the selection button. */
  renderListItem: (item: T, state: { selected: boolean }) => ReactNode
  renderDetail: (item: T) => ReactNode
  emptyList?: ReactNode
  emptySelection?: ReactNode
  missingSelection?: ReactNode
  pager?: ReactNode
  listLabel?: string
  detailLabel?: string
  /** Override the default pathname/tab/list-label scroll scope when needed. */
  scrollKey?: string
  className?: string
}

export function ResourceWorkbench<T>({
  title, description, actions, toolbar, items, getId, selectedId, onSelect,
  renderListItem, renderDetail, emptyList, emptySelection, missingSelection,
  pager, listLabel = 'Resources', detailLabel = 'Selected resource', scrollKey, className,
}: ResourceWorkbenchProps<T>) {
  const locationContext = useContext(UNSAFE_LocationContext)
  const location = locationContext?.location
  const scrollScope = scrollKey ?? (location
    ? `${location.pathname}:${new URLSearchParams(location.search).get('tab') ?? ''}:${listLabel}` : null)
  const titleId = useId()
  const detailRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const rowRefs = useRef(new Map<string, HTMLButtonElement>())
  const previousSelection = useRef(selectedId)
  const selected = items.find((item) => getId(item) === selectedId)

  useLayoutEffect(() => {
    if (!scrollScope) return
    const list = listRef.current
    const previous = scrollPositions.get(scrollScope)
    if (previous) {
      if (list) { list.scrollLeft = previous.listLeft; list.scrollTop = previous.listTop }
      window.scrollTo({ left: previous.left, top: previous.top })
    }
    return () => {
      scrollPositions.delete(scrollScope)
      scrollPositions.set(scrollScope, {
        left: window.scrollX, top: window.scrollY,
        listLeft: list?.scrollLeft ?? 0, listTop: list?.scrollTop ?? 0,
      })
      if (scrollPositions.size > 40) scrollPositions.delete(scrollPositions.keys().next().value!)
    }
  }, [scrollScope])

  useEffect(() => {
    if (previousSelection.current === selectedId) return
    if (selectedId !== null) detailRef.current?.focus({ preventScroll: true })
    else if (previousSelection.current) {
      (rowRefs.current.get(previousSelection.current) ?? listRef.current)?.focus({ preventScroll: true })
    }
    previousSelection.current = selectedId
  }, [selectedId])

  return (
    <section className={[styles.workbench, className].filter(Boolean).join(' ')} aria-labelledby={titleId} data-resource-workbench>
      <header className={styles.header}>
        <div className={styles.titleBlock}>
          <h2 id={titleId}>{title}</h2>
          {description && <div className={styles.description}>{description}</div>}
        </div>
        {actions && <div className={styles.actions}>{actions}</div>}
      </header>
      {toolbar && <div className={styles.toolbar}>{toolbar}</div>}
      <div className={`${styles.body} ${selectedId !== null ? styles.hasSelection : ''}`}>
        <div className={styles.listPane} ref={listRef} role="region" aria-label={listLabel} tabIndex={-1}>
          {items.length ? (
            <ul className={styles.list} aria-label={listLabel}>
              {items.map((item) => {
                const id = getId(item)
                const active = id === selectedId
                return (
                  <li key={id}>
                    <button type="button" className={active ? styles.selected : styles.row}
                      aria-pressed={active} onClick={() => onSelect(id)}
                      ref={(element) => { if (element) rowRefs.current.set(id, element); else rowRefs.current.delete(id) }}>
                      {renderListItem(item, { selected: active })}
                    </button>
                  </li>
                )
              })}
            </ul>
          ) : <div className={styles.empty}>{emptyList ?? 'No resources to show.'}</div>}
          {pager && <div className={styles.pager}>{pager}</div>}
        </div>
        <div className={styles.detail} ref={detailRef} role="region" aria-label={detailLabel} tabIndex={-1}>
          {selectedId !== null && <Button className={styles.back} appearance="subtle" icon={<ArrowLeftRegular />}
            onClick={() => onSelect(null)}>Back to list</Button>}
          {selected
            ? renderDetail(selected)
            : <div className={styles.empty}>{selectedId !== null
              ? missingSelection ?? <>This resource is no longer available. <Button onClick={() => onSelect(null)}>Back to list</Button></>
              : emptySelection ?? 'Select a resource to see its details.'}</div>}
        </div>
      </div>
    </section>
  )
}
