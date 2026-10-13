import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccessRequest } from '../types'
import { RequestNotifications } from './RequestNotifications'

const mocks = vi.hoisted(() => ({ listAccessRequests: vi.fn() }))
vi.mock('../api', () => ({ useMosaicApi: () => mocks }))
function Page() {
  const location = useLocation()
  return <><RequestNotifications /><output>{location.pathname}{location.search}</output></>
}
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <FluentProvider theme={webLightTheme}><MemoryRouter><Page /></MemoryRouter></FluentProvider>
  </QueryClientProvider>)
  return client
}
beforeEach(() => {
  mocks.listAccessRequests.mockReset()
})
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
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Access requested: Chat API' }))
    expect(document.querySelector('output')).toHaveTextContent('/entitlements?tab=requests')
  })
  it('shows a failure and retry instead of claiming the queue is empty', async () => {
    mocks.listAccessRequests.mockRejectedValue(new Error('Unavailable'))
    const client = renderPage()
    await waitFor(() => expect(client.getQueryState(['access-requests', 'pending'])?.status).toBe('error'))
    fireEvent.click(screen.getByRole('button', { name: 'Notifications' }))
    expect(await screen.findByRole('menuitem', { name: 'Requests unavailable — retry' })).toBeVisible()
    expect(screen.queryByText('No pending access requests')).not.toBeInTheDocument()
  })
  it('includes the loaded count in the notification button name when the queue is empty', async () => {
    mocks.listAccessRequests.mockResolvedValue([])
    renderPage()
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeVisible()
    expect(await screen.findByRole('button', { name: 'Notifications, 0 pending requests' })).toBeVisible()
  })
})
