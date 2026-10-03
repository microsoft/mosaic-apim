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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { useLocation } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { describeAccessMethods, describeLimits, describePoolSafeguard } from '../entitlement-limits'
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
  const queryClient = useQueryClient()
  const [closed, setClosed] = useState(false)
  const [secret, setSecret] = useState<{ slot: KeySlot; key: string } | null>(null)
  const [revealing, setRevealing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [keyMessage, setKeyMessage] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<'delete' | 'rotate-primary' | 'rotate-secondary' | null>(null)
  const [copyError, setCopyError] = useState<string | null>(null)
  const [copyMessage, setCopyMessage] = useState<string | null>(null)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const isMcp = entitlement.resource.kind === 'mcpServer'
  // A pool model's grant has no binding: its pool's governed access is the trusted path.
  const isPool = entitlement.resource.kind === 'poolModel'
  const connection = useQuery<ModelConnection | McpConnection>({
    queryKey: ['entitlement-connection', identity, entitlement.id],
    queryFn: () => isMcp ? api.getMcpConnection(entitlement.id) : api.getEntitlementConnection(entitlement.id),
  })
  const info = connection.data
  const modelInfo = !isMcp ? (info as ModelConnection | undefined) : undefined
  const mcpInfo = isMcp ? (info as McpConnection | undefined) : undefined
  const keyExists = modelInfo?.keyExists ?? entitlement.runtime?.keyExists ?? true
  const keysAllowedByCostCenter = modelInfo?.keysAllowedByCostCenter !== false
  const keyCapable = Boolean(
    !closed && !connection.isError && modelInfo && modelInfo.keysAvailable !== false
    && keysAllowedByCostCenter
    && entitlement.enabled && entitlement.subject.kind !== 'group'
    && (isPool || (entitlement.resource.kind === 'modelApi' && entitlement.binding?.source === 'orchestrated'))
    && entitlement.runtime?.status === 'applied' && entitlement.runtime.appliedMethods?.keysEnabled
    && modelInfo.runtime?.status === 'applied' && modelInfo.appliedMethods?.keysEnabled
    && modelInfo.entitlementId === entitlement.id
    && modelInfo.publicationId === entitlement.runtime.publicationId,
  )
  const eligible = Boolean(keyCapable && keyExists)
  const sharedWith = modelInfo?.keySharedWith ?? []
  const sharedNames = sharedWith.map((model) => model.displayName).join(', ')

  function keyErrorMessage(failure: unknown) {
    const body = (failure as { body?: { details?: { reason?: unknown } } })?.body
    const reason = body?.details?.reason
    if (reason === 'noKey' || reason === 'keyMissing') return 'No key exists for this grant. Create one, then reveal it.'
    if (reason === 'keyExists') return 'A key already exists for this grant. Reveal or rotate it instead.'
    if (reason === 'costCenterKeysOff') return 'Keys are off for this cost center. Turn keys on and apply the model before creating or rotating keys.'
    if (reason === 'keyNotOwned') return "API Management already has a subscription with this key's name that MOSAIC didn't create. Remove it there, then try again."
    if (reason === 'keyScopeChanged') return "This key is no longer scoped to its model in API Management. Re-plan and apply the model's access."
    return failure instanceof Error ? failure.message : 'Unable to manage the key.'
  }

  const refreshKeys = async () => {
    setSecret(null)
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
    ])
  }
  const createKey = useMutation({
    mutationFn: () => api.createEntitlementKey(entitlement.id),
    onSuccess: async () => {
      setKeyMessage('Created the grant key.')
      await refreshKeys()
    },
    onError: (failure) => setError(keyErrorMessage(failure)),
  })
  const rotateKey = useMutation({
    mutationFn: (slot: KeySlot) => api.rotateEntitlementKey(entitlement.id, slot),
    onSuccess: async (_, slot) => {
      setConfirmation(null)
      setKeyMessage(`Rotated the ${slot} key.`)
      await refreshKeys()
    },
    onError: (failure) => setError(keyErrorMessage(failure)),
  })
  const deleteKey = useMutation({
    mutationFn: () => api.deleteEntitlementKey(entitlement.id),
    onSuccess: async () => {
      setConfirmation(null)
      setKeyMessage('Deleted the grant key.')
      await refreshKeys()
    },
    onError: (failure) => setError(keyErrorMessage(failure)),
  })

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

  // A reveal that fails shows why. Move focus there, so a screen reader reads it. A reveal that succeeds
  // leaves focus on its button and says which key it revealed, but never the key: focusing the key would
  // have a screen reader read it aloud. Fluent places focus as the dialog opens.
  const errorRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  function close() {
    generation.current += 1
    controller.current?.abort()
    setSecret(null)
    setError(null)
    setCopyError(null)
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
    setCopyError(null)
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
        setError(keyErrorMessage(failure))
      }
    } finally {
      if (mounted.current && generation.current === request) setRevealing(false)
    }
  }

  async function copy() {
    if (!secret || !eligible) return
    const request = generation.current
    setCopyError(null)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(secret.key)
      if (mounted.current && generation.current === request) {
        setCopyMessage('Key copied. The clipboard is sensitive and is not cleared automatically.')
      }
    } catch {
      if (mounted.current && generation.current === request) {
        setCopyError('Could not copy the key. Your browser may require clipboard permission; copy it manually or try again.')
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
                  {isPool && modelInfo.poolName && <div><dt>Pool</dt><dd>{modelInfo.poolName}</dd></div>}
                  <div><dt>{isPool ? 'Model name to send' : 'Deployment'}</dt><dd>{modelInfo.deploymentName}</dd></div>
                  <div><dt>Tenant</dt><dd>{modelInfo.tenantId}</dd></div>
                  {modelInfo.costCenter && <div><dt>Cost center</dt><dd>{modelInfo.costCenter.name} ({modelInfo.costCenter.code})</dd></div>}
                  {modelInfo.costCenterHeader && modelInfo.costCenter && <div><dt>Cost center header</dt><dd>{modelInfo.costCenterHeader}: {modelInfo.costCenter.code}</dd></div>}
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
                  <ModelAccessRecovery publicationId={modelInfo.publicationId} target={isPool ? 'pool' : 'model'} />
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
                {(isPool
                  ? [
                      ...describeLimits({ enforcement: modelInfo.grantLimits }),
                      ...describePoolSafeguard(modelInfo.publicationLimits, modelInfo.tokenMetering !== false).map(
                        (limit) => `Pool: ${limit}`,
                      ),
                    ]
                  : describeLimits({ enforcement: modelInfo.grantLimits }, modelInfo.publicationLimits)
                ).map((limit) => <Text key={limit}>{limit}</Text>)}
                {sharedWith.length > 0 && (
                  <Text>
                    This grant&apos;s key also serves {sharedNames} in the same pool, because one key serves
                    every model its holder is granted there under one cost center.
                  </Text>
                )}
                {modelInfo.keysAvailable === false ? (
                  <MessageBar intent="warning">
                    <MessageBarBody>
                      Key authentication is not available for this grant. Use an Entra token.
                    </MessageBarBody>
                  </MessageBar>
                ) : modelInfo.keysAllowedByCostCenter === false ? (
                  <MessageBar intent="warning">
                    <MessageBarBody>
                      Keys are off for this cost center. Use Microsoft Entra bearer tokens or turn keys on for the cost center and apply the model.
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
            {!keyCapable && (
              <Text>
                {isPool
                  ? 'Key reveal requires an enabled direct grant that the pool’s last apply enforces, with key authentication and allowed cost-center keys.'
                  : 'Key reveal requires an enabled, applied direct grant with key authentication, allowed cost-center keys, and a trusted orchestrated binding.'}
              </Text>
            )}
            {keyCapable && !keyExists && <Text>No key exists for this grant yet.</Text>}
            {/* A browser takes focus off a button that becomes disabled, so a busy button stays focusable. */}
            {modelInfo?.keysAvailable !== false && !isMcp && keysAllowedByCostCenter && (
              <>
                <div className={styles.rowActions}>
                  {!keyExists ? (
                    <Button
                      appearance="primary"
                      disabled={!keyCapable || createKey.isPending}
                      onClick={() => createKey.mutate()}
                    >
                      Create key
                    </Button>
                  ) : (
                    <>
                      <Button disabled={!eligible} disabledFocusable={revealing} onClick={() => void reveal('primary')}>Reveal primary key</Button>
                      <Button disabled={!eligible} disabledFocusable={revealing} onClick={() => void reveal('secondary')}>Reveal secondary key</Button>
                      <Button disabled={!eligible || rotateKey.isPending} onClick={() => setConfirmation('rotate-primary')}>Rotate primary key</Button>
                      <Button disabled={!eligible || rotateKey.isPending} onClick={() => setConfirmation('rotate-secondary')}>Rotate secondary key</Button>
                      <Button disabled={!eligible || deleteKey.isPending} onClick={() => setConfirmation('delete')}>Delete key</Button>
                    </>
                  )}
                </div>
                {keyMessage && <Text role="status">{keyMessage}</Text>}
              </>
            )}
            {revealing && <Loading label="Retrieving the current key from APIM" />}
            {secret && eligible && (
              <div className={styles.cellStack}>
                <Text weight="semibold">Revealed {secret.slot} key</Text>
                <pre data-secret="true" aria-label={`Revealed ${secret.slot} key`} className={styles.secretValue}>{secret.key}</pre>
                <Button onClick={() => void copy()}>Copy revealed key</Button>
              </div>
            )}
            {error && (
              <div ref={errorRef} tabIndex={-1}>
                <MessageBar intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>
              </div>
            )}
            {/* A copy that fails leaves focus on Copy, to try again, so its reason is an alert that a screen reader reads. */}
            {copyError && <MessageBar intent="error" role="alert"><MessageBarBody>{copyError}</MessageBarBody></MessageBar>}
            {copyMessage && <Text role="status">{copyMessage}</Text>}
            {/* Always present, so a screen reader announces each reveal. It says which key, never the key itself. */}
            <span className={styles.srOnly} role="status">
              {secret && eligible && `${secret.slot === 'primary' ? 'Primary' : 'Secondary'} key revealed.`}
            </span>
          </DialogContent>
          <DialogActions><Button onClick={close}>Close</Button></DialogActions>
        </DialogBody>
      </DialogSurface>
      <Dialog open={confirmation !== null} onOpenChange={(_, data) => !data.open && setConfirmation(null)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>
              {confirmation === 'delete' ? 'Delete key' : confirmation === 'rotate-primary' ? 'Rotate primary key' : 'Rotate secondary key'}
            </DialogTitle>
            <DialogContent>
              <Text>
                {confirmation === 'delete'
                  ? 'Every client using this key stops working now. You can create a new key afterwards.'
                  : `Clients using the old ${confirmation === 'rotate-primary' ? 'primary' : 'secondary'} value stop working now. The other slot keeps working.`}
                {sharedWith.length > 0 && ` The same key serves ${sharedNames}, so this changes those models too.`}
              </Text>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setConfirmation(null)}>Cancel</Button>
              <Button
                appearance="primary"
                onClick={() => {
                  if (confirmation === 'delete') deleteKey.mutate()
                  if (confirmation === 'rotate-primary') rotateKey.mutate('primary')
                  if (confirmation === 'rotate-secondary') rotateKey.mutate('secondary')
                }}
              >
                {confirmation === 'delete' ? 'Delete key' : 'Rotate key'}
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </Dialog>
  )
}
