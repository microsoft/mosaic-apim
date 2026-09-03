# ADR 0001: Keep APIM as the runtime plane

**Status:** Accepted

## Context

MOSAIC governs model access, but Azure API Management already provides the runtime AI gateway,
policies, identity forwarding, traffic controls, and gateway telemetry.

## Decision

MOSAIC is a control plane. Cosmos stores desired governance state. APIM remains authoritative for
observed runtime configuration and handles every model request. Reconciliation is modeled as
desired state -> observed state -> deterministic plan -> explicit apply -> audited result.

The foundation grants the backend APIM read access only. Policy preview is deterministic and tested,
but no policy write is performed until apply, failure recovery, and rollback are implemented.

## Consequences

- MOSAIC cannot become an accidental availability dependency in the model traffic path.
- Runtime behavior remains visible and operable with native Azure tooling.
- Later reconciliation must handle drift and partial failures rather than pretending Cosmos writes
  immediately changed APIM.
