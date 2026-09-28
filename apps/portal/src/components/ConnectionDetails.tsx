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
import { ChevronDownRegular, ChevronUpRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { usePortalApi } from '../api'
import {
  buildSamples,
  connectionProblem,
  describeConnectionRuntime,
  isClientError,
  operationUrl,
} from '../connection-format'
import { describeEnforcementLimits, describeTokenLimits } from '../entitlement-format'
import { useFocusHandoff, useFocusHandoffSource, type FocusHandoff } from '../focus-handoff'
import { runtimeConfig } from '../runtime-config'
import type { Entitlement, ModelConnection, ResolvedEntitlement } from '../types'
import { KeyReveal } from './KeyReveal'

/** Expandable connection panel for one model grant. Nothing is fetched until it is opened. */
export function ConnectionDetails({ resolved }: { resolved: ResolvedEntitlement }) {
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()
  const viaGroup = resolved.via === 'group' || resolved.entitlement.subject.kind === 'group'

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
          {viaGroup ? <GroupGrantNotice /> : <DirectGrantConnection entitlement={resolved.entitlement} />}
        </div>
      )}
    </div>
  )
}

function GroupGrantNotice() {
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
  const identity = `${runtimeConfig.authMode}:${account?.homeAccountId ?? ''}:${account?.tenantId ?? ''}:${account?.localAccountId ?? ''}`
  const connection = useQuery({
    queryKey: ['portal', 'connection', identity, entitlement.id],
    queryFn: () => api.getMyEntitlementConnection(entitlement.id),
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
      />
    )
  }

  const info = connection.data
  // Any change to who is signed in, the route, the grant, or its applied state discards a key.
  const keySession = [
    identity,
    location.key,
    entitlement.id,
    entitlement.updatedAt,
    info.entitlementId,
    info.publicationId,
    info.runtime?.status ?? 'none',
    info.runtime?.subscriptionName ?? '',
    String(info.appliedMethods?.keysEnabled),
    String(info.runtime?.appliedMethods?.keysEnabled),
  ].join('|')

  return (
    <>
      <RuntimeSummary connection={info} focusHandoff={focusHandoff} />
      <EndpointSection connection={info} />
      <AuthenticationSection connection={info} />
      <KeyReveal
        key={keySession}
        entitlementId={entitlement.id}
        connection={info}
        onConflict={() => void connection.refetch()}
        focusHandoff={focusHandoff}
      />
      <SamplesSection connection={info} />
      <LimitsSection connection={info} />
    </>
  )
}

function ConnectionError({
  error,
  onRetry,
  focusHandoff,
}: {
  error: Error
  onRetry: () => void
  focusHandoff: FocusHandoff
}) {
  const bar = useRef<HTMLDivElement | null>(null)
  const retry = useRef<HTMLButtonElement | HTMLAnchorElement | null>(null)
  useFocusHandoff(focusHandoff, bar, retry, bar)
  const problem = connectionProblem(error)
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
        <div>
          <dt>Key header</dt>
          <dd><code>{connection.subscriptionHeader}</code></dd>
        </div>
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
  return (
    <section className="connection-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Authentication</h3>
      {methods ? (
        <dl className="fact-list">
          <div>
            <dt>Subscription key</dt>
            <dd>{methods.keysEnabled ? 'Accepted' : 'Not accepted'}</dd>
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
            <div>
              <dt>Audience</dt>
              <dd>{connection.entraAudience ? <code>{connection.entraAudience}</code> : 'Not configured'}</dd>
            </div>
            <div>
              <dt>Scope</dt>
              <dd>{connection.entraScope ? <code>{connection.entraScope}</code> : 'Not configured'}</dd>
            </div>
            {connection.entraClientId && (
              <div>
                <dt>Client ID</dt>
                <dd><code>{connection.entraClientId}</code></dd>
              </div>
            )}
          </dl>
          <Text as="p" size={200} className="connection-note">
            Request an access token for this scope and send it as a bearer token. Your portal
            sign-in is not a model token, and your application may also need an administrator to
            grant it consent.
          </Text>
        </>
      )}
    </section>
  )
}

function SamplesSection({ connection }: { connection: ModelConnection }) {
  const headingId = useId()
  const methods = connection.appliedMethods
  const samples = buildSamples(connection)
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
            {' '}and <code>MOSAIC_API_VERSION</code> to an API version your deployment supports.
            Samples use placeholders and never include your key.
          </Text>
          {samples.credential === 'key' && methods?.entraEnabled && (
            <Text as="p" size={200} className="connection-note">
              To use a token instead, replace the key header with{' '}
              <code>Authorization: Bearer $MOSAIC_ACCESS_TOKEN</code>. Send one credential per
              request; if you send both, both must be valid.
            </Text>
          )}
          <h4>curl (bash)</h4>
          <pre className="code-sample"><code>{samples.curl}</code></pre>
          <h4>Python</h4>
          <pre className="code-sample"><code>{samples.python}</code></pre>
        </>
      ) : (
        <Text as="p">
          {!methods
            ? 'Samples appear after governed access is applied to this model.'
            : !methods.keysEnabled && !methods.entraEnabled
              ? 'No sample is shown because APIM denies every call to this model.'
              : 'No sample is available for these operations.'}
        </Text>
      )}
    </section>
  )
}

function LimitsSection({ connection }: { connection: ModelConnection }) {
  const headingId = useId()
  const publicationLimits = describeTokenLimits(connection.publicationLimits)
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
            ) : (
              'No publication token limit configured'
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
        Your primary key, secondary key, and Entra tokens share this grant&apos;s limits.
        Publication limits apply as well and are counted separately.
      </Text>
    </section>
  )
}
