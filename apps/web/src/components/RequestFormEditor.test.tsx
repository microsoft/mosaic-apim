import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { MosaicThemeProvider } from '../theme'
import { RequestFormEditor } from './RequestFormEditor'
import type { RequestFormPublishInput, RequestFormResolution } from '../types'

const api = vi.hoisted(() => ({
  getRequestForm: vi.fn(), publishRequestForm: vi.fn(), previewRequestForm: vi.fn(),
}))
vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(), useMosaicApi: () => api,
}))
function mount() {
  render(<MosaicThemeProvider><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <RequestFormEditor resources={[
      { label: 'Fictional model', resource: { kind: 'modelApi', id: 'demo' } },
      { label: 'Other fictional model', resource: { kind: 'modelApi', id: 'other' } },
    ]} />
  </QueryClientProvider></MosaicThemeProvider>)
}
async function prepare() {
  await userEvent.click(await screen.findByRole('button', { name: 'Configure request form' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Enable this form for matching new requests' }))
  await userEvent.click(screen.getByRole('button', { name: 'Add question' }))
  await userEvent.type(screen.getByRole('textbox', { name: 'Question 1 label' }), 'Intended use')
  await userEvent.click(screen.getByRole('checkbox', { name: 'Require question 1' }))
  await userEvent.click(screen.getByRole('button', { name: 'Next: Applicability' }))
}
beforeEach(() => {
  vi.resetAllMocks()
  api.getRequestForm.mockResolvedValue({ revision: 0, definition: { formatVersion: 1, enabled: false, questions: [] } })
  api.previewRequestForm.mockResolvedValue({ revision: 0, status: 'configured', contextRevision: 'a'.repeat(64), questions: [] })
  api.publishRequestForm.mockImplementation(async (body: RequestFormPublishInput) => ({ revision: 1, definition: body.definition }))
})
describe('guided request form publication', () => {
  it('clears a different offering preview and prevents selection changes while resolving it', async () => {
    api.previewRequestForm.mockResolvedValueOnce({
      revision: 0, status: 'configured', contextRevision: 'a'.repeat(64), environment: 'development',
      questions: [{ id: 'first', kind: 'paragraph', label: 'First offering evidence', required: true }],
    })
    mount(); await prepare()
    const offering = screen.getByRole('combobox', { name: 'Preview exact offering' })
    await userEvent.selectOptions(offering, JSON.stringify(['modelApi', null, 'demo']))
    await userEvent.click(screen.getByRole('button', { name: 'Preview questions' }))
    expect(await screen.findByText('Current draft preview')).toBeVisible()
    await userEvent.selectOptions(offering, JSON.stringify(['modelApi', null, 'other']))
    expect(screen.queryByText('Current draft preview')).not.toBeInTheDocument()
    expect(screen.queryByText(/First offering evidence -/)).not.toBeInTheDocument()
    let finish!: (value: RequestFormResolution) => void
    api.previewRequestForm.mockImplementationOnce(() => new Promise<RequestFormResolution>(resolve => { finish = resolve }))
    await userEvent.click(screen.getByRole('button', { name: 'Preview questions' }))
    expect(offering).toBeDisabled()
    await act(async () => finish({ revision: 0, status: 'notRequired', contextRevision: 'b'.repeat(64), environment: 'production', questions: [] }))
    expect(await screen.findByText('Current draft preview')).toBeVisible()
    expect(offering).toHaveValue(JSON.stringify(['modelApi', null, 'other']))
    expect(api.publishRequestForm).not.toHaveBeenCalled()
  })
  it('previews without a write and requires explicit enablement at the final publish step', async () => {
    mount(); await prepare()
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Preview exact offering' }), JSON.stringify(['modelApi', null, 'demo']))
    await userEvent.click(screen.getByRole('button', { name: 'Preview questions' }))
    await waitFor(() => expect(api.previewRequestForm).toHaveBeenCalledOnce())
    expect(api.publishRequestForm).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Next: Publish' }))
    expect(screen.getByRole('button', { name: 'Publish revision' })).toBeDisabled()
    await userEvent.click(screen.getByRole('checkbox', { name: 'Enable this form and require human review for matching new requests' }))
    await userEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    await waitFor(() => expect(api.publishRequestForm).toHaveBeenCalledOnce())
    expect(api.publishRequestForm.mock.calls[0][0]).toMatchObject({
      expectedRevision: 0, enableConfirmed: true, definition: { enabled: true, questions: [
        { label: 'Intended use', kind: 'paragraph', maxLength: 2000, required: true },
      ] },
    })
    await screen.findByText(/Published request form v1/)
  })
  it('retains the authored draft and focuses the real conflict instead of replacing it with current data', async () => {
    api.publishRequestForm.mockRejectedValue(new ApiError('The request form changed; reload before publishing.', 409))
    mount(); await prepare()
    await userEvent.click(screen.getByRole('button', { name: 'Next: Publish' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Enable this form and require human review for matching new requests' }))
    await userEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    const error = await screen.findByText('The request form changed; reload before publishing.')
    expect(error.closest('[tabindex="-1"]')).toHaveFocus()
    await userEvent.click(screen.getByRole('button', { name: 'Previous step' }))
    await userEvent.click(screen.getByRole('button', { name: 'Previous step' }))
    expect(screen.getByRole('textbox', { name: 'Question 1 label' })).toHaveValue('Intended use')
    expect(api.publishRequestForm).toHaveBeenCalledOnce()
  })
})
