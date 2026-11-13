import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { CatalogSummaryEditor } from './CatalogSummaryEditor'

function renderEditor(summary: string | null, onSave = vi.fn(async (_summary: string | null) => {})) {
  const onSaved = vi.fn()
  const onClose = vi.fn()
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><FluentProvider theme={webLightTheme}><MemoryRouter>
    <CatalogSummaryEditor displayName="Demo chat" summary={summary} context={<span>Development</span>}
      visibilityLabel="Entitled users only" mutationKey={['summary-test']}
      onSave={onSave} onSaved={onSaved} onClose={onClose} />
  </MemoryRouter></FluentProvider></QueryClientProvider>)
  return { onSave, onSaved, onClose }
}

describe('CatalogSummaryEditor', () => {
  it('reviews without saving and keeps the draft when returning to edit', async () => {
    const user = userEvent.setup()
    const { onSave, onSaved } = renderEditor('Saved copy')
    expect(screen.getByRole('button', { name: 'Review summary' })).toBeDisabled()
    const input = screen.getByRole('textbox', { name: 'Catalog summary (optional)' })
    await user.clear(input)
    await user.type(input, '  Revised copy  ')
    await user.click(screen.getByRole('button', { name: 'Review summary' }))
    const preview = screen.getByRole('region', { name: 'Portal summary preview' })
    expect(within(preview).getByText('Revised copy')).toBeVisible()
    expect(screen.getByText('Saved copy')).toBeVisible()
    expect(onSave).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Back to edit' }))
    expect(screen.getByRole('textbox', { name: 'Catalog summary (optional)' })).toHaveValue('  Revised copy  ')
    await user.click(screen.getByRole('button', { name: 'Review summary' }))
    await user.click(screen.getByRole('button', { name: 'Save summary' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledExactlyOnceWith('Revised copy'))
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('retains the draft after a save error and protects cancel with Stay and Discard', async () => {
    const user = userEvent.setup()
    const save = vi.fn(async (_summary: string | null) => { throw new Error('Permission denied. Check your administrator role.') })
    const { onSaved, onClose } = renderEditor(null, save)
    await user.type(screen.getByRole('textbox', { name: 'Catalog summary (optional)' }), 'Unsaved text')
    await user.click(screen.getByRole('button', { name: 'Review summary' }))
    await user.click(screen.getByRole('button', { name: 'Save summary' }))
    expect(await screen.findByText(/Permission denied/)).toBeVisible()
    expect(onSaved).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Back to edit' }))
    const input = screen.getByRole('textbox', { name: 'Catalog summary (optional)' })
    expect(input).toHaveValue('Unsaved text')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(input).toHaveValue('Unsaved text')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('validates Unicode code points without truncating legacy text', async () => {
    const user = userEvent.setup()
    const { onSave } = renderEditor('x'.repeat(2001))
    const input = screen.getByRole('textbox', { name: 'Catalog summary (optional)' })
    expect(input).toHaveValue('x'.repeat(2001))
    expect(screen.getByText(/shorten it before reviewing/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Review summary' })).toBeDisabled()
    const text = '\u{1f600}'.repeat(2000)
    fireEvent.change(input, { target: { value: text } })
    expect(screen.getByRole('button', { name: 'Review summary' })).toBeEnabled()
    fireEvent.change(input, { target: { value: text + 'x' } })
    expect(screen.getByRole('button', { name: 'Review summary' })).toBeDisabled()
    fireEvent.change(input, { target: { value: text } })
    await user.click(screen.getByRole('button', { name: 'Review summary' }))
    await user.click(screen.getByRole('button', { name: 'Save summary' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledExactlyOnceWith(text))
  })

  it('clears only on explicit save and renders preview markup literally', async () => {
    const user = userEvent.setup()
    const { onSave } = renderEditor('Old copy')
    const input = screen.getByRole('textbox', { name: 'Catalog summary (optional)' })
    fireEvent.change(input, { target: { value: '<b>Plain text</b>\nSecond line' } })
    await user.click(screen.getByRole('button', { name: 'Review summary' }))
    expect(within(screen.getByRole('region', { name: 'Portal summary preview' })).getByText('<b>Plain text</b> Second line')).toBeVisible()
    expect(onSave).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Back to edit' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Catalog summary (optional)' }), { target: { value: ' \n ' } })
    await user.click(screen.getByRole('button', { name: 'Review summary' }))
    expect(screen.getByText('Summary will be removed')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save summary' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledExactlyOnceWith(null))
  })
})
