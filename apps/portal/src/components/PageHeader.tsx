import { Text, Title1 } from '@fluentui/react-components'
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
        <div className="page-title-row">
          <Title1 as="h1">{title}</Title1>
        </div>
        {description && <Text className="page-description">{description}</Text>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}
