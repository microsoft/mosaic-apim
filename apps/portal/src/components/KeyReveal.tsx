import {
  Button,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Text,
} from '@fluentui/react-components'
import { CopyRegular, EyeOffRegular, EyeRegular } from '@fluentui/react-icons'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { usePortalApi } from '../api'
import {
  GROUP_GRANT_ENTRA_ONLY,
  KEY_VISIBLE_MS,
  UnexpectedCredentialError,
  isConflict,
  isExpectedKey,
  keyAvailability,
  keyRevealProblem,
  type KeyAvailability,
} from '../connection-format'
import { useFocusHandoff, type FocusHandoff } from '../focus-handoff'
import type { KeySlot, ModelConnection } from '../types'

interface RevealVariables {
  slot: KeySlot
  request: number
  signal: AbortSignal
}

/** Everything the mutation cache sees. The key itself is never part of it. */
interface RevealOutcome {
  slot: KeySlot
  shown: boolean
}

const slotLabels: Record<KeySlot, string> = {
  primary: 'Primary key',
  secondary: 'Secondary key',
}

/**
 * Reveals one APIM subscription key on explicit request. The key lives only in this
 * component's state: never in a query or mutation cache, browser storage, the URL, or logs.
 * Parents remount this component (via `key`) whenever the account, route, grant, or applied
 * state changes, which discards any key on screen. If keyboard focus was inside when that
 * happens, `focusHandoff` moves it to the new instance's explanation or heading.
 */
export function KeyReveal({
  entitlementId,
  connection,
  onConflict,
  focusHandoff,
}: {
  entitlementId: string
  connection: ModelConnection
  onConflict: () => void
  focusHandoff?: FocusHandoff
}) {
  const api = usePortalApi()
  const queryClient = useQueryClient()
  const headingId = useId()
  const availability: KeyAvailability =
    connection.entitlementId === entitlementId
      ? keyAvailability(connection)
      : {
          available: false,
          reason: 'The connection details do not match this grant. Refresh My access.',
        }
  const [secret, setSecret] = useState<{ slot: KeySlot; key: string } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [copyFailed, setCopyFailed] = useState(false)
  const [confirming, setConfirming] = useState<{ kind: 'rotate'; slot: KeySlot } | { kind: 'delete' } | null>(null)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const mounted = useRef(false)
  const display = useRef<HTMLDivElement | null>(null)
  const showButtons = useRef<Record<KeySlot, HTMLElement | null>>({
    primary: null,
    secondary: null,
  })
  const section = useRef<HTMLElement | null>(null)
  const heading = useRef<HTMLHeadingElement | null>(null)
  const reasonText = useRef<HTMLElement | null>(null)
  useFocusHandoff(focusHandoff, section, reasonText, heading)

  const reveal = useMutation<RevealOutcome, Error, RevealVariables>({
    gcTime: 0,
    retry: false,
    mutationFn: async ({ slot, request, signal }) => {
      const current = () => mounted.current && generation.current === request
      try {
        const result = await api.revealMyEntitlementKey(entitlementId, slot, signal)
        if (!current()) return { slot, shown: false }
        if (!isExpectedKey(result, connection, slot)) throw new UnexpectedCredentialError()
        setSecret({ slot, key: result.key })
        setNotice(`${slotLabels[slot]} shown. It hides automatically after 60 seconds.`)
        return { slot, shown: true }
      } catch (failure) {
        // A superseded or abandoned request must neither show a key nor report an error.
        if (!current()) return { slot, shown: false }
        throw failure
      }
    },
    onError: (failure) => {
      if (isConflict(failure)) onConflict()
    },
  })
  const keyExists = connection.keyExists ?? connection.runtime?.keyExists ?? true
  const costCenterLabel = connection.costCenter
    ? `${connection.costCenter.name} (${connection.costCenter.code})`
    : 'this grant'
  const refreshAfterKeyChange = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['portal', 'entitlements'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'connection'] }),
    ])
    onConflict()
  }
  const createKey = useMutation({
    mutationFn: () => api.createMyEntitlementKey(entitlementId),
    onMutate: () => {
      generation.current += 1
      controller.current?.abort()
      setSecret(null)
      setNotice(null)
      setCopyFailed(false)
    },
    onSuccess: async () => {
      setNotice('Key created. You can show either slot now.')
      await refreshAfterKeyChange()
    },
    onError: (failure) => {
      if (isConflict(failure)) onConflict()
    },
  })
  const rotateKey = useMutation({
    mutationFn: (slot: KeySlot) => api.rotateMyEntitlementKey(entitlementId, slot),
    onMutate: () => {
      generation.current += 1
      controller.current?.abort()
      setSecret(null)
      setNotice(null)
      setCopyFailed(false)
    },
    onSuccess: async (_result, slot) => {
      setConfirming(null)
      setNotice(`${slotLabels[slot]} rotated. Apps using that old value must be updated.`)
      await refreshAfterKeyChange()
    },
    onError: (failure) => {
      if (isConflict(failure)) onConflict()
    },
  })
  const deleteKey = useMutation({
    mutationFn: () => api.deleteMyEntitlementKey(entitlementId),
    onMutate: () => {
      generation.current += 1
      controller.current?.abort()
      setSecret(null)
      setNotice(null)
      setCopyFailed(false)
    },
    onSuccess: async () => {
      setConfirming(null)
      setNotice('Key deleted. You can create a new one if you need key access again.')
      await refreshAfterKeyChange()
    },
    onError: (failure) => {
      if (isConflict(failure)) onConflict()
    },
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
      // Remove the key before a full-page navigation can freeze this document in the bfcache.
      flushSync(() => {
        setSecret(null)
        setNotice(null)
        setCopyFailed(false)
      })
    }
    window.addEventListener('pagehide', clearBeforePageHide)
    return () => window.removeEventListener('pagehide', clearBeforePageHide)
  }, [])

  useEffect(() => {
    if (!secret) return
    const timer = setTimeout(() => {
      const hadFocus = display.current?.contains(document.activeElement) ?? false
      setSecret(null)
      setCopyFailed(false)
      setNotice('Key hidden automatically after 60 seconds.')
      if (hadFocus) showButtons.current[secret.slot]?.focus()
    }, KEY_VISIBLE_MS)
    return () => clearTimeout(timer)
  }, [secret])

  function show(slot: KeySlot) {
    if (!availability.available) return
    const request = ++generation.current
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    setSecret(null)
    setNotice(null)
    setCopyFailed(false)
    reveal.mutate({ slot, request, signal: abort.signal })
  }

  function hide() {
    const slot = secret?.slot ?? 'primary'
    generation.current += 1
    controller.current?.abort()
    setSecret(null)
    setCopyFailed(false)
    setNotice('Key hidden.')
    showButtons.current[slot]?.focus()
  }

  async function copy() {
    if (!secret) return
    const request = generation.current
    setCopyFailed(false)
    setNotice(null)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(secret.key)
      if (mounted.current && generation.current === request) {
        setNotice('Key copied. The clipboard is sensitive and is not cleared automatically.')
      }
    } catch {
      if (mounted.current && generation.current === request) setCopyFailed(true)
    }
  }

  const visible = availability.available ? secret : null
  const keyActionError = createKey.error ?? rotateKey.error ?? deleteKey.error
  const problem = reveal.isError
    ? keyRevealProblem(reveal.error)
    : keyActionError
      ? keyRevealProblem(keyActionError)
      : null
  const entraOnlyGroupGrant = !availability.available && availability.reason === GROUP_GRANT_ENTRA_ONLY
  // Applies don't create keys: until someone asks for one, there's nothing to show or rotate.
  const awaitingKey = availability.available && !keyExists
  const costCenterKeysOff =
    !availability.available && availability.reason.startsWith('Subscription keys are turned off for this cost center')

  if (entraOnlyGroupGrant || costCenterKeysOff) {
    return (
      <section className="connection-section" aria-labelledby={headingId} ref={section}>
        <h3 id={headingId} ref={heading} tabIndex={-1}>
          Subscription key
        </h3>
        <Text as="p" className="connection-note" ref={reasonText} tabIndex={-1}>
          {availability.reason}
        </Text>
      </section>
    )
  }

  return (
    <section className="connection-section" aria-labelledby={headingId} ref={section}>
      <h3 id={headingId} ref={heading} tabIndex={-1}>
        Subscription key
      </h3>
      <Text as="p" size={200} className="connection-note">
        This grant charges {costCenterLabel}. APIM holds your keys. MOSAIC reads the current key
        only when you ask, shows it for 60 seconds, and never saves it in this browser. Anyone with
        the key can use this grant, so do not share it.
      </Text>
      {awaitingKey && (
        <>
          <Text as="p" size={200} className="connection-note">
            No subscription key exists yet. Create one when an app needs key-based access.
          </Text>
          <div className="key-actions">
            <Button
              appearance="primary"
              onClick={() => createKey.mutate()}
              disabled={createKey.isPending}
            >
              Create key
            </Button>
          </div>
        </>
      )}
      {!awaitingKey && (
        <>
          <div className="key-display" ref={display}>
            <span className="key-display-label">{visible ? slotLabels[visible.slot] : 'Key'}</span>
            {visible ? (
              <code className="secret-value" data-secret="true">
                {visible.key}
              </code>
            ) : reveal.isPending ? (
              <Spinner size="tiny" label="Retrieving the current key from APIM" />
            ) : (
              <span className="masked-key">
                <span aria-hidden="true">••••••••••••••••••••••••</span>
                <span className="visually-hidden">Hidden</span>
              </span>
            )}
            {visible && (
              <div className="key-actions">
                <Button icon={<CopyRegular />} onClick={() => void copy()}>
                  Copy key
                </Button>
                <Button icon={<EyeOffRegular />} onClick={hide}>
                  Hide key
                </Button>
              </div>
            )}
          </div>
          <div className="key-actions">
            <Button
              ref={(element) => {
                showButtons.current.primary = element
              }}
              icon={<EyeRegular />}
              disabled={!availability.available || !keyExists}
              disabledFocusable={availability.available && keyExists && reveal.isPending}
              onClick={() => show('primary')}
            >
              Show primary key
            </Button>
            <Button
              ref={(element) => {
                showButtons.current.secondary = element
              }}
              icon={<EyeRegular />}
              disabled={!availability.available || !keyExists}
              disabledFocusable={availability.available && keyExists && reveal.isPending}
              onClick={() => show('secondary')}
            >
              Show secondary key
            </Button>
            {availability.available && keyExists && (
              <>
                <Button disabled={rotateKey.isPending} onClick={() => setConfirming({ kind: 'rotate', slot: 'primary' })}>
                  Rotate primary key
                </Button>
                <Button disabled={rotateKey.isPending} onClick={() => setConfirming({ kind: 'rotate', slot: 'secondary' })}>
                  Rotate secondary key
                </Button>
                <Button disabled={deleteKey.isPending} onClick={() => setConfirming({ kind: 'delete' })}>
                  Delete key
                </Button>
              </>
            )}
          </div>
        </>
      )}
      {confirming?.kind === 'rotate' && (
        <div className="inline-confirmation" role="group" aria-label={`Confirm rotate ${confirming.slot} key`}>
          <Text>
            Apps using the old {confirming.slot} value stop working. The other slot keeps working.
          </Text>
          <div className="key-actions">
            <Button appearance="primary" onClick={() => rotateKey.mutate(confirming.slot)} disabled={rotateKey.isPending}>
              Rotate {confirming.slot} key
            </Button>
            <Button onClick={() => setConfirming(null)} disabled={rotateKey.isPending}>Cancel</Button>
          </div>
        </div>
      )}
      {confirming?.kind === 'delete' && (
        <div className="inline-confirmation" role="group" aria-label="Confirm delete key">
          <Text>
            Every app using this key stops working. You can create a new one later.
          </Text>
          <div className="key-actions">
            <Button appearance="primary" onClick={() => deleteKey.mutate()} disabled={deleteKey.isPending}>
              Delete key
            </Button>
            <Button onClick={() => setConfirming(null)} disabled={deleteKey.isPending}>Cancel</Button>
          </div>
        </div>
      )}
      {!availability.available && (
        <Text as="p" className="connection-note" ref={reasonText} tabIndex={-1}>
          {availability.reason}
        </Text>
      )}
      {problem && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>{problem.title}</MessageBarTitle>
            {problem.message}
          </MessageBarBody>
        </MessageBar>
      )}
      {copyFailed && (
        <MessageBar intent="warning">
          <MessageBarBody>
            Could not copy the key. Your browser may require clipboard permission; copy it
            manually or try again.
          </MessageBarBody>
        </MessageBar>
      )}
      <Text as="p" size={200} role="status" aria-live="polite" className="key-status">
        {notice}
      </Text>
    </section>
  )
}
