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
  application: 'Applications & managed identities',
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
  return result.displayName?.trim() || `Unnamed ${PRINCIPAL_KIND_LABELS[result.kind].toLowerCase()}`
}

export function DirectoryPrincipalPicker({
  kind: selectedKind,
  onKindChange,
  onCreated,
  onManualFallback,
  defaultCostCenterId,
}: {
  /** What to search for. Leave it unset and the picker keeps its own choice, starting with people. */
  kind?: DirectorySearchKind
  onKindChange?: (kind: DirectorySearchKind) => void
  onCreated?: (principal: Principal) => void
  onManualFallback?: () => void
  defaultCostCenterId?: string | null
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

  const currentResults = query.trim() === debouncedQuery && debouncedQuery.length >= 2

  const createPrincipal = useMutation({
    mutationFn: (result: DirectoryObject) =>
      api.createPrincipal({
        objectId: result.objectId,
        kind: result.kind,
        label: result.displayName?.trim() || undefined,
        ...(result.kind !== 'securityGroup' && defaultCostCenterId
          ? { defaultCostCenterId }
          : {}),
      }),
    onSuccess: async (principal) => {
      await queryClient.invalidateQueries({ queryKey: ['principals'] })
      await queryClient.invalidateQueries({ queryKey: ['directory', 'search'] })
      onCreated?.(principal)
    },
  })

  const status = useMemo(() => {
    if (query.trim().length < 2) {
      return 'Enter at least 2 characters to search.'
    }
    if (!currentResults) return 'Waiting to search.'
    if (search.isPending) {
      return 'Searching directory.'
    }
    if (search.isSuccess) {
      return `${search.data.length} result${search.data.length === 1 ? '' : 's'} found.`
    }
    return ''
  }, [query, currentResults, search.data?.length, search.isPending, search.isSuccess])

  function add(result: DirectoryObject) {
    if (result.principalId || !currentResults || createPrincipal.isPending) {
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
            placeholder={kind === 'application' ? 'Type a name prefix, object ID, or app ID' : 'Type a name, UPN, app ID, or mail nickname'}
            onChange={(_, data) => setQuery(data.value)}
          />
        </Field>
      </div>

      <div role="status" aria-live="polite" className={styles.liveRegion}>
        {status}
      </div>
      <Text size={200}>{status}</Text>

      {currentResults && search.isError && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Directory search failed</MessageBarTitle>
            {directoryErrorMessage(search.error)}
            <div>
              <Button onClick={() => search.refetch()} disabled={search.isFetching}>Retry directory search</Button>
              {' '}<a href="/settings?tab=runtime">Check directory configuration</a>
            </div>
          </MessageBarBody>
        </MessageBar>
      )}
      {createPrincipal.isError && <ErrorState title="Unable to add identity" error={createPrincipal.error} />}
      {search.isPending && currentResults && <Loading label="Searching directory" />}

      {currentResults && search.isSuccess && search.data.length === 0 && (
        <Text>No matching directory identities. Try another name or use manual entry.</Text>
      )}
      {currentResults && search.data && search.data.length > 0 && (
        <ul className={styles.results} aria-label="Directory search results">
          {search.data.map((result) => {
            const alreadyAdded = Boolean(result.principalId)
            const ambiguous = (search.data ?? []).filter((item) => resultLabel(item) === resultLabel(result)).length > 1
            const choiceLabel = ambiguous
              ? `${resultLabel(result)} (${PRINCIPAL_KIND_LABELS[result.kind]}; ${result.detail || result.appId || result.objectId})`
              : resultLabel(result)
            return (
              <li key={`${result.kind}:${result.objectId}`}>
                <button
                  type="button"
                  className={styles.resultButton}
                  disabled={alreadyAdded || createPrincipal.isPending}
                  aria-label={`${alreadyAdded ? 'Already added: ' : 'Add '}${choiceLabel}`}
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
                </button>
                <details className={styles.secondary}>
                  <summary>Technical details for {resultLabel(result)}</summary>
                  Microsoft Entra · {PRINCIPAL_KIND_LABELS[result.kind]} · {result.objectId}
                  {result.appId && <div>Application ID: {result.appId}</div>}
                </details>
              </li>
            )
          })}
        </ul>
      )}

      {onManualFallback && <Button
        appearance="secondary"
        type="button"
        className={styles.manualEntry}
        onClick={onManualFallback}
      >
        Use manual entry
      </Button>}
    </div>
  )
}
