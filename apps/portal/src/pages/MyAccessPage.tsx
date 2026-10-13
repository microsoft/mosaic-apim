import {
  Badge,
  Button,
  Card,
  CardHeader,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Text,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { BudgetBanner } from '../components/BudgetBanner'
import { ConnectionDetails } from '../components/ConnectionDetails'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { DetailPanel } from '../components/DetailPanel'
import {
  describeAttribution,
  describeBinding,
  describeLimits,
  describeRuntime,
  gatewayLabel,
  isModelResource,
  isSecurityGroupGrant,
  resourceTitle,
  withResourceKind,
} from '../entitlement-format'
import { usePortalEnvironments } from '../environments'
import type { ResolvedEntitlement } from '../types'

function costCenterText(resolved: ResolvedEntitlement) {
  return resolved.costCenter ? `${resolved.costCenter.name} · ${resolved.costCenter.code}` : 'Default cost center'
}

export function MyAccessPage() {
  const api = usePortalApi()
  const [params, setParams] = useSearchParams()
  const profile = useQuery({ queryKey: ['portal', 'profile'], queryFn: api.getProfile })
  const entitlements = useQuery({
    queryKey: ['portal', 'entitlements'],
    queryFn: api.listEntitlements,
  })
  const visibleEntitlements =
    entitlements.data
      ?.filter((resolved) => resolved.effective !== false)
      .sort((a, b) => {
        const nameA = resourceTitle(a.entitlement.resource, a.resourceDisplayName, a.resourceSummary)
        const nameB = resourceTitle(b.entitlement.resource, b.resourceDisplayName, b.resourceSummary)
        return nameA.localeCompare(nameB) || costCenterText(a).localeCompare(costCenterText(b))
      }) ?? []
  const environments = usePortalEnvironments()
  const selected = visibleEntitlements.find((resolved) => resolved.entitlement.id === params.get('grant'))
  function selectGrant(id: string | null) {
    const next = new URLSearchParams(params)
    if (id) next.set('grant', id)
    else next.delete('grant')
    setParams(next)
  }

  return (
    <>
      <PageHeader
        title="My access"
        description="Your recorded grants and their last reported APIM deployment state. Applied configuration is not proof of a successful model call."
      />
      <BudgetBanner />
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
          {' '}<Link to="/catalog">Discover resources in the catalog</Link>.
        </EmptyState>
      )}
      {entitlements.isSuccess && visibleEntitlements.length > 0 && (
        <div className={selected ? 'resource-workspace with-details' : 'resource-workspace'}>
        <div className="access-list">
          {visibleEntitlements.map((resolved) => (
            <Card key={resolved.entitlement.id} className="access-card">
              <CardHeader
                header={
                  <h2>
                    {resourceTitle(
                      resolved.entitlement.resource,
                      resolved.resourceDisplayName,
                      resolved.resourceSummary,
                    )}
                    {resolved.resourceSummary?.available === false && (
                      <Badge className="inline-status-badge" appearance="outline" color="warning">
                        No longer available
                      </Badge>
                    )}
                  </h2>
                }
                description={
                  <Text>
                    {withResourceKind(
                      resolved.entitlement.resource,
                      resolved.resourceDisplayName,
                      describeAttribution(resolved),
                      resolved.resourceSummary,
                    )}
                  </Text>
                }
                action={<Badge className="card-header-badge" appearance={resolved.entitlement.runtime?.status === 'applied' ? 'filled' : 'tint'}>{describeRuntime(resolved.entitlement)}</Badge>}
              />
              <div className="badge-row">
                <Badge appearance="tint">{costCenterText(resolved)}</Badge>
                <EnvironmentBadge environment={resolved.resourceSummary?.environment ?? null} environments={environments.data} />
              </div>
              <div className="resource-select">
                <p>{runtimeGuidance(resolved)}</p>
                <Button onClick={() => selectGrant(resolved.entitlement.id)}>View grant details</Button>
              </div>
            </Card>
          ))}
        </div>
        {selected && (
          <DetailPanel key={selected.entitlement.id} title={`Grant for ${resourceTitle(selected.entitlement.resource, selected.resourceDisplayName, selected.resourceSummary)}`}
            onClose={() => selectGrant(null)}>
              <p>{runtimeGuidance(selected)}</p>
              <div className="access-card-grid">
                <section>
                  <h3>Configured grant limits</h3>
                  <ul className="plain-list">
                    {describeLimits(selected.entitlement).map((limit) => (
                      <li key={limit}>{limit}</li>
                    ))}
                  </ul>
                  <Text size={200}>
                    {selected.entitlement.resource.kind === 'poolModel' ? 'Model' : 'Publication'} and
                    gateway limits may also apply. Pending changes are not yet enforced by the gateway.
                    {isSecurityGroupGrant(selected) && ' Group access limits apply to each person individually.'}
                  </Text>
                </section>
                <section>
                  <h3>Usage attribution</h3>
                  <Text>{describeBinding(selected.entitlement)}</Text>
                  <dl className="compact-facts">
                    <div>
                      <dt>Gateway</dt>
                      <dd>{gatewayLabel(selected.resourceSummary)}</dd>
                    </div>
                  </dl>
                </section>
              </div>
              {(isModelResource(selected.entitlement.resource.kind) ||
                selected.entitlement.resource.kind === 'mcpServer') && (
                <ConnectionDetails resolved={selected} />
              )}
          </DetailPanel>
        )}
        </div>
      )}
    </>
  )
}

function runtimeGuidance(resolved: ResolvedEntitlement) {
  switch (resolved.entitlement.runtime?.status) {
    case 'pending': return 'Waiting for an administrator to review and apply gateway access.'
    case 'applying': return 'Gateway access is being applied. Check again before connecting.'
    case 'applied': return 'Gateway configuration is applied. Open the grant details for connection instructions.'
    case 'failed': return 'Gateway access could not be applied. Ask an administrator to review the failure.'
    case 'revocationPending': return 'Removal is awaiting application at the gateway.'
    case 'revoked': return 'Gateway access has been revoked. Request new access from the catalog if needed.'
    default: return 'Gateway readiness has not been confirmed. Open the grant details or ask an administrator.'
  }
}
