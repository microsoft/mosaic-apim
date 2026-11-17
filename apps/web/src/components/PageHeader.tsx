import { Badge, Text, Title1 } from '@fluentui/react-components'
import { type ReactNode, type RefObject, useCallback } from 'react'

export type DataSourceKind = 'live' | 'sample' | 'local'

const sourceLabels: Record<DataSourceKind, string> = {
  live: 'Live data',
  sample: 'Sample data',
  local: 'Local preview',
}

export function DataSourceBadge({ kind }: { kind: DataSourceKind }) {
  if (kind === 'live') return null
  return (
    <Badge className={`source-badge source-badge-${kind}`} appearance="tint">
      {sourceLabels[kind]}
    </Badge>
  )
}

export function PageHeader({
  title,
  description,
  actions,
  source,
  focusReturnRef,
}: {
  title: string
  description?: string
  actions?: ReactNode
  source?: DataSourceKind
  focusReturnRef?: RefObject<HTMLElement | null>
}) {
  const headerRef = useCallback((node: HTMLElement | null) => {
    if (focusReturnRef) focusReturnRef.current = node?.querySelector<HTMLElement>('h1') ?? null
  }, [focusReturnRef])
  return (
    <header className="page-header" ref={headerRef}>
      <div className="page-heading">
        <nav className="breadcrumbs" aria-label="Breadcrumb">
          <span>MOSAIC</span>
          <span aria-hidden="true">/</span>
          <span>{title}</span>
        </nav>
        <div className="page-title-row">
          <Title1 as="h1" tabIndex={focusReturnRef ? -1 : undefined}>{title}</Title1>
          {source && <DataSourceBadge kind={source} />}
        </div>
        {description && <Text className="page-description">{description}</Text>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}

export function PreviewNotice({
  kind = 'sample',
  children,
}: {
  kind?: Exclude<DataSourceKind, 'live'>
  children: ReactNode
}) {
  return (
    <div className={`preview-notice preview-notice-${kind}`} role="note">
      <DataSourceBadge kind={kind} />
      <Text>{children}</Text>
    </div>
  )
}
