import { Badge, Card, CardHeader, Text } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import {
  describeAttribution,
  describeBinding,
  describeLimits,
  resourceLabel,
} from '../entitlement-format'

export function MyAccessPage() {
  const api = usePortalApi()
  const entitlements = useQuery({
    queryKey: ['portal', 'entitlements'],
    queryFn: api.listEntitlements,
  })

  return (
    <>
      <PageHeader
        title="My access"
        description="Resources you can use through MOSAIC and the limits that apply to each grant."
      />
      {entitlements.isLoading && <Loading label="Loading your access" />}
      {entitlements.isError && <ErrorState error={entitlements.error} />}
      {entitlements.isSuccess && entitlements.data.length === 0 && (
        <EmptyState title="No access granted yet">
          Your account has the portal role, but no model APIs or MCP servers are entitled to you yet.
        </EmptyState>
      )}
      {entitlements.isSuccess && entitlements.data.length > 0 && (
        <div className="access-list">
          {entitlements.data.map((resolved) => (
            <Card key={resolved.entitlement.id} className="access-card">
              <CardHeader
                header={<h2>{resourceLabel(resolved.entitlement.resource)}</h2>}
                description={
                  <Text>{describeAttribution(resolved)}</Text>
                }
                action={<Badge appearance={resolved.entitlement.enabled ? 'filled' : 'tint'}>{resolved.entitlement.enabled ? 'Enabled' : 'Disabled'}</Badge>}
              />
              <div className="access-card-grid">
                <section>
                  <h3>Limits</h3>
                  <ul className="plain-list">
                    {describeLimits(resolved.entitlement).map((limit) => (
                      <li key={limit}>{limit}</li>
                    ))}
                  </ul>
                </section>
                <section>
                  <h3>Usage attribution</h3>
                  <Text>{describeBinding(resolved.entitlement)}</Text>
                </section>
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  )
}
