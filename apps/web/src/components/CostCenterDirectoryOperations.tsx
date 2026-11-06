import { Badge, Button, MessageBar, MessageBarBody, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import type { CostCenterDirectoryOperationView } from '../types'
import { ErrorState } from './AsyncState'
import styles from './CostCenterSetup.module.css'

const KIND_LABELS: Record<CostCenterDirectoryOperationView['kind'], string> = {
  createGroup: 'Create group', addMember: 'Add member', removeMember: 'Remove member',
}
const STATE_LABELS: Record<CostCenterDirectoryOperationView['state'], string> = {
  pending: 'Pending · Not confirmed', succeeded: 'Directory updated', failed: 'Failed · Not updated', unknown: 'Unknown outcome',
}

function DirectoryOperation({ value }: { value: CostCenterDirectoryOperationView }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [needsObservation, setNeedsObservation] = useState(false)
  const action = useMutation({
    mutationFn: (kind: 'refresh' | 'retry' | 'recover') => kind === 'refresh'
      ? api.getCostCenterDirectoryOperation(value.costCenterId, value.id)
      : kind === 'retry'
        ? api.retryCostCenterDirectoryOperation(value.costCenterId, value.id)
        : api.recoverCostCenterDirectoryOperation(value.costCenterId, value.id),
    retry: false,
    onMutate: (kind) => { if (kind === 'retry') setNeedsObservation(true) },
    onSuccess: async (operation) => {
      setNeedsObservation(false)
      queryClient.setQueryData<CostCenterDirectoryOperationView[]>(['cost-centers', value.costCenterId, 'directory-operations'],
        (current) => current?.map((item) => item.id === operation.id ? operation : item))
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['cost-centers'] }),
        queryClient.invalidateQueries({ queryKey: ['cost-center-requests'] }),
        ...(operation.state === 'succeeded' && operation.directoryUpdated
          ? [queryClient.invalidateQueries({ queryKey: ['entitlements'] })] : []),
      ])
    },
  })
  const uncertain = value.state === 'pending' || value.state === 'unknown'
  const confirmed = value.state === 'succeeded' && value.directoryUpdated
  const details = value.errorDetails
  return <li className={styles.operation}>
    <div className={styles.operationHeader}>
      <Text weight="semibold">{KIND_LABELS[value.kind]}</Text>
      <Badge appearance="tint" color={needsObservation ? 'warning' : confirmed ? 'success' : value.state === 'failed' ? 'danger' : 'warning'}>
        {needsObservation ? 'Outcome needs refresh' : value.state === 'succeeded' && !value.directoryUpdated ? 'Outcome not confirmed' : STATE_LABELS[value.state]}
      </Badge>
    </div>
    <Text>Operation ID: <code>{value.id}</code></Text>
    {value.groupObjectId && <Text>Group: <code>{value.groupObjectId}</code></Text>}
    {value.memberObjectId && <Text>Member: <code>{value.memberObjectId}</code></Text>}
    <Text size={200}>Updated: {new Date(value.updatedAt).toLocaleString()}</Text>
    {value.errorMessage && <MessageBar intent={uncertain ? 'warning' : 'error'}><MessageBarBody>
      {value.errorMessage}{value.errorCode ? ` (${value.errorCode})` : ''}
    </MessageBarBody></MessageBar>}
    {details && <details>
      <summary>Directory error details</summary>
      <dl className={styles.facts}>
        {details.httpStatus && <div><dt>HTTP status</dt><dd>{details.httpStatus}</dd></div>}
        {details.graphCode && <div><dt>Graph error code</dt><dd>{details.graphCode}</dd></div>}
        {details.graphMessage && <div><dt>Graph message</dt><dd>{details.graphMessage}</dd></div>}
        {details.requestId && <div><dt>Graph request ID</dt><dd>{details.requestId}</dd></div>}
        {details.clientRequestId && <div><dt>Client request ID</dt><dd>{details.clientRequestId}</dd></div>}
        {details.retryAfter != null && <div><dt>Retry-after observation</dt><dd>{details.retryAfter} seconds. Recheck current permissions and throttling before a safe retry.</dd></div>}
      </dl>
    </details>}
    {value.recoveryNotice && <Text>{value.recoveryNotice}</Text>}
    {uncertain && <Text>
      Do not repeat this write. Investigate the recorded operation and actual Entra state.
      {value.kind === 'createGroup' && !value.groupObjectId ? ' A display-name match is not proof of identity; only an administrator can investigate an unrecorded creation outcome.' : ' Recover is a read-only reconciliation, not a resubmission.'}
    </Text>}
    {value.propagationNotice && <Text>{value.propagationNotice}</Text>}
    {value.keyRevocationNotice && <Text>{value.keyRevocationNotice}</Text>}
    {action.isError && <ErrorState title="Operation action did not finish" error={action.error} />}
    {needsObservation && !action.isPending && <Text>The retry response is unavailable and may have reached Entra. Refresh this durable operation before another write. A previous Failed status is not proof that the retried operation failed.</Text>}
    <div className={styles.actions}>
      <Button type="button" disabled={action.isPending} onClick={() => action.mutate('refresh')}>Refresh operation</Button>
      {value.state === 'failed' && value.canRetry && !needsObservation && <Button type="button" disabled={action.isPending} onClick={() => action.mutate('retry')}>
        Retry recorded failed operation
      </Button>}
      {uncertain && <Button type="button" disabled={action.isPending} onClick={() => action.mutate('recover')}>Recover without repeating write</Button>}
    </div>
  </li>
}

export function CostCenterDirectoryOperations({ costCenterId }: { costCenterId: string }) {
  const api = useMosaicApi()
  const operations = useQuery({
    queryKey: ['cost-centers', costCenterId, 'directory-operations'],
    queryFn: () => api.listCostCenterDirectoryOperations(costCenterId),
    retry: false,
  })
  return <section className={styles.section} aria-label="Directory operations">
    <div className={styles.operationHeader}>
      <Title3 as="h3">Directory operations</Title3>
      <Button disabled={operations.isFetching} onClick={() => void operations.refetch()}>Refresh operation history</Button>
    </div>
    <Text>Durable directory outcomes, separate from center setup and gateway apply. Only a confirmed Directory updated result reports a completed write.</Text>
    {operations.isPending && <Text role="status">Loading recorded directory operations…</Text>}
    {operations.isError && <ErrorState title="Directory operation history unavailable" error={operations.error} />}
    {operations.isSuccess && operations.data.length === 0 && <Text>No directory writes are recorded for this center.</Text>}
    <ul className={styles.operations}>
      {operations.data?.map((operation) => <DirectoryOperation key={operation.id} value={operation} />)}
    </ul>
  </section>
}
