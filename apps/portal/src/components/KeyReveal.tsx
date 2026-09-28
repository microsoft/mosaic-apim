import {
  Button,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Text,
} from '@fluentui/react-components'
import { CopyRegular, EyeOffRegular, EyeRegular } from '@fluentui/react-icons'
import { useMutation } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { usePortalApi } from '../api'
import {
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
  const problem = reveal.isError ? keyRevealProblem(reveal.error) : null

  return (
    <section className="connection-section" aria-labelledby={headingId} ref={section}>
      <h3 id={headingId} ref={heading} tabIndex={-1}>
        Subscription key
      </h3>
      <Text as="p" size={200} className="connection-note">
        APIM holds your keys. MOSAIC reads the current key only when you ask, shows it for 60
        seconds, and never saves it in this browser. Anyone with the key can use this grant, so do
        not share it.
      </Text>
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
          disabled={!availability.available}
          disabledFocusable={availability.available && reveal.isPending}
          onClick={() => show('primary')}
        >
          Show primary key
        </Button>
        <Button
          ref={(element) => {
            showButtons.current.secondary = element
          }}
          icon={<EyeRegular />}
          disabled={!availability.available}
          disabledFocusable={availability.available && reveal.isPending}
          onClick={() => show('secondary')}
        >
          Show secondary key
        </Button>
      </div>
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
