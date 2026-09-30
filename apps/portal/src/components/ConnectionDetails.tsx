import { useMsal } from '@azure/msal-react'
import {
  Badge,
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Text,
} from '@fluentui/react-components'
import { ChevronDownRegular, ChevronUpRegular, CopyRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { usePortalApi } from '../api'
import {
  buildSamples,
  buildTokenSample,
  connectionProblem,
  describeConnectionRuntime,
  isClientError,
  operationUrl,
} from '../connection-format'
import {
  describeEnforcementLimits,
  describeRequestLimits,
  describeTokenLimits,
  isSecurityGroupGrant,
} from '../entitlement-format'
import { useFocusHandoff, useFocusHandoffSource, type FocusHandoff } from '../focus-handoff'
import { runtimeConfig } from '../runtime-config'
import type { Entitlement, McpConnection, ModelConnection, ResolvedEntitlement } from '../types'
import { KeyReveal } from './KeyReveal'

/** Expandable connection panel for one model grant. Nothing is fetched until it is opened. */
export function ConnectionDetails({ resolved }: { resolved: ResolvedEntitlement }) {
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()
  // MOSAIC groups live only in MOSAIC and the gateway never enforces them, so there's nothing to
  // connect with; the API answers 404 for them.
  const mosaicGroup = resolved.via === 'group' || resolved.entitlement.subject.kind === 'group'
  const securityGroup = isSecurityGroupGrant(resolved)
  const resourceKind = resolved.entitlement.resource.kind === 'mcpServer' ? 'mcpServer' : 'modelApi'

  return (
    <div className="connection-details">
      <Button
        appearance="secondary"
        icon={expanded ? <ChevronUpRegular /> : <ChevronDownRegular />}
        iconPosition="after"
        aria-expanded={expanded}
        aria-controls={expanded ? panelId : undefined}
        onClick={() => setExpanded((open) => !open)}
      >
        Connection details
      </Button>
      {expanded && (
        <div id={panelId} className="connection-panel">
          {mosaicGroup && !securityGroup ? (
            <GroupGrantNotice resourceKind={resourceKind} />
          ) : (
            <DirectGrantConnection entitlement={resolved.entitlement} />
          )}
        </div>
      )}
    </div>
  )
}

function GroupGrantNotice({ resourceKind }: { resourceKind: 'modelApi' | 'mcpServer' }) {
  if (resourceKind === 'mcpServer') {
    return (
      <MessageBar intent="info">
        <MessageBarBody>
          <MessageBarTitle>Connection details are for direct and Entra group grants</MessageBarTitle>
          You have this access through a MOSAIC group, which the gateway doesn&apos;t enforce. Ask
          an administrator for a direct grant, or a grant to a Microsoft Entra security group
          you&apos;re in, if you need to call this MCP server.
        </MessageBarBody>
      </MessageBar>
    )
  }
  return (
    <MessageBar intent="info">
      <MessageBarBody>
        <MessageBarTitle>Credentials are issued for direct grants only</MessageBarTitle>
        You have this access through a group. MOSAIC does not issue keys or connection details for
        group grants yet. Ask an administrator for a direct grant if you need to call this model.
      </MessageBarBody>
    </MessageBar>
  )
}

function DirectGrantConnection({ entitlement }: { entitlement: Entitlement }) {
  const api = usePortalApi()
  const { accounts } = useMsal()
  const location = useLocation()
  const account = accounts[0]
  const resourceKind: 'modelApi' | 'mcpServer' =
    entitlement.resource.kind === 'mcpServer' ? 'mcpServer' : 'modelApi'
  const identity = `${runtimeConfig.authMode}:${account?.homeAccountId ?? ''}:${account?.tenantId ?? ''}:${account?.localAccountId ?? ''}`
  const connection = useQuery<ModelConnection | McpConnection>({
    queryKey: ['portal', 'connection', resourceKind, identity, entitlement.id],
    queryFn: () =>
      resourceKind === 'mcpServer'
        ? api.getMcpConnection(entitlement.id)
        : api.getMyEntitlementConnection(entitlement.id),
    // A 4xx answer describes the grant's state; asking again will not change it.
    retry: (failureCount, error) => !isClientError(error) && failureCount < 1,
  })
  // Keeps keyboard focus in the panel when a reload replaces the part of it that held focus.
  const focusHandoff = useFocusHandoffSource()

  if (connection.isPending) {
    return <Spinner size="small" label="Loading connection details" />
  }
  if (connection.isError) {
    return (
      <ConnectionError
        error={connection.error}
        onRetry={() => void connection.refetch()}
        focusHandoff={focusHandoff}
        resourceKind={resourceKind}
      />
    )
  }

  const info = connection.data
  if (resourceKind === 'mcpServer') {
    return <McpConnectionPanel connection={info as McpConnection} focusHandoff={focusHandoff} />
  }
  const model = info as ModelConnection
  // Any change to who is signed in, the route, the grant, or its applied state discards a key.
  const keySession = [
    identity,
    location.key,
    entitlement.id,
    entitlement.updatedAt,
    model.entitlementId,
    model.publicationId,
    model.runtime?.status ?? 'none',
    model.runtime?.subscriptionName ?? '',
    String(model.appliedMethods?.keysEnabled),
    String(model.runtime?.appliedMethods?.keysEnabled),
    String(model.keysAvailable),
  ].join('|')

  return (
    <>
      <RuntimeSummary connection={model} focusHandoff={focusHandoff} />
      <EndpointSection connection={model} />
      <AuthenticationSection connection={model} />
      <KeyReveal
        key={keySession}
        entitlementId={entitlement.id}
        connection={model}
        onConflict={() => void connection.refetch()}
        focusHandoff={focusHandoff}
      />
      <SamplesSection connection={model} />
      <LimitsSection connection={model} />
    </>
  )
}

function ConnectionError({
  error,
  onRetry,
  focusHandoff,
  resourceKind = 'modelApi',
}: {
  error: Error
  onRetry: () => void
  focusHandoff: FocusHandoff
  resourceKind?: 'modelApi' | 'mcpServer'
}) {
  const bar = useRef<HTMLDivElement | null>(null)
  const retry = useRef<HTMLButtonElement | HTMLAnchorElement | null>(null)
  useFocusHandoff(focusHandoff, bar, retry, bar)
  const problem = connectionProblem(error, resourceKind)
  return (
    <MessageBar intent="error" ref={bar} tabIndex={-1}>
      <MessageBarBody>
        <MessageBarTitle>{problem.title}</MessageBarTitle>
        {problem.message}
      </MessageBarBody>
      {!isClientError(error) && (
        <MessageBarActions>
          <Button ref={retry} onClick={onRetry}>
            Try again
          </Button>
        </MessageBarActions>
      )}
    </MessageBar>
  )
}

function McpConnectionPanel({
  connection,
  focusHandoff,
}: {
  connection: McpConnection
  focusHandoff: FocusHandoff
}) {
  const summary = useRef<HTMLDivElement | null>(null)
  useFocusHandoff(focusHandoff, summary, summary)
  return (
    <>
      <div className="connection-status" ref={summary} tabIndex={-1}>
        <Badge appearance={connection.enforced ? 'filled' : 'tint'}>
          {connection.enforced ? 'Enforced by the gateway' : 'Recorded, not enforced'}
        </Badge>
        <Text>{connection.statusMessage}</Text>
        {connection.runtime?.error && (
          <MessageBar intent="warning" className="connection-status-error">
            <MessageBarBody>
              <MessageBarTitle>Last gateway error</MessageBarTitle>
              {connection.runtime.error}
            </MessageBarBody>
          </MessageBar>
        )}
      </div>
      <McpEndpointSection connection={connection} />
      <McpAuthenticationSection connection={connection} />
      <McpLimitsSection connection={connection} />
      <McpAdvancedSection connection={connection} />
    </>
  )
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [notice, setNotice] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  async function copy() {
    setNotice(null)
    setFailed(false)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(value)
      setNotice(`${label} copied.`)
    } catch {
      setFailed(true)
    }
  }
  return (
    <>
      <Button icon={<CopyRegular />} onClick={() => void copy()}>
        Copy {label}
      </Button>
      <Text as="span" size={200} role="status" aria-live="polite" className="key-status">
        {notice}
      </Text>
      {failed && (
        <MessageBar intent="warning">
          <MessageBarBody>
            Could not copy {label.toLowerCase()}. Your browser may require clipboard permission; copy it
            manually or try again.
          </MessageBarBody>
        </MessageBar>
      )}
    </>
  )
}

function CopyableCode({ value, label }: { value: string; label: string }) {
  return (
    <div className="copyable-value">
      <code>{value}</code>
      <div className="key-actions">
        <CopyButton value={value} label={label} />
      </div>
    </div>
  )
}

function mcpSnippet(connection: McpConnection) {
  if (!connection.serverUrl) return null
  return JSON.stringify(
    {
      servers: {
        [connection.displayName]: {
          type: 'http',
          url: connection.serverUrl,
        },
      },
    },
    null,
    2,
  )
}

function McpEndpointSection({ connection }: { connection: McpConnection }) {
  const headingId = useId()
  const snippet = mcpSnippet(connection)
  const showVsCode =
    Boolean(snippet) &&
    (connection.principalKind === 'user' ||
      connection.principalKind === 'agentUser' ||
      connection.principalKind === 'securityGroup' ||
      Boolean(connection.delegatedScope))
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Server</h3>
      <dl className="fact-list">
        <div>
          <dt>Server URL</dt>
          <dd>
            {connection.serverUrl ? (
              <CopyableCode value={connection.serverUrl} label="Server URL" />
            ) : (
              "The gateway URL isn't known yet."
            )}
          </dd>
        </div>
        <div>
          <dt>Transport</dt>
          <dd>{connection.transport}</dd>
        </div>
      </dl>
      {showVsCode && snippet && (
        <>
          <h4>VS Code</h4>
          <Text as="p" size={200} className="connection-note">
            Add this entry to <code>.vscode/mcp.json</code>. VS Code signs in with Microsoft Entra
            ID and discovers the resource metadata from the server URL. Your tenant may need an
            administrator to consent to{' '}
            {connection.delegatedScope ? <code>{connection.delegatedScope}</code> : 'the delegated scope'}.
          </Text>
          <pre className="code-sample"><code>{snippet}</code></pre>
          <div className="key-actions">
            <CopyButton value={snippet} label="VS Code snippet" />
          </div>
        </>
      )}
    </section>
  )
}

function McpAuthenticationSection({ connection }: { connection: McpConnection }) {
  const headingId = useId()
  const isAppCaller =
    connection.principalKind === 'agentIdentity' ||
    connection.principalKind === 'servicePrincipal' ||
    connection.principalKind === 'managedIdentity'
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Authentication</h3>
      <dl className="fact-list">
        <div>
          <dt>Tenant ID</dt>
          <dd><code>{connection.tenantId}</code></dd>
        </div>
        {connection.entraAudience && (
          <div>
            <dt>Audience</dt>
            <dd><code>{connection.entraAudience}</code></dd>
          </div>
        )}
        {connection.delegatedScope && (
          <div>
            <dt>Delegated scope</dt>
            <dd><code>{connection.delegatedScope}</code></dd>
          </div>
        )}
        {connection.applicationScope && (
          <div>
            <dt>Application scope</dt>
            <dd><code>{connection.applicationScope}</code></dd>
          </div>
        )}
        {connection.requiredAppRole && (
          <div>
            <dt>Required app role</dt>
            <dd><code>{connection.requiredAppRole}</code></dd>
          </div>
        )}
        {connection.clientId && (
          <div>
            <dt>Client ID</dt>
            <dd><code>{connection.clientId}</code></dd>
          </div>
        )}
      </dl>
      {connection.principalKind === 'agentUser' && connection.clientId && (
        <Text as="p" size={200} className="connection-note">
          This is the parent agent identity client ID. Use it when the agent user signs in for the
          delegated scope.
        </Text>
      )}
      {(isAppCaller || connection.applicationScope || connection.requiredAppRole) && (
        <Text as="p" size={200} className="connection-note">
          Agent identities and applications request the application scope with client credentials.
          Assign <code>{connection.requiredAppRole ?? 'Mcp.Invoke.Application'}</code> to the agent,
          application, or agent blueprint before it calls this server.
        </Text>
      )}
      {(connection.viaGroupId || connection.principalKind === 'securityGroup') && (
        <MessageBar intent="info">
          <MessageBarBody>
            <MessageBarTitle>Access is via group</MessageBarTitle>
            {connection.viaGroupName ?? connection.viaGroupId ?? 'This group'} grants this access.
            Each member gets the group&apos;s limits separately. App roles are not inherited through
            groups, so agents and apps need the required app role assigned directly or through their
            blueprint.
          </MessageBarBody>
        </MessageBar>
      )}
    </section>
  )
}

function McpLimitsSection({ connection }: { connection: McpConnection }) {
  const headingId = useId()
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Call limits</h3>
      <ul className="plain-list">
        {describeRequestLimits(connection.limits).map((limit) => (
          <li key={limit}>{limit}</li>
        ))}
      </ul>
      <Text as="p" size={200} className="connection-note">
        MCP grants are limited by calls, not tokens.
      </Text>
    </section>
  )
}

function McpAdvancedSection({ connection }: { connection: McpConnection }) {
  const headingId = useId()
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <details>
        <summary id={headingId}>Advanced details</summary>
        <dl className="fact-list">
          <div>
            <dt>Resource metadata URL</dt>
            <dd>
              {connection.resourceMetadataUrl ? (
                <CopyableCode value={connection.resourceMetadataUrl} label="Resource metadata URL" />
              ) : (
                'Not available yet'
              )}
            </dd>
          </div>
          <div>
            <dt>MCP server ID</dt>
            <dd><code>{connection.mcpServerId}</code></dd>
          </div>
          {connection.publicationId && (
            <div>
              <dt>Publication ID</dt>
              <dd><code>{connection.publicationId}</code></dd>
            </div>
          )}
        </dl>
      </details>
    </section>
  )
}

function RuntimeSummary({
  connection,
  focusHandoff,
}: {
  connection: ModelConnection
  focusHandoff: FocusHandoff
}) {
  const summary = useRef<HTMLDivElement | null>(null)
  useFocusHandoff(focusHandoff, summary, summary)
  const runtime = describeConnectionRuntime(connection.runtime)
  return (
    <div className="connection-status" ref={summary} tabIndex={-1}>
      <Badge appearance={runtime.applied ? 'filled' : 'tint'}>{runtime.label}</Badge>
      <Text>{runtime.explanation}</Text>
      {connection.runtime?.error && (
        <MessageBar intent="warning" className="connection-status-error">
          <MessageBarBody>
            <MessageBarTitle>Last APIM error</MessageBarTitle>
            {connection.runtime.error}
          </MessageBarBody>
        </MessageBar>
      )}
    </div>
  )
}

function EndpointSection({ connection }: { connection: ModelConnection }) {
  const headingId = useId()
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Endpoint</h3>
      <dl className="fact-list">
        <div>
          <dt>Base URL</dt>
          <dd><code>{connection.endpoint}</code></dd>
        </div>
        <div>
          <dt>Deployment</dt>
          <dd><code>{connection.deploymentName}</code></dd>
        </div>
        {connection.keysAvailable !== false && (
          <div>
            <dt>Key header</dt>
            <dd><code>{connection.subscriptionHeader}</code></dd>
          </div>
        )}
      </dl>
      <h4>Operations</h4>
      {connection.operations.length > 0 ? (
        <ul className="operation-list">
          {connection.operations.map((operation) => (
            <li key={`${operation.name}:${operation.method}:${operation.path}`}>
              <span className="operation-method">{operation.method.toUpperCase()}</span>
              <code>{operationUrl(connection.endpoint, operation.path)}</code>
              <span className="operation-name">{operation.name}</span>
            </li>
          ))}
        </ul>
      ) : (
        <Text as="p">No operations are published for this model.</Text>
      )}
    </section>
  )
}

function AuthenticationSection({ connection }: { connection: ModelConnection }) {
  const headingId = useId()
  const methods = connection.appliedMethods
  const keysAccepted = connection.keysAvailable === false ? false : methods?.keysEnabled
  const groupGrant = connection.principalKind === 'securityGroup' || connection.keysAvailable === false
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Authentication</h3>
      {methods ? (
        <dl className="fact-list">
          <div>
            <dt>Subscription key</dt>
            <dd>{keysAccepted ? 'Accepted' : 'Not accepted'}</dd>
          </div>
          <div>
            <dt>Microsoft Entra ID token</dt>
            <dd>{methods.entraEnabled ? 'Accepted' : 'Not accepted'}</dd>
          </div>
        </dl>
      ) : (
        <Text as="p">
          No access methods are applied yet. APIM accepts no credentials for this model until an
          administrator applies governed access.
        </Text>
      )}
      {methods && !methods.keysEnabled && !methods.entraEnabled && (
        <Text as="p">Both methods are turned off, so APIM denies every call to this model.</Text>
      )}
      {methods?.entraEnabled && (
        <>
          <h4>Microsoft Entra ID</h4>
          <dl className="fact-list">
            <div>
              <dt>Tenant ID</dt>
              <dd><code>{connection.tenantId}</code></dd>
            </div>
            {connection.entraClientId && (
              <div>
                <dt>Client ID</dt>
                <dd><code>{connection.entraClientId}</code></dd>
              </div>
            )}
            <div>
              <dt>Scope</dt>
              <dd>{connection.entraScope ? <code>{connection.entraScope}</code> : 'Not configured'}</dd>
            </div>
            <div>
              <dt>Audience</dt>
              <dd>{connection.entraAudience ? <code>{connection.entraAudience}</code> : 'Not configured'}</dd>
            </div>
          </dl>
          <Text as="p" size={200} className="connection-note">
            {connection.entraClientId
              ? 'Sign in with the client ID above and request the scope to get an access token.'
              : 'Request an access token for the scope above.'}{' '}
            Send it as a bearer token; your portal sign-in is not a model token.
          </Text>
          {groupGrant && (connection.entraApplicationScope || connection.requiredAppRole) && (
            <details className="connection-note">
              <summary>Agents and apps in this group</summary>
              <Text as="p" size={200}>
                Request the application scope
                {connection.entraApplicationScope ? (
                  <>
                    {' '}<code>{connection.entraApplicationScope}</code>
                  </>
                ) : (
                  ' configured for this model'
                )}
                {connection.requiredAppRole ? (
                  <>
                    {' '}and make sure <code>{connection.requiredAppRole}</code> is assigned to the
                    agent or app itself.
                  </>
                ) : (
                  ' and make sure the required app role is assigned to the agent or app itself.'
                )}
              </Text>
            </details>
          )}
          {!connection.entraClientId && (
            <Text as="p" size={200} className="connection-note">
              MOSAIC has no client ID for you to sign in with for this grant. Ask an administrator
              which client ID to use, or to reapply this model's access.
            </Text>
          )}
        </>
      )}
    </section>
  )
}

function SamplesSection({ connection }: { connection: ModelConnection }) {
  const headingId = useId()
  const methods = connection.appliedMethods
  const samples = buildSamples(connection)
  const tokenSample = buildTokenSample(connection)
  const keysAccepted = connection.keysAvailable === false ? false : methods?.keysEnabled
  const tokenBlock = tokenSample && (
    <>
      <h4>Get a token (Python)</h4>
      <Text as="p" size={200} className="connection-note">
        Signs you in with a device code and prints an access token for the scope above. Install{' '}
        <code>msal</code>, save this as <code>get_token.py</code>, and run{' '}
        <code>export MOSAIC_ACCESS_TOKEN="$(python get_token.py)"</code>. Tokens expire after
        about an hour.
      </Text>
      <pre className="code-sample"><code>{tokenSample}</code></pre>
    </>
  )
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Code samples</h3>
      {samples ? (
        <>
          <Text as="p" size={200} className="connection-note">
            {samples.credential === 'key' ? (
              <>Set <code>MOSAIC_API_KEY</code> to a key shown above</>
            ) : (
              <>Set <code>MOSAIC_ACCESS_TOKEN</code> to an access token for the scope above</>
            )}
            {samples.kind === 'messages' ? (
              '.'
            ) : (
              <>
                {' '}and <code>MOSAIC_API_VERSION</code> to an API version your deployment supports.
              </>
            )}
            {' '}Samples use placeholders and never include your key.
          </Text>
          {samples.kind === 'messages' && (
            <Text as="p" size={200} className="connection-note">
              This model uses the Anthropic Messages API, which takes no API version. The gateway
              adds the <code>anthropic-version</code> header when a request omits it. With an
              Anthropic SDK, use <code>{operationUrl(connection.endpoint, 'anthropic')}</code> as the
              base URL
              {keysAccepted && (
                <>
                  . The gateway removes <code>x-api-key</code>, so send a key in the{' '}
                  <code>{connection.subscriptionHeader}</code> header
                </>
              )}
              {methods?.entraEnabled && (
                <>
                  {keysAccepted ? ', or' : ' and'} pass a token as <code>auth_token</code>
                </>
              )}
              .
            </Text>
          )}
          {samples.credential === 'key' && methods?.entraEnabled && (
            <Text as="p" size={200} className="connection-note">
              To use a token instead, replace the key header with{' '}
              <code>Authorization: Bearer $MOSAIC_ACCESS_TOKEN</code>. Send one credential per
              request; if you send both, both must be valid.
            </Text>
          )}
          {samples.credential === 'token' && tokenBlock}
          <h4>curl (bash)</h4>
          <pre className="code-sample"><code>{samples.curl}</code></pre>
          <h4>Python</h4>
          <pre className="code-sample"><code>{samples.python}</code></pre>
          {samples.credential === 'key' && tokenBlock}
        </>
      ) : (
        <>
          <Text as="p">
            {!methods
              ? 'Samples appear after governed access is applied to this model.'
              : !methods.keysEnabled && !methods.entraEnabled
                ? 'No sample is shown because APIM denies every call to this model.'
                : 'No sample is available for these operations.'}
          </Text>
          {tokenBlock}
        </>
      )}
    </section>
  )
}

function LimitsSection({ connection }: { connection: ModelConnection }) {
  const headingId = useId()
  const publicationLimits = describeTokenLimits(connection.publicationLimits)
  const keySubject = connection.keysAvailable === false ? 'Entra tokens share' : 'Your primary key, secondary key, and Entra tokens share'
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Limits</h3>
      <dl className="fact-list">
        <div>
          <dt>Publication limits</dt>
          <dd>
            {publicationLimits.length > 0 ? (
              <ul className="plain-list">
                {publicationLimits.map((limit) => <li key={limit}>{limit}</li>)}
              </ul>
            ) : connection.publicationLimits ? (
              'No publication token limit configured'
            ) : (
              "Token limits are unavailable for this model on this gateway's tier"
            )}
          </dd>
        </div>
        <div>
          <dt>Grant limits</dt>
          <dd>
            <ul className="plain-list">
              {describeEnforcementLimits(connection.grantLimits).map((limit) => (
                <li key={limit}>{limit}</li>
              ))}
            </ul>
          </dd>
        </div>
      </dl>
      <Text as="p" size={200} className="connection-note">
        {keySubject} this grant&apos;s limits.
        {connection.publicationLimits && ' Publication limits apply as well and are counted separately.'}
        {connection.keysAvailable === false && ' Each person or app that uses this group grant is counted separately.'}
      </Text>
    </section>
  )
}
