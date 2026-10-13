import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { PageSearch } from './PageSearch'

function Page() {
  const location = useLocation()
  return <><PageSearch destinations={[
    { label: 'Models', to: '/models' },
    { label: 'Models · Model endpoints', to: '/models?tab=endpoints' },
  ]} /><output>{location.pathname}{location.search}</output></>
}
describe('PageSearch', () => {
  it('shows section matches and submits the matching route', () => {
    render(<MemoryRouter><Page /></MemoryRouter>)
    fireEvent.change(screen.getByRole('textbox', { name: 'Search MOSAIC pages' }), { target: { value: 'endpoint' } })
    expect(screen.getByRole('link', { name: 'Models · Model endpoints' })).toBeVisible()
    fireEvent.submit(screen.getByRole('search'))
    expect(screen.getByRole('status')).toHaveTextContent('/models?tab=endpoints')
  })
  it('reports no match without redirecting, and supports Escape', () => {
    render(<MemoryRouter><Page /></MemoryRouter>)
    const input = screen.getByRole('textbox', { name: 'Search MOSAIC pages' })
    fireEvent.change(input, { target: { value: 'missing' } })
    expect(screen.getByText('No pages or sections match “missing”.')).toBeVisible()
    fireEvent.submit(screen.getByRole('search'))
    expect(document.querySelector('output')).toHaveTextContent('/')
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByText('No pages or sections match “missing”.')).not.toBeInTheDocument()
  })
})
