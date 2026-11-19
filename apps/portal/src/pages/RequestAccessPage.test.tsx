import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { MosaicThemeProvider } from '../theme'
import type { CatalogEntry, RequestFormResolution } from '../types'
import { RequestAccessPage } from './RequestAccessPage'

const api = vi.hoisted(() => ({
  getProfile: vi.fn(), resolveRequestForm: vi.fn(), createAccessRequest: vi.fn(), listAccessRequests: vi.fn(),
}))
vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(), usePortalApi: () => api,
}))
const entry: CatalogEntry = {
  id: 'demo', kind: 'modelApi', displayName: 'Fictional model', summary: null, gatewayId: 'gateway',
  gatewayName: 'Demo gateway', environment: 'development', entitled: false, requestState: null, enforced: null,
  costCenterAccess: [{ costCenter: { id: 'center', name: 'Fictional research', code: 'DEMO' }, mode: 'requestOnly', isDefault: true, keysAllowed: true }],
}
const resolution: RequestFormResolution = {
  revision: 3, status: 'configured', contextRevision: 'a'.repeat(64), environment: 'development',
  questions: [
    { id: 'purpose', kind: 'paragraph', label: 'Intended use', helpText: 'High-level context only.', required: true, maxLength: 2000, options: [] },
    { id: 'data', kind: 'singleChoice', label: 'Data class', helpText: null, required: true, maxLength: null, options: [{ id: 'synthetic', label: 'Synthetic' }, { id: 'unsure', label: 'Unsure' }] },
    { id: 'ack', kind: 'acknowledgment', label: 'I follow the data policy.', helpText: null, required: true, maxLength: null, options: [] },
  ],
}
function Result() { return <div>My requests: {useLocation().search}</div> }
function mount(offering: CatalogEntry = entry) {
  const router = createMemoryRouter([
    { path: '/catalog', element: <RequestAccessPage entry={offering}
      environments={[
        { key: 'development', displayName: 'Development review', description: null, color: 'brand', production: false, order: 10 },
        { key: 'production', displayName: 'Production review', description: null, color: 'danger', production: true, order: 20 },
      ]}
      initialCostCenterId="center" onBack={() => router.navigate('/back')} /> },
    { path: '/requests', element: <Result /> }, { path: '/back', element: <div>Catalog restored</div> },
  ], { initialEntries: ['/catalog'] })
  render(<MosaicThemeProvider><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <RouterProvider router={router} />
  </QueryClientProvider></MosaicThemeProvider>)
}
async function fill() {
  await userEvent.click(await screen.findByRole('button', { name: 'Next: Questions' }))
  expect(screen.getByText('Development review')).toBeVisible()
  await userEvent.type(await screen.findByRole('textbox', { name: /Intended use/ }), 'Only fictional research notes.')
  await userEvent.selectOptions(screen.getByRole('combobox', { name: /Data class/ }), 'synthetic')
  await userEvent.click(screen.getByRole('checkbox', { name: /I acknowledge/ }))
  await userEvent.click(screen.getByRole('button', { name: 'Next: Review' }))
}
beforeEach(() => {
  vi.resetAllMocks()
  api.getProfile.mockResolvedValue({ defaultCostCenter: { id: 'center', name: 'Fictional research', code: 'DEMO' } })
  api.resolveRequestForm.mockResolvedValue(resolution)
  api.createAccessRequest.mockResolvedValue({ id: 'created' })
  api.listAccessRequests.mockResolvedValue([])
})
describe('guided resource request', () => {
  it('collects a write-in instead of a single choice and alongside multiple choices', async () => {
    const user = userEvent.setup()
    const data = resolution.questions[1]
    api.resolveRequestForm.mockResolvedValue({
      ...resolution, formatVersion: 2,
      questions: [{ ...data, allowCustomAnswer: true }, {
        ...data, id: 'useCases', kind: 'multipleChoice', label: 'Use cases', allowCustomAnswer: true,
        options: [{ id: 'search', label: 'Search' }, { id: 'summary', label: 'Summaries' }],
      }],
    } satisfies RequestFormResolution)
    mount()
    await user.click(await screen.findByRole('button', { name: 'Next: Questions' }))
    const single = await screen.findByRole('combobox', { name: 'Data class' })
    const next = screen.getByRole('button', { name: 'Next: Review' })
    await user.selectOptions(single, 'synthetic')
    await user.selectOptions(single, screen.getByRole('option', { name: 'Other (write in)' }))
    await user.click(next)
    const singleCustom = screen.getByRole('textbox', { name: /Custom answer for Data class/ })
    expect(singleCustom).toHaveFocus()
    await user.type(singleCustom, 'Fake documents')
    const search = screen.getByRole('checkbox', { name: 'Search' })
    await user.click(next)
    expect(search).toHaveFocus()
    await user.keyboard(' ')
    await user.click(screen.getByRole('checkbox', { name: 'Summaries' }))
    const other = screen.getByRole('checkbox', { name: 'Other (write in)' })
    await user.click(other)
    await user.type(screen.getByRole('textbox', { name: /Custom answer for Use cases/ }), 'Discard this')
    await user.click(other)
    await user.click(other)
    const multiCustom = screen.getByRole('textbox', { name: /Custom answer for Use cases/ })
    expect(multiCustom).toHaveValue('')
    await user.type(multiCustom, 'Fictional extra use')
    await user.click(next)
    expect(screen.getByText('Fictional extra use', { exact: false })).toBeVisible()
    expect(screen.getByText('Summaries')).toBeVisible()
    expect(screen.queryByText('Synthetic')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Request access' }))
    await waitFor(() => expect(api.createAccessRequest).toHaveBeenCalledOnce())
    expect(api.createAccessRequest.mock.calls[0][0].requestForm.answers).toEqual([
      { questionId: 'data', value: { optionIds: [], customText: 'Fake documents' } },
      { questionId: 'useCases', value: { optionIds: ['search', 'summary'], customText: 'Fictional extra use' } },
    ])
  })
  it('validates, retains Back edits and submits only the reviewed version on the final action', async () => {
    mount()
    await userEvent.click(await screen.findByRole('button', { name: 'Next: Questions' }))
    await screen.findByRole('textbox', { name: /Intended use/ })
    await userEvent.click(screen.getByRole('button', { name: 'Next: Review' }))
    expect(screen.getByRole('textbox', { name: /Intended use/ })).toHaveFocus()
    expect(api.createAccessRequest).not.toHaveBeenCalled()
    await userEvent.type(screen.getByRole('textbox', { name: /Intended use/ }), 'Only fictional research notes.')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /Data class/ }), 'synthetic')
    await userEvent.click(screen.getByRole('checkbox', { name: /I acknowledge/ }))
    fireEvent.submit(screen.getByRole('textbox', { name: /Intended use/ }).closest('form')!)
    expect(api.createAccessRequest).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Next: Review' }))
    expect(screen.getByRole('heading', { name: 'Review request' })).toHaveFocus()
    expect(screen.getByText('High-level context only.')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('textbox', { name: /Intended use/ })).toHaveValue('Only fictional research notes.')
    await userEvent.click(screen.getByRole('button', { name: 'Next: Review' }))
    await userEvent.click(screen.getByRole('button', { name: 'Request access' }))
    await waitFor(() => expect(api.createAccessRequest).toHaveBeenCalledOnce())
    expect(api.createAccessRequest.mock.calls[0][0]).toMatchObject({
      resource: { kind: 'modelApi', id: 'demo' }, costCenterId: 'center',
      requestForm: { revision: 3, contextRevision: resolution.contextRevision, answers: [
        { questionId: 'purpose', value: 'Only fictional research notes.' }, { questionId: 'data', value: 'synthetic' }, { questionId: 'ack', value: true },
      ] },
    })
    await screen.findByText('My requests: ?request=created')
  })
  it('requires explicit stale reload and clears changed acknowledgment wording without auto-submission', async () => {
    api.createAccessRequest.mockRejectedValue(new ApiError('The form changed.', 409, { details: { reason: 'requestFormChanged' } }))
    mount()
    await fill()
    await userEvent.click(screen.getByRole('button', { name: 'Request access' }))
    await screen.findByText(/Your draft is retained/)
    api.resolveRequestForm.mockResolvedValue({ ...resolution, revision: 4, contextRevision: 'b'.repeat(64),
      questions: resolution.questions.map(q => q.id === 'ack' ? { ...q, label: 'I acknowledge revised wording.' } : q) })
    await userEvent.click(screen.getByRole('button', { name: 'Reload current form' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Keep compatible answers and review' }))
    expect(screen.getByRole('textbox', { name: /Intended use/ })).toHaveValue('Only fictional research notes.')
    expect(screen.getByRole('checkbox', { name: /I acknowledge/ })).not.toBeChecked()
    expect(api.createAccessRequest).toHaveBeenCalledOnce()
  })
  it.each([
    { environment: 'production', label: 'Production review' },
    { environment: null, label: 'Unclassified' },
  ])('shows the resolved $label context after stale recovery instead of the cached offering environment', async ({ environment, label }) => {
    api.createAccessRequest.mockRejectedValueOnce(new ApiError('The context changed.', 409, { details: { reason: 'requestFormChanged' } }))
    mount(); await fill()
    await userEvent.click(screen.getByRole('button', { name: 'Request access' }))
    await screen.findByText(/Your draft is retained/)
    api.resolveRequestForm.mockResolvedValue({ ...resolution, revision: 4, contextRevision: 'b'.repeat(64), environment })
    await userEvent.click(screen.getByRole('button', { name: 'Reload current form' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Keep compatible answers and review' }))
    expect(screen.getByText(label)).toBeVisible()
    expect(screen.queryByText('Development review')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /Intended use/ })).toHaveValue('Only fictional research notes.')
    await userEvent.click(screen.getByRole('button', { name: 'Next: Review' }))
    await userEvent.click(screen.getByRole('button', { name: 'Request access' }))
    await waitFor(() => expect(api.createAccessRequest).toHaveBeenCalledTimes(2))
    expect(api.createAccessRequest.mock.calls[1][0].requestForm.contextRevision).toBe('b'.repeat(64))
  })
  it('locks the selected cost center until its pending resolution completes', async () => {
    let finish!: (value: RequestFormResolution) => void
    api.resolveRequestForm.mockImplementationOnce(() => new Promise<RequestFormResolution>(resolve => { finish = resolve }))
    mount({ ...entry, costCenterAccess: [
      ...entry.costCenterAccess!,
      { costCenter: { id: 'alternate', name: 'Other fictional team', code: 'OTHER' }, mode: 'requestOnly', isDefault: false, keysAllowed: true },
    ] })
    await userEvent.click(await screen.findByRole('button', { name: 'Next: Questions' }))
    const center = screen.getByRole('combobox', { name: 'Cost center' })
    expect(center).toBeDisabled()
    await userEvent.selectOptions(center, 'alternate')
    expect(center).toHaveValue('center')
    await act(async () => finish(resolution))
    await screen.findByRole('textbox', { name: /Intended use/ })
    expect(api.resolveRequestForm).toHaveBeenCalledWith({ kind: 'modelApi', id: 'demo', scopeId: null }, 'center')
    expect(api.createAccessRequest).not.toHaveBeenCalled()
  })
  it('does not turn a failed form lookup into a legacy submission path', async () => {
    api.resolveRequestForm.mockRejectedValue(new Error('Form storage unavailable.'))
    mount()
    await userEvent.click(await screen.findByRole('button', { name: 'Next: Questions' }))
    await screen.findByText('Form storage unavailable.')
    expect(screen.queryByRole('button', { name: 'Request access' })).not.toBeInTheDocument()
    expect(api.createAccessRequest).not.toHaveBeenCalled()
  })
  it('surfaces aggregate server validation without echoing or discarding draft answers', async () => {
    api.createAccessRequest.mockRejectedValue(new ApiError('Request failed with status 422', 422, {
      detail: [{ loc: ['body', 'requestForm'], msg: 'Answers must fit within 16 KiB.' }],
    }))
    mount(); await fill()
    await userEvent.click(screen.getByRole('button', { name: 'Request access' }))
    const message = await screen.findByText('Answers must fit within 16 KiB.')
    expect(message.closest('[tabindex="-1"]')).toHaveFocus()
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('textbox', { name: /Intended use/ })).toHaveValue('Only fictional research notes.')
    expect(api.createAccessRequest).toHaveBeenCalledOnce()
  })
})
