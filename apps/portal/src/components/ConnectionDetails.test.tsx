import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  apiFailure,
  chatUrl,
  connection,
  directGrant,
  directResolved,
  endpoint,
  groupResolved,
  persistedText,
  responsesUrl,
  revealedPrimary,
} from '../test/connection'
import type { KeyRevealResult, ModelConnection, ResolvedEntitlement } from '../types'
import { ConnectionDetails } from './ConnectionDetails'

const auth = vi.hoisted(() => ({
  accounts: [{ homeAccountId: 'user-a', localAccountId: 'user-a', tenantId: 'tenant-1' }],
}))
vi.mock('@azure/msal-react', () => ({ useMsal: () => auth }))
const api = vi.hoisted(() => ({
  getMyEntitlementConnection: vi.fn(),
  revealMyEntitlementKey: vi.fn(),
}))
vi.mock('../api', () => ({ usePortalApi: () => api }))

function renderDetails(resolved: ResolvedEntitlement = directResolved) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  })
  function Navigation() {
    const navigate = useNavigate()
    return (
      <button type="button" onClick={() => void navigate('/access?view=all')}>
        Navigate within the portal
      </button>
    )
  }
  const tree = (grant: ResolvedEntitlement) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/access']}>
        <Navigation />
        <ConnectionDetails resolved={grant} />
      </MemoryRouter>
    </QueryClientProvider>
  )
  const rendered = render(tree(resolved))
  return {
    ...rendered,
    queryClient,
    rerenderDetails: (grant: ResolvedEntitlement = resolved) => rendered.rerender(tree(grant)),
  }
}

const toggle = () => screen.getByRole('button', { name: 'Connection details' })

async function openDetails(user: UserEvent, info: ModelConnection = connection) {
  api.getMyEntitlementConnection.mockResolvedValue(info)
  await user.click(toggle())
  expect(await screen.findByText(info.endpoint)).toBeVisible()
}

function section(name: string) {
  return screen.getByRole('region', { name })
}

function fact(container: HTMLElement, term: string) {
  const title = within(container).getByText(term, { selector: 'dt' })
  const value = title.nextElementSibling
  if (!(value instanceof HTMLElement)) throw new Error(`No value for ${term}`)
  return value
}

function samples() {
  return Array.from(document.querySelectorAll('pre.code-sample'), (element) => element.textContent ?? '')
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe('ConnectionDetails', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    auth.accounts = [{ homeAccountId: 'user-a', localAccountId: 'user-a', tenantId: 'tenant-1' }]
    api.revealMyEntitlementKey.mockResolvedValue(revealedPrimary)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('loads connection details only after the panel is expanded', async () => {
    const user = userEvent.setup()
    const loading = deferred<ModelConnection>()
    api.getMyEntitlementConnection.mockReturnValue(loading.promise)
    renderDetails()

    expect(toggle()).toHaveAttribute('aria-expanded', 'false')
    expect(toggle()).not.toHaveAttribute('aria-controls')
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()

    await user.click(toggle())

    expect(toggle()).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(toggle().getAttribute('aria-controls') ?? '')).toBeInTheDocument()
    expect(screen.getByText('Loading connection details')).toBeInTheDocument()
    expect(api.getMyEntitlementConnection).toHaveBeenCalledExactlyOnceWith(directGrant.id)
    await act(async () => {
      loading.resolve(connection)
    })
    expect(await screen.findByText(endpoint)).toBeVisible()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()

    await user.click(toggle())

    expect(toggle()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(endpoint)).not.toBeInTheDocument()
  })

  it('shows the endpoint, full operation URLs, deployment, and key header', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    const endpointSection = section('Endpoint')
    expect(fact(endpointSection, 'Base URL')).toHaveTextContent(endpoint)
    expect(fact(endpointSection, 'Deployment')).toHaveTextContent(/^gpt-4o$/)
    expect(fact(endpointSection, 'Key header')).toHaveTextContent(/^Ocp-Apim-Subscription-Key$/)
    const operations = within(endpointSection).getAllByRole('listitem')
    expect(operations).toHaveLength(2)
    expect(operations[0]).toHaveTextContent(`POST${chatUrl}chat-completions`)
    expect(operations[1]).toHaveTextContent(`POST${responsesUrl}responses`)
  })

  it('shows the applied access methods and the Entra details needed for a token', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    const authentication = section('Authentication')
    expect(fact(authentication, 'Subscription key')).toHaveTextContent(/^Accepted$/)
    expect(fact(authentication, 'Microsoft Entra ID token')).toHaveTextContent(/^Accepted$/)
    expect(within(authentication).getByRole('heading', { name: 'Microsoft Entra ID' })).toBeVisible()
    expect(fact(authentication, 'Tenant ID')).toHaveTextContent(/^tenant-1$/)
    expect(fact(authentication, 'Audience')).toHaveTextContent(connection.entraAudience!)
    expect(fact(authentication, 'Scope')).toHaveTextContent(connection.entraScope!)
    expect(within(authentication).queryByText('Client ID')).not.toBeInTheDocument()
  })

  it('shows the Entra client ID when the API provides one', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, entraClientId: '99999999-8888-7777-6666-555555555555' })

    expect(fact(section('Authentication'), 'Client ID')).toHaveTextContent(
      /^99999999-8888-7777-6666-555555555555$/,
    )
  })

  it('omits the Entra details when Entra ID tokens are not accepted', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, {
      ...connection,
      appliedMethods: { keysEnabled: true, entraEnabled: false },
      entraClientId: '99999999-8888-7777-6666-555555555555',
    })

    const authentication = section('Authentication')
    expect(fact(authentication, 'Subscription key')).toHaveTextContent(/^Accepted$/)
    expect(fact(authentication, 'Microsoft Entra ID token')).toHaveTextContent(/^Not accepted$/)
    expect(within(authentication).queryByRole('heading', { name: 'Microsoft Entra ID' })).not.toBeInTheDocument()
    expect(within(authentication).queryByText('Tenant ID')).not.toBeInTheDocument()
    expect(within(authentication).queryByText('Client ID')).not.toBeInTheDocument()
    expect(screen.queryByText(/To use a token instead/)).not.toBeInTheDocument()
  })

  it('shows publication and grant limits', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    const limits = section('Limits')
    expect(fact(limits, 'Publication limits')).toHaveTextContent('20,000 tokens per minute')
    expect(fact(limits, 'Publication limits')).toHaveTextContent('1,000,000 tokens per month')
    expect(fact(limits, 'Grant limits')).toHaveTextContent(/^5,000 tokens per minute$/)
    expect(within(limits).getByText(/share this grant's limits/)).toBeVisible()
  })

  it('says when the grant has no limits of its own', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, grantLimits: null })

    expect(fact(section('Limits'), 'Grant limits')).toHaveTextContent(/^No additional grant limits configured$/)
  })

  it('shows that the grant is applied to APIM', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    expect(screen.getByText('Applied to APIM')).toBeVisible()
    expect(screen.getByText(/APIM enforces this grant/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Show primary key' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Show secondary key' })).toBeEnabled()
  })

  it('shows pending changes and holds back keys until they are applied', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, runtime: { ...connection.runtime!, status: 'pending' } })

    expect(screen.getByText('APIM changes pending')).toBeVisible()
    expect(screen.getByText(/saved but not applied to APIM yet/)).toBeVisible()
    expect(
      screen.getByText('Keys are available only while this grant is applied to APIM. Current status: APIM changes pending.'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Show primary key' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Show secondary key' })).toBeDisabled()
  })

  it('reports the last APIM error for a grant that failed to apply', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, {
      ...connection,
      runtime: { ...connection.runtime!, status: 'failed', error: 'The subscription could not be created.' },
    })

    expect(screen.getByText('APIM apply failed')).toBeVisible()
    expect(screen.getByText('Last APIM error')).toBeVisible()
    expect(screen.getByText('The subscription could not be created.')).toBeVisible()
  })

  it('explains a model whose governed access is not set up yet', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, runtime: null, appliedMethods: null })

    expect(screen.getByText('Not set up in APIM')).toBeVisible()
    expect(screen.getByText(/No access methods are applied yet/)).toBeVisible()
    expect(screen.getByText('Samples appear after governed access is applied to this model.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Show primary key' })).toBeDisabled()
    expect(samples()).toHaveLength(0)
  })

  it.each([
    [403, 'Forbidden', 'Connection details are not available to you', /MOSAIC User role/],
    [404, 'Entitlement was not found', 'This grant is not available for your account', /It may have been removed/],
    [
      409,
      'Synchronize the gateway to discover its model API endpoint',
      'This model is not ready to connect',
      'Synchronize the gateway to discover its model API endpoint. Ask your administrator to finish setting it up.',
    ],
    [401, 'Not authenticated', 'Sign in again', 'Your sign-in has expired. Refresh the page to sign in again.'],
  ])('explains a %s answer without retrying it', async (status, serverMessage, title, message) => {
    const user = userEvent.setup()
    api.getMyEntitlementConnection.mockRejectedValue(apiFailure(status, serverMessage))
    renderDetails()

    await user.click(toggle())

    expect(await screen.findByText(title)).toBeVisible()
    expect(screen.getByText(message)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show primary key' })).not.toBeInTheDocument()
    expect(api.getMyEntitlementConnection).toHaveBeenCalledOnce()
  })

  it('offers another attempt after a server failure', async () => {
    const user = userEvent.setup()
    api.getMyEntitlementConnection.mockRejectedValue(apiFailure(500, 'Request failed with status 500'))
    renderDetails()

    await user.click(toggle())

    expect(await screen.findByText('Unable to load connection details')).toBeVisible()
    expect(
      screen.getByText('Something went wrong while loading connection details. Try again.'),
    ).toBeVisible()
    expect(api.getMyEntitlementConnection).toHaveBeenCalledTimes(2)

    api.getMyEntitlementConnection.mockResolvedValue(connection)
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText(endpoint)).toBeVisible()
  })

  it('explains when MOSAIC cannot be reached', async () => {
    const user = userEvent.setup()
    api.getMyEntitlementConnection.mockRejectedValue(new TypeError('Failed to fetch'))
    renderDetails()

    await user.click(toggle())

    expect(
      await screen.findByText('MOSAIC could not be reached. Check your network connection and try again.'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible()
  })

  it('explains that group grants do not issue credentials, without calling the API', async () => {
    const user = userEvent.setup()
    renderDetails(groupResolved)

    await user.click(toggle())

    expect(screen.getByText('Credentials are issued for direct grants only')).toBeVisible()
    expect(screen.getByText(/You have this access through a group/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Show primary key' })).not.toBeInTheDocument()
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
  })

  it('builds key samples from placeholders', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    const [curl, python] = samples()
    expect(curl).toContain(`curl "${chatUrl}?api-version=$MOSAIC_API_VERSION"`)
    expect(curl).toContain('-H "Ocp-Apim-Subscription-Key: $MOSAIC_API_KEY"')
    expect(curl).toContain(`-d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'`)
    expect(python).toContain(JSON.stringify(chatUrl))
    expect(python).toContain('params={"api-version": os.environ["MOSAIC_API_VERSION"]}')
    expect(python).toContain('headers={"Ocp-Apim-Subscription-Key": os.environ["MOSAIC_API_KEY"]}')
    expect(screen.getByText(/To use a token instead/)).toBeVisible()
    expect(screen.getByText(/Samples use placeholders and never include your key/)).toBeVisible()
  })

  it('builds token samples when only Entra ID tokens are accepted', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: true } })

    const [curl, python] = samples()
    expect(curl).toContain('-H "Authorization: Bearer $MOSAIC_ACCESS_TOKEN"')
    expect(python).toContain('headers={"Authorization": "Bearer " + os.environ["MOSAIC_ACCESS_TOKEN"]}')
    expect(`${curl}\n${python}`).not.toContain('MOSAIC_API_KEY')
  })

  it('uses the Responses API when chat completions are not published', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, {
      ...connection,
      operations: [
        { name: 'embeddings', method: 'POST', path: '/openai/deployments/gpt-4o/embeddings' },
        { name: 'responses', method: 'POST', path: '/openai/responses' },
      ],
    })

    const [curl, python] = samples()
    expect(curl).toContain(`curl "${responsesUrl}?api-version=$MOSAIC_API_VERSION"`)
    expect(curl).toContain(`-d '{"model": "gpt-4o", "input": "Hello"}'`)
    expect(python).toContain('"input": "Hello"')
  })

  it.each([
    [
      'every method is turned off',
      { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: false } },
      'No sample is shown because APIM denies every call to this model.',
    ],
    [
      'no published operation has a sample',
      {
        ...connection,
        operations: [{ name: 'embeddings', method: 'POST', path: '/openai/deployments/gpt-4o/embeddings' }],
      },
      'No sample is available for these operations.',
    ],
  ])('shows no sample when %s', async (_case, info, message) => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, info)

    expect(screen.getByText(message)).toBeVisible()
    expect(samples()).toHaveLength(0)
  })

  it('warns that APIM denies every call when both methods are off', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: false } })

    expect(screen.getByText('Both methods are turned off, so APIM denies every call to this model.')).toBeVisible()
    expect(fact(section('Authentication'), 'Subscription key')).toHaveTextContent(/^Not accepted$/)
  })

  it('never puts a revealed key in samples, caches, storage, the URL, or logs', async () => {
    const user = userEvent.setup()
    const logged = (['debug', 'error', 'info', 'log', 'warn'] as const).map((method) => vi.spyOn(console, method))
    const { queryClient } = renderDetails()
    await openDetails(user)

    await user.click(screen.getByRole('button', { name: 'Show primary key' }))

    expect(await screen.findByText(revealedPrimary.key)).toHaveAttribute('data-secret', 'true')
    expect(document.querySelectorAll('[data-secret]')).toHaveLength(1)
    expect(document.body.innerHTML.split(revealedPrimary.key)).toHaveLength(2)
    expect(samples().join('\n')).toContain('$MOSAIC_API_KEY')
    expect(samples().join('\n')).not.toContain(revealedPrimary.key)
    expect(persistedText(queryClient)).not.toContain(revealedPrimary.key)
    for (const spy of logged) {
      expect(spy.mock.calls.flat().map(String).join('\n')).not.toContain(revealedPrimary.key)
    }
  })

  it('clears the key when the panel is collapsed', async () => {
    const user = userEvent.setup()
    const { queryClient } = renderDetails()
    await openDetails(user)
    await user.click(screen.getByRole('button', { name: 'Show primary key' }))
    expect(await screen.findByText(revealedPrimary.key)).toBeVisible()

    await user.click(toggle())
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
    await user.click(toggle())

    expect(await screen.findByText(endpoint)).toBeVisible()
    expect(screen.getByText('Hidden')).toBeInTheDocument()
    expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
    expect(api.revealMyEntitlementKey).toHaveBeenCalledOnce()
    expect(persistedText(queryClient)).not.toContain(revealedPrimary.key)
  })

  it('abandons a pending reveal when the panel is collapsed', async () => {
    const user = userEvent.setup()
    const pending = deferred<KeyRevealResult>()
    api.revealMyEntitlementKey.mockReturnValue(pending.promise)
    renderDetails()
    await openDetails(user)
    await user.click(screen.getByRole('button', { name: 'Show primary key' }))
    const signal = api.revealMyEntitlementKey.mock.calls[0][2] as AbortSignal

    await user.click(toggle())
    expect(signal.aborted).toBe(true)
    await act(async () => {
      pending.resolve(revealedPrimary)
    })
    await user.click(toggle())

    expect(await screen.findByText(endpoint)).toBeVisible()
    expect(document.body).not.toHaveTextContent(revealedPrimary.key)
  })

  it.each(['navigation', 'account', 'grant'] as const)(
    'clears a shown key when the %s changes',
    async (change) => {
      const user = userEvent.setup()
      const rendered = renderDetails()
      await openDetails(user)
      await user.click(screen.getByRole('button', { name: 'Show primary key' }))
      expect(await screen.findByText(revealedPrimary.key)).toBeVisible()

      if (change === 'navigation') {
        await user.click(screen.getByRole('button', { name: 'Navigate within the portal' }))
      } else if (change === 'account') {
        auth.accounts = [{ homeAccountId: 'user-b', localAccountId: 'user-b', tenantId: 'tenant-1' }]
        rendered.rerenderDetails()
      } else {
        rendered.rerenderDetails({
          ...directResolved,
          entitlement: { ...directGrant, updatedAt: '2026-09-02T08:00:00Z' },
        })
      }

      expect(await screen.findByText(endpoint)).toBeVisible()
      expect(screen.queryByText(revealedPrimary.key)).not.toBeInTheDocument()
      expect(api.revealMyEntitlementKey).toHaveBeenCalledOnce()
    },
  )

  it('reloads connection details after a reveal conflict and explains the new state', async () => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey.mockRejectedValue(
      apiFailure(409, 'Apply the pending changes to this grant before revealing its key', 'conflict'),
    )
    renderDetails()
    await openDetails(user)
    api.getMyEntitlementConnection.mockResolvedValue({
      ...connection,
      runtime: { ...connection.runtime!, status: 'pending' },
    })

    await user.click(screen.getByRole('button', { name: 'Show primary key' }))

    expect(
      await screen.findByText('Keys are available only while this grant is applied to APIM. Current status: APIM changes pending.'),
    ).toBeVisible()
    expect(api.getMyEntitlementConnection).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: 'Show primary key' })).toBeDisabled()
  })
})
