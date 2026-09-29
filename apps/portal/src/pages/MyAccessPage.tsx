import { Badge, Card, CardHeader, Text } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ConnectionDetails } from '../components/ConnectionDetails'
import { PageHeader } from '../components/PageHeader'
import {
  describeAttribution,
  describeBinding,
  describeLimits,
  describeRuntime,
  resourceTitle,
  withResourceKind,
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
        description="Your recorded grants and their last reported APIM deployment state. Applied configuration is not proof of a successful model call."
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
                header={
                  <h2>{resourceTitle(resolved.entitlement.resource, resolved.resourceDisplayName)}</h2>
                }
                description={
                  <Text>
                    {withResourceKind(
                      resolved.entitlement.resource,
                      resolved.resourceDisplayName,
                      describeAttribution(resolved),
                    )}
                  </Text>
                }
                action={<Badge appearance={resolved.entitlement.runtime?.status === 'applied' ? 'filled' : 'tint'}>{describeRuntime(resolved.entitlement)}</Badge>}
              />
              <div className="access-card-grid">
                <section>
                  <h3>Configured grant limits</h3>
                  <ul className="plain-list">
                    {describeLimits(resolved.entitlement).map((limit) => (
                      <li key={limit}>{limit}</li>
                    ))}
                  </ul>
                  <Text size={200}>
                    Publication and gateway limits may also apply. Pending changes are not yet
                    enforced by APIM.
                  </Text>
                </section>
                <section>
                  <h3>Usage attribution</h3>
                  <Text>{describeBinding(resolved.entitlement)}</Text>
                </section>
              </div>
              {resolved.entitlement.resource.kind === 'modelApi' && (
                <ConnectionDetails resolved={resolved} />
              )}
            </Card>
          ))}
        </div>
      )}
    </>
  )
}
