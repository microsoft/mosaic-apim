import {
  MessageBar,
  MessageBarBody,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Title3,
} from '@fluentui/react-components'
import { describeAccessMethods, describeLimits, describePublicationLimits } from '../entitlement-limits'
import type { PublishPlan } from '../types'
import styles from './ImportFromGatewayDialog.module.css'

export function ModelAccessReview({ plan }: { plan: PublishPlan }) {
  const snapshot = plan.accessSnapshot
  if (!snapshot) return null

  return (
    <section className={styles.nameCell} aria-label="Model-wide access review">
      <Title3 as="h3">Model-wide access changes</Title3>
      <Text>
        This plan applies the complete target below, including every pending grant and method
        change for this model, not just one selected grant.
      </Text>
      {plan.previousAccessVersion == null && (
        <MessageBar intent="warning">
          <MessageBarBody>
            Applying governed access retires this model&apos;s generic bootstrap subscription.
            Existing clients must use a dedicated grant key or an authorized model-runtime Entra token.
          </MessageBarBody>
        </MessageBar>
      )}
      <Text weight="semibold">Target methods: {describeAccessMethods(snapshot.settings)}</Text>
      <Text>Keys: {snapshot.settings.keysEnabled ? 'enabled' : 'disabled'} · Entra: {snapshot.settings.entraEnabled ? 'enabled' : 'disabled'}</Text>
      <Text size={200}>
        Both methods use the same grant and limits. When both credentials are sent, they must
        resolve to the same grant; invalid or disabled credentials are not ignored.
      </Text>
      {snapshot.audience && <Text size={200}>Model-runtime audience: {snapshot.audience}</Text>}
      <Text size={200}>Access version: {plan.previousAccessVersion ?? 'legacy'} → {snapshot.version}</Text>
      <Text weight="semibold">Inherited publication limits</Text>
      {describePublicationLimits(snapshot.publicationEnforcement).map((limit) => <Text key={limit}>{limit}</Text>)}
      <Text size={200}>These safeguards apply in addition to each grant&apos;s limits.</Text>
      {snapshot.grants.length === 0 ? (
        <Text>No direct grants are in this target. No caller will be authorized.</Text>
      ) : (
        <div className={styles.tableScroll}>
          <Table size="small" aria-label="All target model grants">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Subject</TableHeaderCell>
                <TableHeaderCell>Target access</TableHeaderCell>
                <TableHeaderCell>Subscription</TableHeaderCell>
                <TableHeaderCell>Grant limits</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {snapshot.grants.map((grant) => (
                <TableRow key={grant.entitlementId}>
                  <TableCell>
                    <Text block weight="semibold">{grant.displayName}</Text>
                    <Text block size={200}>{grant.subject.kind} · {grant.objectId}</Text>
                    <Text block size={200}>Grant: {grant.entitlementId}</Text>
                  </TableCell>
                  <TableCell>{grant.enabled ? 'Enabled' : 'Disabled — revoke both methods'}</TableCell>
                  <TableCell>{grant.subscriptionName}</TableCell>
                  <TableCell>
                    {describeLimits(grant).map((limit) => <Text block key={limit}>{limit}</Text>)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </section>
  )
}
