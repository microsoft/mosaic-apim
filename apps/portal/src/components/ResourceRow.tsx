import { Button } from '@fluentui/react-components'
import { ChevronRightRegular } from '@fluentui/react-icons'
import type { ReactNode } from 'react'

export function ResourceRow({
  title, subtitle, environments, status, action, actionLabel, onSelect,
}: {
  title: string
  subtitle: ReactNode
  environments: ReactNode
  status: ReactNode
  action: string
  actionLabel: string
  onSelect: () => void
}) {
  return (
    <li className="resource-row">
      <div className="resource-row-name">
        <h2>{title}</h2>
        <span className="resource-row-meta">{subtitle}</span>
      </div>
      <div className="resource-row-environments">{environments}</div>
      <div className="resource-row-status">{status}</div>
      <Button appearance="subtle" icon={<ChevronRightRegular />} iconPosition="after"
        aria-label={actionLabel} onClick={onSelect}>{action}</Button>
    </li>
  )
}
