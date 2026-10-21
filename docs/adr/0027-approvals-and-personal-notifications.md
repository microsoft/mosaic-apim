# ADR 0027: Approvals and personal notifications

**Status:** Accepted

**Email and delivery update:** [Notification setup](../notification-setup.md) replaces the ACS
email option with Microsoft Graph and mailbox-scoped Exchange access. All external channels
automatically retry transient and uncertain sends within normal finite limits, without
duplicate-review holds. Saved configuration, read-only access checks, explicit test acceptance
and observed receipt remain distinct evidence.

**Builds on:** [ADR 0001](0001-apim-runtime-boundary.md),
[ADR 0002](0002-cosmos-partitioning.md), [ADR 0008](0008-portal-identity-and-role-separation.md),
[ADR 0019](0019-usage-telemetry.md), and [ADR 0023](0023-budgets-and-notifications.md).

## Context

Administrators need to review several requests efficiently, schedule explicit low-risk approval
rules, and obtain advisory AI recommendations. Both administrators and end users need control of
request, access, operational, usage, budget, and integration notifications. Saved integration
configuration must not be confused with working delivery or applied gateway access.

## Decision

**Approval remains desired state.** Bulk review reuses the individual approval mutation, with
independent outcomes and a server limit of 100 explicit request IDs. Policies are disabled by
default and require structured scope, current eligibility, a minimum age, and a local-time window.
Production needs explicit opt-in; moved/unclassified environments and ambiguous rules remain
manual. No approval mode applies a gateway plan.

**AI advises through governed access.** A configured existing model is called through its APIM
endpoint as MOSAIC's dedicated authorized caller, subject to normal cost centers, limits, and
budgets. Strict structured recommendations do not authorize decisions, tools, policy changes, or
gateway writes. Changed facts invalidate earlier recommendations.

**Source state and notification delivery are different records.** A deterministic source event is
committed with the source mutation where a tenant-partition transaction permits it. Cross-container
operations are reconciled from durable source records with paginated checkpoints. Inboxes,
recipient fan-out, claimed deliveries, provider attempts, digests, incidents, and worker health
have independent identities and conditional writes. Reprocessing a source must not create another
logical event.

**Personal choice is scoped by persona and current authorization.** In-app starts on; email,
Teams, and recurring summaries start off. Tenant channel/security constraints and audience
eligibility precede routing and personal opt-in. The same user's administrator and portal
preferences do not overwrite one another. Claims recheck preferences and current eligibility.
Legacy deliberately configured budget recipients are preserved, not silently enrolled into all
general notifications.

**Administrator enumeration follows enterprise-app role semantics.** Direct role-assigned users
and direct user members of role-assigned groups can be resolved. Nested groups do not confer
enterprise-app role assignment. Missing Graph evidence makes all-admin routing incomplete rather
than silently substituting people who previously signed in.

**Teams has distinct shared and private delivery.** Administrator shared destinations use Teams
Workflows with Key Vault-backed secrets and deployment-controlled host restrictions. Personal
notifications require an authenticated, tenant/cloud-validated personal bot installation and
user opt-in. A missing, blocked, or uninstalled app never redirects personal data to a channel.
Neither transport executes approval actions.

**Delivery evidence is explicit.** Configuration saved, connectivity checked, provider accepted,
confirmed delivered, and read are not synonyms. Retry records preserve provider identifiers and
unknown outcomes. External exactly-once delivery is not claimed when a provider cannot guarantee
deduplication after an ambiguous timeout. Readiness checks never send a test message or change
cloud permissions implicitly.

**Summaries do not invent enforcement.** Daily, weekly, and monthly summaries cover completed
local calendar periods; durable UTC identities handle DST and catch-up. Existing dollar budgets
remain monthly UTC budgets. Personal attribution is the portal's existing attribution, not all
members of a shared grant. Delayed/unpriced/incomplete rollups do not justify exact live RPM/TPM
remaining counters, fabricated personal budgets, zero-shaped costs, or healthy recovery claims.

**Scheduling needs an awake host.** In-process workers use leases and bounded work, with durable
heartbeats, checkpoints, pause state, and error evidence. Always On/minimum replicas are operational
prerequisites. A configuration flag declaring them is not proof of hosting availability.

## Consequences

Bulk success survives other items' failures, while policy and AI behavior remain auditable and
within existing authorization. More additive Cosmos document types and optional Graph, ACS,
Teams, and governed-model prerequisites must be operated. Delivery records use bounded retention
without deleting source or audit history. Operators need actionable setup and repair information,
not a single configuration-based green status.

See [Approvals and notifications](../approvals-and-notifications.md) for setup, semantics,
operations, and rollback.
