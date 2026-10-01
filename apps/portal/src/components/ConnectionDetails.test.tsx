import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  apiFailure,
  chatUrl,
  claudeConnection,
  claudeEndpoint,
  connection,
  directGrant,
  directResolved,
  endpoint,
  groupResolved,
  mcpAgentIdentityConnection,
  mcpAgentIdentityResolved,
  mcpAgentUserConnection,
  mcpAgentUserResolved,
  mcpConnection,
  mcpMetadataUrl,
  mcpMosaicGroupResolved,
  mcpResolved,
  mcpSecurityGroupConnection,
  mcpSecurityGroupResolved,
  mcpServerUrl,
  messagesUrl,
  modelClientId,
  persistedText,
  responsesUrl,
  revealedPrimary,
  securityGroupConnection,
  securityGroupResolved,
} from '../test/connection'
import type { KeyRevealResult, ModelConnection, ResolvedEntitlement } from '../types'
import { ConnectionDetails } from './ConnectionDetails'

const auth = vi.hoisted(() => ({
  accounts: [{ homeAccountId: 'user-a', localAccountId: 'user-a', tenantId: 'tenant-1' }],
}))
vi.mock('@azure/msal-react', () => ({ useMsal: () => auth }))
const api = vi.hoisted(() => ({
  getMyEntitlementConnection: vi.fn(),
  getMcpConnection: vi.fn(),
  revealMyEntitlementKey: vi.fn(),
  createMyEntitlementKey: vi.fn(),
  rotateMyEntitlementKey: vi.fn(),
  deleteMyEntitlementKey: vi.fn(),
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

async function openMcpDetails(user: UserEvent, info = mcpConnection, resolved: ResolvedEntitlement = mcpResolved) {
  api.getMcpConnection.mockResolvedValue(info)
  renderDetails(resolved)
  await user.click(toggle())
  expect(await screen.findByText(info.statusMessage)).toBeVisible()
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

function sampleHeadings() {
  return within(section('Code samples'))
    .getAllByRole('heading', { level: 4 })
    .map((heading) => heading.textContent)
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

  it('shows the cost center header with a copy-ready Entra token curl', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    renderDetails()
    await openDetails(user)

    const header = section('Cost center header')
    expect(fact(header, 'Header name')).toHaveTextContent('x-mosaic-cost-center')
    expect(fact(header, 'Header value')).toHaveTextContent('RES')
    expect(header).toHaveTextContent(/Without the header, the gateway uses your grant under your default cost center first/)
    expect(header).toHaveTextContent('A key always charges its own grant, so a key needs no header.')
    expect(within(header).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'x-mosaic-remaining-tokens Your tokens this minute',
      'x-mosaic-remaining-quota-tokens Your token quota',
      'x-mosaic-remaining-calls Your calls in this rate window',
      "x-mosaic-cost-center-remaining-quota-tokens The cost center's pooled tokens this month, shared with everyone who charges it",
    ])
    const tokenCurl = header.querySelector('pre.header-sample')?.textContent ?? ''
    expect(tokenCurl).toContain(`curl "${chatUrl}?api-version=$MOSAIC_API_VERSION"`)
    expect(tokenCurl).toContain(`-H "Authorization: ${'Bearer'} $MOSAIC_ACCESS_TOKEN"`)
    expect(tokenCurl).toContain('-H "x-mosaic-cost-center: RES"')
    expect(tokenCurl).not.toContain('MOSAIC_API_KEY')
    // A key charges its own grant's cost center, so the key samples send no header.
    expect(samples().join('\n')).not.toContain('x-mosaic-cost-center')

    await user.click(screen.getByRole('button', { name: 'Copy cost center header value' }))
    expect(copy).toHaveBeenCalledWith('RES')
    await user.click(screen.getByRole('button', { name: 'Copy curl with the cost center header' }))
    expect(copy).toHaveBeenLastCalledWith(tokenCurl)
  })

  it('leaves the token curl to the code samples when only tokens are accepted', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: true } })

    const header = section('Cost center header')
    expect(header.querySelector('pre.header-sample')).toBeNull()
    expect(samples().join('\n')).toContain('-H "x-mosaic-cost-center: RES"')
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
    expect(fact(authentication, 'Client ID')).toHaveTextContent(new RegExp(`^${modelClientId}$`))
    expect(fact(authentication, 'Scope')).toHaveTextContent(connection.entraScope!)
    expect(fact(authentication, 'Audience')).toHaveTextContent(connection.entraAudience!)
    const entraFacts = authentication.querySelectorAll('dl')[1]
    expect(Array.from(entraFacts?.querySelectorAll('dt') ?? [], (term) => term.textContent)).toEqual([
      'Tenant ID',
      'Client ID',
      'Scope',
      'Audience',
    ])
    expect(within(authentication).getByText(/^Sign in with the client ID above and request the scope/)).toBeVisible()
    expect(within(authentication).queryByText(/Ask an administrator/)).not.toBeInTheDocument()
  })

  it.each([
    ['null', null],
    ['omitted by an older API', undefined],
  ])('asks for an administrator instead of showing a client ID when it is %s', async (_case, entraClientId) => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, entraClientId })

    const authentication = section('Authentication')
    expect(fact(authentication, 'Tenant ID')).toHaveTextContent(/^tenant-1$/)
    expect(fact(authentication, 'Scope')).toHaveTextContent(connection.entraScope!)
    expect(within(authentication).queryByText('Client ID')).not.toBeInTheDocument()
    expect(within(authentication).getByText(/^Request an access token for the scope above\./)).toBeVisible()
    expect(
      within(authentication).getByText(
        "MOSAIC has no client ID for you to sign in with for this grant. Ask an administrator which client ID to use, or to reapply this model's access.",
      ),
    ).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Get a token (Python)' })).not.toBeInTheDocument()
    expect(samples()).toHaveLength(2)
  })

  it('omits the Entra details when Entra ID tokens are not accepted', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, {
      ...connection,
      appliedMethods: { keysEnabled: true, entraEnabled: false },
    })

    const authentication = section('Authentication')
    expect(fact(authentication, 'Subscription key')).toHaveTextContent(/^Accepted$/)
    expect(fact(authentication, 'Microsoft Entra ID token')).toHaveTextContent(/^Not accepted$/)
    expect(within(authentication).queryByRole('heading', { name: 'Microsoft Entra ID' })).not.toBeInTheDocument()
    expect(within(authentication).queryByText('Tenant ID')).not.toBeInTheDocument()
    expect(within(authentication).queryByText('Client ID')).not.toBeInTheDocument()
    expect(screen.queryByText(/To use a token instead/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Get a token (Python)' })).not.toBeInTheDocument()
    expect(samples()).toHaveLength(2)
  })

  it('shows publication and grant limits', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    const limits = section('Limits')
    expect(fact(limits, 'Publication limits')).toHaveTextContent('20,000 tokens per minute')
    expect(fact(limits, 'Publication limits')).toHaveTextContent('1,000,000 tokens per month')
    expect(fact(limits, 'Grant limits')).toHaveTextContent(/^5,000 tokens per minute$/)
    expect(within(limits).getByText(/share this grant's limits/)).toHaveTextContent(
      /Publication limits apply as well and are counted separately\.$/,
    )
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

  it('explains a failed apply without showing the raw APIM error an older API still sends', async () => {
    const user = userEvent.setup()
    renderDetails()
    const apimError =
      'policyFragment mosaic-contoso-chat: The Azure operation did not succeed (Failed). ValidationError: Subscription mosaic-grant-contoso-other is not valid for this API.'
    await openDetails(user, {
      ...connection,
      runtime: { ...connection.runtime!, status: 'failed', error: apimError },
    })

    expect(screen.getByText('APIM apply failed')).toBeVisible()
    expect(
      screen.getByText('The last attempt to apply this grant to APIM failed. Ask your administrator to retry it.'),
    ).toBeVisible()
    expect(
      screen.getByText('Keys are available only while this grant is applied to APIM. Current status: APIM apply failed.'),
    ).toBeVisible()
    expect(screen.queryByText('Last APIM error')).not.toBeInTheDocument()
    expect(screen.queryByText(apimError)).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/mosaic-contoso|ValidationError/)
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
    expect(document.activeElement).toHaveClass('connection-status')
    expect(document.activeElement).toHaveTextContent('Applied to APIM')
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

  it('explains that MOSAIC group grants on an MCP server have no connection details, without calling the API', async () => {
    const user = userEvent.setup()
    renderDetails(mcpMosaicGroupResolved)

    await user.click(toggle())

    expect(screen.getByText('Connection details are for direct and Entra group grants')).toBeVisible()
    expect(screen.getByText(/through a MOSAIC group, which the gateway doesn't enforce/)).toBeVisible()
    expect(api.getMcpConnection).not.toHaveBeenCalled()
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
  })

  it('loads security-group grant connection details with Entra-only authentication', async () => {
    const user = userEvent.setup()
    renderDetails(securityGroupResolved)
    await openDetails(user, securityGroupConnection)

    const authentication = section('Authentication')
    expect(fact(authentication, 'Subscription key')).toHaveTextContent(/^Not accepted$/)
    expect(fact(authentication, 'Microsoft Entra ID token')).toHaveTextContent(/^Accepted$/)
    expect(fact(authentication, 'Client ID')).toHaveTextContent(new RegExp(`^${modelClientId}$`))
    expect(fact(authentication, 'Scope')).toHaveTextContent(securityGroupConnection.entraScope!)
    expect(
      screen.getByText(
        "Access granted to a group uses Microsoft Entra sign-in only, so there's no key. Sign in with your own account to get a token.",
      ),
    ).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Show primary key' })).not.toBeInTheDocument()
    expect(sampleHeadings()).toEqual(['Get a token (Python)', 'curl (bash)', 'Python'])
    expect(samples().join('\n')).toContain('MOSAIC_ACCESS_TOKEN')
    expect(samples().join('\n')).not.toContain('MOSAIC_API_KEY')
    expect(api.getMyEntitlementConnection).toHaveBeenCalledExactlyOnceWith(securityGroupResolved.entitlement.id)
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
  })

  it('shows the collapsed agent and app note for security-group grants', async () => {
    const user = userEvent.setup()
    renderDetails(securityGroupResolved)
    await openDetails(user, securityGroupConnection)

    const note = screen.getByText('Agents and apps in this group')
    expect(note).toBeVisible()
    expect(note.closest('details')).not.toHaveAttribute('open')
    expect(screen.getByText(securityGroupConnection.entraApplicationScope!)).toBeInTheDocument()
    expect(screen.getByText(securityGroupConnection.requiredAppRole!)).toBeInTheDocument()
  })

  it('shows MCP connection details for a person, including copyable URL, VS Code snippet, limits, and metadata', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    await openMcpDetails(user)

    expect(api.getMcpConnection).toHaveBeenCalledExactlyOnceWith(mcpResolved.entitlement.id)
    expect(api.getMyEntitlementConnection).not.toHaveBeenCalled()
    expect(api.revealMyEntitlementKey).not.toHaveBeenCalled()
    expect(screen.getByText('Enforced by the gateway')).toBeVisible()
    const server = section('Server')
    expect(fact(server, 'Server URL')).toHaveTextContent(mcpServerUrl)
    expect(fact(server, 'Transport')).toHaveTextContent('streamable')
    expect(screen.getByText('VS Code')).toBeVisible()
    expect(screen.getByText(/VS Code signs in with Microsoft Entra ID/)).toHaveTextContent(
      mcpConnection.delegatedScope!,
    )
    const snippet = samples()[0]
    expect(snippet).toContain('"Weather tools"')
    expect(snippet).toContain(`"url": "${mcpServerUrl}"`)
    expect(snippet).toContain('"x-mosaic-cost-center": "RES"')
    expect(fact(section('Cost center header'), 'Header value')).toHaveTextContent('RES')
    expect(fact(section('Authentication'), 'Delegated scope')).toHaveTextContent(mcpConnection.delegatedScope!)
    expect(fact(section('Authentication'), 'Client ID')).toHaveTextContent(modelClientId)
    expect(section('Call limits')).toHaveTextContent('120 calls per 60 seconds')
    expect(section('Call limits')).toHaveTextContent('10,000 calls per month')
    expect(section('Call limits')).toHaveTextContent('MCP grants are limited by calls, not tokens.')

    await user.click(screen.getByText('Advanced details'))
    expect(fact(section('Advanced details'), 'Resource metadata URL')).toHaveTextContent(mcpMetadataUrl)

    await user.click(screen.getByRole('button', { name: 'Copy Server URL' }))
    expect(copy).toHaveBeenCalledWith(mcpServerUrl)
    expect(await screen.findByText('Server URL copied.')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Copy VS Code snippet' }))
    expect(copy).toHaveBeenLastCalledWith(snippet)
  })

  it('shows MCP client-credentials guidance for an agent identity', async () => {
    const user = userEvent.setup()
    await openMcpDetails(user, mcpAgentIdentityConnection, mcpAgentIdentityResolved)

    const authentication = section('Authentication')
    expect(fact(authentication, 'Application scope')).toHaveTextContent(mcpAgentIdentityConnection.applicationScope!)
    expect(fact(authentication, 'Required app role')).toHaveTextContent(mcpAgentIdentityConnection.requiredAppRole!)
    expect(fact(authentication, 'Client ID')).toHaveTextContent(mcpAgentIdentityConnection.clientId!)
    expect(screen.getByText(/Agent identities and applications request the application scope/)).toHaveTextContent(
      /Assign Mcp\.Invoke\.Application to the agent, application, or agent blueprint/,
    )
    expect(screen.queryByText('VS Code')).not.toBeInTheDocument()
  })

  it('shows MCP delegated guidance for an agent user with the parent agent client ID', async () => {
    const user = userEvent.setup()
    await openMcpDetails(user, mcpAgentUserConnection, mcpAgentUserResolved)

    expect(fact(section('Authentication'), 'Client ID')).toHaveTextContent(mcpAgentUserConnection.clientId!)
    expect(screen.getByText(/This is the parent agent identity client ID/)).toBeVisible()
    expect(screen.getByText('VS Code')).toBeVisible()
  })

  it('shows MCP security-group grants as via group with separate member limits and direct app-role guidance', async () => {
    const user = userEvent.setup()
    await openMcpDetails(user, mcpSecurityGroupConnection, mcpSecurityGroupResolved)

    expect(screen.getByText('Access is via group')).toBeVisible()
    expect(screen.getByText(/AI builders grants this access/)).toHaveTextContent(
      /Each member gets the group's limits separately/,
    )
    expect(screen.getByText(/AI builders grants this access/)).toHaveTextContent(
      /App roles are not inherited through groups/,
    )
    expect(fact(section('Authentication'), 'Application scope')).toHaveTextContent(mcpSecurityGroupConnection.applicationScope!)
    expect(fact(section('Authentication'), 'Required app role')).toHaveTextContent(mcpSecurityGroupConnection.requiredAppRole!)
  })

  it('explains an MCP connection whose server URL is not known yet', async () => {
    const user = userEvent.setup()
    await openMcpDetails(user, { ...mcpConnection, serverUrl: null, resourceMetadataUrl: null, enforced: false })

    expect(screen.getByText('Recorded, not enforced')).toBeVisible()
    expect(fact(section('Server'), 'Server URL')).toHaveTextContent("The gateway URL isn't known yet.")
    expect(screen.queryByText('VS Code')).not.toBeInTheDocument()
    await user.click(screen.getByText('Advanced details'))
    expect(fact(section('Advanced details'), 'Resource metadata URL')).toHaveTextContent('Not available yet')
  })

  it('explains an MCP connection error without showing model credentials', async () => {
    const user = userEvent.setup()
    api.getMcpConnection.mockRejectedValue(apiFailure(409, 'Apply this MCP server before connecting'))
    renderDetails(mcpResolved)

    await user.click(toggle())

    expect(await screen.findByText('This MCP server is not ready to connect')).toBeVisible()
    expect(screen.getByText('Apply this MCP server before connecting. Ask your administrator to finish setting it up.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Show primary key' })).not.toBeInTheDocument()
  })

  it('builds key samples from placeholders', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    expect(sampleHeadings()).toEqual(['curl (bash)', 'Python', 'Get a token (Python)'])
    const [curl, python, token] = samples()
    expect(token).toContain('msal.PublicClientApplication(')
    expect(curl).toContain(`curl "${chatUrl}?api-version=$MOSAIC_API_VERSION"`)
    expect(curl).toContain('-H "Ocp-Apim-Subscription-Key: $MOSAIC_API_KEY"')
    expect(curl).not.toContain('x-mosaic-cost-center')
    expect(curl).toContain(`-d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'`)
    expect(python).toContain(JSON.stringify(chatUrl))
    expect(python).toContain('params={"api-version": os.environ["MOSAIC_API_VERSION"]}')
    expect(python).toContain('"Ocp-Apim-Subscription-Key": os.environ["MOSAIC_API_KEY"]')
    expect(python).not.toContain('x-mosaic-cost-center')
    expect(screen.getByText(/To use a token instead/)).toBeVisible()
    expect(screen.getByText(/Samples use placeholders and never include your key/)).toBeVisible()
    expect(screen.queryByText(/uses the Anthropic Messages API/)).not.toBeInTheDocument()
  })

  it('builds token samples when only Entra ID tokens are accepted', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...connection, appliedMethods: { keysEnabled: false, entraEnabled: true } })

    expect(sampleHeadings()).toEqual(['Get a token (Python)', 'curl (bash)', 'Python'])
    const [token, curl, python] = samples()
    expect(token).toContain('msal.PublicClientApplication(')
    expect(curl).toContain('-H "Authorization:')
    expect(curl).toContain('-H "x-mosaic-cost-center: RES"')
    expect(python).toContain('"Authorization": "Bearer " + os.environ["MOSAIC_ACCESS_TOKEN"]')
    expect(python).toContain('"x-mosaic-cost-center": "RES"')
    expect(`${token}\n${curl}\n${python}`).not.toContain('MOSAIC_API_KEY')
  })

  it('builds a device code sign-in sample from the non-secret sign-in IDs', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user)

    expect(samples()[2]).toBe(
      [
        'import sys',
        '',
        'import msal',
        '',
        'app = msal.PublicClientApplication(',
        `    "${modelClientId}",`,
        '    authority="https://login.microsoftonline.com/tenant-1",',
        ')',
        'flow = app.initiate_device_flow(scopes=["api://11111111-2222-3333-4444-555555555555/Models.Invoke"])',
        'if "user_code" not in flow:',
        '    sys.exit(flow.get("error_description", "Device code sign-in could not start"))',
        'print(flow["message"], file=sys.stderr)',
        'result = app.acquire_token_by_device_flow(flow)',
        'if "access_token" not in result:',
        `    sys.exit(f"{result.get('error')}: {result.get('error_description')}")`,
        'print(result["access_token"])',
      ].join('\n'),
    )
    expect(screen.getByText(/Signs you in with a device code and prints an access token/)).toHaveTextContent(
      'export MOSAIC_ACCESS_TOKEN="$(python get_token.py)"',
    )
  })

  it('still shows how to get a token when no operation has a call sample', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, {
      ...connection,
      operations: [{ name: 'embeddings', method: 'POST', path: '/openai/deployments/gpt-4o/embeddings' }],
    })

    expect(screen.getByText('No sample is available for these operations.')).toBeVisible()
    expect(sampleHeadings()).toEqual(['Get a token (Python)'])
    expect(samples()).toHaveLength(1)
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

  it('builds Anthropic Messages samples without an API version for a Claude model', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, claudeConnection)

    expect(within(section('Endpoint')).getByRole('listitem')).toHaveTextContent(`POST${messagesUrl}messages`)
    expect(sampleHeadings()).toEqual(['curl (bash)', 'Python', 'Get a token (Python)'])
    const [curl, python] = samples()
    expect(curl).toBe(
      [
        `curl "${messagesUrl}" \\`,
        '  -H "Ocp-Apim-Subscription-Key: $MOSAIC_API_KEY" \\',
        '  -H "Content-Type: application/json" \\',
        `  -d '{"model": "claude-sonnet-4-5", "max_tokens": 256, "messages": [{"role": "user", "content": "Hello"}]}'`,
      ].join('\n'),
    )
    expect(python).toContain(JSON.stringify(messagesUrl))
    expect(python).toContain('        "max_tokens": 256,')
    expect(python).not.toContain('params=')
    expect(section('Code samples')).not.toHaveTextContent('MOSAIC_API_VERSION')
    expect(screen.getByText(/Samples use placeholders/)).toHaveTextContent(
      'Set MOSAIC_API_KEY to a key shown above. Samples use placeholders and never include your key.',
    )
    expect(screen.getByText(/uses the Anthropic Messages API/)).toHaveTextContent(
      `use ${claudeEndpoint}/anthropic as the base URL. The gateway removes x-api-key, so send a key in the Ocp-Apim-Subscription-Key header, or pass a token as auth_token.`,
    )
  })

  it('builds Anthropic Messages token samples without an API version', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...claudeConnection, appliedMethods: { keysEnabled: false, entraEnabled: true } })

    expect(sampleHeadings()).toEqual(['Get a token (Python)', 'curl (bash)', 'Python'])
    const [, curl, python] = samples()
    expect(curl).toContain(`curl "${messagesUrl}" \\`)
    expect(`${curl}\n${python}`).not.toContain('api-version')
    expect(`${curl}\n${python}`).not.toContain('MOSAIC_API_KEY')
    expect(screen.getByText(/Samples use placeholders/)).toHaveTextContent(
      'Set MOSAIC_ACCESS_TOKEN to an access token for the scope above. Samples use placeholders and never include your key.',
    )
    expect(screen.getByText(/uses the Anthropic Messages API/)).toHaveTextContent(
      /use \S+\/anthropic as the base URL and pass a token as auth_token\.$/,
    )
    expect(screen.getByText(/uses the Anthropic Messages API/)).not.toHaveTextContent('x-api-key')
  })

  it('tells key-only Anthropic SDK callers where the key goes', async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, { ...claudeConnection, appliedMethods: { keysEnabled: true, entraEnabled: false } })

    expect(screen.getByText(/uses the Anthropic Messages API/)).toHaveTextContent(
      /The gateway removes x-api-key, so send a key in the Ocp-Apim-Subscription-Key header\.$/,
    )
  })

  it("explains that the gateway's tier can't apply token limits to a Claude model", async () => {
    const user = userEvent.setup()
    renderDetails()
    await openDetails(user, claudeConnection)

    const limits = section('Limits')
    expect(fact(limits, 'Publication limits')).toHaveTextContent(
      /^Token limits are unavailable for this model on this gateway's tier$/,
    )
    expect(fact(limits, 'Grant limits')).toHaveTextContent(/^60 calls per 60 seconds$/)
    expect(within(limits).getByText(/share this grant's limits/)).toHaveTextContent(
      /^Your primary key, secondary key, and Entra tokens share this grant's limits\.$/,
    )
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
        entraClientId: null,
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
    expect(samples()).toHaveLength(3)
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
      // Focus that was outside the panel stays put; focus inside it is never dropped to the page.
      if (change === 'navigation') {
        expect(screen.getByRole('button', { name: 'Navigate within the portal' })).toHaveFocus()
      } else if (change === 'account') {
        expect(document.activeElement).toHaveClass('connection-status')
      } else {
        expect(screen.getByRole('heading', { name: 'Subscription key' })).toHaveFocus()
      }
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

    const reason =
      'Keys are available only while this grant is applied to APIM. Current status: APIM changes pending.'
    expect(await screen.findByText(reason)).toBeVisible()
    expect(api.getMyEntitlementConnection).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: 'Show primary key' })).toBeDisabled()
    expect(screen.getByText(reason)).toHaveFocus()
  })

  it('keeps keyboard focus in the panel when the reload after a conflict fails', async () => {
    const user = userEvent.setup()
    api.revealMyEntitlementKey.mockRejectedValue(
      apiFailure(409, 'Apply the pending changes to this grant before revealing its key', 'conflict'),
    )
    renderDetails()
    await openDetails(user)
    api.getMyEntitlementConnection.mockRejectedValue(apiFailure(404, 'Entitlement was not found'))

    await user.click(screen.getByRole('button', { name: 'Show primary key' }))

    expect(
      await screen.findByRole('group', { name: 'This grant is not available for your account' }),
    ).toHaveFocus()
    expect(screen.queryByRole('button', { name: 'Show primary key' })).not.toBeInTheDocument()
  })
})
