import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import type { PublicCatalogDraft, PublicCatalogPreview, PublicCatalogStatus } from '../types'
import { PricingCatalogWorkspace } from './PricingCatalogWorkspace'

const api = vi.hoisted(() => ({
  getPublicCatalog: vi.fn(),
  getPublicCatalogTemplate: vi.fn(),
  importPublicCatalog: vi.fn(),
  refreshPublicCatalog: vi.fn(),
  getPublicCatalogDraft: vi.fn(),
  previewPublicCatalog: vi.fn(),
  activatePublicCatalog: vi.fn(),
  recoverPublicCatalog: vi.fn(),
}))
vi.mock('../api', async (original) => ({ ...await original<object>(), useMosaicApi: () => api }))

const draft: PublicCatalogDraft = {
  id: 'catalogdraft-fictional', version: 'draft-etag', baseRevision: 'current-revision',
  method: 'supplied', status: 'readyForReview', error: null, acquiredAt: '2026-11-17T12:00:00Z',
  activationId: null, definitions: 1, prices: 1,
  missingPrices: [],
  mappings: [{
    cloud: 'government', publisher: 'ExampleAI', model: 'example-chat',
    deploymentType: 'Standard', kind: 'tokens',
    meters: { product: 'Example product', input: 'Example input', output: 'Example output' },
  }],
  sources: [{
    id: 'sample', title: 'Fictional Government price evidence', url: 'https://example.invalid/gov',
    retrievedOn: '2026-11-15', method: 'supplied', acquiredAt: '2026-11-17T12:00:00Z', ageDays: 2,
  }],
  evidence: [{
    sourceId: 'sample', cloud: 'government', priceIds: ['sample-price'],
    excerpt: 'Fictional Government input USD 3 and output USD 12 per million tokens.',
  }],
}
const preview: PublicCatalogPreview = {
  draft, baseRevision: 'current-revision', previewDigest: 'a'.repeat(64), effectiveFrom: '2026-11-17',
  totalChanges: 1, added: 0, changed: 1, unchanged: 0, retained: 20,
  protectedManual: 2, protectedBilling: 4, backdated: false,
  sourceDatesPrecedeActivation: true, nextOffset: null, totalGaps: 1,
  coverage: [
    { cloud: 'commercial', deployments: 2, pricedBefore: 2, pricedAfter: 2 },
    { cloud: 'government', deployments: 2, pricedBefore: 0, pricedAfter: 1 },
  ],
  gaps: [{
    key: 'missing', cloud: 'government', model: 'example-unpriced', region: 'usgovvirginia',
    deploymentType: 'Standard', reason: 'noPrice', message: 'No matching Government public price.',
  }],
  changes: [{
    price: {
      id: 'sample-price', cloud: 'government', model: 'example-chat', deploymentType: 'Standard',
      inputPerMillion: 3, outputPerMillion: 12, effectiveFrom: '2026-01-01', sourceIds: ['sample'],
    },
    previous: null, previousLabel: 'No public rate', change: 'changed', effectiveFrom: '2026-11-17',
  }],
}
const status: PublicCatalogStatus = {
  revision: 'current-revision', seedLastUpdated: '2026-09-30',
  sources: draft.sources, definitions: 1, mappings: draft.mappings,
  deployments: 4, pricedDeployments: 2, drafts: [draft], nextOffset: null,
  history: [{
    id: 'prior-revision', draftId: 'old-draft', previousRevision: null,
    effectiveFrom: '2026-09-30', createdAt: '2026-09-30T12:00:00Z', recordedBy: 'demo-admin',
    note: 'Previously reviewed catalog', backdated: false,
  }],
}

function renderWorkspace(initialDraft: string | null = null, initialStage: 'validate' | 'review' = 'validate') {
  function Harness() {
    const [id, setId] = useState(initialDraft)
    const [stage, setStage] = useState(initialStage)
    return <PricingCatalogWorkspace draftId={id} stage={stage}
      onSelectDraft={(next, nextStage) => { setId(next); setStage(nextStage ?? 'validate') }}
      onWorkspaceChange={vi.fn()} />
  }
  return render(<MemoryRouter><QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })}><Harness /></QueryClientProvider></MemoryRouter>)
}

async function acknowledge(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(/Activation note/), 'Reviewed source and remaining gaps')
  await user.click(screen.getByRole('checkbox', { name: /I reviewed the source evidence/ }))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((success, failure) => { resolve = success; reject = failure })
  return { promise, resolve, reject }
}

describe('Public catalog guided checkpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getPublicCatalog.mockResolvedValue(status)
    api.getPublicCatalogDraft.mockResolvedValue(draft)
    api.getPublicCatalogTemplate.mockResolvedValue({ schema: {}, example: { format: 'mosaicPublicCatalog' } })
    api.importPublicCatalog.mockResolvedValue(draft)
    api.refreshPublicCatalog.mockResolvedValue(draft)
    api.recoverPublicCatalog.mockResolvedValue({ ...draft, method: 'recovery' })
    api.previewPublicCatalog.mockImplementation(async (_id: string, effectiveFrom: string) => ({
      ...preview, effectiveFrom, backdated: effectiveFrom < new Date().toISOString().slice(0, 10),
    }))
    api.activatePublicCatalog.mockResolvedValue({
      id: 'new-activation', draftId: draft.id, note: 'Reviewed', effectiveFrom: '2026-11-17',
    })
  })

  it('requires an explicit acquire action, validation checkpoint and separate activation', async () => {
    const user = userEvent.setup()
    renderWorkspace()
    await screen.findByRole('heading', { name: 'Public catalog' })
    expect(api.refreshPublicCatalog).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Fetch Commercial prices' }))
    expect(await screen.findByRole('heading', { name: 'Acquire public pricing data' })).toBeVisible()
    expect(api.refreshPublicCatalog).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Fetch & validate' }))
    await screen.findByRole('heading', { name: 'Validate public catalog' })
    expect(api.refreshPublicCatalog).toHaveBeenCalledWith('current-revision')
    expect(api.previewPublicCatalog).not.toHaveBeenCalled()
    expect(api.activatePublicCatalog).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Review changes' }))
    await screen.findByRole('table', { name: 'Public-price changes' })
    expect(screen.getByRole('button', { name: 'Activate catalog' })).toBeDisabled()
    expect(screen.getByText('2 manual & 4 billing rates')).toBeVisible()
    expect(screen.getByText(/1 deployments remain unpriced/)).toBeVisible()
    expect(screen.getByText(/No zero price or cross-cloud fallback/)).toBeVisible()
  })

  it('imports JSON without automatic source verification or provider calls', async () => {
    const user = userEvent.setup()
    renderWorkspace()
    await user.click(await screen.findByRole('button', { name: 'Import pricing package' }))
    const file = new File(['{"format":"mosaicPublicCatalog"}'], 'catalog.json', { type: 'application/json' })
    await user.upload(screen.getByLabelText('Pricing package (.json)'), file)
    await user.click(screen.getByRole('button', { name: 'Import & validate' }))
    await screen.findByRole('heading', { name: 'Validate public catalog' })
    expect(api.importPublicCatalog).toHaveBeenCalledWith('{"format":"mosaicPublicCatalog"}', 'current-revision')
    expect(api.refreshPublicCatalog).not.toHaveBeenCalled()
    expect(screen.getAllByText(/Not independently fetched/).length).toBeGreaterThan(0)
    await user.click(screen.getByText('Supplied numeric evidence (1)'))
    expect(screen.getByText(draft.evidence[0].excerpt)).toBeVisible()
    expect(api.activatePublicCatalog).not.toHaveBeenCalled()
  })

  it('keeps a pending acquisition bound to its method and file through status refresh and completion', async () => {
    const user = userEvent.setup()
    const imported = deferred<PublicCatalogDraft>()
    const refreshed = deferred<PublicCatalogStatus>()
    api.importPublicCatalog.mockReturnValueOnce(imported.promise)
    renderWorkspace()
    await user.click(await screen.findByRole('button', { name: 'Import pricing package' }))
    const file = new File(['{"format":"mosaicPublicCatalog"}'], 'catalog.json', { type: 'application/json' })
    const input = screen.getByLabelText<HTMLInputElement>('Pricing package (.json)')
    await user.upload(input, file)
    await user.click(screen.getByRole('button', { name: 'Import & validate' }))
    await waitFor(() => expect(api.importPublicCatalog).toHaveBeenCalledOnce())
    try {
      await user.click(screen.getByRole('radio', { name: 'Fetch Commercial prices' }))
      expect(screen.getByRole('radio', { name: 'Import pricing package' })).toBeChecked()
      expect(screen.getByRole('radio', { name: 'Fetch Commercial prices' })).toBeDisabled()
      expect(input).toBeDisabled()
      await user.upload(input, new File(['{}'], 'different.json', { type: 'application/json' }))
      expect(input.files?.[0]).toBe(file)
      expect(screen.getByRole('region', { name: 'Acquire public pricing data' })).toHaveAttribute('aria-busy', 'true')
      await user.click(screen.getByRole('button', { name: 'Import & validate' }))
      expect(api.importPublicCatalog).toHaveBeenCalledOnce()
    } finally {
      api.getPublicCatalog.mockReturnValueOnce(refreshed.promise)
      await act(async () => { imported.resolve(draft); await imported.promise })
    }
    try {
      await waitFor(() => expect(api.getPublicCatalog).toHaveBeenCalledTimes(2))
      expect(screen.getByRole('heading', { name: 'Acquire public pricing data' })).toBeVisible()
      expect(screen.getByRole('radio', { name: 'Import pricing package' })).toBeDisabled()
      expect(input).toBeDisabled()
      expect(screen.queryByRole('heading', { name: 'Public catalog' })).not.toBeInTheDocument()
    } finally {
      await act(async () => { refreshed.resolve({ ...status, revision: 'later-revision' }); await refreshed.promise })
    }
    await screen.findByRole('heading', { name: 'Validate public catalog' })
    expect(api.importPublicCatalog).toHaveBeenCalledWith('{"format":"mosaicPublicCatalog"}', 'current-revision')
    expect(api.getPublicCatalogDraft).toHaveBeenLastCalledWith(draft.id)
    expect(api.refreshPublicCatalog).not.toHaveBeenCalled()
    expect(api.activatePublicCatalog).not.toHaveBeenCalled()
  })

  it('unlocks a failed acquisition for explicit correction and retry without changing its context', async () => {
    const user = userEvent.setup()
    const imported = deferred<PublicCatalogDraft>()
    api.importPublicCatalog.mockReturnValueOnce(imported.promise)
    renderWorkspace()
    await user.click(await screen.findByRole('button', { name: 'Import pricing package' }))
    const input = screen.getByLabelText<HTMLInputElement>('Pricing package (.json)')
    const original = new File(['{"invalid":false}'], 'catalog.json', { type: 'application/json' })
    await user.upload(input, original)
    await user.click(screen.getByRole('button', { name: 'Import & validate' }))
    await waitFor(() => expect(api.importPublicCatalog).toHaveBeenCalledOnce())
    await act(async () => { imported.reject(new ApiError('Invalid public price input', 422)) })
    expect(await screen.findByText('Invalid public price input')).toBeVisible()
    expect(screen.getByRole('radio', { name: 'Import pricing package' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Fetch Commercial prices' })).toBeEnabled()
    expect(input).toBeEnabled()
    expect(input.files?.[0]).toBe(original)
    await user.upload(input, new File(['{"inputPerMillion":0}'], 'corrected.json', { type: 'application/json' }))
    expect(api.importPublicCatalog).toHaveBeenCalledOnce()
    await user.click(screen.getByRole('button', { name: 'Import & validate' }))
    await screen.findByRole('heading', { name: 'Validate public catalog' })
    expect(api.importPublicCatalog).toHaveBeenLastCalledWith('{"inputPerMillion":0}', 'current-revision')
    expect(api.refreshPublicCatalog).not.toHaveBeenCalled()
    expect(api.activatePublicCatalog).not.toHaveBeenCalled()
  })

  it('requires source/gap and historical acknowledgements and preserves valid empty-date state', async () => {
    const user = userEvent.setup()
    renderWorkspace(draft.id, 'review')
    await screen.findByRole('table', { name: 'Public-price changes' })
    const date = screen.getByLabelText(/Effective from \(UTC\)/)
    await user.clear(date)
    expect(screen.getByText('Choose a valid UTC effective date before reviewing activation.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Activate catalog' })).toBeDisabled()
    await user.type(date, '2020-01-01')
    await waitFor(() => expect(api.previewPublicCatalog).toHaveBeenLastCalledWith(draft.id, '2020-01-01', 0))
    await acknowledge(user)
    expect(screen.getByRole('button', { name: 'Activate catalog' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /I acknowledge that this backdated/ }))
    await user.click(screen.getByRole('button', { name: 'Activate catalog' }))
    await waitFor(() => expect(api.activatePublicCatalog).toHaveBeenCalledWith(draft.id, expect.objectContaining({
      effectiveFrom: '2020-01-01', acknowledgeSourceAndGaps: true,
      acknowledgeBackdateImpact: true, draftVersion: 'draft-etag',
    })))
    expect(await screen.findByRole('heading', { name: 'Catalog activation recorded' })).toBeVisible()
    await waitFor(() => expect(screen.getByRole('status', { name: 'Catalog activation result' })).toHaveFocus())
  })

  it('retains the note on a stale preview and requires a fresh review before retry', async () => {
    const user = userEvent.setup()
    api.activatePublicCatalog.mockRejectedValueOnce(new ApiError('The pricing preview changed', 409))
    renderWorkspace(draft.id, 'review')
    await screen.findByRole('table', { name: 'Public-price changes' })
    await acknowledge(user)
    await user.click(screen.getByRole('button', { name: 'Activate catalog' }))
    expect(await screen.findByText('The pricing preview changed')).toBeVisible()
    expect(screen.getByLabelText(/Activation note/)).toHaveValue('Reviewed source and remaining gaps')
    expect(screen.getByRole('button', { name: 'Activate catalog' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Reload and review' }))
    expect(screen.getByRole('checkbox', { name: /I reviewed the source evidence/ })).not.toBeChecked()
    expect(api.activatePublicCatalog).toHaveBeenCalledTimes(1)
  })

  it('reconciles an unknown activation result with reads instead of automatically resubmitting', async () => {
    const user = userEvent.setup()
    api.activatePublicCatalog.mockRejectedValueOnce(new Error('Connection interrupted'))
    renderWorkspace(draft.id, 'review')
    await screen.findByRole('table', { name: 'Public-price changes' })
    await acknowledge(user)
    await user.click(screen.getByRole('button', { name: 'Activate catalog' }))
    expect(await screen.findByText('Activation outcome is not confirmed')).toBeVisible()
    expect(screen.getByLabelText(/Activation note/)).toBeDisabled()
    api.getPublicCatalogDraft.mockResolvedValue({ ...draft, status: 'activated', activationId: 'recorded-operation' })
    await user.click(screen.getByRole('button', { name: 'Check activation status' }))
    expect(await screen.findByRole('heading', { name: 'Catalog activation recorded' })).toBeVisible()
    expect(api.activatePublicCatalog).toHaveBeenCalledTimes(1)
  })

  it('creates a recovery draft without silently activating the selected old catalog', async () => {
    const user = userEvent.setup()
    renderWorkspace()
    const history = await screen.findByRole('table', { name: 'Public catalog activation history' })
    await user.click(within(history).getByRole('button', { name: 'Restore as new activation' }))
    await screen.findByRole('heading', { name: 'Validate public catalog' })
    expect(api.recoverPublicCatalog).toHaveBeenCalledWith('prior-revision')
    expect(api.activatePublicCatalog).not.toHaveBeenCalled()
  })
})
