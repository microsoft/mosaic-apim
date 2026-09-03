import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PolicyDocumentCard, PolicyFragmentCard } from './PolicyFacets'
import type { ObservedPolicyDocument, ObservedPolicyFragment, PolicyFacet } from '../types'

function facet(overrides: Partial<PolicyFacet> = {}): PolicyFacet {
  return {
    kind: 'tokenLimit',
    element: 'llm-token-limit',
    section: 'inbound',
    summary: 'Limits model usage to 10,000 tokens per minute, counted per subscription.',
    details: ['Prompt tokens are estimated before the request reaches the model.'],
    attributes: { 'tokens-per-minute': '10000' },
    confidence: 'recognized',
    managedByMosaic: false,
    ...overrides,
  }
}

const document: ObservedPolicyDocument = {
  id: 'obsPolicy_1',
  scope: 'api',
  scopeId: 'chat-api',
  scopeLabel: 'API: Chat completions',
  contentSha256: 'abc123',
  elementCount: 2,
  facets: [
    facet(),
    facet({
      kind: 'unrecognized',
      element: 'acme-custom-guard',
      summary:
        'MOSAIC does not interpret the acme-custom-guard rule; it was authored outside MOSAIC.',
      details: [],
      attributes: {},
      confidence: 'unrecognized',
    }),
  ],
  unrecognizedElements: ['acme-custom-guard'],
}

describe('policy rendering', () => {
  it('describes rules in plain language', () => {
    render(<PolicyDocumentCard document={document} />)

    expect(screen.getByRole('heading', { name: 'API: Chat completions' })).toBeVisible()
    expect(
      screen.getByText(/Limits model usage to 10,000 tokens per minute, counted per subscription/),
    ).toBeVisible()
    expect(screen.getByText('Token limit')).toBeVisible()
  })

  it('never renders policy markup', () => {
    const { container } = render(<PolicyDocumentCard document={document} />)

    expect(container.textContent).not.toContain('<')
    expect(container.textContent).not.toContain('policies>')
    expect(container.textContent).not.toContain('counter-key')
  })

  it('labels rules it cannot interpret instead of hiding them', () => {
    render(<PolicyDocumentCard document={document} />)

    expect(screen.getByText('Externally authored')).toBeVisible()
    expect(screen.getByText(/authored outside MOSAIC: acme-custom-guard/)).toBeVisible()
  })

  it('marks MOSAIC-authored rule sets', () => {
    const fragment: ObservedPolicyFragment = {
      id: 'obsFragment_1',
      name: 'mosaic-rate-standard',
      description: 'Standard MOSAIC rate limit',
      contentSha256: 'def456',
      managedByMosaic: true,
      facets: [facet({ managedByMosaic: true })],
      unrecognizedElements: [],
    }

    render(<PolicyFragmentCard fragment={fragment} />)

    expect(screen.getAllByText('MOSAIC managed').length).toBeGreaterThan(0)
  })

  it('is explicit when a policy exists but yielded no readable rules', () => {
    render(
      <PolicyDocumentCard
        document={{ ...document, facets: [], unrecognizedElements: [] }}
      />,
    )

    expect(
      screen.getByText('This scope has a policy document with no rules MOSAIC could read.'),
    ).toBeVisible()
  })
})
