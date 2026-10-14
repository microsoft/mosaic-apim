import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { type ReactNode } from 'react'
import { createMemoryRouter, Link, MemoryRouter, Route, RouterProvider, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { WorkspaceDialog } from './WorkspaceDialog'
import { SectionLayout } from './SectionLayout'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'

function Fluent({ children }: { children: ReactNode }) {
  return <FluentProvider theme={webLightTheme}>{children}</FluentProvider>
}

describe('WorkspaceEditorFrame and leave guard', () => {
  it('guards controlled close in legacy MemoryRouter and restores focus after Stay', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<Fluent><MemoryRouter><WorkspaceEditorFrame title="Edit model" dirty onBack={onBack}>
      <input aria-label="Name" defaultValue="Model" />
    </WorkspaceEditorFrame></MemoryRouter></Fluent>)
    expect(screen.getByRole('heading', { name: 'Edit model' })).toHaveFocus()
    const back = screen.getByRole('button', { name: 'Back' })
    await user.click(back)
    expect(screen.getByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    await waitFor(() => expect(back).toHaveFocus())
    expect(onBack).not.toHaveBeenCalled()
    await user.click(back)
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('guards data-router links and history; discard resumes the exact navigation', async () => {
    const user = userEvent.setup()
    const router = createMemoryRouter([
      { path: '/edit', element: <Fluent><WorkspaceEditorFrame title="Edit" dirty onBack={() => undefined}>
        <Link to="/list?tab=published">Go to list</Link>
      </WorkspaceEditorFrame></Fluent> },
      { path: '/list', element: <p>Resource list</p> },
    ], { initialEntries: ['/list?tab=imported', '/edit'], initialIndex: 1 })
    render(<RouterProvider router={router} />)
    await user.click(screen.getByRole('link', { name: 'Go to list' }))
    expect(router.state.location.pathname).toBe('/edit')
    await user.click(await screen.findByRole('button', { name: 'Stay' }))
    await act(() => router.navigate(-1))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByText('Resource list')).toBeVisible())
    expect(router.state.location.search).toBe('?tab=imported')
  })

  it('does not double-confirm a controlled close that navigates', async () => {
    const user = userEvent.setup()
    const router = createMemoryRouter([
      { path: '/edit', element: <Fluent><WorkspaceEditorFrame title="Edit" dirty
        onBack={() => { void router.navigate('/list') }}>Form</WorkspaceEditorFrame></Fluent> },
      { path: '/list', element: <p>Resource list</p> },
    ], { initialEntries: ['/edit'] })
    render(<RouterProvider router={router} />)
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByText('Resource list')).toBeVisible())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('restores the draft field after cancelling sidebar navigation', async () => {
    const user = userEvent.setup()
    const router = createMemoryRouter([
      { path: '/edit', element: <Fluent>
        <nav><Link to="/list">Other section</Link></nav>
        <WorkspaceEditorFrame title="Edit" dirty onBack={() => undefined}>
          <input aria-label="Draft name" defaultValue="Model" />
        </WorkspaceEditorFrame>
      </Fluent> },
      { path: '/list', element: <p>Resource list</p> },
    ], { initialEntries: ['/edit'] })
    render(<RouterProvider router={router} />)
    const input = screen.getByRole('textbox', { name: 'Draft name' })
    await user.click(input)
    await user.click(screen.getByRole('link', { name: 'Other section' }))
    await user.click(await screen.findByRole('button', { name: 'Stay' }))
    await waitFor(() => expect(input).toHaveFocus())
    expect(router.state.location.pathname).toBe('/edit')
    expect(input).toHaveValue('Model')
  })

  it('prefers the last draft field over the Back button after Stay', async () => {
    const user = userEvent.setup()
    render(<Fluent><MemoryRouter><WorkspaceEditorFrame title="Edit" dirty onBack={() => undefined}>
      <input aria-label="Draft field" />
    </WorkspaceEditorFrame></MemoryRouter></Fluent>)
    const input = screen.getByRole('textbox', { name: 'Draft field' })
    await user.type(input, 'Unsaved name')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    await waitFor(() => expect(input).toHaveFocus())
    expect(input).toHaveValue('Unsaved name')
  })

  it('keeps the destination accessible after Stay then Discard unmounts the guarded frame', async () => {
    const user = userEvent.setup()
    const router = createMemoryRouter([
      { path: '/edit', element: <>
        <nav><Link to="/list">Other section</Link></nav>
        <WorkspaceEditorFrame title="Edit" dirty onBack={() => undefined}>
          <input aria-label="Draft name" />
        </WorkspaceEditorFrame>
      </> },
      { path: '/list', element: <h2>Destination workspace</h2> },
    ], { initialEntries: ['/edit'] })
    const { container } = render(<Fluent><RouterProvider router={router} /></Fluent>)
    await user.type(screen.getByRole('textbox', { name: 'Draft name' }), 'Unsaved')
    await user.click(screen.getByRole('link', { name: 'Other section' }))
    await user.click(await screen.findByRole('button', { name: 'Stay' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument())
    await user.click(screen.getByRole('link', { name: 'Other section' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('heading', { name: 'Destination workspace' })).toBeVisible()
    expect(container).not.toHaveAttribute('aria-hidden', 'true')
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
  })

  it('allows completed save navigation while mutation callbacks are still pending', async () => {
    const user = userEvent.setup()
    const router = createMemoryRouter([
      { path: '/edit', element: <Fluent><WorkspaceEditorFrame title="Edit" dirty pending
        onBack={() => undefined} actions={<button onClick={() => completeWorkspaceNavigation(() => router.navigate('/list?selected=saved'))}>
          Complete successful save
        </button>}>Form</WorkspaceEditorFrame></Fluent> },
      { path: '/list', element: <p>Saved resource list</p> },
    ], { initialEntries: ['/edit'] })
    render(<RouterProvider router={router} />)
    await user.click(screen.getByRole('button', { name: 'Complete successful save' }))
    await waitFor(() => expect(screen.getByText('Saved resource list')).toBeVisible())
    expect(router.state.location.search).toBe('?selected=saved')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('supports descendant Routes under a catchall data router without inactive blockers taking over', async () => {
    const user = userEvent.setup()
    const router = createMemoryRouter([{ path: '*', element: <Fluent><Routes>
      <Route path="/edit" element={<>
        <WorkspaceEditorFrame title="Dirty workspace" dirty onBack={() => undefined}>Draft</WorkspaceEditorFrame>
        <WorkspaceEditorFrame title="Clean workspace" onBack={() => undefined}>Read-only</WorkspaceEditorFrame>
        <Link to="/list">Navigate</Link>
      </>} />
      <Route path="/list" element={<p>Resource list</p>} />
    </Routes></Fluent> }], { initialEntries: ['/edit'] })
    render(<RouterProvider router={router} />)
    await user.click(screen.getByRole('link', { name: 'Navigate' }))
    expect(router.state.location.pathname).toBe('/edit')
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByText('Resource list')).toBeVisible())
  })

  it('blocks reload while dirty and removes the beforeunload handler when clean', () => {
    const { rerender } = render(<Fluent><WorkspaceEditorFrame title="Edit" dirty onBack={() => undefined}>Form</WorkspaceEditorFrame></Fluent>)
    const first = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(first)
    expect(first.defaultPrevented).toBe(true)
    rerender(<Fluent><WorkspaceEditorFrame title="Edit" onBack={() => undefined}>Form</WorkspaceEditorFrame></Fluent>)
    const second = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(second)
    expect(second.defaultPrevented).toBe(false)
  })

  it('does not allow leaving an active mutation', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<Fluent><WorkspaceEditorFrame title="Publishing" pending onBack={onBack}
      primaryAction={{ label: 'Apply plan', onClick: vi.fn() }}>Plan</WorkspaceEditorFrame></Fluent>)
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('dialog', { name: 'Operation in progress' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Discard changes' })).not.toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
  })

  it('dismisses a pending-operation notice when the operation finishes cleanly', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    const view = (pending: boolean) => <Fluent><WorkspaceEditorFrame title="Applying" pending={pending}
      onBack={onBack}>Plan</WorkspaceEditorFrame></Fluent>
    const { rerender } = render(view(true))
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('dialog', { name: 'Operation in progress' })).toBeVisible()
    rerender(view(false))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(onBack).not.toHaveBeenCalled()
  })

  it('allows unguarded clean close without requiring any router', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(<Fluent><WorkspaceEditorFrame title="New model" onBack={onBack}>Form</WorkspaceEditorFrame></Fluent>)
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('focuses error summaries', async () => {
    render(<Fluent><WorkspaceEditorFrame title="Edit" onBack={() => undefined}
      validationSummary="Choose a gateway.">Form</WorkspaceEditorFrame></Fluent>)
    expect(screen.getByText('Choose a gateway.').closest('[tabindex]')).toHaveFocus()
  })
})

describe('WorkspaceDialog takeover', () => {
  it('replaces section content in flow, leaves section navigation and restores content', async () => {
    const onOpenChange = vi.fn()
    const view = (open: boolean) => <Fluent><MemoryRouter><section>
      <SectionLayout label="Models" sections={[{ value: 'published', label: 'Published' }]}
        active="published" onSelect={() => undefined}><p>Underlying resource list</p></SectionLayout>
      <WorkspaceDialog open={open} onOpenChange={onOpenChange} title="Publish model">
        <input aria-label="Model name" />
      </WorkspaceDialog>
    </section></MemoryRouter></Fluent>
    const { rerender } = render(view(true))
    expect(screen.getByRole('heading', { name: 'Publish model' })).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText('Underlying resource list')).not.toBeVisible()
    expect(screen.getByRole('navigation', { name: 'Models sections' })).toBeVisible()
    rerender(view(false))
    expect(screen.getByText('Underlying resource list')).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Publish model' })).not.toBeInTheDocument()
  })

  it('detects user edits and guards the wrapper Back action', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    render(<Fluent><div><p>List</p><WorkspaceDialog open onOpenChange={onOpenChange} title="Import">
      <input aria-label="Gateway" />
    </WorkspaceDialog></div></Fluent>)
    await user.type(screen.getByRole('textbox', { name: 'Gateway' }), 'test')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(onOpenChange).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(onOpenChange).toHaveBeenCalledWith(undefined, { open: false })
  })

  it('takes over the outer section rather than leaving a list beside a nested detail editor', () => {
    render(<Fluent><MemoryRouter>
      <SectionLayout label="Pools" sections={[{ value: 'pools', label: 'Pools' }]}
        active="pools" onSelect={() => undefined}>
        <div>Persistent pool list</div>
        <SectionLayout label="Pool detail" sections={[{ value: 'configuration', label: 'Configuration' }]}
          active="configuration" onSelect={() => undefined}>
          <p>Pool detail contents</p>
          <WorkspaceDialog open onOpenChange={() => undefined} title="Review pool">Plan preview</WorkspaceDialog>
        </SectionLayout>
      </SectionLayout>
    </MemoryRouter></Fluent>)
    expect(screen.getByRole('heading', { name: 'Review pool' })).toBeVisible()
    expect(screen.getByText('Persistent pool list')).not.toBeVisible()
    expect(screen.getByText('Pool detail contents')).not.toBeVisible()
    expect(screen.getByRole('navigation', { name: 'Pools sections' })).toBeVisible()
  })

  it('provides a guarded close callback for existing form footer buttons', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    render(<Fluent><div><WorkspaceDialog open dirty onOpenChange={onOpenChange} title="Publish">
      {(requestClose) => <button onClick={requestClose}>Close review</button>}
    </WorkspaceDialog></div></Fluent>)
    await user.click(screen.getByRole('button', { name: 'Close review' }))
    expect(onOpenChange).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(onOpenChange).toHaveBeenCalledOnce()
  })
})
