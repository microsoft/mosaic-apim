import { Badge, Text, Title1 } from '@fluentui/react-components'
import type { ReactNode } from 'react'

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div className="page-heading">
        <nav className="breadcrumbs" aria-label="Breadcrumb">
          <span>MOSAIC</span>
          <span aria-hidden="true">/</span>
          <span>Portal</span>
          <span aria-hidden="true">/</span>
          <span>{title}</span>
        </nav>
        <div className="page-title-row">
          <Title1 as="h1">{title}</Title1>
          <Badge appearance="tint">Personal view</Badge>
        </div>
        {description && <Text className="page-description">{description}</Text>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}
