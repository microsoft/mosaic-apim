import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { useWorkbenchLocation } from './useWorkbenchLocation'

function Example() {
  const workbench = useWorkbenchLocation()
  const location = useLocation()
  const navigate = useNavigate()
  const [, setParams] = useSearchParams()
  return <>
    <output data-testid="url">{location.search}</output>
    <output data-testid="selected">{workbench.selectedId ?? 'none'}</output>
    <output data-testid="mode">{workbench.mode ?? 'list'}</output>
    <button onClick={() => workbench.select('record')}>Select</button>
    <button onClick={() => workbench.open('edit')}>Edit</button>
    <button onClick={() => workbench.open('new')}>New</button>
    <button onClick={() => workbench.close('saved')}>Save</button>
    <button onClick={() => workbench.close()}>Cancel</button>
    <button onClick={() => navigate(-1)}>History back</button>
    <button onClick={() => navigate(`/mcps${location.search}`)}>Change resource domain</button>
    <button onClick={() => setParams((current) => { const next = new URLSearchParams(current); next.set('tab', 'imported'); return next })}>Change section</button>
  </>
}

function setup(url = '/models?tab=published&gateway=gateway-1&q=azure') {
  const router = createMemoryRouter([{ path: '*', element: <Example /> }], { initialEntries: [url] })
  render(<RouterProvider router={router} />)
  return router
}

describe('useWorkbenchLocation', () => {
  it('preserves tabs and filters across selection, editing and save', async () => {
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByRole('button', { name: 'Select' }))
    await user.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByTestId('mode')).toHaveTextContent('edit')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByTestId('selected')).toHaveTextContent('saved')
    const params = new URLSearchParams(screen.getByTestId('url').textContent!)
    expect(params.get('tab')).toBe('published')
    expect(params.get('gateway')).toBe('gateway-1')
    expect(params.get('q')).toBe('azure')
    expect(params.has('mode')).toBe(false)
  })

  it('supports direct links and browser back/forward', async () => {
    const user = userEvent.setup()
    const router = setup('/models?tab=published&selected=record&mode=review')
    expect(screen.getByTestId('selected')).toHaveTextContent('record')
    expect(screen.getByTestId('mode')).toHaveTextContent('review')
    await user.click(screen.getByRole('button', { name: 'Edit' }))
    await user.click(screen.getByRole('button', { name: 'History back' }))
    expect(screen.getByTestId('mode')).toHaveTextContent('review')
    await act(() => router.navigate(1))
    expect(screen.getByTestId('mode')).toHaveTextContent('edit')
  })

  it('drops stale section selections without dropping filters', async () => {
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByRole('button', { name: 'Select' }))
    await user.click(screen.getByRole('button', { name: 'Edit' }))
    await user.click(screen.getByRole('button', { name: 'Change section' }))
    await waitFor(() => expect(screen.getByTestId('selected')).toHaveTextContent('none'))
    expect(screen.getByTestId('mode')).toHaveTextContent('list')
    expect(new URLSearchParams(screen.getByTestId('url').textContent!).get('gateway')).toBe('gateway-1')
    expect(new URLSearchParams(screen.getByTestId('url').textContent!).has('selected')).toBe(false)
  })

  it('rejects invalid modes and cancels without removing selection', async () => {
    const user = userEvent.setup()
    setup('/models?tab=published&selected=record&mode=secret')
    await waitFor(() => expect(screen.getByTestId('url')).not.toHaveTextContent('mode='))
    await user.click(screen.getByRole('button', { name: 'New' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByTestId('selected')).toHaveTextContent('record')
    expect(screen.getByTestId('mode')).toHaveTextContent('list')
  })

  it('scopes direct-link selections before a section changes', async () => {
    const user = userEvent.setup()
    setup('/models?tab=published&selected=record')
    await user.click(screen.getByRole('button', { name: 'Change section' }))
    await waitFor(() => expect(screen.getByTestId('selected')).toHaveTextContent('none'))
  })

  it('does not reuse a selection across resource domains with the same tab', async () => {
    const user = userEvent.setup()
    const router = setup()
    await user.click(screen.getByRole('button', { name: 'Select' }))
    await user.click(screen.getByRole('button', { name: 'Change resource domain' }))
    await waitFor(() => expect(screen.getByTestId('selected')).toHaveTextContent('none'))
    expect(router.state.location.pathname).toBe('/mcps')
    expect(new URLSearchParams(router.state.location.search).get('tab')).toBe('published')
  })
})
