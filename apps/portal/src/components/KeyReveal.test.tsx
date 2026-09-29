import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { KEY_VISIBLE_MS } from '../connection-format'
import {
  apiFailure,
  connection,
  directGrant,
  persistedText,
  revealedPrimary,
  revealedSecondary,
  securityGroupConnection,
} from '../test/connection'
import type { KeyRevealResult, ModelConnection } from '../types'
import { KeyReveal } from './KeyReveal'

const api = vi.hoisted(() => ({
  getMyEntitlementConnection: vi.fn(),
  revealMyEntitlementKey: vi.fn(),
}))
vi.mock('../api', () => ({ usePortalApi: () => api }))

function renderKeyReveal(info: ModelConnection = connection) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onConflict = vi.fn()
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <KeyReveal entitlementId={directGrant.id} connection={info} onConflict={onConflict} />
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient, onConflict }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function spyOnConsole() {
  const spies = (['debug', 'error', 'info', 'log', 'warn'] as const).map((method) =>
    vi.spyOn(console, method),
  )
  return () =>
    spies
      .flatMap((spy) => spy.mock.calls.flat())
      .map((argument: unknown) => {
        if (typeof argument === 'string') return argument
        try {
          return JSON.stringify(argument)
        } catch {
          return String(argument)
        }
      })
      .join('\n')
}

const showPrimary = () => screen.getByRole('button', { name: 'Show primary key' })
const showSecondary = () => screen.getByRole('button', { name: 'Show secondary key' })

describe('KeyReveal', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.revealMyEntitlementKey.mockResolvedValue(revealedPrimary)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('keeps the key masked and requests nothing until a key is asked for', async () => {
    const user = userEvent.setup()
    const logs = spyOnConsole()
    const { queryClient } = renderKeyReveal()

    expect(screen.getByText('Hidden')).toBeInTheDocument()
    expect(document.querySelector('[data-secret]')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Hide key' })).not.toBeInTheDocument()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()

    await user.click(showPrimary())

    expect(api.revealMyEntitlementKey).toHaveBeenCalledWith(directGrant.id, 'primary', expect.any(AbortSignal))
    const secret = await screen.findByText(revealedPrimary.key)
    expect(secret).toBeVisible()
    expect(secret).toHaveAttribute('data-secret', 'true')
    expect(screen.getByText('Primary key')).toBeVisible()
    expect(screen.getByText('Primary key shown. It hides automatically after 60 seconds.')).toBeInTheDocument()
    expect(document.body.innerHTML.split(revealedPrimary.key)).toHaveLength(2)
    expect(persistedText(queryClient)).not.toContain(revealedPrimary.key)
    expect(logs()).not.toContain(revealedPrimary.key)
  })

  it('reveals the secondary key on request', async () => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey.mockResolvedValue(revealedSecondary)
    const { queryClient } = renderKeyReveal()

    await user.click(showSecondary())

    expect(api.revealMyEntitlementKey).toHaveBeenCalledWith(directGrant.id, 'secondary', expect.any(AbortSignal))
    expect(await screen.findByText(revealedSecondary.key)).toHaveAttribute('data-secret', 'true')
    expect(screen.getByText('Secondary key')).toBeVisible()
    expect(persistedText(queryClient)).not.toContain(revealedSecondary.key)
  })

  it('shows progress and keeps focus while the key is retrieved, without a second request', async () => {
    const user = userEvent.setup()
    const pending = deferred<KeyRevealResult>()
    api.revealMyEntitlementKey.mockReturnValue(pending.promise)
    renderKeyReveal()

    await user.click(showPrimary())

    expect(screen.getByText('Retrieving the current key from APIM')).toBeInTheDocument()
    expect(showPrimary()).toHaveAttribute('aria-disabled', 'true')
    expect(showPrimary()).toHaveFocus()
    await user.click(showSecondary())
    expect(api.revealMyEntitlementKey).toHaveBeenCalledOnce()

    await act(async () => {
      pending.resolve(revealedPrimary)
    })

    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()
    expect(showPrimary()).not.toHaveAttribute('aria-disabled', 'true')
  })

  it('hides the key on request and returns focus to the button that showed it', async () => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey.mockResolvedValue(revealedSecondary)
    renderKeyReveal()
    await user.click(showSecondary())
    expect(await screen.findByText(revealedSecondary.key)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Hide key' }))

    expect(screen.queryByText(revealedSecondary.key)).not.toBeInTheDocument()
    expect(document.querySelector('[data-secret]')).toBeNull()
    expect(screen.getByText('Key hidden.')).toBeInTheDocument()
    expect(showSecondary()).toHaveFocus()
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument()
  })

  it('copies the key only when asked', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    renderKeyReveal()
    await user.click(showPrimary())
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()
    expect(copy).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Copy key' }))

    expect(copy).toHaveBeenCalledWith(revealedPrimary.key)
    expect(
      await screen.findByText('Key copied. The clipboard is sensitive and is not cleared automatically.'),
    ).toBeInTheDocument()
  })

  it('explains a clipboard failure and leaves the key visible to copy by hand', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('Denied'))
    renderKeyReveal()
    await user.click(showPrimary())
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Copy key' }))

    expect(await screen.findByText(/Could not copy the key/)).toBeVisible()
    expect(screen.queryByText(/Key copied/)).not.toBeInTheDocument()
    expect(screen.getByText(revealedPrimary.key)).toBeVisible()
  })

  it('hides the key automatically after 60 seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    renderKeyReveal()
    await user.click(showPrimary())
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()
    act(() => screen.getByRole('button', { name: 'Copy key' }).focus())

    act(() => {
      vi.advanceTimersByTime(KEY_VISIBLE_MS - 1_000)
    })
    expect(screen.getByText(revealedPrimary.key)).toBeVisible()

    act(() => {
      vi.advanceTimersByTime(1_000)
    })
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
    expect(document.querySelector('[data-secret]')).toBeNull()
    expect(screen.getByText('Key hidden automatically after 60 seconds.')).toBeInTheDocument()
    expect(showPrimary()).toHaveFocus()
  })

  it('aborts a pending request on unmount and ignores its late answer', async () => {
    const user = userEvent.setup()
    const pending = deferred<KeyRevealResult>()
    api.revealMyEntitlementKey.mockReturnValue(pending.promise)
    const { unmount, queryClient } = renderKeyReveal()
    await user.click(showPrimary())
    const signal = api.revealMyEntitlementKey.mock.calls[0][2] as AbortSignal
    expect(signal.aborted).toBe(false)

    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => {
      pending.resolve(revealedPrimary)
    })

    expect(document.body).not.toHaveTextContent(revealedPrimary.key)
    expect(persistedText(queryClient)).not.toContain(revealedPrimary.key)
  })

  it('removes a shown key, and abandons a pending one, before the page is hidden', async () => {
    const user = userEvent.setup()
    renderKeyReveal()
    await user.click(showPrimary())
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()

    act(() => {
      window.dispatchEvent(new Event('pagehide'))
    })
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()

    const pending = deferred<KeyRevealResult>()
    api.revealMyEntitlementKey.mockReturnValue(pending.promise)
    await user.click(showSecondary())
    const signal = api.revealMyEntitlementKey.mock.calls[1][2] as AbortSignal
    act(() => {
      window.dispatchEvent(new Event('pagehide'))
    })
    expect(signal.aborted).toBe(true)
    await act(async () => {
      pending.resolve(revealedSecondary)
    })
    expect(screen.queryByText(revealedSecondary.key)).not.toBeInTheDocument()
    expect(document.querySelector('[data-secret]')).toBeNull()
  })

  it.each([
    ['another slot', { ...revealedPrimary, slot: 'secondary' as const }],
    ['another grant', { ...revealedPrimary, entitlementId: 'entitlement_other' }],
    ['another subscription', { ...revealedPrimary, subscriptionName: 'other-subscription' }],
    ['an empty key', { ...revealedPrimary, key: '' }],
  ])('does not show a key returned for %s', async (_case, result) => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey.mockResolvedValue(result)
    renderKeyReveal()

    await user.click(showPrimary())

    expect(
      await screen.findByText('The service returned unexpected credential metadata. No key was displayed.'),
    ).toBeVisible()
    expect(document.querySelector('[data-secret]')).toBeNull()
    expect(document.body).not.toHaveTextContent(revealedPrimary.key)
  })

  it.each([
    [
      apiFailure(409, 'Apply the pending changes to this grant before revealing its key', 'conflict'),
      'The key is not available right now',
      'Apply the pending changes to this grant before revealing its key.',
    ],
    [
      apiFailure(403, 'API Management denied access', 'gateway_forbidden'),
      'MOSAIC cannot read this key',
      /not permitted to read subscription keys from API Management/,
    ],
    [
      apiFailure(404, 'API Management resource was not found', 'gateway_not_found'),
      'Key not found in API Management',
      /could not find the subscription for this grant/,
    ],
    [
      apiFailure(403, 'Forbidden', 'forbidden'),
      'You cannot reveal this key',
      'Your account is not allowed to reveal keys for this grant.',
    ],
    [
      apiFailure(404, 'Entitlement was not found', 'not_found'),
      'This grant is not available for your account',
      'MOSAIC could not find this grant for your account. Refresh My access.',
    ],
    [
      apiFailure(502, 'API Management could not be reached', 'gateway_unreachable'),
      'Could not retrieve the key',
      'API Management did not return the key. Try again in a moment.',
    ],
    [
      new TypeError('Failed to fetch'),
      'Could not show the key',
      'MOSAIC could not be reached. Check your network connection and try again.',
    ],
  ])('explains a failed reveal (%#)', async (failure, title, message) => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey.mockRejectedValue(failure)
    const { onConflict } = renderKeyReveal()

    await user.click(showPrimary())

    expect(await screen.findByText(title)).toBeVisible()
    expect(screen.getByText(message)).toBeVisible()
    expect(document.querySelector('[data-secret]')).toBeNull()
    expect(onConflict).toHaveBeenCalledTimes('status' in failure && failure.status === 409 ? 1 : 0)
    expect(showPrimary()).toBeEnabled()
  })

  it('clears an earlier key when a later reveal fails', async () => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey
      .mockResolvedValueOnce(revealedPrimary)
      .mockRejectedValueOnce(apiFailure(502, 'API Management could not be reached', 'gateway_unreachable'))
    renderKeyReveal()
    await user.click(showPrimary())
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()

    await user.click(showSecondary())

    expect(await screen.findByText('Could not retrieve the key')).toBeVisible()
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument()
  })

  it.each([
    [
      'the grant is not applied yet',
      { ...connection, runtime: { ...connection.runtime!, status: 'pending' as const } },
      'Keys are available only while this grant is applied to APIM. Current status: APIM changes pending.',
    ],
    [
      'the grant failed to apply',
      { ...connection, runtime: { ...connection.runtime!, status: 'failed' as const } },
      'Keys are available only while this grant is applied to APIM. Current status: APIM apply failed.',
    ],
    [
      'governed access is not set up',
      { ...connection, runtime: null, appliedMethods: null },
      'Keys become available after an administrator applies governed access for this model.',
    ],
    [
      'keys are turned off',
      { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: true } },
      'Subscription keys are turned off for this model. Use a Microsoft Entra ID token instead.',
    ],
    [
      'the grant was applied without keys',
      {
        ...connection,
        runtime: { ...connection.runtime!, appliedMethods: { keysEnabled: false, entraEnabled: true } },
      },
      'Subscription keys are turned off for this model. Use a Microsoft Entra ID token instead.',
    ],
    [
      'every method is turned off',
      { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: false } },
      'Subscription keys are turned off for this model, and APIM currently denies every call.',
    ],
    [
      'the details describe another grant',
      { ...connection, entitlementId: 'entitlement_other' },
      'The connection details do not match this grant. Refresh My access.',
    ],
  ])('does not offer keys when %s', (_case, info, reason) => {
    renderKeyReveal(info)

    expect(screen.getByText(reason)).toBeVisible()
    expect(showPrimary()).toBeDisabled()
    expect(showSecondary()).toBeDisabled()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
  })

  it('does not offer key reveal for group grants without keys', () => {
    renderKeyReveal({ ...securityGroupConnection, entitlementId: directGrant.id })

    expect(
      screen.getByText(
        "Access granted to a group uses Microsoft Entra sign-in only, so there's no key. Sign in with your own account to get a token.",
      ),
    ).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Show primary key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show secondary key' })).not.toBeInTheDocument()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
  })
})
