import { describe, expect, it } from 'vitest'
import { holdsApi, isUnpublished, lastAppliedLabel, publicationStatusLabel } from './publication-state'
import type { PublishedResource } from './types'

const appliedAt = '2026-09-01T12:30:00Z'
const unpublishedAt = '2026-09-30T11:20:00Z'

function resource(kind: PublishedResource['kind'], name: string, createdByMosaic = true): PublishedResource {
  return { kind, name, resourceId: `/${kind}/${name}`, createdByMosaic, appliedAt }
}

const live = {
  status: 'published' as const,
  apiName: 'chat',
  resources: [resource('backend', 'chat'), resource('api', 'chat')],
  lastAppliedAt: appliedAt,
}

describe('publication state', () => {
  it('holds its API only when MOSAIC created the API the publication names', () => {
    expect(holdsApi(live)).toBe(true)
    expect(holdsApi({ ...live, resources: [resource('backend', 'chat')] })).toBe(false)
    expect(holdsApi({ ...live, resources: [resource('api', 'chat-prm')] })).toBe(false)
    expect(holdsApi({ ...live, resources: [resource('api', 'chat', false)] })).toBe(false)
  })

  it('names an unpublished publication, and when, instead of a draft with an old apply time', () => {
    const unpublished = { ...live, status: 'draft' as const, resources: [], unpublishedAt }

    expect(isUnpublished(unpublished)).toBe(true)
    expect(publicationStatusLabel(unpublished)).toBe('Unpublished')
    expect(lastAppliedLabel(unpublished)).toBe(`Unpublished ${new Date(unpublishedAt).toLocaleString()}`)
  })

  it('recognises a publication unpublished before MOSAIC recorded when', () => {
    const older = { ...live, status: 'draft' as const, resources: [resource('backend', 'kept', false)] }

    expect(isUnpublished(older)).toBe(true)
    expect(publicationStatusLabel(older)).toBe('Unpublished')
    expect(lastAppliedLabel(older)).toBe('Unpublished')
  })

  it('keeps the status of a publication being published again, but not its old apply time', () => {
    const replanned = { ...live, status: 'planned' as const, resources: [], unpublishedAt }

    expect(publicationStatusLabel(replanned)).toBe('Planned')
    expect(lastAppliedLabel(replanned)).toBe(`Unpublished ${new Date(unpublishedAt).toLocaleString()}`)
  })

  it('leaves drafts never applied and live publications as they were', () => {
    const draft = { ...live, status: 'draft' as const, resources: [], lastAppliedAt: null }

    expect(isUnpublished(draft)).toBe(false)
    expect(publicationStatusLabel(draft)).toBe('Draft')
    expect(lastAppliedLabel(draft)).toBe('Never')
    expect(isUnpublished(live)).toBe(false)
    expect(publicationStatusLabel(live)).toBe('Published')
    expect(lastAppliedLabel(live)).toBe(new Date(appliedAt).toLocaleString())
  })
})
