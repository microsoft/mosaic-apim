import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'
import { SectionLayout, usePageSection } from './SectionLayout'

const sections = [{ value: 'published', label: 'Published models' }, { value: 'endpoints', label: 'Model endpoints' }]
function Page() {
  const [active, select] = usePageSection(sections)
  const location = useLocation()
  return <><SectionLayout label="Test models" sections={sections} active={active} onSelect={select}>
    <h2>{active === 'published' ? 'Published content' : 'Endpoint content'}</h2>
  </SectionLayout><output>{location.search}</output></>
}
beforeEach(() => localStorage.removeItem('mosaic.sections.Test models'))
describe('SectionLayout', () => {
  it('switches actual views and keeps unrelated route filters', () => {
    render(<MemoryRouter initialEntries={['/?environment=production']}><Page /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Model endpoints' }))
    expect(screen.getByRole('heading', { name: 'Endpoint content' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Published content' })).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('?environment=production&tab=endpoints')
  })
  it('handles unknown sections and persists collapse independently', () => {
    render(<MemoryRouter initialEntries={['/?tab=missing']}><Page /></MemoryRouter>)
    expect(screen.getByRole('heading', { name: 'Published content' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse Test models sections' }))
    expect(localStorage.getItem('mosaic.sections.Test models')).toBe('collapsed')
    expect(screen.getByRole('button', { name: 'Model endpoints' })).toHaveAttribute('title', 'Model endpoints')
    expect(screen.getByRole('button', { name: 'Expand Test models sections' })).toHaveAttribute('aria-expanded', 'false')
  })
  it('supports compact section selection', () => {
    render(<MemoryRouter><Page /></MemoryRouter>)
    fireEvent.change(screen.getByRole('combobox', { name: 'Test models section' }), { target: { value: 'endpoints' } })
    expect(screen.getByRole('heading', { name: 'Endpoint content' })).toBeVisible()
  })
})
