import {
  Badge,
  Card,
  CardHeader,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Text,
} from '@fluentui/react-components'
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
  isSecurityGroupGrant,
  resourceLabel,
} from '../entitlement-format'

export function MyAccessPage() {
  const api = usePortalApi()
  const profile = useQuery({ queryKey: ['portal', 'profile'], queryFn: api.getProfile })
  const entitlements = useQuery({
    queryKey: ['portal', 'entitlements'],
    queryFn: api.listEntitlements,
  })
  const visibleEntitlements = entitlements.data?.filter((resolved) => resolved.effective !== false) ?? []

  return (
    <>
      <PageHeader
        title="My access"
        description="Your recorded grants and their last reported APIM deployment state. Applied configuration is not proof of a successful model call."
      />
      {profile.data?.groupsOverage && (
        <MessageBar intent="warning" className="access-overage-notice">
          <MessageBarBody>
            <MessageBarTitle>Group access may be missing</MessageBarTitle>
            You belong to too many groups for your sign-in token to list them, so access granted to
            your groups can&apos;t be applied. Ask an administrator for a direct grant.
          </MessageBarBody>
        </MessageBar>
      )}
      {entitlements.isLoading && <Loading label="Loading your access" />}
      {entitlements.isError && <ErrorState error={entitlements.error} />}
      {entitlements.isSuccess && visibleEntitlements.length === 0 && (
        <EmptyState title="No access granted yet">
          Your account has the portal role, but no model APIs or MCP servers are entitled to you yet.
        </EmptyState>
      )}
      {entitlements.isSuccess && visibleEntitlements.length > 0 && (
        <div className="access-list">
          {visibleEntitlements.map((resolved) => {
            const attribution = describeAttribution(resolved)
            const securityGroupGrant = isSecurityGroupGrant(resolved)
            return (
              <Card key={resolved.entitlement.id} className="access-card">
                <CardHeader
                  header={<h2>{resourceLabel(resolved.entitlement.resource)}</h2>}
                  description={attribution ? <Text>{attribution}</Text> : undefined}
                  action={
                    <Badge appearance={resolved.entitlement.runtime?.status === 'applied' ? 'filled' : 'tint'}>
                      {describeRuntime(resolved.entitlement)}
                    </Badge>
                  }
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
                      enforced by the gateway.
                      {securityGroupGrant && ' Group access limits apply to each person individually.'}
                    </Text>
                  </section>
                  <section>
                    <h3>Usage attribution</h3>
                    <Text>{describeBinding(resolved.entitlement)}</Text>
                  </section>
                </div>
                {(resolved.entitlement.resource.kind === 'modelApi' ||
                  resolved.entitlement.resource.kind === 'mcpServer') && (
                  <ConnectionDetails resolved={resolved} />
                )}
              </Card>
            )
          })}
        </div>
      )}
    </>
  )
}
