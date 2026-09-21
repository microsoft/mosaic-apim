import { Badge, Button, Card, CardHeader, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { requestStateLabel, resourceLabel } from '../entitlement-format'

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
                header={<h2>{resourceLabel(request.resource)}</h2>}
                description={`Opened ${new Date(request.createdAt).toLocaleDateString()}`}
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
