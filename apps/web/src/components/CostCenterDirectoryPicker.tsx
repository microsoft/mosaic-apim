import { Badge, Button, Field, Input, Select, Text } from '@fluentui/react-components'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { DirectoryObject, DirectorySearchKind } from '../types'
import { ErrorState } from './AsyncState'
import styles from './CostCenterSetup.module.css'

export function CostCenterDirectoryPicker({ group = false, selected, onSelect, disabled = false }: {
  group?: boolean
  selected: DirectoryObject | null
  onSelect: (value: DirectoryObject | null) => void
  disabled?: boolean
}) {
  const api = useMosaicApi()
  const [kind, setKind] = useState<DirectorySearchKind>(group ? 'group' : 'user')
  const [query, setQuery] = useState('')
  const [searchText, setSearchText] = useState('')
  const [objectId, setObjectId] = useState('')
  const lookupRevision = useRef(0)
  useEffect(() => {
    const timer = window.setTimeout(() => setSearchText(query.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [query])
  const search = useQuery({
    queryKey: ['directory', 'search', kind, searchText],
    queryFn: () => api.searchDirectory(kind, searchText, 20),
    enabled: !disabled && searchText.length >= 2,
    retry: false,
  })
  const lookup = useMutation({
    mutationFn: ({ id }: { id: string; revision: number }) => api.getDirectoryObject(id),
    onSuccess: (value, { revision }) => {
      if (revision !== lookupRevision.current) return
      if (group ? value.kind === 'securityGroup' : value.kind !== 'securityGroup') onSelect(value)
    },
    retry: false,
  })
  const { reset: resetLookup } = lookup
  useEffect(() => {
    resetLookup()
    return () => { lookupRevision.current += 1 }
  }, [disabled, group, selected, resetLookup])
  function select(value: DirectoryObject | null) {
    lookupRevision.current += 1
    resetLookup()
    onSelect(value)
  }
  const current = query.trim() === searchText && searchText.length >= 2
  const wrongKind = lookup.data && (group ? lookup.data.kind !== 'securityGroup' : lookup.data.kind === 'securityGroup')
  return <div className={styles.stack}>
    <div className={styles.grid}>
      {!group && <Field label="Directory identity type">
        <Select value={kind} aria-label="Directory identity type" disabled={disabled}
          onChange={(_, data) => { setKind(data.value as DirectorySearchKind); select(null) }}>
          <option value="user">People</option>
          <option value="application">Applications &amp; managed identities</option>
          <option value="agent">Agents</option>
        </Select>
      </Field>}
      <Field label={group ? 'Find Entra security group' : 'Find directory member'} hint="Enter at least 2 characters. Search is bounded to 20 results.">
        <Input aria-label={group ? 'Find Entra security group' : 'Find directory member'} value={query} disabled={disabled}
          onChange={(_, data) => { setQuery(data.value); select(null) }}
          onKeyDown={(event) => { if (event.key === 'Enter') event.preventDefault() }} />
      </Field>
    </div>
    {current && search.isPending && <Text role="status">Searching Entra…</Text>}
    {current && search.isError && <div className={styles.stack}>
      <ErrorState title="Directory search unavailable" error={search.error} />
      <Button type="button" disabled={search.isFetching} onClick={() => void search.refetch()}>Retry directory search</Button>
    </div>}
    {current && search.isSuccess && <ul className={styles.results} aria-label={group ? 'Security group results' : 'Directory member results'}>
      {search.data.length === 0 && <li>No matching identities. Try another name or verify an object ID.</li>}
      {search.data.map((value) => {
        const supported = group ? value.kind === 'securityGroup' : value.kind !== 'securityGroup'
        return <li key={`${value.kind}:${value.objectId}`}>
          <button type="button" className={styles.choice} disabled={disabled || !supported}
            aria-pressed={selected?.objectId === value.objectId}
            onClick={() => select(value)}>
            <strong>{value.displayName || 'Unnamed directory identity'}</strong>
            <span>{PRINCIPAL_KIND_LABELS[value.kind]}{value.detail ? ` · ${value.detail}` : ''}</span>
            <span className={styles.technical}>Object ID: {value.objectId}</span>
            {!supported && <span>Nested groups are not editable in this member workflow.</span>}
          </button>
        </li>
      })}
    </ul>}
    <details>
      <summary>Use an object ID</summary>
      <div className={styles.stack}>
        <Field label={group ? 'Group object ID' : 'Member object ID'}>
          <Input aria-label={group ? 'Group object ID' : 'Member object ID'} value={objectId} disabled={disabled}
            onChange={(_, data) => { setObjectId(data.value); select(null) }} />
        </Field>
        <Button type="button" disabled={disabled || !objectId.trim() || lookup.isPending}
          onClick={() => lookup.mutate({ id: objectId.trim(), revision: ++lookupRevision.current })}>Verify directory identity</Button>
        {lookup.isError && <ErrorState title="Identity verification failed" error={lookup.error} />}
        {wrongKind && <Text>Unsupported identity type. {group ? 'Choose an Entra security group.' : 'Choose a person or workload identity; nested groups are not supported here.'}</Text>}
      </div>
    </details>
    {selected && <div className={styles.stack} role="status">
      <Text>Selected: <strong>{selected.displayName || 'Unnamed directory identity'}</strong></Text>
      <div><Badge appearance="tint">{PRINCIPAL_KIND_LABELS[selected.kind]}</Badge></div>
      <Text className={styles.technical}>Entra object ID: {selected.objectId}</Text>
      <Text className={styles.muted}>Selection alone does not add a MOSAIC principal or change Entra membership.</Text>
    </div>}
  </div>
}
