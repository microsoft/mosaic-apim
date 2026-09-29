import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EnvironmentBadge } from './EnvironmentBadge'
import type { EnvironmentCatalogView } from '../types'

const catalog: EnvironmentCatalogView = {
  environments: [
    {
      key: 'production',
      displayName: 'Production',
      description: null,
      color: 'danger',
      production: true,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 10,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

describe('EnvironmentBadge', () => {
  it('renders unclassified, production, and unknown labels with text', () => {
    const { rerender } = render(<EnvironmentBadge environment={null} catalog={catalog} />)
    expect(screen.getByText('Unclassified')).toBeVisible()
    rerender(<EnvironmentBadge environment="production" catalog={catalog} />)
    expect(screen.getByText('Production')).toBeVisible()
    expect(screen.getByLabelText('Production-class')).toBeInTheDocument()
    rerender(<EnvironmentBadge environment="mystery" catalog={catalog} />)
    expect(screen.getByText('mystery')).toBeVisible()
  })
})
