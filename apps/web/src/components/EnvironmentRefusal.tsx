import {
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
} from '@fluentui/react-components'
import type {
  EnvironmentCatalogView,
  GrantsAcknowledgmentRequiredDetails,
  PublicationsBlockedDetails,
} from '../types'
import { EnvironmentBadge } from './EnvironmentBadge'
import styles from './EnvironmentComponents.module.css'

export function PublicationsBlockedRefusal({
  details,
  catalog,
}: {
  details: PublicationsBlockedDetails
  catalog?: EnvironmentCatalogView
}) {
  return (
    <MessageBar intent="error" role="alert">
      <MessageBarBody>
        <MessageBarTitle>Environment rules block this change</MessageBarTitle>
        <div className={styles.stack}>
          <Text>Classify these published pairs first.</Text>
          <div className={styles.tableWrap}>
            <Table size="small" aria-label="Blocked publications">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Gateway</TableHeaderCell>
                  <TableHeaderCell>Endpoint</TableHeaderCell>
                  <TableHeaderCell>Published</TableHeaderCell>
                  <TableHeaderCell>Reason</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {details.publications.map((publication) => {
                  const kind = publication.kind ?? 'model'
                  return (
                    <TableRow
                      key={`${publication.publicationId}:${publication.modelEndpointId ?? publication.mcpEndpointId ?? ''}`}
                    >
                      <TableCell>
                        {publication.gatewayName}{' '}
                        <EnvironmentBadge
                          environment={publication.gatewayEnvironment}
                          catalog={catalog}
                        />
                      </TableCell>
                      <TableCell>
                        {kind === 'mcp'
                          ? publication.mcpEndpointName ?? 'MCP server'
                          : publication.modelEndpointName ?? 'Endpoint'}{' '}
                        <EnvironmentBadge
                          environment={publication.endpointEnvironment}
                          catalog={catalog}
                        />
                      </TableCell>
                      <TableCell>
                        {kind === 'mcp'
                          ? publication.displayName ?? 'MCP server'
                          : kind === 'pool'
                            ? `Pool ${publication.displayName ?? ''}${publication.deploymentName ? ` (${publication.deploymentName})` : ''}`
                            : publication.deploymentName ?? '—'}
                      </TableCell>
                      <TableCell>{publication.verdict.reason}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        </div>
      </MessageBarBody>
    </MessageBar>
  )
}

export function GrantsAcknowledgmentRefusal({
  details,
  catalog,
}: {
  details: GrantsAcknowledgmentRequiredDetails
  catalog?: EnvironmentCatalogView
}) {
  const grants = details.grantCount === 1 ? '1 grant' : `${details.grantCount} grants`
  const principals =
    details.principalCount === 1 ? '1 principal' : `${details.principalCount} principals`
  return (
    <MessageBar intent="warning" role="alert">
      <MessageBarBody>
        <MessageBarTitle>Active grants will carry across environments</MessageBarTitle>
        <div className={styles.stack}>
          <Text>
            {grants} for {principals} {details.grantCount === 1 ? 'is' : 'are'} affected
            {details.truncated ? ', and this list is truncated' : ''}.
          </Text>
          <ul aria-label="Affected grants">
            {details.grants.map((grant) => (
              <li key={grant.entitlementId}>
                {grant.subjectLabel ?? 'Unknown principal'} —{' '}
                {grant.resourceName ??
                  `${GRANT_RESOURCE_LABELS[grant.resource.kind] ?? 'Resource'} on ${grant.movedResource.resourceName}`}
                : <EnvironmentBadge environment={grant.fromEnvironment} catalog={catalog} /> to{' '}
                <EnvironmentBadge environment={grant.toEnvironment} catalog={catalog} />
              </li>
            ))}
          </ul>
        </div>
      </MessageBarBody>
    </MessageBar>
  )
}

const GRANT_RESOURCE_LABELS: Record<string, string> = {
  modelApi: 'Model API',
  mcpServer: 'MCP server',
  poolModel: 'Pool model',
  product: 'Product',
  modelDeployment: 'Model deployment',
}
