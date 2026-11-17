import { Button, Link, MessageBar, MessageBarBody, MessageBarTitle, Text } from '@fluentui/react-components'
import { policyCapabilityFailure, policyDocumentationUrl, policyFieldLabel } from '../policy-capabilities'
import styles from './PolicyCapabilityFeedback.module.css'

interface Props {
  error: unknown
  title: string
  reviewFields?: Readonly<Record<string, HTMLElement>>
  onViewGateway?: (gatewayId: string) => void
}

/** Content for the existing outcome summary; its owner handles focus and announcements. */
export function PolicyCapabilityFeedback({ error, title, reviewFields, onViewGateway }: Props) {
  const failure = policyCapabilityFailure(error)
  const message = error instanceof Error ? error.message : 'An unexpected error occurred.'
  return <>
    <MessageBarTitle>{title}</MessageBarTitle>
    {!failure ? message : <>
      <Text block>
        {failure.boundary === 'save' ? 'The change was not saved.'
          : failure.boundary === 'plan' ? 'No applicable plan was produced.' : 'The plan was not applied.'}
        {' '}Previously applied gateway access is unchanged.
      </Text>
      <ul className={styles.issues}>
        {failure.issues.map((issue, index) => {
          const field = reviewFields?.[issue.field]
          const gatewayId = issue.gatewayId
          const scope = issue.field.startsWith('publication.') ? 'Publication safeguard'
            : issue.field.startsWith('pool.safeguard.') ? 'Pool safeguard'
              : issue.field.startsWith('costCenter.limits.pool.') ? 'Cost-center pooled quota'
                : issue.field.startsWith('costCenter.limits.person.') ? 'Cost-center per-person default'
                  : null
          return <li key={`${issue.gatewayId}:${issue.field}:${issue.policy}:${index}`}>
            <Text weight="semibold">{scope && `${scope}: `}{policyFieldLabel(issue)}</Text>
            <Text className={styles.state}>
              {issue.reason === 'invalidPolicyValue' ? 'Invalid value'
                : issue.support === 'unknown' ? 'Support not verified' : 'Not supported'}
              {issue.skuName && ` · ${issue.skuName}`}
            </Text>
            <Text block>{issue.message}</Text>
            {(scope === 'Publication safeguard' || scope === 'Pool safeguard')
              && <Text size={200}>This shared safeguard remains even when a grant-specific limit is removed.</Text>}
            {issue.field.startsWith('costCenter.') && <Text size={200}>This is an inherited cost-center restriction. Clearing an explicit grant limit does not remove it.</Text>}
            <div className={styles.actions}>
              {field && <Button appearance="transparent" size="small" onClick={() => {
                field.focus()
                field.scrollIntoView({ block: 'center' })
              }}>Review {policyFieldLabel(issue).toLowerCase()}</Button>}
              {gatewayId && (onViewGateway
                ? <Button appearance="transparent" size="small" onClick={() => onViewGateway(gatewayId)}>View gateway</Button>
                : <Link href={`/gateways/${encodeURIComponent(gatewayId)}`} target="_blank"
                  rel="noopener noreferrer" aria-label="View gateway in a new tab">View gateway</Link>)}
              <Link href={policyDocumentationUrl(issue.policy)} target="_blank" rel="noopener noreferrer">Policy reference</Link>
            </div>
          </li>
        })}
      </ul>
      <Text size={200}>Your draft is retained. Review the named limit or its resource; MOSAIC does not remove or weaken enforcement automatically.</Text>
    </>}
  </>
}

export function PolicyCapabilityError({
  announcementKey, ...props
}: Props & { announcementKey?: string | number }) {
  return <MessageBar key={announcementKey} intent="error" layout="multiline">
    <MessageBarBody><PolicyCapabilityFeedback {...props} /></MessageBarBody>
  </MessageBar>
}
