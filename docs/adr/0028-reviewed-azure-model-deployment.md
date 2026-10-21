# ADR 0028: Reviewed creation of Azure model deployments

**Status:** Accepted

## Context

Registering an Azure endpoint previously required an operator to deploy models in Foundry and sync
MOSAIC afterward. Onboarding's Reader boundary must not silently become a cloud writer. Model
creation is also different from publishing an existing deployment through APIM.

## Decision

Support new deployments on existing Azure OpenAI and Azure AI Foundry accounts. Discovery can
start from any visible subscription's suggestion, but registration always precedes deployment.
Registration persists if an administrator subsequently cancels creation. Do not create accounts,
projects, grants, pools, role assignments, Marketplace subscriptions, agreements, or reservations.

Every endpoint defaults to `managementMode: observe`, including legacy records. Enabling `manage`
explicitly checks effective account-scoped deployment permissions. A project-scoped permission
does not authorize its parent account. API-key-only, Bedrock, and OpenAI-compatible registrations
remain outside this boundary. All reads and writes use the stored Azure cloud and connection.

Keep `CognitiveServicesClient` read-only and put the single deployment PUT in
`ModelDeploymentWriter`. Keep the tested `2024-10-01` Cognitive Services API version. The
administrator-only API provides live catalogue/access reads, persisted review plans, idempotent
plan apply, operation history/status, reconciliation, and inventory-refresh retry.

The live account catalogue preserves format, name, exact version, publisher, source, and source
account. It advertises every returned SKU, rather than a hardcoded list. Validate Azure's capacity
bounds, increments and allowed values, lifecycle/deprecation, existing RAI policy selection,
version-upgrade behavior, and live name availability. Match subscription quota by the SKU's
`usageName`; do not guess quota identifiers or multiply all capacity by 1,000 TPM. Invalid,
ambiguous, missing, unreadable, blocked, or insufficient quota blocks apply. For provisioned SKUs,
also verify physical capacity for the selected model, version, SKU, and account region.
Unverifiable required reads fail closed; truncated catalogues are errors, not successful empty
results. An unreadable RAI-policy list disables custom policy selection but allows an explicitly
reviewed Azure-default policy with a warning.

Use a full console workspace with explicit opt-in, configuration, review, **Deploy to Azure**,
and a persisted result. Show quota/billing, residency, provisioned-idle-cost, batch limitations,
and Marketplace-prerequisite warnings without claiming to reserve quota or estimate a price.
Repeat permission, quota, catalogue, target, and name checks immediately before the write.

## Execution and recovery

Persist the plan and run in the existing sync-operations container, separate from inventory sync
history. A plan has one deterministic run ID. Conditional run updates use etags. Record the
attempt before PUT; never replay an uncertain PUT, including transport retries or after restart.
Audit request and terminal outcome independently of inventory refresh.
Keep a lock-owning operation visible even when it falls outside recent history. Its ID is also
returned with the live catalogue so the console can open recovery instead of another create.

Hold a durable, non-expiring account-and-cloud lock across execution. Endpoint mode changes and
removal honor that same lock. Persist the recoverable run before acquiring its lock. Short,
record-only leases coordinate lock acquisition and endpoint mutations; conditional Cosmos endpoint
writes also fence stalled record writers. Catalogue reads, planning, reconciliation, and
inventory-refresh retry do not acquire anonymous durable locks that a restart could strand.
Expiring record leases are not held across ARM requests. A 201 Accepted response without polling
headers is not success: read the deployment until its terminal state and reviewed configuration
are verified.

Definite HTTP rejections are failures and expose Azure's reason. Uncertain outcomes retain the
lock and require read-only reconciliation. A denied or throttled read after an accepted PUT is not
a rejected create. Never delete a resource as rollback. An absent resource alone cannot prove an
old worker or Azure operation is finished. Finalizing an unconfirmed operation requires explicit
confirmation that its original worker stopped, even when a terminal resource exists: an old worker
could still be paused before PUT. If the resource is absent, also require confirmation that no
Azure operation is pending. Read-only outcome checks do not overwrite a potentially active worker's
run version. Refuse recovery while the local worker is active. Terminal provisioning remains
terminal even if the following inventory sync fails or is interrupted: recover the refresh
separately without issuing another create.

On success, synchronize inventory automatically and invalidate endpoint, deployment, publication
candidate, pool, and pricing queries. Deployment does not publish to APIM or create access.
Existing synchronous-publishing and pool eligibility rules still exclude batch deployments.

## Limits and consequences

Azure's deployment PUT is an upsert, and its contract does not document `If-None-Match` creation.
MOSAIC refuses names already present at review and immediately before PUT and serializes its own
writers, but **cannot guarantee atomic create-only behavior against external Azure writers**.
Do not claim otherwise or invent conditional-header support. Operators must avoid competing
external changes on the account during this workflow.

Account-scoped deployment actions do not automatically grant subscription-level quota or capacity
reads. Operators grant those separately; MOSAIC never elevates itself. Partner terms, capacity
availability, sovereign-cloud model availability, Azure policy, and commercial charges remain
Azure's authority. The UI reports those blockers and separates remote provisioning from refresh,
publication, gateway inference readiness, and billing.
