import type { PublicationStatus, PublishedResource } from './types'

/** What a model or MCP publication records about its state in API Management. */
export interface PublicationRecord {
  status: PublicationStatus
  apiName: string
  resources: PublishedResource[]
  lastAppliedAt: string | null
  unpublishedAt?: string | null
}

export const PUBLICATION_STATUS_LABELS: Record<PublicationStatus, string> = {
  draft: 'Draft',
  planned: 'Planned',
  applying: 'Applying',
  published: 'Published',
  failed: 'Failed',
  rolledBack: 'Rolled back',
}

export function formatTimestamp(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : 'Never'
}

/**
 * Whether the publication's own API is in API Management, as MOSAIC recorded it. The portal offers
 * a model or MCP server MOSAIC publishes only then, and the API applies the same rule.
 */
export function holdsApi(publication: Pick<PublicationRecord, 'apiName' | 'resources'>): boolean {
  return publication.resources.some(
    (resource) =>
      resource.kind === 'api' && resource.name === publication.apiName && resource.createdByMosaic,
  )
}

/**
 * Whether an unpublish removed the publication and nothing has published it since. Publications
 * unpublished before MOSAIC recorded when are drafts an apply once published that own nothing now.
 */
export function isUnpublished(publication: PublicationRecord): boolean {
  return (
    Boolean(publication.unpublishedAt) ||
    (publication.status === 'draft' &&
      Boolean(publication.lastAppliedAt) &&
      !publication.resources.some((resource) => resource.createdByMosaic))
  )
}

/** The status to show. An unpublished draft says so rather than looking like one never published. */
export function publicationStatusLabel(publication: PublicationRecord): string {
  return publication.status === 'draft' && isUnpublished(publication)
    ? 'Unpublished'
    : PUBLICATION_STATUS_LABELS[publication.status]
}

/** "Last applied", where the last change applied to an unpublished publication was its removal. */
export function lastAppliedLabel(publication: PublicationRecord): string {
  if (isUnpublished(publication)) {
    return publication.unpublishedAt
      ? `Unpublished ${formatTimestamp(publication.unpublishedAt)}`
      : 'Unpublished'
  }
  return formatTimestamp(publication.lastAppliedAt)
}
