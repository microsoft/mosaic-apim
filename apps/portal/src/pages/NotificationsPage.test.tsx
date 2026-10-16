import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import { notificationItem } from '../test/notification-fixtures'
import { NotificationsPage } from './NotificationsPage'

const mocks = vi.hoisted(() => ({ api: {} as PortalApi, account: 'user-a' }))
vi.mock('../api', () => ({ ApiError: class ApiError extends Error { status = 500 }, usePortalApi: () => mocks.api }))
vi.mock('@azure/msal-react', () => ({ useMsal: () => ({ accounts: [{ tenantId: 'tenant-1', homeAccountId: mocks.account }] }) }))

function renderPage(api: Partial<PortalApi>) {
  mocks.api = api as PortalApi
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const result = render(<QueryClientProvider client={client}><MemoryRouter><NotificationsPage /></MemoryRouter></QueryClientProvider>)
  return { ...result, client }
}

describe('NotificationsPage', () => {
  it('isolates cached personal inbox data when the authenticated account changes', async () => {
    mocks.account = 'user-a'
    const listNotifications = vi.fn(async () => ({
      items: [notificationItem({ title: mocks.account === 'user-a' ? 'First account update' : 'Second account update' })],
      unreadCount: 1, continuationToken: null,
    }))
    const { rerender, client } = renderPage({ listNotifications })
    expect(await screen.findByRole('heading', { name: 'First account update' })).toBeVisible()
    mocks.account = 'user-b'
    rerender(<QueryClientProvider client={client}><MemoryRouter><NotificationsPage /></MemoryRouter></QueryClientProvider>)
    expect(screen.queryByRole('heading', { name: 'First account update' })).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Second account update' })).toBeVisible()
    mocks.account = 'user-a'
  })
  it('renders real inbox data with exact request links and persists read/unread before changing state', async () => {
    const user = userEvent.setup()
    let readAt: string | null = null
    const listNotifications = vi.fn(async () => ({
      items: [notificationItem({ readAt })], unreadCount: readAt ? 0 : 1, continuationToken: null,
    }))
    const setNotificationRead = vi.fn(async (_id: string, read: boolean) => {
      readAt = read ? '2026-10-16T12:00:00Z' : null
      return notificationItem({ readAt })
    })
    renderPage({ listNotifications, setNotificationRead })
    expect(await screen.findByRole('heading', { name: 'Request approved' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'View request' })).toHaveAttribute('href', '/requests?request=request%2F1')
    expect(screen.getByText(/Approval creates a grant; it does not mean gateway access is ready/)).toBeVisible()
    expect(screen.queryByText('user-1')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Mark read' }))
    expect(await screen.findByRole('button', { name: 'Mark unread' })).toBeEnabled()
    expect(setNotificationRead).toHaveBeenCalledWith('notice-1', true)
    await user.click(screen.getByRole('button', { name: 'Mark unread' }))
    expect(await screen.findByRole('button', { name: 'Mark read' })).toBeEnabled()
    expect(setNotificationRead).toHaveBeenLastCalledWith('notice-1', false)
    expect(listNotifications).toHaveBeenCalledTimes(3)
  })

  it('retains unread state and reports failed persistence without a success fallback', async () => {
    const user = userEvent.setup()
    renderPage({
      listNotifications: async () => ({ items: [notificationItem()], unreadCount: 1, continuationToken: null }),
      setNotificationRead: async () => { throw new Error('Service unavailable') },
    })
    await user.click(await screen.findByRole('button', { name: 'Mark read' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Read status was not saved. Service unavailable')
    expect(screen.getByRole('button', { name: 'Mark read' })).toBeEnabled()
    expect(screen.getByText('Unread')).toBeVisible()
  })

  it('paginates and resets continuation when filters change', async () => {
    const user = userEvent.setup()
    const listNotifications = vi.fn<PortalApi['listNotifications']>(async (filter) => {
      if (filter?.unreadOnly) return { items: [], unreadCount: 0, continuationToken: null }
      return filter?.continuationToken
        ? { items: [notificationItem({ id: 'notice-2', title: 'Access changed' })], unreadCount: 2, continuationToken: null }
        : { items: [notificationItem()], unreadCount: 2, continuationToken: 'next-page' }
    })
    renderPage({ listNotifications })
    await user.click(await screen.findByRole('button', { name: 'Load more notifications' }))
    expect(await screen.findByRole('heading', { name: 'Access changed' })).toBeVisible()
    expect(within(screen.getByRole('list', { name: 'Your notifications' })).getAllByRole('listitem')).toHaveLength(2)
    expect(listNotifications).toHaveBeenCalledWith(expect.objectContaining({ continuationToken: 'next-page' }))
    await user.selectOptions(screen.getByLabelText('Category'), 'accessLifecycle')
    await waitFor(() => expect(listNotifications).toHaveBeenCalledWith(expect.objectContaining({ category: 'accessLifecycle', continuationToken: undefined })))
    await user.click(screen.getByRole('checkbox', { name: 'Unread only' }))
    expect(await screen.findByText('No unread notifications')).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Request approved' })).not.toBeInTheDocument()
  })

  it('shows loading and retryable failure without invented inbox records', async () => {
    const user = userEvent.setup()
    const listNotifications = vi.fn(async () => { throw new Error('Inbox is unavailable') })
    renderPage({ listNotifications })
    expect(screen.getByText('Loading your notifications')).toBeVisible()
    expect(await screen.findByText('Inbox is unavailable')).toBeVisible()
    expect(screen.queryByText('No notifications match')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh inbox' }))
    await waitFor(() => expect(listNotifications).toHaveBeenCalledTimes(2))
  })
})
