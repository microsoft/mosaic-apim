import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccessRequest } from '../types'
import { costCenterCreationRequest, costCenterMembershipRequest } from '../test/cost-center-fixtures'
import { RequestNotifications } from './RequestNotifications'

const mocks = vi.hoisted(() => ({ listAccessRequests: vi.fn(), listPrincipals: vi.fn(), getDirectoryObject: vi.fn(), getRequestCounts: vi.fn(), listCostCenterRequests: vi.fn(), listCostCenterMembershipRequests: vi.fn() }))
vi.mock('../api', () => ({ useMosaicApi: () => mocks }))
function Page() {
  const location = useLocation()
  return <><RequestNotifications /><output>{location.pathname}{location.search}</output></>
}
function renderPage(initialPrincipals?: Array<{ id: string; objectId: string; label: string }>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (initialPrincipals) client.setQueryData(['principals'], initialPrincipals)
  render(<QueryClientProvider client={client}>
    <FluentProvider theme={webLightTheme}><MemoryRouter><Page /></MemoryRouter></FluentProvider>
  </QueryClientProvider>)
  return client
}
beforeEach(() => {
  mocks.listAccessRequests.mockReset()
  mocks.listCostCenterRequests.mockReset().mockResolvedValue([])
  mocks.listCostCenterMembershipRequests.mockReset().mockResolvedValue([])
  mocks.getRequestCounts.mockReset().mockImplementation(async () => {
    const requests = await mocks.listAccessRequests('pending')
    const centers = await mocks.listCostCenterRequests('pending')
    return { pendingResourceCount: requests.length, pendingCostCenterCount: centers.length, pendingCount: requests.length + centers.length }
  })
  mocks.listPrincipals.mockReset().mockResolvedValue([])
  mocks.getDirectoryObject.mockReset().mockResolvedValue({ displayName: 'Demo analyst' })
})
const request: AccessRequest = {
  id: 'request-test', tenantId: 'tenant-test', requesterObjectId: 'PERSON-TEST',
  resource: { kind: 'modelApi', id: 'model-test' }, state: 'pending',
  resourceSnapshot: { displayName: 'Chat API', gatewayName: null, gatewayId: null },
  createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z',
}
describe('RequestNotifications', () => {
  it('includes membership counts, person names, center context and kind-aware links', async () => {
    mocks.listAccessRequests.mockResolvedValue([])
    mocks.listCostCenterMembershipRequests.mockResolvedValue([costCenterMembershipRequest])
    mocks.getRequestCounts.mockResolvedValue({ pendingResourceCount: 0, pendingCostCenterCount: 0, pendingMembershipCount: 1, pendingCount: 1 })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 1 pending request' }))
    expect(await screen.findByText('0 resource · 0 cost-center creation · 1 membership')).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: /Demo analyst Requests to join Research/ }))
    expect(document.querySelector('output')).toHaveTextContent(`requestKind=costCenterMembership`)
    expect(document.querySelector('output')).toHaveTextContent(`selected=${costCenterMembershipRequest.id}`)
  })
  it('shows pending requests and opens the actionable queue', async () => {
    const request: AccessRequest = {
      id: 'request-test', tenantId: 'tenant-test', requesterObjectId: 'person-test',
      resource: { kind: 'modelApi', id: 'model-test' }, state: 'pending',
      resourceSnapshot: { displayName: 'Chat API', gatewayName: null, gatewayId: null },
      createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z',
    }
    mocks.listAccessRequests.mockResolvedValue([request])
    renderPage()
    expect(await screen.findByText('1')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Pending approvals, 1 pending request' })).toBeVisible()
    expect(mocks.listAccessRequests).toHaveBeenCalledWith('pending')
    fireEvent.click(screen.getByRole('button', { name: 'Pending approvals, 1 pending request' }))
    expect(await screen.findByText('Demo analyst')).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: /Demo analyst Requests access to Chat API/ }))
    expect(document.querySelector('output')).toHaveTextContent('/entitlements?tab=requests&selected=request-test')
  })
  it('shows a failure and retry instead of claiming the queue is empty', async () => {
    mocks.listAccessRequests.mockRejectedValue(new Error('Unavailable'))
    mocks.getRequestCounts.mockResolvedValue({ pendingResourceCount: 1, pendingCostCenterCount: 0, pendingCount: 1 })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 1 pending request' }))
    expect(await screen.findByRole('button', { name: 'Requests unavailable — retry' })).toBeVisible()
    expect(screen.queryByText('No pending requests')).not.toBeInTheDocument()
  })
  it('includes the loaded count in the notification button name when the queue is empty', async () => {
    mocks.listAccessRequests.mockResolvedValue([])
    renderPage()
    expect(screen.getByRole('button', { name: 'Pending approvals' })).toBeVisible()
    expect(await screen.findByRole('button', { name: 'Pending approvals, 0 pending requests' })).toBeVisible()
  })
  it('uses a cached principal name with case-insensitive object matching instead of calling the directory', async () => {
    const principal = { id: 'principal-test', objectId: 'person-test', label: 'Registered analyst' }
    mocks.listPrincipals.mockResolvedValue([principal])
    mocks.listAccessRequests.mockResolvedValue([request])
    renderPage([principal])
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 1 pending request' }))

    expect(await screen.findByText('Registered analyst')).toBeVisible()
    expect(mocks.getDirectoryObject).not.toHaveBeenCalled()
  })
  it('waits for registered names before looking up an unregistered requester', async () => {
    let resolve!: (principals: Array<{ id: string; objectId: string; label: string }>) => void
    mocks.listPrincipals.mockReturnValue(new Promise((done) => { resolve = done }))
    mocks.listAccessRequests.mockResolvedValue([request])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 1 pending request' }))
    expect(await screen.findByText('Loading requester name...')).toBeVisible()
    expect(mocks.getDirectoryObject).not.toHaveBeenCalled()
    resolve([{ id: 'principal-test', objectId: 'person-test', label: 'Registered analyst' }])
    expect(await screen.findByText('Registered analyst')).toBeVisible()
    expect(mocks.getDirectoryObject).not.toHaveBeenCalled()
  })
  it('shows a recoverable name lookup failure without replacing it with an object ID', async () => {
    mocks.listAccessRequests.mockResolvedValue([request])
    mocks.getDirectoryObject.mockRejectedValueOnce(new Error('Directory unavailable'))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 1 pending request' }))
    expect(await screen.findByText('Requester name unavailable')).toBeVisible()
    expect(screen.queryByText(request.requesterObjectId)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry name lookup' }))
    expect(await screen.findByText('Demo analyst')).toBeVisible()
  })
  it('bounds directory lookups to the five displayed requests', async () => {
    mocks.listAccessRequests.mockResolvedValue(Array.from({ length: 8 }, (_, index) => ({
      ...request, id: `request-${index}`, requesterObjectId: `person-${index}`,
    })))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 8 pending requests' }))
    await waitFor(() => expect(mocks.getDirectoryObject).toHaveBeenCalledTimes(5))
    expect(await screen.findAllByText('Demo analyst')).toHaveLength(5)
    expect(screen.getByRole('button', { name: 'Review requests' })).toBeVisible()
  })
  it('uses the combined count without loading rosters or request lists until opened', async () => {
    mocks.getRequestCounts.mockResolvedValue({ pendingResourceCount: 1, pendingCostCenterCount: 2, pendingCount: 3 })
    renderPage()
    expect(await screen.findByRole('button', { name: 'Pending approvals, 3 pending requests' })).toBeVisible()
    expect(mocks.listAccessRequests).not.toHaveBeenCalled()
    expect(mocks.listCostCenterRequests).not.toHaveBeenCalled()
    expect(mocks.listPrincipals).not.toHaveBeenCalled()
    expect(mocks.getDirectoryObject).not.toHaveBeenCalled()
  })
  it('keeps same-ID request kinds distinct and opens a kind-aware cost-center deep link', async () => {
    mocks.listAccessRequests.mockResolvedValue([request])
    mocks.listCostCenterRequests.mockResolvedValue([{ ...costCenterCreationRequest, id: request.id }])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Pending approvals, 2 pending requests' }))
    expect(await screen.findByText('Resource access')).toBeVisible()
    expect(screen.getByText('Cost-center creation')).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: /Demo analyst Requests cost center Research/ }))
    expect(document.querySelector('output')).toHaveTextContent('/entitlements?tab=requests&selected=request-test&requestKind=costCenter')
  })
  it('surfaces a count failure without synthesizing a zero pending badge', async () => {
    mocks.getRequestCounts.mockRejectedValue(new Error('Counts unavailable'))
    mocks.listAccessRequests.mockResolvedValue([])
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Pending approvals' }))
    expect(await screen.findByRole('button', { name: 'Pending count unavailable — retry' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Pending approvals, 0 pending requests' })).not.toBeInTheDocument()
  })
})
