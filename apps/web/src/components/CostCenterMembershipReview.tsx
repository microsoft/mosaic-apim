import { Badge, Button, Checkbox, Field, MessageBar, MessageBarBody, Text, Textarea, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { useCostCenterCapabilities } from '../cost-center-setup'
import { membershipNeedsAttention } from '../request-queue'
import type { AdminCostCenterMembershipRequest } from '../types'
import { ErrorState } from './AsyncState'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './CostCenterSetup.module.css'

export function RequesterName({ objectId, name, retryButton = true }: { objectId: string; name?: string | null; retryButton?: boolean }) {
  const api = useMosaicApi()
  const directory = useQuery({
    queryKey: ['directory-object', objectId], queryFn: () => api.getDirectoryObject(objectId),
    enabled: !name?.trim(), retry: false, staleTime: 300_000,
  })
  return <>{name?.trim() || directory.data?.displayName?.trim() || (directory.isPending ? 'Loading requester name…' : 'Requester name unavailable')}
    {retryButton && !name?.trim() && (directory.isError || (directory.isSuccess && !directory.data.displayName?.trim())) &&
      <Button size="small" appearance="subtle" onClick={(event) => { event.stopPropagation(); void directory.refetch() }}>Retry name lookup</Button>}
  </>
}

export function CostCenterMembershipSummary({ request, requester, onReview }: {
  request: AdminCostCenterMembershipRequest; requester?: string | null; onReview?: () => void
}) {
  return <section className={styles.stack} aria-label="Cost-center membership request">
    <Title3 as="h3">Join {request.costCenter.name}</Title3>
    <div className={styles.actions}>
      <Badge appearance="outline">Cost-center membership</Badge><Badge appearance="tint">{request.state}</Badge>
      {membershipNeedsAttention(request) && <Badge color="warning" appearance="tint">Administrator action needed</Badge>}
    </div>
    <dl className={styles.facts}>
      <div><dt>Requester</dt><dd><RequesterName objectId={request.requesterObjectId} name={requester} /></dd></div>
      <div><dt>Cost center</dt><dd>{request.costCenter.name} ({request.costCenter.code})</dd></div>
      <div><dt>Justification</dt><dd>{request.justification}</dd></div>
      <div><dt>Decision</dt><dd>{request.state}</dd></div>
      <div><dt>Membership outcome</dt><dd>{request.membershipState === 'succeeded'
        ? 'Directory membership confirmed; token propagation still applies'
        : request.membershipState ? `${request.membershipState} — membership not confirmed` : 'Not confirmed'}</dd></div>
      <div><dt>Requested</dt><dd>{new Date(request.createdAt).toLocaleString()}</dd></div>
      {request.decisionNote && <div><dt>Decision note</dt><dd>{request.decisionNote}</dd></div>}
    </dl>
    <MessageBar intent="warning"><MessageBarBody>Approval authorizes adding this person to the center&apos;s real Entra security group. Other applications using that group may also be affected. Members share existing pooled quotas and budget; joining increases neither.</MessageBarBody></MessageBar>
    <Text>Only already-applied Automatic resource access can become available after a fresh runtime token carries the group claim. Request-only resources still need individual approval. Joining creates no key, does not apply a gateway plan, and changes no default preference.</Text>
    {request.membershipError && <MessageBar intent="warning"><MessageBarBody>{request.membershipError}</MessageBarBody></MessageBar>}
    <Link to={`/cost-centers/${encodeURIComponent(request.costCenter.id)}?tab=members`}>Inspect center members</Link>
    <Link to={`/cost-centers/${encodeURIComponent(request.costCenter.id)}?tab=overview`}>Inspect directory operations and recovery</Link>
    {request.operationId && <Text>Recorded directory operation: {request.operationId}. Failed writes require explicit safe retry; unknown writes require read-only recovery, never a blind repeat.</Text>}
    {onReview && (request.state === 'pending' || membershipNeedsAttention(request)) && <div><Button appearance="primary" onClick={onReview}>
      {request.state === 'pending' ? 'Review membership request' : 'Resume recorded membership approval'}
    </Button></div>}
  </section>
}

export function CostCenterMembershipReview({ request, requester, onBack, recordRefreshing = false, recordError }: {
  request: AdminCostCenterMembershipRequest; requester?: string | null; onBack: () => void; recordRefreshing?: boolean; recordError?: unknown
}) {
  const api = useMosaicApi()
  const client = useQueryClient()
  const [note, setNote] = useState('')
  const [acknowledge, setAcknowledge] = useState(false)
  const [mustRefresh, setMustRefresh] = useState(false)
  const capabilities = useCostCenterCapabilities()
  const center = useQuery({ queryKey: ['cost-centers', request.costCenter.id], queryFn: () => api.getCostCenter(request.costCenter.id), retry: false })
  const action = useMutation({
    mutationFn: (decision: 'approve' | 'deny' | 'resume') => decision === 'approve'
      ? api.approveCostCenterMembershipRequest(request.id, { ...(note.trim() ? { note: note.trim() } : {}), acknowledgeMembershipChanges: true })
      : decision === 'deny' ? api.denyCostCenterMembershipRequest(request.id, { ...(note.trim() ? { note: note.trim() } : {}) })
        : api.resumeCostCenterMembershipRequest(request.id, { acknowledgeMembershipChanges: true }),
    retry: false,
    onSuccess: (saved) => {
      client.setQueryData(['cost-center-membership-requests', 'selected', request.id], saved)
      completeWorkspaceNavigation(onBack)
    },
    onError: () => setMustRefresh(true),
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ['cost-center-membership-requests'] }),
        client.invalidateQueries({ queryKey: ['request-counts'] }),
        client.invalidateQueries({ queryKey: ['cost-centers'] }),
      ])
    },
  })
  const refresh = useMutation({
    mutationFn: () => api.getCostCenterMembershipRequest(request.id), retry: false,
    onSuccess: (saved) => { client.setQueryData(['cost-center-membership-requests', 'selected', request.id], saved); setMustRefresh(false) },
  })
  const blocked = action.isPending || refresh.isPending || recordRefreshing || Boolean(recordError) || mustRefresh
  const ready = center.data?.setupState === 'ready' && !center.data.builtIn
  const canDecide = request.state === 'pending' || membershipNeedsAttention(request)
  return <WorkspaceEditorFrame title={request.state === 'pending' ? 'Review membership request' : 'Resume membership approval'}
    description="Manual person-and-center review. The saved decision and real directory membership outcome are separate."
    onBack={onBack} onCancel={onBack} pending={action.isPending}
    dirty={Boolean(note.trim() || acknowledge)}
    error={action.error?.message}
    primaryAction={canDecide ? {
      label: request.state === 'pending' ? 'Approve membership request' : 'Resume recorded approval',
      disabled: blocked || !acknowledge || !ready || center.isFetching,
      onClick: () => action.mutate(request.state === 'pending' ? 'approve' : 'resume'),
    } : undefined}>
    <div className={styles.stack}>
      <CostCenterMembershipSummary request={request} requester={requester} />
      {recordRefreshing && <Text role="status">Refreshing recorded request…</Text>}
      {recordError != null && <ErrorState title="Current request unavailable" error={recordError} />}
      {center.isPending && <Text role="status">Checking current center readiness…</Text>}
      {center.isError && <><ErrorState title="Center readiness unavailable" error={center.error} /><Button onClick={() => void center.refetch()}>Retry center readiness</Button></>}
      {center.data && <Text>Current center setup: {center.data.setupState}. The server revalidates the original target and actual membership before any write. An already-added person completes without another directory write.</Text>}
      {capabilities.data && <Text>Directory add-people capability: {capabilities.data.available && capabilities.data.canAddUsers ? 'Available (target restrictions still apply)' : 'Unavailable; approval may need administrator recovery. Enabling requests does not grant Graph consent.'}</Text>}
      {capabilities.isError && <ErrorState title="Directory capability observation unavailable" error={capabilities.error} />}
      {request.state !== 'pending' && <Text>Resume reconciles this recorded approval and its durable operation. It does not create another request or blindly repeat a failed or uncertain membership write. Use the center&apos;s operation tools where required.</Text>}
      {mustRefresh && <Text>Outcome may be partial. Refresh the recorded request before another decision.</Text>}
      <Button disabled={action.isPending || refresh.isPending || recordRefreshing} onClick={() => refresh.mutate()}>Refresh recorded request</Button>
      {refresh.isError && <ErrorState title="Recorded request unavailable" error={refresh.error} />}
      <Checkbox label="I acknowledge that this changes a real Entra group and can affect other applications using it."
        checked={acknowledge} disabled={blocked || !canDecide} onChange={(_, data) => setAcknowledge(Boolean(data.checked))} />
      {request.state === 'pending' && <>
        <Field label="Decision note"><Textarea maxLength={2000} value={note} disabled={blocked} onChange={(_, data) => setNote(data.value)} /></Field>
        <div><Button disabled={blocked} onClick={() => action.mutate('deny')}>Deny membership request</Button></div>
      </>}
    </div>
  </WorkspaceEditorFrame>
}
