import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EnvironmentBadge } from './EnvironmentBadge'
import type { PortalEnvironment } from '../types'

const environments: PortalEnvironment[] = [
  { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, order: 50 },
  { key: 'development', displayName: 'Development', description: null, color: 'brand', production: false, order: 10 },
]

describe('EnvironmentBadge', () => {
  it('renders a known environment label', () => {
    render(<EnvironmentBadge environment="development" environments={environments} />)

    expect(screen.getByText('Development')).toBeVisible()
  })

  it('renders production-class labels with text visible', () => {
    render(<EnvironmentBadge environment="production" environments={environments} />)

    expect(screen.getByText('Production')).toBeVisible()
  })

  it('renders unclassified without a catalog definition', () => {
    render(<EnvironmentBadge environment={null} environments={environments} />)

    expect(screen.getByText('Unclassified')).toBeVisible()
  })

  it('renders an unknown key with the administrator tooltip text', async () => {
    render(<EnvironmentBadge environment="experimental" environments={environments} />)

    const badge = screen.getByText('experimental')
    expect(badge).toBeVisible()
    expect(badge).toHaveAttribute('title', 'Not defined by your administrator')
    expect(badge.closest('[aria-describedby]')).not.toBeNull()
  })
})
