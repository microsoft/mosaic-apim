import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ResourceWorkbench } from './ResourceWorkbench'

const items = [{ id: 'one', name: 'First model' }, { id: 'two', name: 'Second model' }]

function Example({ initial = null }: { initial?: string | null }) {
  const [selected, setSelected] = useState<string | null>(initial)
  return <FluentProvider theme={webLightTheme}>
    <ResourceWorkbench title="Published models" description="Saved publications."
      items={items} getId={(item) => item.id} selectedId={selected} onSelect={setSelected}
      renderListItem={(item) => <strong>{item.name}</strong>}
      renderDetail={(item) => <h3>{item.name} details</h3>}
      pager={<button>Next page</button>} />
  </FluentProvider>
}

describe('ResourceWorkbench', () => {
  it('offers native keyboard selection, announced state and focused details', async () => {
    const user = userEvent.setup()
    render(<Example />)
    expect(screen.getByRole('heading', { name: 'Published models' })).toBeVisible()
    expect(screen.getByRole('list', { name: 'Resources' })).toBeVisible()
    expect(screen.getByText('Select a resource to see its details.')).toBeVisible()
    const row = screen.getByRole('button', { name: 'First model' })
    row.focus()
    await user.keyboard('{Enter}')
    expect(row).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('heading', { name: 'First model details' })).toBeVisible()
    expect(screen.getByRole('region', { name: 'Selected resource' })).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Next page' })).toBeVisible()
  })

  it('returns focus to the selected row when going back to the list', async () => {
    const user = userEvent.setup()
    render(<Example initial="two" />)
    await user.click(screen.getByText('Back to list'))
    expect(screen.getByRole('button', { name: 'Second model' })).toHaveFocus()
    expect(screen.getByText('Select a resource to see its details.')).toBeVisible()
  })

  it('shows a recoverable missing selection and returns focus to the list', async () => {
    const user = userEvent.setup()
    render(<Example initial="removed" />)
    expect(screen.getByText('This resource is no longer available.')).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'First model details' })).not.toBeInTheDocument()
    await user.click(screen.getAllByText('Back to list')[1])
    expect(screen.getByRole('region', { name: 'Resources' })).toHaveFocus()
  })

  it('uses the explicit empty list state', () => {
    render(<ResourceWorkbench title="Models" items={[]} getId={(item: { id: string }) => item.id}
      selectedId={null} onSelect={() => undefined} renderListItem={() => null}
      renderDetail={() => null} emptyList="No published models yet." />)
    expect(screen.getByText('No published models yet.')).toBeVisible()
  })

  it('preserves page and list scrolling when a workspace is replaced and remounted', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'scrollY')
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const view = <MemoryRouter initialEntries={['/scroll-preservation?tab=published']}><Example /></MemoryRouter>
    try {
      Object.defineProperty(window, 'scrollY', { configurable: true, value: 320 })
      const first = render(view)
      screen.getByRole('region', { name: 'Resources' }).scrollTop = 88
      first.unmount()
      Object.defineProperty(window, 'scrollY', { configurable: true, value: 0 })
      render(view)
      expect(scrollTo).toHaveBeenLastCalledWith({ left: 0, top: 320 })
      expect(screen.getByRole('region', { name: 'Resources' }).scrollTop).toBe(88)
    } finally {
      if (original) Object.defineProperty(window, 'scrollY', original)
      else Reflect.deleteProperty(window, 'scrollY')
      scrollTo.mockRestore()
    }
  })
})
