import { MessageBar, MessageBarBody, MessageBarTitle } from '@fluentui/react-components'
import { isAutomaticAccess } from '../portal-access'
import type { ResolvedEntitlement } from '../types'

export function CostCenterAccessNotice({ resolved }: { resolved: ResolvedEntitlement }) {
  const runtime = resolved.entitlement.runtime
  const automatic = isAutomaticAccess(resolved)
  return <section className="cost-center-access-notice" aria-label="Cost-center access conditions">
    {automatic && <p>This access is inherited through your cost center&apos;s Entra group. It uses one shared group entitlement, not an individual grant. No personal key is inherited. Caller-side Entra token support and the required invocation permission and group claims must be enabled.</p>}
    {(automatic || resolved.costCenter) && <p>Custom cost centers require verified group setup and membership claims for token access. General is exempt.
      Directory changes need propagation and new tokens; existing JWT claims last until expiry, typically 60–90 minutes from issuance for people.
      Managed-identity caches can last around 24 hours. Previously copied keys require separate revocation and gateway apply.</p>}
    {(runtime?.eligibilityRecheckPending || runtime?.keyRevocationPending) && <MessageBar intent="warning">
      <MessageBarBody>
        <MessageBarTitle>Eligibility or key review pending</MessageBarTitle>
        {runtime.eligibilityRecheckPending && 'An administrator must check this grant’s current membership eligibility. '}
        {runtime.keyRevocationPending && 'Previously copied keys may still work until they are revoked and the gateway changes are applied. '}
        Key creation, rotation, and reveal use fresh membership verification and may be refused even while an older token still has group claims.
      </MessageBarBody>
    </MessageBar>}
    {resolved.entitlement.resource.kind === 'mcpServer' && <p>An MCP grant does not grant model access. The server needs separate authorization for its model calls.</p>}
  </section>
}
