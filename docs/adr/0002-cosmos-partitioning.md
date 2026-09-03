# ADR 0002: Partition initial Cosmos containers by tenant

**Status:** Accepted

## Context

The initial deployment is single-tenant, but MOSAIC's contracts must not prevent tenant isolation.
Most control-plane operations query or mutate a tenant's complete governance state.

## Decision

All entities include `tenantId`. Use three containers partitioned by `/tenantId`:

- `desired-state` for domain entities and the transactional audit outbox
- `sync-operations` for reconciliation plans and outcomes
- `audit-events` for append-only mutation records

## Consequences

- Tenant-local queries and transactional batches within an aggregate are straightforward.
- Directory mutations and audit outbox records share a transactional batch. Projection into
  `audit-events` is idempotent; failed projections remain durable and are retried by readiness checks.
- Audit and sync retention can evolve independently.
- A very large tenant can create a hot/logical partition. We will adopt hierarchical or sharded
  keys only after measured volume establishes the right secondary dimension.
