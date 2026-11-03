# Capabilities and roadmap

[Documentation](README.md) | [MOSAIC](../README.md)

- [What is implemented](#what-is-implemented)
- [Azure Government from a commercial deployment](#azure-government-from-a-commercial-deployment)
- [Current boundaries and deferred work](#current-boundaries-and-deferred-work)
- [Roadmap](#roadmap)

This is the detailed feature inventory, not the starting tutorial. Use [architecture](architecture.md),
[workflows](workflows.md), and [FAQ](faq.md) for the operating model. Stored governance intent,
applied runtime enforcement, and measured reporting are deliberately different capabilities.

## What is implemented

- Python 3.12 FastAPI API with OpenAPI, structured JSON logging, Azure Monitor OpenTelemetry,
  correlation IDs, anonymous `/healthz` and dependency-aware `/readyz`
- Entra issuer, audience, signature, tenant, expiry, and algorithm validation, with app-role
  authorization decided per route: `Admin` for every administrative route, `User` for the
  end-user portal surface
- Principal, group, and membership CRUD with validation, stable errors, and audit events,
  including a read-only Microsoft Graph lookup baseline for users, agent identities, agent users
  and Entra security groups; separately consented cost-center group creation and direct-member
  editing through the API identity, without an editable local shadow roster
- Multi-gateway onboarding: register any existing API Management service by resource ID, verify
  access, and mirror its APIs, endpoints, products, subscriptions, users, groups, backends, and
  named value metadata into Cosmos
- Entitlements as desired state: grants to a user, MOSAIC group, Entra security group, application
  or agent over a model API, MCP server, product, or model deployment; token and request limits;
  catalog visibility; access requests, whose approval creates and links the requester's grant
  intent; and effective-access resolution that reports whether a grant arrived directly or through
  a group
- Governed access for direct user/application/agent grants and Entra security-group grants to
  MOSAIC-published model APIs: reviewed APIM deployment, key or Entra authentication for direct
  grants, Entra-only group grants, shared token/request limits, revocation, and distinct desired
  versus applied state
- MCP publishing and governed access for MOSAIC-registered servers: reviewed APIM deployment of a
  passthrough MCP API, per-publication resource metadata, Entra direct and security-group
  grants in Commercial, key-only direct grants in Government, call limits, credential stripping,
  backend managed identity and fail-closed recovery
- Current-user entitlement and connection APIs, plus audited on-demand key retrieval, which the
  portal's My access page uses to show connection details and reveal keys for applied direct
  model grants
- Model endpoint onboarding: register Azure OpenAI and Azure AI Foundry resources, verify MOSAIC's
  control-plane access, discover the deployments and available models on them, and report — per
  registered gateway — whether that gateway's managed identity can actually call them, judged by
  the data actions its roles grant on the resource the published API calls and by whether the
  gateway has a network path to it. A resource MOSAIC can't reach, such as one in another tenant,
  can be registered by URL with an API key held in Key Vault, with its deployments declared and
  published through a Key Vault-backed named value
- Azure model deployment: query an account's live model/version/SKU catalogue, verify deployment
  permissions and subscription quota, configure native Azure capacity and existing content filters,
  and explicitly review and create a new deployment. Endpoints default to read-only. Provisioned
  and batch SKUs include capacity/billing warnings; successful creation refreshes inventory without
  a manual sync. APIM publishing, grants, and pool membership remain separate.
- MCP server registration: register a Model Context Protocol server by URL, connect to it as a
  read-only client, and record the tools it declares — including the input schemas, output schemas,
  and behaviour annotations that API Management's management plane does not expose
- MCP servers already present in a registered gateway are detected and counted
- Plain-language policy view: policy XML is parsed in memory and reduced to a digest plus redacted
  semantic facets, so administrators never see markup and MOSAIC never stores it
- AI surface detection that identifies which APIs and backends front large language models, across
  Azure OpenAI, Azure AI Foundry, Azure AI inference, OpenAI, Anthropic, Google Vertex AI, and
  AWS Bedrock, including an API whose policy routes to a backend, or to a load-balanced pool of
  them, rather than naming a service URL
- MCP server discovery, and import of selected model APIs and MCP servers from a synchronised
  gateway into MOSAIC's own desired state
- Model publishing: expose an observed deployment through a gateway by creating its backend, policy
  fragment, API, operations, API policy, product, product link, and subscription — through a
  persisted deterministic plan, an explicit apply, per-step results, and a rollback that deletes
  only the resources that apply created
- MCP publishing: expose a registered MCP server through a gateway by creating its backend, policy
  fragment, passthrough MCP API, API policy, and a protected-resource-metadata operation and its
  policy on the gateway's shared metadata API — through the same reviewed plan and explicit apply
  model
- Model pools: serve one vendor's models from deployments on many endpoints and regions through one
  API, so callers see one base URL and the pool's model names and never the deployments behind
  them. Breaker and preferential pools balance their deployments with API Management backend pools
  and circuit breakers; linear pools try them in order. A member can also be reached with an API key
  from Key Vault, on an Azure endpoint registered with one or, for Claude, on AWS Bedrock, and is
  then tried on its own. A pool is published, unpublished, and recovered through the same reviewed
  plan and explicit apply as a publication. A governed pool admits only callers granted one of its
  models under a cost center, with one key per person and cost center for all of the pool's models
  they hold, and its calls count toward cost center limits, budgets, Analytics, pricing, and the
  usage report. Each pool's page shows how its deployments answered, from a trace its policy writes
  for every attempt, and a re-plan warns when someone changed the pool's resources in API
  Management. The console suggests pools for models deployed on two or more endpoints, names the
  pools that use each deployment, warns when portal users would see a model twice, and lists pools
  with problems on the dashboard. See [Model pools](model-pools.md)
- Async repository abstraction with explicit in-memory and Cosmos implementations
- React/TypeScript/Vite administrator console using Fluent UI, React Router, TanStack Query, and
  MSAL, with responsive navigation and persisted light/dark/system themes. It confirms the caller
  holds the `Admin` role before it shows any of that; a caller with only `User`, or with no MOSAIC
  role, is told the console isn't for them and what to ask an administrator for
- Runtime browser configuration; Azure IDs and service URLs are not baked into the web image
- Typed APIM read and write boundaries kept in separate classes, plus Foundry import and
  deterministic policy authoring that never returns markup
- Separate non-root frontend/backend containers
- End-user portal: a separate SPA on its own Entra registration and the `User` app role, where
  a non-administrator sees what they are entitled to, how each grant reached them, the catalog
  of governed resources, and can request access to something they cannot yet use. A model or MCP
  server MOSAIC publishes is in the catalog only while it's published into API Management. My access
  and My requests head each grant and request with its resource's name, as the catalog shows it, and
  keep its kind visible, including for a resource since made private. They never show a raw
  resource ID. For an applied direct model grant, the portal also shows the endpoint, operations,
  accepted credentials, limits, and placeholder code samples, and reveals a key on request. A model
  served by a pool appears as a model like any other, with one base URL and the model name to
  send, and the portal never names the pool, its deployments, or their regions
- Environments: an administrator-defined catalog (Development, Test, QC, Staging, Production,
  Sandbox, and custom environments) that classifies gateways, model endpoints, and MCP servers.
  Compatibility rules are enforced when models and MCP servers are published, and Azure
  `environment` tags and legacy labels become suggestions an administrator confirms.
  Re-classification is guarded, and advisory findings flag blocked pairings MOSAIC's rules didn't
  stop. The portal shows each catalog entry's and grant's environment, so people request
  development and production access separately
- End-user usage report: a caller-scoped `/me/usage` contract and the portal's **Usage & cost**
  page, measured from the gateway's logs and priced from the price list. Each governed model and
  MCP call is tagged at the gateway with the grant it matched and the calling client, so key,
  token, security-group, and MCP grants are each attributed to the right person. Only local and
  test runs simulate the figures, from the caller's real grants and limits, and they label them
  **Sample figures**
- Usage analytics: a background job rolls API Management's gateway and LLM logs up from Log
  Analytics into Cosmos every 15 minutes, keeping daily and monthly figures after the workspace's
  own retention ends. The console's Dashboard and Analytics pages show requests, tokens, callers,
  errors, latency, quota and rate-limit use, denials, unused grants, and unattributed calls, by
  range, gateway, environment, resource, and kind of subject, with CSV export. Each gateway's
  Telemetry section checks that its usage can be measured and says how to fix what's missing. See
  [Usage analytics](usage-analytics.md) and
  [ADR 0019](adr/0019-usage-telemetry.md)
- Pricing: a sourced, dated price list turns measured usage into cost. MOSAIC ships list prices for
  Azure Commercial and Azure Government from the Azure Retail Prices API, each citing its source.
  Administrators add prices for custom clouds and other providers, or override one from a date, as
  audited versions that never rewrite the days before them. Analytics, the Dashboard, and the
  portal show cost at list price, with this month's spend and a month-end forecast, provisioned
  throughput shared by each caller's share of its tokens, and a chargeback export. Usage MOSAIC
  can't price shows **No price**, never $0. See [Pricing](pricing.md) and
  [ADR 0020](adr/0020-price-list.md)
- Cost centers: every grant, and so every call, is charged to a cost center that spans gateways,
  with responsible contacts, a keys-allowed switch, default limits, and pooled monthly quotas.
  Each custom center exclusively binds one verified Entra group for membership and chooses
  Automatic, Request-only, or Not available per exact model, pool-model offering, or MCP resource.
  Automatic uses one stable group JWT grant, not one entitlement or key per member. Individual
  token grants also require the center group; explicit individual keys remain where enabled.
  General is exempt from the mandatory group. People choose an eligible center for a resource
  request or call; defaults and `x-mosaic-cost-center` do not create membership. Portal users can
  separately request a center without directory-write privileges.
  Analytics, the chargeback, and the portal report spend and quotas per cost center. See
  [Cost centers](cost-centers.md) and [ADR 0030](adr/0030-entra-backed-cost-centers.md)
- Budgets: a monthly budget per cost center, in dollars across every gateway, emails its owners
  and any other address at 80% and 100%, and either lets calls continue or blocks them at the
  gateway until it's raised or the UTC month ends. An organization budget only warns. Email goes
  through Microsoft Graph as MOSAIC's managed identity, scoped to one Exchange Online mailbox
  and off until it's set up in Settings. The Dashboard shows each budget's progress and forecast, and the portal tells people
  when a cost center they charge is near or past its budget. See
  [Budgets and alerts](budgets-and-alerts.md) and
  [ADR 0023](adr/0023-budgets-and-notifications.md)
- Bulk request review, explicit scheduled approval policies, advisory AI through a governed
  model, and personal/admin notification preferences. Approval still requires a separate
  gateway plan apply. See [Approvals and notifications](approvals-and-notifications.md)
  for opt-ins, recipient routing, readiness, privacy, and hosting prerequisites.
- ACR remote builds for every image, so deployment does not depend on a local Docker daemon
- `azd` and modular Bicep for three Linux Web Apps on one plan, ACR, Cosmos, Key Vault, APIM,
  Log Analytics, Application Insights, diagnostics, managed identities, and narrow RBAC
- Idempotent Entra application/service-principal setup through `azd` hooks

The Gateways workspace, the Identity workspace, the Models and MCPs workspaces, the Entitlements
workspace, Cost centers and their budgets, Settings → Environments and Notifications, model publishing,
the Dashboard, Analytics, Pricing, the end-user portal, and the deterministic policy preview use
live API contracts. Usage figures in
Azure are measured from gateway logs; only local and test runs simulate the portal's, labeled
**Sample figures**. The administrator console contains no sample policy records, invented profile
activity, mock integrations, or simulated support tickets. Unavailable data remains explicit, and
reviewing a deterministic policy or enforcement plan never applies it.

Existing deployments need `azd provision` (or a manual role grant) before publishing works: the
API's identity moves from the API Management reader role to contributor. Until it is granted,
preflight reports the missing write permissions precisely rather than failing during an apply.
Measured usage also needs `azd provision`, which adds the `usage-rollups` container, the
gateway's `azuremonitor` logger, and Monitoring Reader on the gateway for the API's identity.
Publications applied before budgets existed don't check them until they're applied again, which
also creates each gateway's `mosaic-blocked-cost-centers` named value. Email needs an existing
Exchange Online mailbox and mailbox-scoped **Application Mail.Send** for the API managed identity.
Configure it in **Settings > Notifications > Email**; provisioning does not grant Exchange access.
Old email endpoints require migration and are never reused. See
[Notification setup](notification-setup.md) for email and both Teams paths, access checks,
explicit tests, and corrective administrator commands.

## Azure Government from a commercial deployment

Use **Settings > Azure connections** to configure a Government-tenant service principal whose
client secret stays in a separate commercial management-credentials vault. Select the connection
when registering resources; **AzGov** identifies the cloud independently of **Production** or
other environment labels. Commercial sign-in and shared cost centers remain unchanged.
The infrastructure update configures `MOSAIC_MANAGEMENT_KEY_VAULT_URI`; without that isolated
vault, existing Commercial resources work but new external management credentials cannot be saved.

Government runtime offerings use grant-scoped subscription keys, not commercial Entra tokens.
They remain outside the new Entra-backed center setup/access-mode workflow.
Initial keys are prepared after an eligible direct grant is successfully applied; reveal remains
explicit and audited. Government gateways use their own Government identity for backend calls.
This arrangement transfers management metadata and usage into commercial MOSAIC and requires
organizational approval; it is not a sovereignty/compliance claim. See the
[Government operator guide](azure-government.md) for identity, permissions, networking,
capability checks, and consolidated pricing/reporting.

## Current boundaries and deferred work

- Approval, import, backend deployment creation, APIM publishing, and pool membership are separate
  operations. Saving intent does not imply gateway enforcement.
- Generic MOSAIC-local groups do not authorize APIM calls. The local managed-center roster and
  APIM-local membership/inherited-key proposal are superseded/historical, not runtime fallbacks.
  Old MVP custom centers require explicit reconfiguration/recreation, not a production migration,
  silent member copy, data deletion, or charge fallback to General.
- Directory consent, group verification, membership propagation, resource apply, and individual-key
  revocation have separate states. Removal does not invalidate issued JWTs or copied keys;
  out-of-band membership/key eligibility needs explicit recheck, not a continuous directory watcher.
- The direct Members listing uses Graph beta to include service principals omitted by v1.0.
  That directory-listing dependency is preview and not production-supported; APIM's stable local
  JWT group-grant check does not depend on it. See [membership limitations](cost-centers.md#members-and-who-may-charge-a-cost-center).
- Usage and dollar-budget feedback are asynchronous. Costs are priced estimates, not invoices;
  Azure Cost Management ingestion and automatic invoice-based repricing are deferred.
- Provider detection does not imply every provider/API shape supports publishing, token metering,
  or automatic pricing. Gateway tier, backend shape, and authentication constrain the path.
- Re-planning and applying are explicit; there is no general continuous drift-repair worker.
- Reference architecture is not a scale benchmark or service SLA. See [FAQ](faq.md) for the
  operational implications and [E2E evidence](e2e/roadmap.md) for demonstrated journeys.

The Entra-backed workflow's fictional adapters, contract tests, and policy checks are not live
group-mutation or APIM interoperability evidence. Such checks require a separately authorized
non-production target; neither the historical feasibility probe nor screenshots prove production
performance.

## Roadmap

This numbered sequence includes work already implemented, not just future commitments. The
inventory above describes current capabilities; the boundaries above distinguish deferred work.

1. **Foundation:** secure deployment, domain, directory CRUD, runtime configuration, observability
   wiring, typed APIM/Foundry/reconciliation boundaries.
2. **Gateway onboarding:** multi-gateway registry, access verification with guided
   remediation, full inventory synchronisation, AI surface detection, and plain-language policy.
3. **Model and MCP onboarding:** discover MCP servers, detect model-fronting APIs
   across Azure and third-party providers and import a chosen selection into desired state,
   register Azure OpenAI and Foundry endpoints to enumerate their deployed models and verify each
   gateway's runtime access to them, and register MCP servers directly to record the tools they
   declare.
4. **Model publishing:** expose an observed deployment, or one declared on an endpoint reached with
   an API key ([ADR 0018](adr/0018-key-authenticated-backends.md)), through a gateway by writing
   its backend, policy fragment, API, operations, product and subscription, through a deterministic
   plan, an explicit apply, per-step results, and rollback that removes only what it created. This
   is the orchestration [ADR 0009](adr/0009-entitlement-subjects-resources-and-apim-binding.md)
   defers to, for models.
5. **Governed model access:** direct user/application/agent grants become APIM
   subscriptions and/or Entra authorization, and Entra security-group grants become token-only APIM
   authorization, with shared limits, explicit apply/revoke, trusted `orchestrated` bindings, and
   on-demand key retrieval for direct grants. Approving an access request creates the requester's
   grant intent but does not apply it.
   The end-user portal now provides My access (including connection details and on-demand key
   reveal for applied direct model grants), catalog, and access-request screens, gated by the
   `User` app role and the `mosaic-<env>-portal` registration; see
   [ADR 0008](adr/0008-portal-identity-and-role-separation.md).
6. **MCP publishing and enforcement:** publish registered streamable MCP servers through managed
   gateways with a passthrough MCP API, per-publication resource metadata, Commercial Entra grants
   or Government key-only direct grants, call limits and fail-closed recovery; see
   [ADR 0017](adr/0017-mcp-gateway-enforcement.md).
7. **Insights and chargeback:** usage is measured now. Gateway and LLM logs are rolled up from Log
   Analytics into Cosmos, attributed to each grant, member, and client application, and shown in
   the portal against each grant's own limits and in the console's Dashboard and Analytics; see
   [ADR 0019](adr/0019-usage-telemetry.md). A sourced, dated price list turns that usage into
   cost at list price, with a month-end forecast and a chargeback export; see
   [ADR 0020](adr/0020-price-list.md). Cost centers charge every grant and call to a team or
   budget line, with pooled quotas and spend per cost center; see
   [ADR 0022](adr/0022-cost-centers.md). Monthly budgets warn by email at 80% and 100%, and
   can block a cost center's calls at the gateway; see
   [ADR 0023](adr/0023-budgets-and-notifications.md).
8. **Model pools:** serve one vendor's models from many deployments, across endpoints and regions,
   behind one API whose callers never see the deployments.
   - Built: the first four phases. Administrators create **breaker** (load-balanced, with circuit
     breakers), **linear** (ordered failover), and **preferential** (provisioned throughput first,
     with pay-as-you-go overflow) pools of Azure deployments, and publish, unpublish, and recover
     them through reviewed plans. The Models page shows each deployment's capacity type
     (provisioned, pay-as-you-go, or batch), its processing scope, any Azure spillover, and the
     pools that use it. A governed pool admits only people, applications, and agents granted one of
     its models under a cost center, each with one key per pool and cost center, created on request.
     Pools reuse cost center limits, budget blocking, and grant attribution, each call is priced at
     the member deployment that served it, and the portal lists a pool's models rather than its
     deployments. A pool's members can be reached with an API key, on an Azure endpoint registered
     with one or, for Claude, on AWS Bedrock. Each pool shows how its deployments answered, a
     re-plan warns about drift, and the console suggests pools and lists those with problems on the
     Dashboard. See [Model pools](model-pools.md).
   - Next: a model router pool type, once API Management's unified model API is generally
     available.

   See [ADR 0024](adr/0024-model-pools.md).
9. **Catalog ecosystem:** API Center experiences, MCP tool-level governance, broader self-service
   workflows, and environment chains that relate the same model across environments and clouds.
10. **Production hardening:** private networking, multi-region/production APIM tiers, CMK where
    required, measured partition scaling, retention and operational SLOs.

See [the architecture decisions](adr) for the durable rationale behind this foundation.
