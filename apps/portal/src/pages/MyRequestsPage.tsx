import { Badge, Button, Card, CardHeader, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { requestStateLabel, resourceTitle, withResourceKind } from '../entitlement-format'
import type { AccessRequest } from '../types'

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
                header={<h2>{resourceTitle(request.resource, request.resourceDisplayName)}</h2>}
                description={withResourceKind(
                  request.resource,
                  request.resourceDisplayName,
                  `Opened ${new Date(request.createdAt).toLocaleDateString()}`,
                )}
                action={<Badge appearance="tint">{requestStateLabel(request.state)}</Badge>}
              />
              <dl className="metadata-list">
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
