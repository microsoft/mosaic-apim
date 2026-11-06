import { Badge, Button, Checkbox, Field, MessageBar, MessageBarBody, Text, Textarea, Title3 } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { COST_CENTER_SETUP_LABELS } from '../cost-centers'
import { costCenterDraftErrors, costCenterDraftPayload, newCostCenterDraft, useCostCenterCapabilities } from '../cost-center-setup'
import type { AdminCostCenterRequest, CostCenterRequestApproval } from '../types'
import { ErrorState } from './AsyncState'
import { CostCenterSetupFields } from './CostCenterSetupForm'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './CostCenterSetup.module.css'

function recordedApproval(request: AdminCostCenterRequest): CostCenterRequestApproval | null {
  return request.approval ? {
    costCenter: request.approval.costCenter,
    addRequester: request.approval.addRequester,
    note: request.approval.note ?? null,
  } : null
}

export function CostCenterRequestSummary({ request, requester, onReview }: {
  request: AdminCostCenterRequest
  requester: string
  onReview?: () => void
}) {
  const needsAttention = request.needsAdministratorAttention || Boolean(request.preparationError || request.requesterMembershipError)
  const cancelledPreparation = request.approval && (request.state === 'denied' || request.state === 'withdrawn')
  const centerId = request.costCenter?.id ?? request.approval?.costCenterId ?? request.costCenterId
  const reviewedGroup = request.approval?.costCenter.groupSetup
  const suggestionUsed = reviewedGroup?.mode === 'attach'
    && reviewedGroup.groupObjectId.toLowerCase() === request.suggestedGroupObjectId?.toLowerCase()
  return <section className={styles.stack} aria-label="Cost-center creation request">
    <Title3 as="h3">{request.name}</Title3>
    <div className={styles.actions}>
      <Badge appearance="outline">Cost-center creation</Badge>
      <Badge appearance="tint">{request.state}</Badge>
      {request.setupState && <Badge appearance="tint" color={request.setupState === 'setupRequired' ? 'warning' : 'informative'}>{COST_CENTER_SETUP_LABELS[request.setupState]}</Badge>}
      {needsAttention && <Badge appearance="tint" color="warning">Administrator action needed</Badge>}
    </div>
    <dl className={styles.facts}>
      <div><dt>Requester</dt><dd>{requester}</dd></div>
      <div><dt>Requester object ID</dt><dd>{request.requesterObjectId}</dd></div>
      <div><dt>Proposed code</dt><dd>{request.code || 'Administrator chooses'}</dd></div>
      <div><dt>Description</dt><dd>{request.description || 'No description supplied'}</dd></div>
      <div><dt>Justification</dt><dd>{request.justification}</dd></div>
      {request.suggestedGroupObjectId && <div><dt>Suggested Entra group</dt><dd>{request.suggestedGroupObjectId} · {suggestionUsed
        ? 'Selected in the recorded approval; inspect the saved center for current verification'
        : request.approval ? 'Not selected in the recorded approval' : 'Suggestion only; not adopted or verified'}</dd></div>}
      <div><dt>Request ID</dt><dd>{request.id}</dd></div>
      <div><dt>Requested</dt><dd>{new Date(request.createdAt).toLocaleString()}</dd></div>
      {request.approval && <>
        <div><dt>Reserved center ID</dt><dd>{request.approval.costCenterId}</dd></div>
        <div><dt>Reviewed configuration</dt><dd>{request.approval.costCenter.name} ({request.approval.costCenter.code}) · Immutable after preparation starts</dd></div>
        <div><dt>Requester addition</dt><dd>{request.approval.addRequester ? 'Explicitly requested by administrator' : 'Not requested; no automatic enrollment'}</dd></div>
      </>}
      {request.requesterMembershipState && <div><dt>Requester membership outcome</dt><dd>
        {request.requesterMembershipState === 'succeeded' ? 'Directory addition confirmed; token propagation still applies' : `${request.requesterMembershipState} · Not confirmed ready`}
      </dd></div>}
      {request.decisionNote && <div><dt>Decision note</dt><dd>{request.decisionNote}</dd></div>}
    </dl>
    {request.preparationError && <MessageBar intent="warning"><MessageBarBody>Center preparation needs attention: {request.preparationError}</MessageBarBody></MessageBar>}
    {request.requesterMembershipError && <MessageBar intent="warning"><MessageBarBody>Requester membership needs attention: {request.requesterMembershipError}</MessageBarBody></MessageBar>}
    {request.state === 'approved' && <MessageBar intent={needsAttention || request.setupState !== 'ready' ? 'warning' : 'info'}><MessageBarBody>
      {request.setupState === 'ready'
        ? 'Approval is saved and the group binding is ready. This does not confirm requester membership, propagated tokens, keys, or applied gateway access.'
        : 'Approval is saved, but setup is incomplete. The linked center is not ready for custom-center access; finish its recorded setup rather than creating another center.'}
    </MessageBarBody></MessageBar>}
    {cancelledPreparation && <MessageBar intent="warning"><MessageBarBody>
      Preparation was cancelled by the final request decision. A reserved or saved unbound local center may remain.
      Inspect the recorded center; it has not been silently deleted, and the request cannot be approved again.
    </MessageBarBody></MessageBar>}
    {centerId && <Link to={`/cost-centers/${encodeURIComponent(centerId)}`}>
      {request.costCenter ? `Open saved center: ${request.costCenter.name}` : 'Inspect reserved cost-center record'}
    </Link>}
    {request.requesterMembershipState === 'unknown' && <Text>
      Do not repeat requester enrollment. Inspect the linked center&apos;s directory operation and use read-only recovery or administrator investigation.
    </Text>}
    {request.requesterMembershipState === 'failed' && <Text>
      Use the linked center&apos;s failed directory operation for an explicit safe retry, then refresh this recorded approval.
    </Text>}
    {onReview && (request.state === 'pending' || (request.state === 'approved' && needsAttention)) && <div>
      <Button appearance="primary" onClick={onReview}>{request.approval ? 'Resume recorded approval' : 'Review cost-center request'}</Button>
    </div>}
  </section>
}

export function CostCenterRequestReview({ request, requester, onBack, recordRefreshing = false, recordError }: {
  request: AdminCostCenterRequest
  requester: string
  onBack: () => void
  recordRefreshing?: boolean
  recordError?: unknown
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const initial = useRef(newCostCenterDraft(request.approval?.costCenter ?? {
    name: request.name, code: request.code || request.name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').slice(0, 64),
    description: request.description,
  }))
  const [draft, setDraft] = useState(initial.current)
  const [addRequester, setAddRequester] = useState(request.approval?.addRequester ?? false)
  const [note, setNote] = useState(request.approval?.note ?? '')
  const [submitted, setSubmitted] = useState<CostCenterRequestApproval | null>(null)
  const capabilities = useCostCenterCapabilities()
  const locked = recordedApproval(request) ?? submitted
  const errors = locked ? [] : costCenterDraftErrors(draft, capabilities.data, { suggestedGroup: request.suggestedGroupObjectId })
  async function invalidate() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cost-center-requests'] }),
      queryClient.invalidateQueries({ queryKey: ['request-counts'] }),
      queryClient.invalidateQueries({ queryKey: ['cost-centers'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
      queryClient.invalidateQueries({ queryKey: ['publications'] }),
      queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
      queryClient.invalidateQueries({ queryKey: ['model-pools'] }),
    ])
  }
  const approve = useMutation({
    mutationFn: (payload: CostCenterRequestApproval) => api.approveCostCenterRequest(request.id, payload),
    retry: false,
    onSuccess: (saved) => {
      queryClient.setQueryData(['cost-center-requests', 'selected', request.id], saved)
      completeWorkspaceNavigation(onBack)
    },
    onSettled: invalidate,
  })
  const deny = useMutation({
    mutationFn: () => api.denyCostCenterRequest(request.id, note.trim() || null),
    retry: false,
    onSuccess: (saved) => {
      queryClient.setQueryData(['cost-center-requests', 'selected', request.id], saved)
      completeWorkspaceNavigation(onBack)
    },
    onSettled: invalidate,
  })
  const refresh = useMutation({
    mutationFn: () => api.getCostCenterRequest(request.id),
    retry: false,
    onSuccess: (saved) => {
      queryClient.setQueryData(['cost-center-requests', 'selected', request.id], saved)
      if (!saved.approval && saved.state === 'pending') setSubmitted(null)
    },
  })
  const pending = approve.isPending || deny.isPending
  const blocked = pending || recordRefreshing || Boolean(recordError)
  function approveReviewed() {
    if (blocked || errors.length) return
    const payload = locked ?? { costCenter: costCenterDraftPayload(draft), addRequester, note: note.trim() || null }
    setSubmitted(payload)
    approve.mutate(payload)
  }
  const approvable = request.state === 'pending' || (request.state === 'approved' && request.needsAdministratorAttention)
  return <WorkspaceEditorFrame title={locked ? 'Resume cost-center approval' : 'Review cost-center request'}
    description="Approval reserves exactly one center and the reviewed configuration. Group setup, requester membership, and gateway apply are separate outcomes."
    onBack={onBack} onCancel={onBack} pending={pending}
    dirty={!locked && (JSON.stringify(draft) !== JSON.stringify(initial.current) || addRequester || Boolean(note.trim()))}
    primaryAction={approvable ? {
      label: locked ? 'Resume unchanged approval' : 'Approve cost-center request',
      onClick: approveReviewed, disabled: Boolean(errors.length || blocked),
    } : undefined}
    error={approve.isError ? approve.error.message : deny.isError ? deny.error.message : undefined}>
    <div className={styles.stack}>
      <CostCenterRequestSummary request={request} requester={requester} />
      {locked && <MessageBar intent="warning"><MessageBarBody>
        The original reviewed configuration is locked. Resume sends exactly that configuration and the same reserved center ID.
        No new center is requested, and a previously successful requester addition must not be replayed after later removal.
        Use the saved center&apos;s directory operations for failed/unknown writes.
      </MessageBarBody></MessageBar>}
      {(approve.isError || deny.isError) && <Text>The recorded outcome may be partial. Refresh the request before changing any unreserved configuration; never start another creation request to recover this one.</Text>}
      {recordRefreshing && <Text role="status">Refreshing the exact recorded request before another decision…</Text>}
      {recordError != null && <ErrorState title="Current recorded request unavailable" error={recordError} />}
      <Button disabled={refresh.isPending || pending || recordRefreshing} onClick={() => refresh.mutate()}>Refresh recorded request</Button>
      {refresh.isError && <ErrorState title="Recorded request unavailable" error={refresh.error} />}
      <CostCenterSetupFields value={locked ? newCostCenterDraft(locked.costCenter) : draft} onChange={setDraft} disabled={blocked || Boolean(locked) || !approvable}
        recorded={Boolean(locked)} suggestedGroup={locked ? undefined : request.suggestedGroupObjectId} />
      <section className={styles.section} aria-label="Approval choices">
        <Title3 as="h3">Explicit approval choices</Title3>
        <Checkbox label="Add requester to the Entra group" checked={locked?.addRequester ?? addRequester} disabled={blocked || Boolean(locked) || !approvable}
          onChange={(_, data) => setAddRequester(Boolean(data.checked))} />
        <Text>Off by default. Checking this authorizes a real user-membership change once the group is verified and write permissions allow it. Contact emails and approval alone do not enroll the requester.</Text>
        <Field label="Decision note"><Textarea value={locked ? locked.note ?? '' : note} maxLength={2000} disabled={blocked || Boolean(locked) || !approvable} onChange={(_, data) => setNote(data.value)} /></Field>
        {request.state === 'pending' && <div><Button disabled={blocked} onClick={() => deny.mutate()}>Deny cost-center request</Button></div>}
        {request.state === 'pending' && request.approval && <Text>Denying after preparation may leave the recorded local center unbound. This does not delete the center or its Entra group.</Text>}
      </section>
    </div>
  </WorkspaceEditorFrame>
}
