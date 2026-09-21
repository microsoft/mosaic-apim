import { Badge, Button, Card, CardHeader, Textarea, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import {
  requestStateLabel,
  resourceFromCatalog,
  resourceKindLabel,
  sameResource,
} from '../entitlement-format'
import type { CatalogEntry } from '../types'

function CatalogAction({ entry }: { entry: CatalogEntry }) {
  const api = usePortalApi()
  const queryClient = useQueryClient()
  const requests = useQuery({ queryKey: ['portal', 'requests'], queryFn: api.listAccessRequests })
  const [justification, setJustification] = useState('')
  const resource = resourceFromCatalog(entry)
  const pendingRequest = requests.data?.find(
    (request) => request.state === 'pending' && sameResource(request.resource, resource),
  )
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['portal', 'catalog'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'requests'] }),
      queryClient.invalidateQueries({ queryKey: ['portal', 'profile'] }),
    ])
  }
  const create = useMutation({
    mutationFn: () =>
      api.createAccessRequest({
        resource,
        justification: justification.trim() || undefined,
      }),
    onSuccess: invalidate,
  })
  const withdraw = useMutation({
    mutationFn: (requestId: string) => api.withdrawAccessRequest(requestId),
    onSuccess: invalidate,
  })

  if (entry.entitled) {
    return <Badge appearance="filled">Already entitled</Badge>
  }
  if (entry.requestState === 'pending') {
    return (
      <div className="action-stack">
        <Text>A request is already open.</Text>
        <Button
          onClick={() => pendingRequest && withdraw.mutate(pendingRequest.id)}
          disabled={!pendingRequest || withdraw.isPending}
        >
          Withdraw
        </Button>
        {withdraw.isError && <Text className="form-error">{withdraw.error.message}</Text>}
      </div>
    )
  }
  return (
    <div className="action-stack">
      <Textarea
        value={justification}
        aria-label={`Justification for ${entry.displayName}`}
        placeholder="Optional justification"
        resize="vertical"
        onChange={(_, data) => setJustification(data.value)}
      />
      <Button appearance="primary" onClick={() => create.mutate()} disabled={create.isPending}>
        Request access
      </Button>
      {create.isError && <Text className="form-error">{create.error.message}</Text>}
    </div>
  )
}

export function CatalogPage() {
  const api = usePortalApi()
  const catalog = useQuery({ queryKey: ['portal', 'catalog'], queryFn: api.listCatalog })

  return (
    <>
      <PageHeader
        title="Catalog"
        description="Model APIs and MCP servers published for portal users. Request access when a resource is not already granted."
      />
      {catalog.isLoading && <Loading label="Loading catalog" />}
      {catalog.isError && <ErrorState error={catalog.error} />}
      {catalog.isSuccess && catalog.data.length === 0 && (
        <EmptyState title="No catalog entries">
          The portal catalog is empty. This is not an access denial state.
        </EmptyState>
      )}
      {catalog.isSuccess && catalog.data.length > 0 && (
        <div className="catalog-grid">
          {catalog.data.map((entry) => (
            <Card key={`${entry.kind}:${entry.id}`} className="catalog-card">
              <CardHeader
                header={<h2>{entry.displayName}</h2>}
                description={`${resourceKindLabel(entry.kind)} · ${entry.gatewayName ?? entry.gatewayId}`}
                action={entry.requestState && entry.requestState !== 'pending' ? <Badge appearance="tint">{requestStateLabel(entry.requestState)}</Badge> : undefined}
              />
              <Text>{entry.summary ?? 'No summary provided.'}</Text>
              <CatalogAction entry={entry} />
            </Card>
          ))}
        </div>
      )}
    </>
  )
}
