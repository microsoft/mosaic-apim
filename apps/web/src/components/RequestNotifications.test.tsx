import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccessRequest } from '../types'
import { RequestNotifications } from './RequestNotifications'

const mocks = vi.hoisted(() => ({ listAccessRequests: vi.fn(), listPrincipals: vi.fn(), getDirectoryObject: vi.fn() }))
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
    expect(screen.getByRole('button', { name: 'Notifications, 1 pending request' })).toBeVisible()
    expect(mocks.listAccessRequests).toHaveBeenCalledWith('pending')
    fireEvent.click(screen.getByRole('button', { name: 'Notifications, 1 pending request' }))
    expect(await screen.findByText('Demo analyst')).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: /Demo analyst Requests access to Chat API/ }))
    expect(document.querySelector('output')).toHaveTextContent('/entitlements?tab=requests&selected=request-test')
  })
  it('shows a failure and retry instead of claiming the queue is empty', async () => {
    mocks.listAccessRequests.mockRejectedValue(new Error('Unavailable'))
    const client = renderPage()
    await waitFor(() => expect(client.getQueryState(['access-requests', 'pending'])?.status).toBe('error'))
    fireEvent.click(screen.getByRole('button', { name: 'Notifications' }))
    expect(await screen.findByRole('button', { name: 'Requests unavailable — retry' })).toBeVisible()
    expect(screen.queryByText('No pending access requests')).not.toBeInTheDocument()
  })
  it('includes the loaded count in the notification button name when the queue is empty', async () => {
    mocks.listAccessRequests.mockResolvedValue([])
    renderPage()
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeVisible()
    expect(await screen.findByRole('button', { name: 'Notifications, 0 pending requests' })).toBeVisible()
  })
  it('uses a cached principal name with case-insensitive object matching instead of calling the directory', async () => {
    const principal = { id: 'principal-test', objectId: 'person-test', label: 'Registered analyst' }
    mocks.listPrincipals.mockResolvedValue([principal])
    mocks.listAccessRequests.mockResolvedValue([request])
    renderPage([principal])
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 1 pending request' }))

    expect(await screen.findByText('Registered analyst')).toBeVisible()
    expect(mocks.getDirectoryObject).not.toHaveBeenCalled()
  })
  it('waits for registered names before looking up an unregistered requester', async () => {
    let resolve!: (principals: Array<{ id: string; objectId: string; label: string }>) => void
    mocks.listPrincipals.mockReturnValue(new Promise((done) => { resolve = done }))
    mocks.listAccessRequests.mockResolvedValue([request])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 1 pending request' }))
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
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 1 pending request' }))
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
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 8 pending requests' }))
    await waitFor(() => expect(mocks.getDirectoryObject).toHaveBeenCalledTimes(5))
    expect(await screen.findAllByText('Demo analyst')).toHaveLength(5)
    expect(screen.getByRole('button', { name: 'Review access requests' })).toBeVisible()
  })
})
