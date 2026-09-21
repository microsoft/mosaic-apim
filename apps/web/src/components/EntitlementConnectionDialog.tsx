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
import { runtimeConfig } from '../runtime-config'
import type { Entitlement, KeySlot } from '../types'
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
  const connection = useQuery({
    queryKey: ['entitlement-connection', identity, entitlement.id],
    queryFn: () => api.getEntitlementConnection(entitlement.id),
  })
  const info = connection.data
  const eligible = Boolean(
    !closed && !connection.isError && info
    && entitlement.enabled && entitlement.subject.kind !== 'group'
    && entitlement.resource.kind === 'modelApi' && entitlement.binding?.source === 'orchestrated'
    && entitlement.runtime?.status === 'applied' && entitlement.runtime.appliedMethods?.keysEnabled
    && info.runtime?.status === 'applied' && info.appliedMethods?.keysEnabled
    && info.entitlementId === entitlement.id
    && info.publicationId === entitlement.runtime.publicationId,
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
      // Remove the value before a full-page navigation can freeze this document in bfcache.
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
      // Never place credential material in a query, mutation, shared cache, or persistent store.
      const result = await api.revealEntitlementKey(entitlement.id, slot, abort.signal)
      if (!mounted.current || generation.current !== request) return
      if (result.entitlementId !== entitlement.id || result.slot !== slot || !result.key
        || (info?.runtime?.subscriptionName && result.subscriptionName !== info.runtime.subscriptionName)) {
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
          <DialogTitle>Model connection and keys</DialogTitle>
          <DialogContent className={styles.dialogForm}>
            <Text>Connection metadata is not proof of a successful live invocation. Allow for APIM propagation.</Text>
            {runtimeConfig.authMode === 'local' && (
              <MessageBar intent="warning">
                <MessageBarBody>Local development mode: a simulated response does not verify live APIM access.</MessageBarBody>
              </MessageBar>
            )}
            {connection.isPending && <Loading label="Loading connection information" />}
            {connection.isError && <ErrorState error={connection.error} />}
            {info && (
              <>
                <dl className={styles.detailList}>
                  <div><dt>Endpoint</dt><dd>{info.endpoint}</dd></div>
                  <div><dt>Deployment</dt><dd>{info.deploymentName}</dd></div>
                  <div><dt>Tenant</dt><dd>{info.tenantId}</dd></div>
                  <div><dt>Runtime state</dt><dd>{info.runtime?.status ?? 'unknown'}</dd></div>
                  <div><dt>Last applied methods</dt><dd>{describeAccessMethods(info.appliedMethods)}</dd></div>
                  <div><dt>Model-runtime audience</dt><dd>{info.entraAudience ?? 'Not configured'}</dd></div>
                  <div><dt>Model-runtime scope</dt><dd>{info.entraScope ?? 'Not configured'}</dd></div>
                </dl>
                <Text size={200}>A MOSAIC control-plane token is not a model-runtime token. Entra consent and application permissions are configured separately.</Text>
                {info.runtime?.error && (
                  <MessageBar intent="warning"><MessageBarBody>{info.runtime.error}</MessageBarBody></MessageBar>
                )}
                {info.runtime?.status === 'unknown' && (
                  <ModelAccessRecovery publicationId={info.publicationId} />
                )}
                {info.operations.map((operation) => (
                  <Text key={`${operation.name}:${operation.method}:${operation.path}`}>
                    {operation.method} {operation.path} · {operation.name}
                  </Text>
                ))}
                <Text weight="semibold">Last recorded limits</Text>
                {describeLimits({ enforcement: info.grantLimits }, info.publicationLimits).map((limit) => <Text key={limit}>{limit}</Text>)}
                <Text>Use one enabled credential. Examples contain placeholders, never your actual key:</Text>
                <pre className={styles.secretValue}>{`${info.subscriptionHeader}: <YOUR_APIM_KEY>\n\nOR\n\nAuthorization: Bearer <MODEL_RUNTIME_TOKEN>`}</pre>
              </>
            )}
            <Text>
              APIM holds the keys. Reveal fetches the selected current key only on your explicit
              request. It is cleared from this dialog on close, navigation, or account change.
              Sharing a key delegates this grant&apos;s access.
            </Text>
            {!eligible && <Text>Key reveal requires an enabled, applied direct grant with key authentication and a trusted orchestrated binding.</Text>}
            <div className={styles.rowActions}>
              <Button disabled={!eligible || revealing} onClick={() => void reveal('primary')}>Reveal primary key</Button>
              <Button disabled={!eligible || revealing} onClick={() => void reveal('secondary')}>Reveal secondary key</Button>
            </div>
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
