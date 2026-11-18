import { Badge, Button, Checkbox, Field, MessageBar, MessageBarBody, Select, Textarea } from '@fluentui/react-components'
import { useMsal } from '@azure/msal-react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ApiError, usePortalApi } from '../api'
import { approvalPath, approvalsKey, roleLabel, scopesKey, stateLabel, type ReviewDetail, type ReviewKind } from '../approvals'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { RequestFormAnswers } from '../components/RequestFormAnswers'
import { UnsavedChanges } from '../components/UnsavedChanges'
import './approvals.css'

function isKind(kind: string | undefined): kind is ReviewKind {
  return kind === 'resourceAccess' || kind === 'costCenterMembership'
}

export function ApprovalsPage() {
  const { kind, requestId } = useParams()
  if (!requestId) return <ReviewQueue />
  if (!isKind(kind)) return <Unavailable />
  return <ReviewDesk key={`${kind}:${requestId}`} kind={kind} requestId={requestId} />
}

function Unavailable() {
  return <><PageHeader title="Approval unavailable" description="This request does not exist or is outside your current reviewer assignments." />
    <Link to="/approvals">Back to approvals</Link></>
}

function ReviewQueue() {
  const api = usePortalApi()
  const { accounts } = useMsal()
  const account = accounts[0]?.homeAccountId
  const [params, setParams] = useSearchParams()
  const scopes = useQuery({ queryKey: [...scopesKey, account], queryFn: api.listReviewCenters, retry: false })
  const wantedCenter = params.get('center')
  const scope = wantedCenter ? scopes.data?.find(item => item.center.id === wantedCenter) : scopes.data?.[0]
  const wantedState = params.get('state')
  const state = wantedState === 'approved' || wantedState === 'denied' || wantedState === 'withdrawn' ? wantedState : 'pending'
  const queue = useInfiniteQuery({
    queryKey: [...approvalsKey, account, 'queue', scope?.center.id, state],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.listApprovals(scope!.center.id, state, pageParam),
    getNextPageParam: page => page.nextCursor ?? undefined,
    enabled: scopes.isSuccess && Boolean(scope),
    retry: false,
  })
  const items = queue.data?.pages.flatMap(page => page.items) ?? []
  function filter(key: 'center' | 'state', value: string) {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      next.set(key, value)
      return next
    })
  }
  return <>
    <PageHeader title={scope && scopes.isSuccess ? scope.center.name : 'Approvals'}
      description={scope && scopes.isSuccess
        ? `Approvals · ${scope.roles.map(role => roleLabel[role]).join(' and ')} · This cost center only`
        : 'Review membership and resource access for your assigned cost centers.'} />
    {scopes.isPending && <Loading label="Checking your reviewer assignments" />}
    {scopes.isError && <><ErrorState error={scopes.error} /><Button onClick={() => void scopes.refetch()}>Retry assignments</Button></>}
    {scopes.isSuccess && !scopes.data.length && <EmptyState title="No reviewer assignments">
      An administrator must assign you as a cost-center contact before you can review requests.
    </EmptyState>}
    {scopes.isSuccess && scopes.data.length > 0 && <>
      <div className="approval-filters">
        <Field label="Assigned cost center"><Select value={scope?.center.id ?? ''} onChange={(_, data) => filter('center', data.value)}>
          {!scope && <option value="">Select an assigned cost center</option>}
          {scopes.data.map(item => <option key={item.center.id} value={item.center.id}>{item.center.name}</option>)}
        </Select></Field>
        <Field label="Request status"><Select value={state} onChange={(_, data) => filter('state', data.value)}>
          {Object.entries(stateLabel).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </Select></Field>
      </div>
      {!scope && <EmptyState title="Cost center unavailable">Select one of your current assignments.</EmptyState>}
      {scope && queue.isPending && <Loading label="Loading the approval queue" />}
      {scope && queue.isError && <><ErrorState error={queue.error} /><Button onClick={() => void queue.refetch()}>Reload queue</Button></>}
      {scope && queue.isSuccess && (!items.length
        ? <EmptyState title={`No ${stateLabel[state].toLowerCase()} requests`}>Your own requests stay in My requests for someone else to review.</EmptyState>
        : <section aria-labelledby="approval-queue-title">
          <h2 id="approval-queue-title">Approval queue</h2>
          <ul className="cost-center-list">
          {items.map(item => <li key={`${item.kind}:${item.id}`} className="approval-queue-row">
            <div><Link to={`${approvalPath(item.kind, item.id)}?state=${state}`}>{item.title}</Link>
              <p>{item.requester ?? 'Requester name unavailable'} · {item.kind === 'resourceAccess' ? 'Resource access' : 'Membership'}</p></div>
            <Badge appearance="outline">{stateLabel[item.state]}</Badge>
          </li>)}
          </ul>
        </section>)}
      {scope && queue.isSuccess && queue.hasNextPage && <Button disabled={queue.isFetchingNextPage} onClick={() => void queue.fetchNextPage()}>Load more requests</Button>}
    </>}
  </>
}

function ReviewDesk({ kind, requestId }: { kind: ReviewKind; requestId: string }) {
  const api = usePortalApi()
  const { accounts } = useMsal()
  const queryClient = useQueryClient()
  const [params] = useSearchParams()
  const key = [...approvalsKey, accounts[0]?.homeAccountId, kind, requestId]
  const detail = useQuery({
    queryKey: key, queryFn: () => api.getApproval(kind, requestId),
    retry: false, gcTime: 0, refetchOnMount: 'always',
  })
  const [reason, setReason] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const decide = useMutation({
    mutationFn: ({ outcome, revision }: { outcome: 'approve' | 'deny'; revision: string }) =>
      api.decideApproval(kind, requestId, outcome, {
        revision, reason: reason.trim(), acknowledgeMembershipChanges: acknowledged,
      }),
    retry: false,
    onSuccess: saved => {
      queryClient.setQueryData(key, saved)
      setReason('')
      setAcknowledged(false)
      void queryClient.invalidateQueries({ queryKey: [...approvalsKey, accounts[0]?.homeAccountId, 'queue'] })
    },
    onError: () => { void detail.refetch() },
  })
  if (detail.isError) {
    if (detail.error instanceof ApiError && detail.error.status === 404) return <Unavailable />
    return <><PageHeader title="Review request" /><ErrorState error={detail.error} />
      <Button onClick={() => void detail.refetch()}>Reload request</Button> <Link to="/approvals">Back to approvals</Link></>
  }
  if (detail.isPending) return <Loading label="Checking access to this request" />
  const request = detail.data
  const member = request.kind === 'costCenterMembership'
  const blocked = decide.isPending || detail.isFetching || !reason.trim()
  const back = new URLSearchParams({ center: request.scope.center.id, state: params.get('state') ?? 'pending' })
  return <>
    <UnsavedChanges dirty={request.canDecide && (Boolean(reason.trim()) || acknowledged)} title="Discard this decision draft?" />
    <Link className="approval-back" to={`/approvals?${back}`}>Back to {request.scope.center.name} approvals</Link>
    <PageHeader title={request.scope.center.name}
      description={`Review desk · ${request.scope.roles.map(role => roleLabel[role]).join(' and ')} · This cost center only`} />
    <article className="approval-dossier" aria-labelledby="review-title">
      <header className="approval-heading">
        <div><h2 id="review-title">{request.title}</h2><p>{request.requester ?? 'Requester name unavailable'}
          {request.environment ? ` · ${request.environment}` : ''}</p></div>
        <Badge appearance="outline">{stateLabel[request.state]}</Badge>
      </header>
      <div className="approval-evidence">
        <div>
          {request.justification && <><h3>Requester justification</h3><p className="approval-long-text">{request.justification}</p></>}
          {request.recordedForm ? <RequestFormAnswers form={request.recordedForm} reviewer />
            : !member && <p>No governance answers were recorded for this request.</p>}
          {request.decisionAt && <section><h3>Decision history</h3>
            <p>{stateLabel[request.state]}{request.decidedByYou ? ' by you' : ''} · {new Date(request.decisionAt).toLocaleString()}</p>
            {request.decisionReason && <p className="approval-long-text">{request.decisionReason}</p>}
            <MembershipOutcome request={request} />
          </section>}
        </div>
        <aside className="approval-effect" aria-labelledby="approval-effect-title">
          <h3 id="approval-effect-title">Boundary &amp; effect</h3>
          <p>Only <strong>{request.scope.center.name}</strong> is in scope. Denying does not create access.</p>
          {member ? <p>Approval authorizes adding this requester to the center’s Entra group. Confirmed membership and gateway access are separate. Request-only offerings still need their own approval.</p>
            : <><p>Approval records one grant for this exact offering and cost center. The gateway is unchanged until an administrator reviews and applies the plan.</p>
              <p>{request.hasPersonLimits ? 'The center’s configured per-person limits apply. You cannot override them.'
                : 'No per-person limits are configured for this offering. Other existing controls still apply.'}</p></>}
          {request.canDecide && request.approvalBlocked && <MessageBar intent="warning"><MessageBarBody>
            This request cannot be approved here. Ask an administrator to review it.
          </MessageBarBody></MessageBar>}
        </aside>
      </div>
      {decide.isError && <ErrorState error={decide.error} />}
      {request.canDecide && <form className="approval-decision" onSubmit={event => event.preventDefault()}>
        <div>
          <Field label="Decision reason" required>
            <Textarea value={reason} onChange={(_, data) => setReason(data.value)} maxLength={2000}
              disabled={decide.isPending} resize="vertical" />
          </Field>
          {member && <Checkbox label="I acknowledge the directory membership change" checked={acknowledged}
            disabled={decide.isPending} onChange={(_, data) => setAcknowledged(data.checked === true)} />}
        </div>
        <div className="approval-actions">
          <Button type="button" disabled={blocked} onClick={() => decide.mutate({ revision: request.revision, outcome: 'deny' })}>Deny request</Button>
          <Button type="button" appearance="primary" disabled={blocked || request.approvalBlocked || (member && !acknowledged)}
            onClick={() => decide.mutate({ revision: request.revision, outcome: 'approve' })}>
            {member ? 'Approve membership' : 'Approve resource access'}
          </Button>
        </div>
        <p>Portal and administrator reviewers share one decision. Your current assignment is checked again when you decide.</p>
      </form>}
    </article>
  </>
}

function MembershipOutcome({ request }: { request: ReviewDetail }) {
  if (request.kind !== 'costCenterMembership' || request.state !== 'approved') return null
  if (request.membershipState === 'succeeded') return <p>Directory membership confirmed. Gateway access is separate.</p>
  if (request.membershipState === 'pending') return <p>Membership is not yet confirmed.</p>
  return <p>Membership could not be confirmed. An administrator can review the directory operation.</p>
}
