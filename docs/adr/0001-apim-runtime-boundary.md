# ADR 0001: Keep APIM as the runtime plane

**Status:** Accepted, partially superseded by [ADR 0010](0010-publishing-models-into-apim.md)

## Context

MOSAIC governs model access, but Azure API Management already provides the runtime AI gateway,
policies, identity forwarding, traffic controls, and gateway telemetry.

## Decision

MOSAIC is a control plane. Cosmos stores desired governance state. APIM remains authoritative for
observed runtime configuration and handles every model request. Reconciliation is modeled as
desired state -> observed state -> deterministic plan -> explicit apply -> audited result.

The foundation originally granted the backend APIM read access only. That constraint held until
apply, failure recovery, and rollback existed; ADR 0010 records that they now do, and replaces the
read-only boundary with a `management_mode` gate, deterministic plans, audited runs, and tracked
rollback.

## Consequences

- MOSAIC cannot become an accidental availability dependency in the model traffic path.
- Runtime behavior remains visible and operable with native Azure tooling.
- Later reconciliation must handle drift and partial failures rather than pretending Cosmos writes
  immediately changed APIM.
