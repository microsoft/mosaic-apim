import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EnvironmentPicker } from './EnvironmentPicker'
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

describe('EnvironmentPicker', () => {
  it('shows the suggestion as the initial selection and hint', () => {
    render(
      <EnvironmentPicker
        catalog={catalog}
        suggestion={{ environment: 'production', evidence: 'Azure tag environment = "prod"' }}
      />,
    )
    expect(screen.getByRole('combobox', { name: 'Environment' })).toHaveValue('Production')
    expect(screen.getByText('Suggested: Production — Azure tag environment = "prod"')).toBeVisible()
  })
})
