import { Badge, Card, Text, Title3 } from '@fluentui/react-components'
import type {
  ObservedPolicyDocument,
  ObservedPolicyFragment,
  PolicyFacet,
  PolicyFacetKind,
} from '../types'
import styles from './PolicyFacets.module.css'

const KIND_LABELS: Record<PolicyFacetKind, string> = {
  rateLimit: 'Rate limit',
  tokenLimit: 'Token limit',
  quota: 'Quota',
  authentication: 'Backend authentication',
  authorization: 'Caller authorization',
  routing: 'Routing',
  caching: 'Caching',
  contentSafety: 'Content safety',
  transformation: 'Request shaping',
  observability: 'Telemetry',
  network: 'Network',
  fragmentInclude: 'Shared rule set',
  unrecognized: 'Not interpreted',
}

const SECTION_LABELS: Record<string, string> = {
  inbound: 'On the way in',
  backend: 'At the backend',
  outbound: 'On the way out',
  onError: 'On error',
  unknown: '',
}

function facetLabel(kind: PolicyFacetKind): string {
  return KIND_LABELS[kind] ?? 'Policy'
}

export function PolicyFacetItem({ facet }: { facet: PolicyFacet }) {
  const section = SECTION_LABELS[facet.section] ?? ''
  return (
    <li className={styles.facet}>
      <div className={styles.facetHeader}>
        <Badge
          appearance="tint"
          className={
            facet.confidence === 'unrecognized' ? styles.externalBadge : styles.kindBadge
          }
        >
          {facetLabel(facet.kind)}
        </Badge>
        {facet.managedByMosaic && (
          <Badge appearance="tint" className={styles.managedBadge}>
            MOSAIC managed
          </Badge>
        )}
        {facet.confidence === 'unrecognized' && (
          <Badge appearance="outline" className={styles.externalBadge}>
            Externally authored
          </Badge>
        )}
        {section && <Text size={200}>{section}</Text>}
      </div>
      <Text block>{facet.summary}</Text>
      {facet.details.map((detail) => (
        <Text key={detail} block size={200} className={styles.facetDetail}>
          {detail}
        </Text>
      ))}
    </li>
  )
}

export function PolicyDocumentCard({ document }: { document: ObservedPolicyDocument }) {
  return (
    <Card className={styles.policyCard}>
      <Title3 as="h3">{document.scopeLabel}</Title3>
      {document.facets.length === 0 ? (
        <Text>This scope has a policy document with no rules MOSAIC could read.</Text>
      ) : (
        <ul className={styles.facetList}>
          {document.facets.map((facet, index) => (
            <PolicyFacetItem key={`${document.id}-${facet.element}-${index}`} facet={facet} />
          ))}
        </ul>
      )}
      {document.unrecognizedElements.length > 0 && (
        <Text size={200} className={styles.unrecognizedNote}>
          {document.unrecognizedElements.length} rule
          {document.unrecognizedElements.length === 1 ? '' : 's'} in this scope
          {document.unrecognizedElements.length === 1 ? ' is' : ' are'} authored outside MOSAIC:{' '}
          {document.unrecognizedElements.join(', ')}.
        </Text>
      )}
    </Card>
  )
}

export function PolicyFragmentCard({ fragment }: { fragment: ObservedPolicyFragment }) {
  return (
    <Card className={styles.policyCard}>
      <Title3 as="h3">
        {fragment.name}{' '}
        {fragment.managedByMosaic && (
          <Badge appearance="tint" className={styles.managedBadge}>
            MOSAIC managed
          </Badge>
        )}
      </Title3>
      {fragment.description && <Text block>{fragment.description}</Text>}
      <ul className={styles.facetList}>
        {fragment.facets.map((facet, index) => (
          <PolicyFacetItem key={`${fragment.id}-${facet.element}-${index}`} facet={facet} />
        ))}
      </ul>
    </Card>
  )
}
