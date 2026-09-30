import {
  Badge,
  Button,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Select,
  Text,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { DirectoryObject, DirectorySearchKind, Principal, PrincipalKind } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { PrincipalKindBadge } from './PrincipalKindBadge'
import styles from './DirectoryPrincipalPicker.module.css'

const searchKindLabels: Record<DirectorySearchKind, string> = {
  user: 'People',
  agent: 'Agents',
  group: 'Security groups',
}

function directoryErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 409 && error.body?.code === 'directory_disabled') {
      return 'Directory search is off. Use manual entry instead.'
    }
    if (error.status === 403 && error.body?.code === 'directory_forbidden') {
      return error.message
    }
    if (error.status === 502) {
      return "Couldn't reach Microsoft Graph. Try again."
    }
  }
  return error instanceof Error ? error.message : 'Directory search failed.'
}

function resultLabel(result: DirectoryObject) {
  return result.displayName?.trim() || result.objectId
}

export function DirectoryPrincipalPicker({
  kind: selectedKind,
  onKindChange,
  onCreated,
  onManualFallback,
}: {
  /** What to search for. Leave it unset and the picker keeps its own choice, starting with people. */
  kind?: DirectorySearchKind
  onKindChange?: (kind: DirectorySearchKind) => void
  onCreated?: (principal: Principal) => void
  onManualFallback?: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [ownKind, setOwnKind] = useState<DirectorySearchKind>('user')
  const kind = selectedKind ?? ownKind
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')

  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedQuery(query.trim()), 300)
    return () => window.clearTimeout(handle)
  }, [query])

  const search = useQuery({
    queryKey: ['directory', 'search', kind, debouncedQuery],
    queryFn: () => api.searchDirectory(kind, debouncedQuery, 20),
    enabled: debouncedQuery.length >= 2,
  })

  useEffect(() => {
    if (search.error instanceof ApiError && search.error.status === 409 && search.error.body?.code === 'directory_disabled') {
      onManualFallback?.()
    }
  }, [onManualFallback, search.error])

  const createPrincipal = useMutation({
    mutationFn: (result: DirectoryObject) =>
      api.createPrincipal({
        objectId: result.objectId,
        kind: result.kind,
        label: result.displayName?.trim() || undefined,
      }),
    onSuccess: async (principal) => {
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      await queryClient.invalidateQueries({ queryKey: ['directory', 'search'] })
      onCreated?.(principal)
    },
  })

  const status = useMemo(() => {
    if (debouncedQuery.length < 2) {
      return 'Enter at least 2 characters to search.'
    }
    if (search.isPending) {
      return 'Searching directory.'
    }
    if (search.isSuccess) {
      return `${search.data.length} result${search.data.length === 1 ? '' : 's'} found.`
    }
    return ''
  }, [debouncedQuery.length, search.data?.length, search.isPending, search.isSuccess])

  function add(result: DirectoryObject) {
    if (result.principalId) {
      return
    }
    createPrincipal.reset()
    createPrincipal.mutate(result)
  }

  return (
    <div className={styles.picker}>
      <div className={styles.searchRow}>
        <Field label="Search for">
          <Select
            value={kind}
            aria-label="Directory search kind"
            onChange={(_, data) => {
              const nextKind = data.value as DirectorySearchKind
              setOwnKind(nextKind)
              onKindChange?.(nextKind)
            }}
          >
            {Object.entries(searchKindLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Directory search">
          <Input
            value={query}
            aria-label="Directory search"
            placeholder="Type a name, UPN, app ID, or mail nickname"
            onChange={(_, data) => setQuery(data.value)}
          />
        </Field>
      </div>

      <div role="status" aria-live="polite" className={styles.liveRegion}>
        {status}
      </div>
      <Text size={200}>{status}</Text>

      {search.isError && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Directory search failed</MessageBarTitle>
            {directoryErrorMessage(search.error)}
          </MessageBarBody>
        </MessageBar>
      )}
      {createPrincipal.isError && <ErrorState title="Unable to add identity" error={createPrincipal.error} />}
      {search.isPending && debouncedQuery.length >= 2 && <Loading label="Searching directory" />}

      {search.data && search.data.length > 0 && (
        <ul className={styles.results} aria-label="Directory search results">
          {search.data.map((result) => {
            const alreadyAdded = Boolean(result.principalId)
            return (
              <li key={`${result.kind}:${result.objectId}`}>
                <button
                  type="button"
                  className={styles.resultButton}
                  disabled={alreadyAdded || createPrincipal.isPending}
                  aria-label={`${alreadyAdded ? 'Already added: ' : 'Add '}${resultLabel(result)}`}
                  onClick={() => add(result)}
                >
                  <span className={styles.resultHeader}>
                    <PrincipalKindBadge kind={result.kind as PrincipalKind} />
                    {alreadyAdded && <Badge appearance="tint">Already added</Badge>}
                    <span className={styles.resultName}>{resultLabel(result)}</span>
                  </span>
                  {result.detail && <span className={styles.secondary}>{result.detail}</span>}
                  {result.kind === 'agentUser' && result.identityParentId && (
                    <span className={styles.secondary}>
                      Parent agent {result.identityParentId}
                    </span>
                  )}
                  {result.blueprintId && (
                    <span className={styles.secondary}>Blueprint {result.blueprintId}</span>
                  )}
                  <span className={styles.secondary}>
                    {PRINCIPAL_KIND_LABELS[result.kind]} · {result.objectId}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      <Button
        appearance="secondary"
        type="button"
        className={styles.manualEntry}
        onClick={onManualFallback}
      >
        Use manual entry
      </Button>
    </div>
  )
}
