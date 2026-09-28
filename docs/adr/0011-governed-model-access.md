# ADR 0011: Governed model access and explicit credential disclosure

**Status:** Accepted

**Update:** The portal's My access page now uses the current-user APIs. Each model grant's
**Connection details** panel loads the connection only when opened. For an applied direct grant,
it reveals one key on explicit request, holds it only in transient component state (never a
query cache, browser storage, the URL, or logs), and hides it after 60 seconds, on unmount, and
on navigation. Code samples use placeholders, never a revealed key. Group grants still receive
no credentials.

## Context

ADR 0009 made an entitlement durable governance intent, but did not enforce it. ADR 0010
published models with a separate subscription and token policy. Those two domains were not
joined, so saving or revoking a grant did not change a caller's ability to invoke a model.

Users need one working path from an administrator's grant to a real model request. That path
must support both subscription-key clients and clients using Entra identities, without
duplicating credentials or putting MOSAIC in the inference path.

## Decision

**Governed access is an explicit opt-in on a MOSAIC-owned publication.** Existing publications
keep their behavior until a reviewed plan changes them. A published API has a canonical
`ModelApi` record linked to its publication, not a fabricated inventory snapshot. Import refreshes
preserve that link and administrator-authored catalog metadata.

**This slice orchestrates direct users and applications only.** A user's Entra object ID, or an
application/managed identity's service-principal object ID, identifies the runtime subject.
Application client IDs are not interchangeable with service-principal object IDs. Group
expansion, customer-owned imported APIs, MCP enforcement, and access-request automation remain
desired-state-only or future work.

**Either authentication method works independently.** A governed model can enable subscription
keys, Entra tokens, both, or neither. Neither means deny all, not anonymous access. A key is a
bearer credential: labeling it with a person's name does not prove that person is its caller.

A dedicated API-scoped subscription identifies each grant's key path. Native APIM acceptance
of a key alone is insufficient: the owned API policy must also recognize that subscription as
an enabled grant. Generic bootstrap, all-access, and unrelated subscription keys are not
grant authorization.

Entra model tokens use a dedicated single-tenant runtime audience, separate from the MOSAIC
control-plane API. APIM validates the token, its invocation scope/role, and the direct grant.
The delegated scope is `Models.Invoke`; the app-only role is `Models.Invoke.Application`.
Entra rejects duplicate permission values across the scope and app-role collections, so these
values must remain distinct even though both paths authorize model invocation.
MOSAIC login, portal access, and model invocation are separate authorizations. APIM's managed
identity still authenticates the backend model connection.

If a caller supplies both credentials, both must be valid, enabled, and name the same grant.
Invalid credentials must not silently fall back to the other method. Authentication and
authorization precede routing and limit enforcement; caller credentials are not forwarded
to the model provider.

**Limits identify the entitlement, not its authentication method.** Primary/secondary keys and
Entra tokens share a stable counter identity. Publication safeguards use separate counters.
No additional grant limits does not remove publication limits. Native policies retain their
documented distributed-counter behavior; they are not an exact global billing ledger.
Unsupported operation/policy combinations are refused or explicitly denied rather than
silently left unmetered. In particular, opting into token-governed access is not a promise
that image, audio, or embedding calls are covered by the chat/response token policy.

**Desired state and applied access are different facts.** A publication-scoped plan includes
all changes it will deploy, the relevant identity/permission inputs, and an applied-state
version. Saving intent never writes APIM. Apply is serialized with persisted conditional
ownership, and stale plans are rejected. A process restart must not steal another instance's
in-flight write.

Subscriptions are prepared without granting access, the authorization policy is installed
before relaxing native subscription requirements, and successful activation is recorded.
Revocation removes both paths and suspends the subscription. Temporary disablement does not
unnecessarily rotate its keys.

Typed applied snapshots support safe failure recovery without persisting policy XML. Cleanup
must not restore a revoked grant or leave a failed new grant secretly usable through Entra.
Ambiguous results remain failed/unknown until reconciled. Intent or ownership cannot be
deleted while that would forget live managed access.

**APIM owns the keys; MOSAIC is an authorized retrieval service.** MOSAIC stores identifiers
and ownership evidence, never a Key Vault or Cosmos copy of APIM subscription keys. An explicit
reveal uses ARM's `2024-05-01` subscription `listSecrets` operation and returns only the requested
primary or secondary key.

The caller must have a MOSAIC portal role and own the applied direct grant, or use an explicitly
administrator-only handoff route. Tenant membership, a known application ID, a client-supplied
binding, and membership in a MOSAIC group do not authorize disclosure. The service checks the
subscription's current active API scope and rechecks the grant after the ARM round trip.

Reveals require durable, secret-free audit records and non-cacheable responses. Normal listing,
planning, publishing, and synchronization never call `listSecrets`. Administrator UI reveals
use transient state, not query caches or browser storage. Upstream errors never substitute a
cached credential.

This supersedes ADR 0010's product policy that MOSAIC never calls `listSecrets`; it does not
expand the existing Contributor role or pretend that application-level authorization is an
Azure RBAC inability to read secrets.

## Consequences

- Administrators can deploy and revoke actual model access without writing policy XML.
- Entra clients need ordinary resource-API consent/application permission setup in addition
  to their MOSAIC grant. MOSAIC does not acquire Graph permissions to do this silently.
- Switching authentication methods cannot create another grant budget.
- Changing an existing publication to governed access can intentionally stop clients using
  its former generic key; that impact belongs in the reviewed plan.
- APIM remains authoritative for rotation: the next reveal sees the current key without a
  synchronization job. Already-revealed bearer keys cannot be recalled from a user's memory.
- Turning off a method or revoking a grant takes effect as APIM propagates the configuration,
  not when desired state is saved.
- The current-user APIs back the portal's My access connection details and key reveal, alongside
  its catalog and access-request screens. Group/application-owner delegation, live analytics, and
  automatic background drift repair remain deferred.
- Mocked policy and API checks cannot establish live Entra/APIM interoperability. The opt-in
  live verifier and actual gateway checks must report unavailable prerequisites honestly.
