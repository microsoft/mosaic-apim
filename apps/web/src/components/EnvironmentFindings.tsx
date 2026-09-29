import {
  Badge,
  Card,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import { environmentFindingsQueryKey, useEnvironmentCatalog } from '../environments'
import type { EnvironmentFinding } from '../types'
import { ErrorState } from './AsyncState'
import { EnvironmentBadge } from './EnvironmentBadge'
import styles from './EnvironmentComponents.module.css'

const confidenceLabels: Record<EnvironmentFinding['confidence'], string> = {
  certain: 'Certain',
  high: 'High',
  medium: 'Medium',
}

const subjectKindLabels: Record<EnvironmentFinding['subject']['kind'], string> = {
  publication: 'Publication',
  backend: 'Backend',
  api: 'API',
  mcpServer: 'MCP server',
}

export function EnvironmentFindings({
  gatewayId,
  title = 'Environment findings',
  hideWhenEmpty = false,
}: {
  gatewayId?: string
  title?: string
  hideWhenEmpty?: boolean
}) {
  const api = useMosaicApi()
  const catalog = useEnvironmentCatalog()
  const findings = useQuery({
    queryKey: environmentFindingsQueryKey(gatewayId),
    queryFn: () => api.listEnvironmentFindings(gatewayId),
  })

  if (findings.isLoading) {
    if (hideWhenEmpty) return null
    return <Spinner label="Loading environment findings" />
  }
  if (findings.isError) {
    return (
      <Card className={styles.findingsCard}>
        <Title3 as="h2">{title}</Title3>
        <ErrorState error={findings.error} />
      </Card>
    )
  }
  const data = findings.data
  if (!data || (data.items.length === 0 && hideWhenEmpty)) return null

  return (
    <Card className={styles.findingsCard}>
      <Title3 as="h2">{title}</Title3>
      {data.items.length === 0 ? (
        <Text>No environment findings.</Text>
      ) : (
        <div className={styles.findingsList}>
          {data.items.map((finding) => (
            <MessageBar key={finding.id} intent="warning">
              <MessageBarBody>
                <MessageBarTitle>{finding.message}</MessageBarTitle>
                <div className={styles.findingDetails}>
                  <Text size={200}>
                    {subjectKindLabels[finding.subject.kind]}: {finding.subject.name}
                  </Text>
                  <Text size={200}>
                    Target: {finding.target.resourceName}{' '}
                    <EnvironmentBadge environment={finding.target.environment} catalog={catalog.data} size="small" />
                  </Text>
                  <Text size={200}>
                    Gateway: {finding.gatewayName}{' '}
                    <EnvironmentBadge environment={finding.gatewayEnvironment} catalog={catalog.data} size="small" />
                  </Text>
                  <Badge appearance="tint" color="warning">
                    {confidenceLabels[finding.confidence]}
                  </Badge>
                  <Text size={200} className={styles.hint}>
                    {finding.evidence}
                  </Text>
                </div>
              </MessageBarBody>
            </MessageBar>
          ))}
        </div>
      )}
      {data.limitations.length > 0 && (
        <Text size={200} className={styles.hint}>
          Limitations: {data.limitations.join(' ')}
        </Text>
      )}
    </Card>
  )
}
