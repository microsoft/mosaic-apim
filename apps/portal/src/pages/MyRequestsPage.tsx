import { Badge, Button, Card, CardHeader, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { useState } from 'react'
import { ApiError, usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { CostCenterRequestDetails } from '../components/CostCenterRequestDetails'
import { gatewayLabel, requestStateLabel, withResourceKind } from '../entitlement-format'
import { modelTitle } from '../model-presentation'
import { usePortalEnvironments } from '../environments'
import { costCenterRequestsKey, invalidateRequests, requestCountsKey, resourceRequestsKey } from '../request-queries'
import { formatCount } from '../usage-format'
import type { AccessRequest, PortalRequest, RequestKind, ResourceSummary } from '../types'

/** What names a request: its live summary, else what it recorded about the resource when made. */
function headingSummary(request: AccessRequest): ResourceSummary | null {
  if (request.resourceSummary) return request.resourceSummary
  if (!request.resourceSnapshot) return null
  return {
    kind: request.resource.kind,
    id: request.resource.id,
    scopeId: request.resource.scopeId,
    displayName: request.resourceSnapshot.displayName,
    gatewayId: request.resourceSnapshot.gatewayId,
    gatewayName: request.resourceSnapshot.gatewayName,
    environment: request.requestedEnvironment,
    azureCloud: request.resourceSnapshot.azureCloud,
    available: false,
  }
}

function offeringLabel(request: AccessRequest) {
  const summary = headingSummary(request)
  const name = request.resourceDisplayName?.trim() || summary?.displayName?.trim()
  return name && name !== modelTitle(request.resource, request.resourceDisplayName, summary, request.model) ? name : null
}

function GrantStatus({ request }: { request: AccessRequest }) {
  if (request.grantedEntitlementId) {
    return (
      <>
        Approval created your grant. It may not work until an administrator applies it.{' '}
        <Link to={`/access?grant=${encodeURIComponent(request.grantedEntitlementId)}`}>See its status in My access</Link>
      </>
    )
  }
  return <>Approved before approvals created grants, so no grant is linked to this request.</>
}

export function MyRequestsPage() {
  const api = usePortalApi()
  const [params, setParams] = useSearchParams()
  const filter = ['pending', 'completed', 'withdrawn'].includes(params.get('status') ?? '') ? params.get('status')! : 'all'
  const kindFilter = ['resource', 'costCenter'].includes(params.get('kind') ?? '') ? params.get('kind')! : 'all'
  const selectedRequest = params.get('request')
  const selectedKind: RequestKind = params.get('requestKind') === 'costCenter' ? 'costCenter' : 'resource'
  const [withdrawingId, setWithdrawingId] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const requests = useQuery({ queryKey: resourceRequestsKey, queryFn: api.listAccessRequests })
  const costCenterRequests = useQuery({ queryKey: costCenterRequestsKey, queryFn: api.listCostCenterRequests })
  const counts = useQuery({ queryKey: requestCountsKey, queryFn: api.getRequestCounts })
  const linkedCostCenter = useQuery({
    queryKey: [...costCenterRequestsKey, 'detail', selectedRequest],
    queryFn: () => {
      if (!selectedRequest) throw new Error('No request selected')
      return api.getCostCenterRequest(selectedRequest)
    },
    enabled: Boolean(selectedRequest && selectedKind === 'costCenter'),
    retry: false,
  })
  const environments = usePortalEnvironments()
  const withdraw = useMutation({
    mutationFn: async (row: PortalRequest) => row.requestKind === 'costCenter'
      ? api.withdrawCostCenterRequest(row.request.id) : api.withdrawAccessRequest(row.request.id),
    onSuccess: async () => {
      setWithdrawingId(null)
      await invalidateRequests(queryClient)
    },
  })
  const rows: PortalRequest[] = [
    ...(requests.data ?? []).map((request): PortalRequest => ({ requestKind: 'resource', request })),
    ...(costCenterRequests.data ?? []).map((request): PortalRequest => ({ requestKind: 'costCenter', request })),
  ]
  const visible: PortalRequest[] = selectedRequest && selectedKind === 'costCenter'
    ? linkedCostCenter.isSuccess ? [{ requestKind: 'costCenter', request: linkedCostCenter.data }] : []
    : rows.filter((row) => selectedRequest ? row.requestKind === selectedKind && row.request.id === selectedRequest :
      (kindFilter === 'all' || row.requestKind === kindFilter) &&
      (filter === 'all' || row.request.state === filter ||
        (filter === 'completed' && (row.request.state === 'approved' || row.request.state === 'denied'))))
  visible.sort((a, b) => b.request.createdAt.localeCompare(a.request.createdAt))
  const loading = selectedRequest ? selectedKind === 'costCenter' ? linkedCostCenter.isLoading : requests.isLoading :
    requests.isLoading || costCenterRequests.isLoading
  const loadComplete = selectedRequest ? selectedKind === 'costCenter' ? linkedCostCenter.isSuccess : requests.isSuccess :
    requests.isSuccess && costCenterRequests.isSuccess
  const linkedUnavailable = linkedCostCenter.error instanceof ApiError && [403, 404].includes(linkedCostCenter.error.status)

  return (
    <>
      <PageHeader
        title="My requests"
        description="Track resource access and cost-center creation requests. Approval does not mean gateway access is ready."
        actions={<Link className="workspace-primary-link" to="/requests/new-cost-center">Request a cost center</Link>}
      />
      {selectedRequest && <p className="request-filters">Showing the linked request.
        <Button onClick={() => {
          const next = new URLSearchParams(params)
          next.delete('request')
          next.delete('requestKind')
          setParams(next)
        }}>Show all requests</Button>
      </p>}
      {!selectedRequest && <>
        {counts.isSuccess && <p className="request-counts">
          {formatCount(counts.data.pendingResourceCount, 'resource request')} pending · {formatCount(counts.data.pendingCostCenterCount, 'cost-center request')} pending
        </p>}
        {counts.isError && <p className="connection-note">Pending counts are unavailable. <Button appearance="transparent" onClick={() => void counts.refetch()}>Retry counts</Button></p>}
        <div className="request-filters">
          <label htmlFor="request-status">Request status</label>
          <select id="request-status" value={filter} onChange={(event) => {
            const next = new URLSearchParams(params)
            next.set('status', event.target.value)
            setParams(next)
          }}>
            <option value="all">All requests</option>
            <option value="pending">Pending review</option>
            <option value="completed">Approved or denied</option>
            <option value="withdrawn">Withdrawn</option>
          </select>
          <label htmlFor="request-kind">Request type</label>
          <select id="request-kind" value={kindFilter} onChange={(event) => {
            const next = new URLSearchParams(params)
            next.set('kind', event.target.value)
            setParams(next)
          }}>
            <option value="all">All request types</option>
            <option value="resource">Resource access</option>
            <option value="costCenter">Cost center</option>
          </select>
        </div>
      </>}
      {loading && <Loading label="Loading your requests" />}
      {(!selectedRequest || selectedKind === 'resource') && requests.isError && <div className="request-load-error">
        <ErrorState error={requests.error} />
        <Button onClick={() => void requests.refetch()}>Retry resource requests</Button>
      </div>}
      {!selectedRequest && costCenterRequests.isError && <div className="request-load-error">
        <ErrorState error={costCenterRequests.error} />
        <Button onClick={() => void costCenterRequests.refetch()}>Retry cost-center requests</Button>
      </div>}
      {selectedRequest && selectedKind === 'costCenter' && linkedCostCenter.isError && (linkedUnavailable
        ? <EmptyState title="Request unavailable">This request is no longer available to your account. Show all requests to continue.</EmptyState>
        : <div className="request-load-error"><ErrorState error={linkedCostCenter.error} /><Button onClick={() => void linkedCostCenter.refetch()}>Retry request</Button></div>)}
      {loadComplete && visible.length === 0 && <EmptyState title={selectedRequest ? 'Request unavailable' : rows.length === 0 ? 'No requests opened' : 'No requests match these filters'}>
          {selectedRequest ? 'This request is no longer available to your account. Show all requests to continue.' :
            rows.length === 0 ? 'Request a resource from the catalog or propose a cost center. Track each decision here.' : 'Choose another status or request type to see your requests.'}
        </EmptyState>}
        <div className="request-list">
          {visible.map((row) => {
            const request = row.request
            const key = `${row.requestKind}:${request.id}`
            const canWithdraw = request.state === 'pending' && (row.requestKind === 'resource' || !row.request.preparationStarted)
            return <Card key={key} className="request-card">
              {row.requestKind === 'costCenter' ? <>
                <CardHeader
                  header={<h2>{row.request.name}</h2>}
                  description={`Cost center · Opened ${new Date(row.request.createdAt).toLocaleDateString()}`}
                  action={<Badge appearance="tint">{requestStateLabel(row.request.state)}</Badge>}
                />
                <CostCenterRequestDetails request={row.request} />
              </> : <>
              <CardHeader
                header={
                  <h2>
                    {modelTitle(
                      row.request.resource,
                      row.request.resourceDisplayName,
                      headingSummary(row.request),
                      row.request.model,
                    )}
                    {row.request.resourceSummary?.available === false && (
                      <Badge className="inline-status-badge" appearance="outline" color="warning">
                        No longer available
                      </Badge>
                    )}
                  </h2>
                }
                description={<>
                  {withResourceKind(
                    row.request.resource,
                    row.request.resourceDisplayName,
                    `Opened ${new Date(request.createdAt).toLocaleDateString()}`,
                    headingSummary(row.request),
                  )}
                  {offeringLabel(row.request) && <> · <span className="resource-offering">{offeringLabel(row.request)}</span></>}
                  {row.request.model?.version?.trim() && <> · Version {row.request.model.version.trim()}</>}
                </>}
                action={<Badge appearance="tint">{requestStateLabel(request.state)}</Badge>}
              />
              <div className="badge-row">
                <EnvironmentBadge
                  azureCloud={headingSummary(row.request)?.azureCloud ?? 'commercial'}
                  environment={row.request.resourceSummary?.environment ?? row.request.requestedEnvironment}
                  environments={environments.data}
                />
              </div>
              <dl className="metadata-list">
                <div>
                  <dt>Gateway</dt>
                  <dd>{gatewayLabel(row.request.resourceSummary, row.request.resourceSnapshot?.gatewayName)}</dd>
                </div>
                {request.costCenter && (
                  <div>
                    <dt>Cost center</dt>
                    <dd>
                      {request.costCenter.name} ({request.costCenter.code})
                    </dd>
                  </div>
                )}
                {request.justification && <div>
                  <dt>Justification</dt>
                  <dd>{request.justification}</dd>
                </div>}
                {request.decisionNote && <div>
                  <dt>Decision note</dt>
                  <dd>{request.decisionNote}</dd>
                </div>}
                {request.state === 'approved' && (
                  <div>
                    <dt>Grant</dt>
                    <dd><GrantStatus request={row.request} /></dd>
                  </div>
                )}
              </dl>
              </>}
              {canWithdraw && (
                <div className="request-actions">
                  <Button onClick={() => { withdraw.reset(); setWithdrawingId(key) }} disabled={withdraw.isPending}>
                    Withdraw
                  </Button>
                  {withdrawingId === key && <>
                    <Text>Withdraw this request? You can submit a new request later.</Text>
                    <Button onClick={() => withdraw.mutate(row)} disabled={withdraw.isPending}>Confirm withdrawal</Button>
                    <Button onClick={() => setWithdrawingId(null)} disabled={withdraw.isPending}>Keep request</Button>
                  </>}
                  {withdraw.isError && withdraw.variables?.request.id === request.id && withdraw.variables.requestKind === row.requestKind && (
                    <Text role="alert" className="form-error">{withdraw.error.message}</Text>
                  )}
                </div>
              )}
            </Card>
          })}
        </div>
    </>
  )
}
