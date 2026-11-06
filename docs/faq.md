# MOSAIC FAQ

[Documentation](README.md) | [Architecture](architecture.md) | [Workflow diagrams](workflows.md)

## Why use MOSAIC if APIM already has an AI gateway?

APIM provides the runtime gateway and its native policies. MOSAIC adds an opinionated governance
workflow around existing gateways: resource discovery, reviewed publishing, identity-aware grants,
access requests, cost centers, and consolidated usage and estimated cost. It does not replace APIM,
host the models, or require clients to send inference through the MOSAIC API.

See [architecture](architecture.md) and the [capability inventory](overview.md).

## Does each inference call ask MOSAIC whether it is allowed?

No. Applying a reviewed plan installs the authorization rules and limits in APIM. APIM validates
the runtime credential, selects an applied grant, checks the relevant limits and blocked-cost-center
list, and forwards an allowed call. It does not query MOSAIC, Cosmos, Graph, or Log Analytics to
authorize that call. A valid token alone is not sufficient: an applied grant must also match.
If a client supplies both a key and a token, both must validate and select the same grant.

See the [runtime workflow](workflows.md#runtime-call-with-an-entra-security-group-grant).

## Does approving a request make access live immediately?

No. Approval creates or updates the requester's grant intent in MOSAIC. An administrator must still
review and apply the affected gateway plan. Scheduled automatic approval follows the same boundary.
An approved request, an applied grant, and a credential that can call the endpoint are different
states; the portal reports the connection state rather than treating approval as proof.

Importing an existing customer-owned API also does not install MOSAIC enforcement on it. See
[grants](access.md), [approvals](approvals-and-notifications.md), and [publishing](model-publishing.md).

## Does APIM need Microsoft Graph permissions to check security groups?

**No Graph directory-read consent is needed for this runtime check.** Entra issues the caller's
runtime token with security-group object IDs. APIM validates the token and matches those IDs to the
applied group grants. It does not look up the caller's membership in Graph during inference.

| Component | Required access for this path |
| --- | --- |
| APIM gateway | Network access to Entra's public OpenID metadata and signing keys; a correctly configured runtime token-validation policy |
| MOSAIC API identity | Administrator-consented directory reads; separate optional consent for control-plane group creation/member edits |
| Calling person or application | A runtime-audience token, invocation scope/app role, matching applied grant, and the custom center's group claim where required |

Configure security-group claims on the runtime registration. Matching uses **object IDs**, not
display names. APIM still needs network access to its backend; private networking is a separate
readiness requirement. See [identity permissions](architecture.md#which-identity-needs-which-access)
and [Entra client setup](call-models-with-entra-tokens.md).

The platform policy is documented in Microsoft's
[`validate-azure-ad-token` reference](https://learn.microsoft.com/en-us/azure/api-management/validate-azure-ad-token-policy).

## Does MOSAIC create groups or change Entra membership?

Yes, in the **explicitly consented cost-center management workflow**. An administrator can attach
an eligible existing Entra group or ask MOSAIC to create one, and can manage its actual direct
membership when permitted. Attaching requires acknowledgment that changes can affect other apps
using that group. There is no editable internal shadow roster.

Directory lookup retains its read-only baseline: `User.ReadBasic.All`, `GroupMember.Read.All`,
and `AgentIdentity.Read.All`. Optional application lookup/photos remain separate read permissions.
Group creation optionally uses `Group.Create` and explicitly makes the API service principal an
owner. User membership writes optionally use `GroupMember.ReadWrite.All`; adding service
principals additionally needs separately consented `Application.ReadWrite.All`. These permissions
belong to the **API identity**, never APIM, and can have tenant-wide scope even though MOSAIC
restricts write targets.

A MOSAIC Admin role is not consent. Ordinary resource approval does not silently add members,
consent a client, create app registrations, assign invocation/Azure roles, or delete a group.
Responsible contact emails do not assign group ownership or membership.

The direct Members list currently uses Graph **beta** to include service principals, following
[Microsoft's v1.0 workaround](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-1.0).
This listing API is preview and
[not supported for production use](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-beta);
it is separate from the stable local JWT group-grant check in APIM. Live group-management/APIM
interoperability has not been proved by this change.

Generic MOSAIC-local groups remain desired-state records, **not an APIM authorization fallback**.
The local managed-center experiment is superseded by [ADR 0030](adr/0030-entra-backed-cost-centers.md);
the old APIM-local membership/inherited-key proposal is historical and unimplemented.
See [optional setup](deployment.md#optional-cost-center-group-management) and [cost centers](cost-centers.md).

## What is the difference between a center, its group, and a grant?

The center defines **charging and resource policy**. Its dedicated, exclusively bound Entra group
defines **membership**. A grant authorizes **one exact resource under that center**. General is
exempt from the mandatory group by built-in identity; Government offerings are excluded from the
new workflow.

Automatic gives members a stable group grant for an exact model, pool-model offering, or MCP
server after gateway apply. Joining the group creates no per-person automatic entitlements or
APIM writes. Request-only lets eligible members ask for an individual grant; Not available blocks
new requests/grants server-side. The pool scope is part of a pool model's identity, and model/MCP
grants remain independent.

Automatic needs caller JWT support, not a token-authenticated backend. It never silently enables
tokens or creates keys for every member. Explicit individual keys remain available where the
resource and center allow them. An individual token grant still requires the custom center's
group claim; it is not a membership exception. See [resource modes](cost-centers.md#resource-access-modes).

## What if group setup fails, or I need a new center?

MOSAIC saves a center before external group creation. Missing consent or failed creation leaves a
recoverable setup-required center, not usable custom-center access. A timeout is an unknown
outcome: reconcile the actual group identity instead of blindly creating another. A verified
attached group without write consent is different: it can support access with a read-only
membership view.

Portal users can request a center with a name, purpose, and optional existing group object ID,
without directory search/write privileges. An administrator may use or ignore the suggestion and
explicitly chooses initial members. Approval links to one saved center even if setup is incomplete;
it does not itself add the requester, approve a model, or apply a gateway plan.
See [center requests](cost-centers.md#request-a-cost-center).

## Are groups, scopes, app roles, and MOSAIC roles interchangeable?

No. `Admin` and `User` control the MOSAIC administrative and portal APIs. Runtime model calls use a
different audience: people use delegated `Models.Invoke`; app-only callers use
`Models.Invoke.Application`. MCP uses separate `Mcp.Invoke` / `Mcp.Invoke.Application` permissions.
A scope or app role permits that kind of invocation; an applied resource grant decides which
model or server the caller may use.

A security group identifies a set of principals by its object ID. Assigning an application role
to a group does not make that role appear in service-principal tokens: app/agent members still
need the application permission directly or through supported Agent ID blueprint inheritance.
Security-group grants are token-only and do not issue subscription keys.

See [model tokens](call-models-with-entra-tokens.md), [agent identities](call-models-with-agent-identities.md),
and [MCP connections](connect-to-mcp-servers.md).

## What happens when membership changes or a caller belongs to too many groups?

APIM checks the presented token, not live directory membership. Removal does not invalidate
already-issued JWTs. Human tokens normally last **60-90 minutes from issuance**; the actual
remaining `exp` and token policy matter after removal. New claims also depend on propagation.
Managed identities can cache per-resource tokens/authorization for **around 24 hours**, and
membership changes can take several hours. This is not a universal 90-minute bound or a promise
of immediate revocation/CAE.

Keys are independent individual credentials. Removal does not revoke a copied key. MOSAIC exposes
affected individual access for eligibility recheck and explicit revoke/apply; out-of-band membership
changes require an explicit recheck, not an assumed directory watcher. Directory success, token
refresh pending, and key revocation pending are different states.

For group overage, Entra can omit group IDs and emit overage markers. APIM does not resolve these
through Graph. A direct token grant can avoid an optional resource-group match under General,
but **cannot bypass a custom center's mandatory group claim**. Ask an administrator to resolve
claim eligibility or review an allowed individual-key path; never silently move the call to General.

An eligible enabled direct grant still takes precedence over group grants within the selected
center. Otherwise the most generous matching group grant wins. Group counters are per caller's
object ID, not one shared allowance. See [removal](cost-centers.md#removing-a-member),
[grants](access.md), and [ADR 0016](adr/0016-agent-identities-and-security-group-grants.md).

## Where does the remaining-token header come from?

`x-mosaic-remaining-quota-tokens` comes from APIM's native `llm-token-limit` counter for the applied
grant. It is not calculated by a KQL query and is not the backend deployment's available capacity.
The separate `x-mosaic-cost-center-remaining-quota-tokens` header reports a configured pooled
cost-center token quota.

Native APIM limits enforce calls as they arrive, but distributed counters, concurrent calls, and
streaming token estimates are not exact global accounting. Headers appear only when the relevant
limit and gateway capability apply. A pooled cost-center quota is per model/publication and
gateway; cost reporting and dollar budgets can aggregate multiple gateways.

See [response headers](call-models-with-entra-tokens.md) and [pooled quotas](cost-centers.md#limits-and-pooled-quotas).

## Does the cost-center header grant access or just add a log label?

Neither. `x-mosaic-cost-center` selects a cost center under which the caller already has an
authorized grant. APIM rejects an unauthorized selection and strips the header before forwarding
to the backend. A subscription key already selects its cost center; a conflicting header is
rejected. Neither a personal default nor the tenant default adds anyone to a custom center's
Entra group or creates resource permission.

The header is optional. For token calls without one, selection prefers a direct grant under the
caller's default center, then other direct grants oldest first, then matching group grants by
precedence. Limits and attribution follow the selected grant. See [cost-center selection](cost-centers.md#choose-a-cost-center-on-a-call).

## Are the dashboard and portal querying KQL on every page view?

No. The background rollup job reads APIM gateway and LLM logs from Log Analytics, by default every
900 seconds, and persists daily/monthly figures in Cosmos. Normal usage, dashboard, analytics,
and portal views read those rollups. Pool Health and explicit telemetry diagnostics are separate
paths that can query logs directly.

Figures therefore trail calls by ingestion and processing delay. Daily rollups default to 400-day
retention; monthly summaries remain until deleted. Workspace retention is independent, and
per-caller history requires appropriate tenant retention and access controls.

See [usage analytics](usage-analytics.md) and [analytics views](analytics-reference.md).

## Is MOSAIC cost the Azure bill?

No. MOSAIC prices measured tokens using sourced, effective-dated public, manual, or approved
billing-derived prices. That supports estimates, chargeback, and forecasts; it is not invoice
reconciliation. Supported USD billing extracts or read-only Azure/AWS/GCP connections can derive
contract-discounted, amortized rates from a completed month, excluding tax and one-off credits.
Unpriced usage is shown as **No price**, not zero. Cached prompt tokens currently use the ordinary input rate; provisioned
throughput is allocated by token share, with idle capacity reported separately.

Billing retrieval and optional monthly refresh create drafts; an administrator must review
evidence, mappings, and effective date before publishing. There is no automatic activation,
FX, or percentage/fixed-dollar markup policy. Billing data can arrive late even after month end;
a changed extract needs a new review, not a silent historical rewrite. See [pricing](pricing.md) and
[Microsoft's cost-data timing guidance](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data).

## Is a dollar budget a hard spending cap?

No. Dollar budgets are evaluated from delayed priced rollups. A blocking cost-center budget writes
the blocked-center named value on managed gateways; APIM then refuses matching calls. Normal
blocking delay is documented as roughly **15-30 minutes**, with possible overspend. Missing prices,
stale telemetry, concurrent traffic, or a failed gateway update further limit the guarantee.

Organization-wide budgets warn only. Cost-center budgets can warn or block; native token/request
quotas are a separate runtime control, not a perfectly synchronized dollar ledger.
See [budget feedback](workflows.md#usage-pricing-and-dollar-budget-feedback) and [budgets](budgets-and-alerts.md).

## How do model pools handle throttling and failover?

Breaker pools use native APIM weighted backend pools and circuit breakers. Preferential pools try
provisioned capacity before pay-as-you-go overflow. Linear pools start at the first member on
each request and try members in order; they do not have circuit breakers.

Breaker presets honor `Retry-After`, with 10- or 30-second fallback trips, not one universal
one-minute wait. Key-authenticated members are tried separately; do not assume every member uses
the same native breaker mechanism. Members must satisfy model/API-shape, cloud, environment, and
readiness constraints. Responses is excluded because backend conversation state is not
interchangeable. See [model pools](model-pools.md).

## Does discovering a provider mean every feature works with it?

No. Inventory detection, deployment discovery, publication, token metering, and pricing are
different capabilities. Current publishing paths distinguish Azure OpenAI, Foundry model
inference, and Anthropic Messages shapes; Claude on AWS Bedrock is available as a key-authenticated
pool-member path. Anthropic token metering needs an eligible v2 APIM tier; classic tiers can use
call limits instead.

Creating an Azure model deployment is another distinct, explicit, potentially billable operation:
it does not automatically publish it, grant access, or add it to a pool. See
[model endpoints](model-endpoints.md), [publishing](model-publishing.md), and [pools](model-pools.md).

## Can commercial MOSAIC manage Azure Government?

Yes, through an explicitly configured Government management identity. Government model and MCP
runtime offerings are key-only, not commercial-Entra-token offerings. Government gateways use
their own identities for supported backend access. They are excluded from the new Entra-backed
center setup and access-mode workflow; this redesign adds no Government functionality.

Management metadata, usage, and the separately stored management credential are processed in the
commercial hosting arrangement. This requires organizational approval and is not a sovereignty
or compliance guarantee. See [Azure Government](azure-government.md).

## What keeps working if MOSAIC is unavailable?

Existing APIM routes continue using their applied configuration; MOSAIC is not a synchronous
authorization hop. New approvals/applies, revocations, credential reveal, reporting updates, and
budget-block changes can be unavailable or delayed. Existing runtime calls still depend on APIM,
the backend, networking, token-validation metadata/keys, and any configured Key Vault-backed
credentials. This is not a blanket availability guarantee.

MOSAIC also does not continuously repair drift in the background. Re-plan and explicitly apply
reviewed changes. See [reconciliation](architecture.md#reconciliation-boundary) and
[operation recovery](access.md#recovering-an-interrupted-operation).

## How do old MVP centers move to this design?

Explicitly reconfigure or recreate them. A custom center without a verified new Entra binding
remains setup-required. Legacy local group IDs are not Entra object IDs, and MOSAIC does not
silently copy old members, broaden individual grants, delete data, or move charges to General.
There is no production migration or upgrade wizard. See
[earlier records](cost-centers.md#earlier-mvp-records-and-historical-proposals).

## Who operates and supports MOSAIC?

The adopting organization operates its deployment, permissions, networking, upgrades, capacity,
retention, and incident response. MOSAIC is MIT-licensed repository software; this repository
does not establish an Azure product support SLA. Use [CODEOWNERS](../.github/CODEOWNERS) for code
ownership and [SECURITY.md](../SECURITY.md) for security reporting.

Do not treat architectural intent as a published scale benchmark. Size and load-test the actual
gateway tier, traffic, counter behavior, telemetry volume, and control-plane workload. The
[capability inventory](overview.md) and [E2E evidence](e2e/roadmap.md) distinguish implementation
from demonstrated journeys.
