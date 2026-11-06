# MOSAIC workflows

[Documentation](README.md) | [Architecture](architecture.md) | [FAQ](faq.md)

The diagrams distinguish **saved intent**, **applied gateway configuration**, and **asynchronous
feedback**. Moving through one does not automatically complete the others.

- [Onboard and publish a model](#onboard-and-publish-a-model)
- [Set up an Entra-backed cost center](#set-up-an-entra-backed-cost-center)
- [Inherit Automatic access](#inherit-automatic-access)
- [Request access and make it effective](#request-access-and-make-it-effective)
- [Runtime call with an Entra security-group grant](#runtime-call-with-an-entra-security-group-grant)
- [Remove a member and finish credential revocation](#remove-a-member-and-finish-credential-revocation)
- [Usage, pricing, and dollar-budget feedback](#usage-pricing-and-dollar-budget-feedback)
- [Route a model through a pool](#route-a-model-through-a-pool)

## Onboard and publish a model

```mermaid
flowchart TD
    Register["Register gateway and model endpoint"] --> Observe["Verify access and sync inventory"]
    Observe --> Ready["Check cloud, environment, API shape, and runtime readiness"]
    Ready --> Intent["Save publication intent"]
    Intent --> Plan["Generate deterministic plan and review impact"]
    Plan --> Apply["Explicit apply on a gateway in manage mode"]
    Apply --> Result["Record per-step results and applied resources"]
    Result --> Access["Configure grants and apply access changes"]
```

MOSAIC needs control-plane permission to observe or change resources; the gateway separately needs
a network path and suitable credential/identity permissions to call the backend. A successful
inventory read is not proof of runtime inference access.

Import/adoption records existing APIs without taking over their policies. Optional creation of a
new Azure model deployment requires endpoint opt-in, permission/quota checks, and its own explicit
billing-aware review; it does not complete publication, grants, or pool membership.

See [gateways](gateways.md), [model endpoints](model-endpoints.md), and [model publishing](model-publishing.md).

## Set up an Entra-backed cost center

This is the custom-center workflow. General is exempt from the mandatory group; Government
offerings are not available in the new workflow.

```mermaid
flowchart TD
    Request["Optional portal center request:<br/>name, purpose, optional group ID"] --> Admin["Administrator chooses group setup<br/>and explicitly opts in to any requester addition"]
    Admin --> Save["Save center before external creation"]
    Save --> Choice{"Group setup choice"}
    Choice -->|"Attach existing"| Warn["Acknowledge real-group changes<br/>may affect other applications"]
    Warn --> Verify["Verify eligible group identity/type<br/>and exclusive center binding"]
    Choice -->|"Create with MOSAIC"| Consent{"Optional creation consent<br/>available to API identity?"}
    Consent -->|"No"| Incomplete["Keep center setup-required<br/>with setup or recovery guidance"]
    Choice -->|"Finish setup later"| Incomplete
    Consent -->|"Yes"| Create["Persist operation; create assigned<br/>security group with API as owner"]
    Create --> Outcome{"Directory outcome"}
    Outcome -->|"Verified created identity"| Verify
    Outcome -->|"Denied or failed"| Incomplete
    Outcome -->|"Unknown / timeout"| Unknown["Retain uncertain operation;<br/>reconcile identity, do not blindly create again"]
    Unknown --> Verify
    Verify -->|"Unsupported or conflicting"| Incomplete
    Verify -->|"Verified binding"| Members["Read paginated Entra members<br/>(Graph beta listing: preview);<br/>direct edits only with consent"]
    Members --> Modes["Choose exact-resource modes and limits"]
    Modes --> Apply["Review/apply resource gateway plans;<br/>track each result separately"]
```

An approved center request links to one saved center even when setup is incomplete. It does not
silently add the requester, create resource grants, or prove gateway readiness. A verified group
without membership-write consent has a read-only Members view, not an unverified binding.
Setup/capability checks are read-only; they do not create or delete sacrificial groups.
The direct member listing uses the Graph beta workaround for service principals missing from v1.0.
That preview listing dependency is not production-supported; APIM's stable JWT group-claim path
is separate. No live interoperability is established by these diagrams or fictional adapters.

See [center setup and recovery](cost-centers.md#set-up-cost-centers) and
[optional directory consent](deployment.md#optional-cost-center-group-management).

## Inherit Automatic access

Automatic is a resource mode, **not scheduled approval of personal requests**. It requires
caller-side Entra token support on the exact publication; a backend API key does not prevent it.

```mermaid
sequenceDiagram
    actor Admin as Administrator
    participant API as MOSAIC API
    participant Graph as Microsoft Graph
    participant Entra as Microsoft Entra ID
    participant Gateway as APIM
    actor Member
    Admin->>API: Set exact resource to Automatic<br/>under verified center
    API->>API: Save one managed center-group grant
    Admin->>API: Review and explicitly apply plan
    API->>Gateway: Install stable group grant and limits
    Note over API,Gateway: Resource configuration needs apply.<br/>Membership lists are not policy identity
    Admin->>API: Add direct member with optional consent
    API->>Graph: Add relationship to verified bound group
    Graph-->>API: Directory result
    API-->>Admin: Directory updated.<br/>Propagation/token refresh pending
    Note over API,Gateway: Member addition creates no per-user Automatic<br/>grant and makes zero APIM management writes
    Member->>Entra: Acquire runtime token after propagation
    Entra-->>Member: Invocation permission and center group claim
    Member->>Gateway: JWT and optional authorized center selector
    Gateway->>Gateway: Validate token and require center group<br/>and applied exact-resource grant
    Gateway-->>Member: Authorized result or explicit refusal
```

The sequence shows a successful directory write, not a guarantee of immediate claims. Failed or
unknown operations remain visible. Inherited access gives no group key or automatically generated
personal key. Explicit individual keys remain a separate eligible-grant path where enabled.

See [resource modes](cost-centers.md#resource-access-modes) and [keys](cost-centers.md#keys).

## Request access and make it effective

```mermaid
sequenceDiagram
    actor Person
    participant Portal as End-user portal
    participant API as MOSAIC API
    participant State as Cosmos
    actor Admin as Administrator
    participant Gateway as APIM
    Person->>Portal: Choose exact Request-only offering<br/>and eligible cost center
    Portal->>API: Submit access request
    API->>API: Verify center setup, membership,<br/>resource mode and availability
    API->>State: Save request
    Admin->>API: Approve with appropriate limits
    API->>API: Recheck current eligibility and mode
    API->>State: Save linked individual grant intent
    Note over State,Gateway: Approval alone<br/>changes no gateway policy
    Admin->>API: Review plan and explicitly apply
    API->>Gateway: Write reviewed authorization<br/>and limits
    API->>State: Record result<br/>and trusted binding
    Person->>Portal: Open applied<br/>connection details
    Person->>Gateway: Call with membership-bearing runtime token<br/>or separately allowed individual key
```

A scheduled approval policy can replace the manual resource approval step, not the gateway apply.
It still creates an individual grant and is different from a center's Automatic resource mode.
The offering, environment, and cost center remain explicit even when the portal groups visually
similar models. Not available blocks new requests/grants server-side. Security-group grants are
token-only; direct model grants can allow tokens, keys, or both according to resource and center
settings. For a custom center, an individual token grant still requires its group claim.
Default-center preferences are not membership. Government's separate key-only workflow is unchanged
and is excluded from these new center modes.

For an MCP server that calls a model, authorize two independent legs: the client's grant to the
MCP server and the server identity's grant to the model. One grant does not authorize the other.

See [grants](access.md), [approvals](approvals-and-notifications.md), and
[MCP servers that call models](mcp-servers-that-call-models.md).

## Runtime call with an Entra security-group grant

This is the commercial token path. No directory lookup occurs during the inference call.

```mermaid
sequenceDiagram
    actor Caller as Person or application
    participant Entra as Microsoft Entra ID
    participant Gateway as APIM
    participant Model as Model backend
    Caller->>Entra: Request token<br/>for runtime audience
    Entra-->>Caller: Signed token: invocation<br/>permission and group IDs
    Caller->>Gateway: Runtime token and optional<br/>cost-center selector
    Gateway->>Gateway: Validate issuer, signature,<br/>audience, tenant, and time
    Note over Gateway,Entra: Public metadata and signing keys,<br/>not Graph membership reads
    Gateway->>Gateway: Check scope or app role<br/>and select applied grant
    Gateway->>Gateway: Require custom-center group claim,<br/>including for an individual token grant
    Gateway->>Gateway: Check blocked center<br/>and native request/token limits
    alt Authorized and within limits
        Gateway->>Model: Forward using backend identity or key
        Model-->>Gateway: Model response
        Gateway-->>Caller: Response with applicable<br/>remaining-limit headers
    else Invalid, unauthorized, blocked, or limited
        Gateway-->>Caller: Refuse without forwarding to model
    end
```

The backend credential is separate from the caller's token. APIM compares group **object IDs**
from the validated JWT; it does not ask MOSAIC or Graph to resolve names or membership.
Invocation scopes/app roles and the resource grant are both required. App/agent callers need
their application role directly or through supported blueprint inheritance.

Existing token claims can outlive a membership change. A group-overage token cannot prove the
required custom-center membership; a direct token grant does not bypass that check. General
retains its exempt direct-token path. An enabled eligible direct grant wins over group grants
within the selected center; group counters are per member.

See [identity boundaries](architecture.md#which-identity-needs-which-access) and
[Entra calls](call-models-with-entra-tokens.md).

## Remove a member and finish credential revocation

```mermaid
sequenceDiagram
    actor Admin as Administrator
    participant API as MOSAIC API
    participant Graph as Microsoft Graph
    participant Entra as Microsoft Entra ID
    participant Gateway as APIM
    actor Caller
    Admin->>API: Remove a direct center-group member
    API->>Graph: Delete membership relationship only (/$ref)
    alt Directory updated
        Graph-->>API: Relationship removed
        API-->>Admin: Directory updated.<br/>Propagation and key follow-up remain
        Note over Entra,Gateway: Already-issued JWT claims survive until exp.<br/>Human tokens usually total 60-90 minutes.<br/>Managed-identity caching can be around 24 hours
        Caller->>Entra: Acquire token after propagation/cache refresh
        Entra-->>Caller: Token without center group claim
        Caller->>Gateway: Present refreshed runtime token
        Gateway->>Gateway: Deny custom-center JWT access,<br/>even with an individual token grant
        API->>API: Identify affected individual grants/keys.<br/>Retain incomplete rechecks
        API-->>Admin: Review individual-key revocation pending
        Admin->>API: Recheck eligibility and review revoke/apply
        API->>Gateway: Apply affected individual grant/key changes
        Note over API,Gateway: Membership removal itself neither rebuilds<br/>the Automatic grant nor revokes an existing key
    else Denied, throttled, failed, or unknown
        Graph-->>API: Actual failure or uncertain result
        API-->>Admin: Not confirmed removed.<br/>Retain operation and recovery guidance
    end
```

The relevant delay after removal is the token's **remaining** lifetime, plus directory/cache
behavior, not a universal 90-minute window. There is no immediate
JWT invalidation or CAE promise for this path.

If membership changed outside MOSAIC, explicitly recheck individual-key eligibility and finish
revoke/apply. There is no directory watcher to discover and revoke keys automatically. Removing
one direct relationship also does not remove a separate inherited membership path.

See [removal and credentials](cost-centers.md#removing-a-member).

## Usage, pricing, and dollar-budget feedback

```mermaid
flowchart TB
    Calls["APIM handles calls<br/>and native limits"] --> Logs["Log Analytics ingestion<br/>Gateway and LLM logs"]
    Logs -->|"Roll up every 15 minutes<br/>by default"| Rollups[("Cosmos<br/>Daily and monthly usage")]
    Prices["Sourced prices<br/>with effective dates"] --> Cost["Price measured usage"]
    Rollups --> Cost
    Cost --> Views["Dashboard, analytics,<br/>and portal"]
    Cost --> Judge["Background dollar-budget<br/>evaluation"]
    Judge --> Notify["Configured alerts"]
    Judge -->|"Blocking cost-center budget"| Block["Audited blocked-list update<br/>on managed gateways"]
    Block --> Calls
```

The remaining-quota response header comes from APIM's native counter, **not this pipeline**.
The pipeline supplies reporting, price estimates, forecasts, and dollar-budget decisions.
Monthly dollar budgets aggregate gateways; pooled native quotas remain gateway-scoped.

Dollar-budget blocking normally trails usage by 15-30 minutes and can overshoot. Missing prices,
ingestion delay, worker downtime, or failed blocked-list updates weaken the protection. An
organization budget only warns. Alerts additionally depend on enabled workers, recipient
preferences, and provider readiness; saved settings do not prove delivery.

See [usage analytics](usage-analytics.md), [pricing](pricing.md), [budgets](budgets-and-alerts.md),
and [notification readiness](approvals-and-notifications.md).

## Route a model through a pool

```mermaid
flowchart TD
    Call["Call pool API with model name"] --> Grant["Authorize exact pool model and cost center"]
    Grant --> Mode{"Configured pool mode"}
    Mode -->|Breaker| Weighted["Weighted native APIM backends and breakers"]
    Mode -->|Preferential| Priority["Provisioned first, pay-as-you-go overflow"]
    Mode -->|Linear| Ordered["First member on each request, then ordered failover"]
    Weighted --> Target["Eligible backend attempt"]
    Priority --> Target
    Ordered --> Target
    Target --> Response["Return result and record attempt telemetry"]
```

The caller sees the pool's model name and base URL, not its deployment topology. Pools do not
relax cloud, environment, API-shape, or readiness checks, and their models retain separate grants
and limits. Native breakers do not apply uniformly to every member: key-authenticated targets
are separately attempted, and linear pools do not use breakers.

This diagram summarizes routing choices, not a guarantee of successful failover. Breaker behavior,
retryable responses, token-metering support, and excluded APIs are described in [model pools](model-pools.md).
