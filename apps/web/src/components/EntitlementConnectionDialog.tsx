import { useMsal } from '@azure/msal-react'
import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  MessageBar,
  MessageBarBody,
  Text,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { useLocation } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { describeAccessMethods, describeLimits } from '../entitlement-limits'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import { runtimeConfig } from '../runtime-config'
import type { Entitlement, KeySlot, McpConnection, ModelConnection } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { ModelAccessRecovery } from './ModelAccessRecovery'
import styles from '../pages/EntitlementsPage.module.css'

export function EntitlementConnectionDialog({
  open,
  entitlement,
  onClose,
}: {
  open: boolean
  entitlement: Entitlement
  onClose: () => void
}) {
  const { accounts } = useMsal()
  const location = useLocation()
  const account = accounts[0]
  const identity = `${runtimeConfig.authMode}:${account?.homeAccountId ?? ''}:${account?.tenantId ?? ''}:${account?.localAccountId ?? ''}`
  if (!open) return null
  return (
    <ConnectionSession
      key={`${identity}:${location.key}:${entitlement.id}:${entitlement.updatedAt}:${entitlement.enabled}:${entitlement.runtime?.status}:${entitlement.runtime?.appliedMethods?.keysEnabled}`}
      identity={identity}
      entitlement={entitlement}
      onClose={onClose}
    />
  )
}

function PrincipalConnectionNotice({ info }: { info: ModelConnection }) {
  if (!info.principalKind) return null
  if (info.principalKind === 'agentIdentity') {
    return (
      <MessageBar intent="info">
        <MessageBarBody>
          Agent identity signs in as itself with its blueprint&apos;s credentials. Use client ID{' '}
          {info.entraClientId ?? 'from the agent identity'} and scope{' '}
          {info.entraScope ?? '.default'}; assign the{' '}
          {info.requiredAppRole ?? 'required'} app role.
        </MessageBarBody>
      </MessageBar>
    )
  }

  if (info.principalKind === 'agentUser') {
    return (
      <MessageBar intent="info">
        <MessageBarBody>
          The parent agent identity {info.entraClientId ?? 'client'} requests a delegated token for
          this agent user with scope {info.entraScope ?? 'Models.Invoke'}.
        </MessageBarBody>
      </MessageBar>
    )
  }
  if (info.principalKind === 'securityGroup') {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          Members sign in as themselves. People use delegated scope{' '}
          {info.entraScope ?? 'Models.Invoke'}; agents and applications use{' '}
          {info.entraApplicationScope ?? '.default'} and need{' '}
          {info.requiredAppRole ?? 'the required app role'} assigned to themselves. Keys are not
          available, and limits apply to each member.
        </MessageBarBody>
      </MessageBar>
    )
  }
  return (
    <MessageBar intent="info">
      <MessageBarBody>
        Principal type: {PRINCIPAL_KIND_LABELS[info.principalKind]}.
      </MessageBarBody>
    </MessageBar>
  )
}

function codeSnippet(info: McpConnection): string {
  const name = info.displayName || 'mosaic-mcp'
  return JSON.stringify({
    servers: {
      [name]: {
        type: 'http',
        url: info.serverUrl ?? '<server URL unavailable>',
      },
    },
  }, null, 2)
}

function McpConnectionDetails({ info }: { info: McpConnection }) {
  return (
    <>
      <MessageBar intent={info.enforced ? 'success' : 'warning'}>
        <MessageBarBody>
          {info.enforced ? 'Enforced by the gateway.' : 'Recorded, not enforced.'}{' '}
          {info.statusMessage}
        </MessageBarBody>
      </MessageBar>
      <dl className={styles.detailList}>
        <div><dt>Server</dt><dd>{info.displayName}</dd></div>
        <div><dt>Server URL</dt><dd>{info.serverUrl ?? 'Not available'}</dd></div>
        <div><dt>Transport</dt><dd>{info.transport}</dd></div>
        <div><dt>Tenant</dt><dd>{info.tenantId}</dd></div>
        <div><dt>Runtime state</dt><dd>{info.runtime?.status ?? (info.enforced ? 'unknown' : 'recorded')}</dd></div>
        {info.resourceMetadataUrl && <div><dt>Resource metadata URL</dt><dd>{info.resourceMetadataUrl}</dd></div>}
        {info.entraAudience && <div><dt>MCP audience</dt><dd>{info.entraAudience}</dd></div>}
        {info.delegatedScope && <div><dt>Delegated scope</dt><dd>{info.delegatedScope}</dd></div>}
        {info.applicationScope && <div><dt>Application scope</dt><dd>{info.applicationScope}</dd></div>}
        {info.requiredAppRole && <div><dt>Required app role</dt><dd>{info.requiredAppRole}</dd></div>}
        {info.clientId && <div><dt>Client ID</dt><dd>{info.clientId}</dd></div>}
        {info.viaGroupName && <div><dt>Security group</dt><dd>{info.viaGroupName}</dd></div>}
      </dl>
      {info.principalKind === 'securityGroup' && (
        <MessageBar intent="warning">
          <MessageBarBody>
            Members sign in as themselves. People use delegated tokens; agents and applications use
            the application scope and required app role. Limits apply to each member.
          </MessageBarBody>
        </MessageBar>
      )}
      <Text weight="semibold">Last recorded limits</Text>
      {describeLimits({ enforcement: info.limits }).map((limit) => <Text key={limit}>{limit}</Text>)}
      <Text weight="semibold">VS Code mcp.json</Text>
      <pre className={styles.secretValue}>{codeSnippet(info)}</pre>
    </>
  )
}

function ConnectionSession({
  identity,
  entitlement,
  onClose,
}: {
  identity: string
  entitlement: Entitlement
  onClose: () => void
}) {
  const api = useMosaicApi()
  const [closed, setClosed] = useState(false)
  const [secret, setSecret] = useState<{ slot: KeySlot; key: string } | null>(null)
  const [revealing, setRevealing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copyMessage, setCopyMessage] = useState<string | null>(null)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const isMcp = entitlement.resource.kind === 'mcpServer'
  const connection = useQuery<ModelConnection | McpConnection>({
    queryKey: ['entitlement-connection', identity, entitlement.id],
    queryFn: () => isMcp ? api.getMcpConnection(entitlement.id) : api.getEntitlementConnection(entitlement.id),
  })
  const info = connection.data
  const modelInfo = !isMcp ? (info as ModelConnection | undefined) : undefined
  const mcpInfo = isMcp ? (info as McpConnection | undefined) : undefined
  const eligible = Boolean(
    !closed && !connection.isError && modelInfo && modelInfo.keysAvailable !== false
    && entitlement.enabled && entitlement.subject.kind !== 'group'
    && entitlement.resource.kind === 'modelApi' && entitlement.binding?.source === 'orchestrated'
    && entitlement.runtime?.status === 'applied' && entitlement.runtime.appliedMethods?.keysEnabled
    && modelInfo.runtime?.status === 'applied' && modelInfo.appliedMethods?.keysEnabled
    && modelInfo.entitlementId === entitlement.id
    && modelInfo.publicationId === entitlement.runtime.publicationId,
  )

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      generation.current += 1
      controller.current?.abort()
    }
  }, [])

  useEffect(() => {
    function clearBeforePageHide() {
      generation.current += 1
      controller.current?.abort()
      flushSync(() => {
        setSecret(null)
        setClosed(true)
        onClose()
      })
    }
    window.addEventListener('pagehide', clearBeforePageHide)
    return () => window.removeEventListener('pagehide', clearBeforePageHide)
  }, [onClose])

  useEffect(() => {
    if (!eligible) {
      generation.current += 1
      controller.current?.abort()
      setSecret(null)
      setRevealing(false)
      setCopyMessage(null)
    }
  }, [eligible])

  function close() {
    generation.current += 1
    controller.current?.abort()
    setSecret(null)
    setError(null)
    setCopyMessage(null)
    setClosed(true)
    onClose()
  }

  async function reveal(slot: KeySlot) {
    if (!eligible) return
    const request = ++generation.current
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    setSecret(null)
    setError(null)
    setCopyMessage(null)
    setRevealing(true)
    try {
      const result = await api.revealEntitlementKey(entitlement.id, slot, abort.signal)
      if (!mounted.current || generation.current !== request) return
      if (result.entitlementId !== entitlement.id || result.slot !== slot || !result.key
        || (modelInfo?.runtime?.subscriptionName && result.subscriptionName !== modelInfo.runtime.subscriptionName)) {
        throw new Error('The service returned unexpected credential metadata. No key was displayed.')
      }
      setSecret({ slot, key: result.key })
    } catch (failure) {
      if (mounted.current && generation.current === request) {
        setError(failure instanceof Error ? failure.message : 'Unable to reveal the key.')
      }
    } finally {
      if (mounted.current && generation.current === request) setRevealing(false)
    }
  }

  async function copy() {
    if (!secret || !eligible) return
    const request = generation.current
    setError(null)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(secret.key)
      if (mounted.current && generation.current === request) {
        setCopyMessage('Key copied. The clipboard is sensitive and is not cleared automatically.')
      }
    } catch {
      if (mounted.current && generation.current === request) {
        setError('Could not copy the key. Your browser may require clipboard permission; copy it manually or try again.')
      }
    }
  }

  if (closed) return null
  return (
    <Dialog open onOpenChange={(_, data) => !data.open && close()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{isMcp ? 'MCP connection' : 'Model connection and keys'}</DialogTitle>
          <DialogContent className={styles.dialogForm}>
            <Text>Connection metadata is not proof of a successful live invocation. Allow for APIM propagation.</Text>
            {runtimeConfig.authMode === 'local' && (
              <MessageBar intent="warning">
                <MessageBarBody>Local development mode: a simulated response does not verify live APIM access.</MessageBarBody>
              </MessageBar>
            )}
            {connection.isPending && <Loading label="Loading connection information" />}
            {connection.isError && <ErrorState error={connection.error} />}
            {mcpInfo && <McpConnectionDetails info={mcpInfo} />}
            {modelInfo && (
              <>
                <dl className={styles.detailList}>
                  <div><dt>Endpoint</dt><dd>{modelInfo.endpoint}</dd></div>
                  <div><dt>Deployment</dt><dd>{modelInfo.deploymentName}</dd></div>
                  <div><dt>Tenant</dt><dd>{modelInfo.tenantId}</dd></div>
                  <div><dt>Runtime state</dt><dd>{modelInfo.runtime?.status ?? 'unknown'}</dd></div>
                  <div><dt>Last applied methods</dt><dd>{describeAccessMethods(modelInfo.appliedMethods)}</dd></div>
                  <div><dt>Model-runtime audience</dt><dd>{modelInfo.entraAudience ?? 'Not configured'}</dd></div>
                  <div><dt>Model-runtime scope</dt><dd>{modelInfo.entraScope ?? 'Not configured'}</dd></div>
                  {modelInfo.entraClientId && <div><dt>Client ID</dt><dd>{modelInfo.entraClientId}</dd></div>}
                  {modelInfo.entraApplicationScope && <div><dt>Application scope</dt><dd>{modelInfo.entraApplicationScope}</dd></div>}
                  {modelInfo.requiredAppRole && <div><dt>Required app role</dt><dd>{modelInfo.requiredAppRole}</dd></div>}
                  {modelInfo.viaGroupName && <div><dt>Security group</dt><dd>{modelInfo.viaGroupName}</dd></div>}
                </dl>
                <PrincipalConnectionNotice info={modelInfo} />
                <Text size={200}>A MOSAIC control-plane token is not a model-runtime token. Entra consent and application permissions are configured separately.</Text>
                {modelInfo.runtime?.error && (
                  <MessageBar intent="warning"><MessageBarBody>{modelInfo.runtime.error}</MessageBarBody></MessageBar>
                )}
                {modelInfo.runtime?.status === 'unknown' && (
                  <ModelAccessRecovery publicationId={modelInfo.publicationId} />
                )}
                {modelInfo.operations.map((operation) => (
                  <Text key={`${operation.name}:${operation.method}:${operation.path}`}>
                    {operation.method} {operation.path} · {operation.name}
                  </Text>
                ))}
                {modelInfo.apiShape === 'anthropicMessages' && (
                  <MessageBar intent="info">
                    <MessageBarBody>
                      This model uses the Anthropic Messages API. Set the request&apos;s model to{' '}
                      {modelInfo.deploymentName}. The gateway adds anthropic-version when a request omits it.
                      With an Anthropic SDK, use {modelInfo.endpoint}/anthropic as the base URL and send the
                      key in the {modelInfo.subscriptionHeader} header; the gateway removes x-api-key.
                    </MessageBarBody>
                  </MessageBar>
                )}
                <Text weight="semibold">Last recorded limits</Text>
                {describeLimits({ enforcement: modelInfo.grantLimits }, modelInfo.publicationLimits).map((limit) => <Text key={limit}>{limit}</Text>)}
                {modelInfo.keysAvailable === false ? (
                  <MessageBar intent="warning">
                    <MessageBarBody>
                      Key authentication is not available for this grant. Use an Entra token.
                    </MessageBarBody>
                  </MessageBar>
                ) : (
                  <>
                    <Text>Use one enabled credential. Examples contain placeholders, never your actual key:</Text>
                    <pre className={styles.secretValue}>{`${modelInfo.subscriptionHeader}: <YOUR_APIM_KEY>\n\nEntra access token: <YOUR_ENTRA_TOKEN>`}</pre>
                  </>
                )}
              </>
            )}
            <Text>
              APIM holds the keys. Reveal fetches the selected current key only on your explicit
              request. It is cleared from this dialog on close, navigation, or account change.
              Sharing a key delegates this grant&apos;s access.
            </Text>
            {!eligible && <Text>Key reveal requires an enabled, applied direct grant with key authentication and a trusted orchestrated binding.</Text>}
            {modelInfo?.keysAvailable !== false && !isMcp && (
              <div className={styles.rowActions}>
                <Button disabled={!eligible || revealing} onClick={() => void reveal('primary')}>Reveal primary key</Button>
                <Button disabled={!eligible || revealing} onClick={() => void reveal('secondary')}>Reveal secondary key</Button>
              </div>
            )}
            {revealing && <Loading label="Retrieving the current key from APIM" />}
            {secret && eligible && (
              <div className={styles.cellStack}>
                <Text weight="semibold">Revealed {secret.slot} key</Text>
                <pre aria-label={`Revealed ${secret.slot} key`} className={styles.secretValue}>{secret.key}</pre>
                <Button onClick={() => void copy()}>Copy revealed key</Button>
              </div>
            )}
            {error && <MessageBar intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}
            {copyMessage && <Text role="status">{copyMessage}</Text>}
          </DialogContent>
          <DialogActions><Button onClick={close}>Close</Button></DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
