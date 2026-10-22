import { Badge, Button, MessageBar, MessageBarBody, Text, Title3 } from '@fluentui/react-components'
import type { BadgeProps } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import type { DirectoryStatus, DirectoryVerificationStatus } from '../types'
import { ErrorState, Loading } from './AsyncState'
import styles from './DirectoryGroupVerification.module.css'

const STATES: Record<DirectoryVerificationStatus['state'], {
  label: string
  color: BadgeProps['color']
  description: string
}> = {
  disabled: { label: 'Unavailable', color: 'subtle', description: 'Directory lookup requires Entra authentication. This integration cannot verify group membership.' },
  notChecked: { label: 'Not checked', color: 'subtle', description: 'No complete group read has been verified in this API process. Configured lookup and consent are not functional evidence.' },
  checking: { label: 'Checking', color: 'informative', description: 'A complete group read is in progress. Status refreshes do not start another verification.' },
  ready: { label: 'Verified', color: 'success', description: 'Every transitive-members page was read successfully for the group shown below. This does not verify other groups or grant model access.' },
  stale: { label: 'Stale', color: 'warning', description: 'No current complete-read observation is available for this group. Verify again before relying on this diagnostic.' },
  forbidden: { label: 'Permission denied', color: 'danger', description: 'The group read could not be authorized. Check the API identity, tenant, group scope, and Graph permissions before retrying.' },
  throttled: { label: 'Throttled', color: 'warning', description: 'Microsoft Graph asked MOSAIC to wait. Retry after the time shown below; verification is never retried automatically.' },
  unavailable: { label: 'Read failed', color: 'danger', description: 'The complete group read did not succeed. Check directory connectivity and the reported error, then retry.' },
  cancelled: { label: 'Cancelled', color: 'warning', description: 'The group read was cancelled and did not produce successful evidence. You can verify again.' },
}

function timestamp(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Invalid timestamp reported' : date.toLocaleString()
}

export function DirectoryGroupVerification({
  groupObjectId,
}: {
  groupObjectId?: string
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const verification = useMutation({
    mutationKey: ['directory', 'group-check'],
    mutationFn: (objectId: string) => api.checkDirectoryGroup(objectId),
    retry: false,
    onSuccess: (membershipCheck) => {
      queryClient.setQueriesData<DirectoryStatus>(
        { queryKey: ['directory', 'status'] },
        (cached) => cached ? { ...cached, membershipCheck } : cached,
      )
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['directory', 'status'] }),
  })
  const status = useQuery({
    queryKey: ['directory', 'status'],
    queryFn: () => api.getDirectoryStatus(),
    retry: false,
    refetchInterval: (query) => {
      const check = query.state.data?.membershipCheck
      if (verification.isPending || check?.state === 'checking') return 2000
      const boundary = check?.state === 'ready' ? check.freshUntil : check?.retryAt
      const delay = boundary ? Date.parse(boundary) - Date.now() : NaN
      return Number.isFinite(delay) && delay > 0 ? Math.max(1000, delay + 100) : false
    },
  })
  const check = status.data?.membershipCheck
  const now = Date.now()
  const checkedAt = check?.checkedAt ? Date.parse(check.checkedAt) : NaN
  const freshUntil = check?.freshUntil ? Date.parse(check.freshUntil) : NaN
  const state = check?.state === 'ready' && (
    !Number.isFinite(checkedAt) || !Number.isFinite(freshUntil)
    || now < checkedAt || now >= freshUntil
  ) ? 'stale' : check?.state
  const presentation = state ? STATES[state] : undefined
  const otherGroup = Boolean(groupObjectId && check?.groupObjectId
    && groupObjectId.toLowerCase() !== check.groupObjectId.toLowerCase())
  const coolingDown = Boolean(check?.retryAt && Date.parse(check.retryAt) > now)
  const previousSuccessfulAt = check?.lastSuccessfulAt && (
    check.lastSuccessfulAt !== check.checkedAt
    || check.lastSuccessfulGroupObjectId !== check.groupObjectId
    || (state !== 'ready' && state !== 'stale')
  ) ? check.lastSuccessfulAt : null

  return (
    <section className={styles.root} aria-label="Complete group-read verification">
      <div className={styles.heading}>
        <Title3 as="h3">Complete group-read verification</Title3>
        {status.data && !status.isError && (
          <Badge appearance="tint" color={verification.isPending ? 'informative' : otherGroup ? 'subtle' : presentation?.color ?? 'subtle'}>
            {verification.isPending ? 'Verification requested' : otherGroup ? `Other group: ${presentation?.label ?? 'not reported'}` : presentation?.label ?? 'Not reported'}
          </Badge>
        )}
      </div>
      <Text block>
        Explicitly verify every transitive-members page for one Entra security group.
        A bounded member preview is not this check.
      </Text>
      {status.isPending ? <Loading label="Loading group verification status" /> : status.isError ? (
        <ErrorState title="Group verification status unavailable" error={status.error} />
      ) : (
        <>
          <Text block role={verification.isPending ? undefined : 'status'}>{presentation?.description ?? 'This API has not reported functional group-read status. Refresh status or check that the API supports directory group verification.'}</Text>
          {otherGroup && <Text block>That observation does not verify the selected group.</Text>}
          {check && (
            <dl className={styles.facts}>
              {check.groupObjectId && <div><dt>Checked group object ID</dt><dd><code>{check.groupObjectId}</code></dd></div>}
              {check.checkedAt && <div><dt>Read started</dt><dd>{timestamp(check.checkedAt)}</dd></div>}
              {check.completedAt && <div><dt>Completed</dt><dd>{timestamp(check.completedAt)}</dd></div>}
              {check.freshUntil && <div><dt>Diagnostic freshness until</dt><dd>{timestamp(check.freshUntil)}</dd></div>}
              {previousSuccessfulAt && <div><dt>Last successful read</dt><dd>{timestamp(previousSuccessfulAt)}</dd></div>}
              {previousSuccessfulAt && check.lastSuccessfulGroupObjectId && <div><dt>Last successful group object ID</dt><dd><code>{check.lastSuccessfulGroupObjectId}</code></dd></div>}
              {check.retryAt && <div><dt>Retry after</dt><dd>{timestamp(check.retryAt)}</dd></div>}
            </dl>
          )}
          {check?.message && !verification.isError && (
            <MessageBar intent={state === 'forbidden' || state === 'unavailable' ? 'error' : 'warning'}>
              <MessageBarBody>{check.message}</MessageBarBody>
            </MessageBar>
          )}
        </>
      )}
      {verification.isError && <ErrorState title="Group verification failed" error={verification.error} />}
      {verification.isPending && <Text role="status">Verifying the selected group. A complete read can take up to 60 seconds.</Text>}
      <div className={styles.actions}>
        {groupObjectId ? (
          <Button
            disabled={status.isError || !status.data?.lookupEnabled || !check || verification.isPending || state === 'checking' || coolingDown}
            onClick={() => verification.mutate(groupObjectId)}
          >
            {verification.isPending ? 'Verifying group read' : 'Verify complete group read'}
          </Button>
        ) : status.data?.lookupEnabled ? (
          <Link to="/identity?tab=security-groups">Choose a group in Identity to verify</Link>
        ) : null}
        <Button appearance="subtle" disabled={status.isFetching} onClick={() => void status.refetch()}>
          Refresh group-check status
        </Button>
      </div>
      <Text block size={200} className={styles.hint}>
        Diagnostic only, not owner eligibility or gateway sync. The five-minute observation is
        process-local, can differ between API instances, and resets on restart. It never changes
        grants, keys, directory membership, or APIM.
      </Text>
    </section>
  )
}
