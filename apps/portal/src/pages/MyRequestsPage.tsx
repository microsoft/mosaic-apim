import { Badge, Button, Card, CardHeader, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { gatewayLabel, requestStateLabel, resourceTitle, withResourceKind } from '../entitlement-format'
import { usePortalEnvironments } from '../environments'
import type { AccessRequest, ResourceSummary } from '../types'

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
    available: false,
  }
}

function GrantStatus({ request }: { request: AccessRequest }) {
  if (request.grantedEntitlementId) {
    return (
      <>
        Approval created your grant. It may not work until an administrator applies it.{' '}
        <Link to="/access">See its status in My access</Link>
      </>
    )
  }
  return <>Approved before approvals created grants, so no grant is linked to this request.</>
}

export function MyRequestsPage() {
  const api = usePortalApi()
  const queryClient = useQueryClient()
  const requests = useQuery({ queryKey: ['portal', 'requests'], queryFn: api.listAccessRequests })
  const environments = usePortalEnvironments()
  const withdraw = useMutation({
    mutationFn: (requestId: string) => api.withdrawAccessRequest(requestId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['portal', 'requests'] }),
        queryClient.invalidateQueries({ queryKey: ['portal', 'catalog'] }),
        queryClient.invalidateQueries({ queryKey: ['portal', 'profile'] }),
      ])
    },
  })

  return (
    <>
      <PageHeader
        title="My requests"
        description="Access requests you opened and any decision notes returned by administrators."
      />
      {requests.isLoading && <Loading label="Loading your requests" />}
      {requests.isError && <ErrorState error={requests.error} />}
      {requests.isSuccess && requests.data.length === 0 && (
        <EmptyState title="No requests opened">
          Requests you submit from the catalog will appear here.
        </EmptyState>
      )}
      {requests.isSuccess && requests.data.length > 0 && (
        <div className="request-list">
          {requests.data.map((request) => (
            <Card key={request.id} className="request-card">
              <CardHeader
                header={
                  <h2>
                    {resourceTitle(
                      request.resource,
                      request.resourceDisplayName,
                      headingSummary(request),
                    )}
                    {request.resourceSummary?.available === false && (
                      <Badge className="inline-status-badge" appearance="outline" color="warning">
                        No longer available
                      </Badge>
                    )}
                  </h2>
                }
                description={withResourceKind(
                  request.resource,
                  request.resourceDisplayName,
                  `Opened ${new Date(request.createdAt).toLocaleDateString()}`,
                  headingSummary(request),
                )}
                action={<Badge appearance="tint">{requestStateLabel(request.state)}</Badge>}
              />
              <div className="badge-row">
                <EnvironmentBadge
                  environment={request.resourceSummary?.environment ?? request.requestedEnvironment}
                  environments={environments.data}
                />
              </div>
              <dl className="metadata-list">
                <div>
                  <dt>Gateway</dt>
                  <dd>{gatewayLabel(request.resourceSummary, request.resourceSnapshot?.gatewayName)}</dd>
                </div>
                <div>
                  <dt>Justification</dt>
                  <dd>{request.justification || 'No justification provided.'}</dd>
                </div>
                <div>
                  <dt>Decision note</dt>
                  <dd>{request.decisionNote || 'No decision note.'}</dd>
                </div>
                {request.state === 'approved' && (
                  <div>
                    <dt>Grant</dt>
                    <dd><GrantStatus request={request} /></dd>
                  </div>
                )}
              </dl>
              {request.state === 'pending' && (
                <div className="request-actions">
                  <Button onClick={() => withdraw.mutate(request.id)} disabled={withdraw.isPending}>
                    Withdraw
                  </Button>
                  {withdraw.isError && <Text className="form-error">{withdraw.error.message}</Text>}
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
    </>
  )
}
