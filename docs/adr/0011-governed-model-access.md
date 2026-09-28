# ADR 0011: Governed model access and explicit credential disclosure

**Status:** Accepted

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

**People get runtime tokens through a MOSAIC model client.** `Models.Invoke` requires admin
consent, so a person holding a user grant had no client that could request it without a
consent prompt, and nothing named a client ID to use. Bootstrap therefore creates
`mosaic-<env>-model-client`. It is a single-tenant public client with public client flows
enabled and an `http://localhost` loopback redirect, so both interactive and device code
sign-in work. Its only required permission is `Models.Invoke`, and it has no secrets,
certificates or app roles. Bootstrap grants it tenant-wide delegated consent for that scope
with an `AllPrincipals` `oauth2PermissionGrant`. Connection details show its client ID as
`entraClientId`.

We chose that grant over pre-authorizing the client on the runtime registration, for three
reasons:

- An `AllPrincipals` grant is tenant-wide admin consent by definition. The console and portal
  are pre-authorized on the control-plane API (ADR 0008), but that scope is user-consentable.
  Microsoft Entra documentation doesn't establish that pre-authorization satisfies an
  admin-restricted scope such as `Models.Invoke`.
- The grant appears in the client's enterprise application permissions, where administrators
  review and revoke consent, and the audit log records it. Pre-authorization creates no grant to
  review there; it is a setting on the runtime registration.
- A dedicated client gives administrators one target for Conditional Access and sign-in logs,
  instead of pre-authorizing first-party tools (ADR 0004).

Consent lets Entra issue a token; it doesn't authorize a model call. APIM still requires an
applied direct grant for the token's object ID. Bootstrap doesn't fail deployment when it can't
grant consent. It warns and prints the exact command for an administrator, and re-runs neither
duplicate nor narrow an existing grant. Because a re-run grants consent again,
`MOSAIC_ENTRA_MODEL_CLIENT=false` is how an operator opts out or withdraws it. That setting
leaves existing registrations, grants and a bring-your-own `MOSAIC_MODEL_CLIENT_ID` untouched.
`entraClientId` is set only for user grants whose applied audience is the current runtime
registration, because that is the only one the model client is consented for. Application
grants sign in as themselves and request `/.default`.

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
- People can sign in with the bootstrap-consented model client. Other delegated clients and
  applications still need ordinary resource-API consent or application permission setup in
  addition to their MOSAIC grant. Bootstrap's only consent is the model client's
  `Models.Invoke` grant, and MOSAIC does not acquire Graph permissions to do this silently.
- MSAL always requests the OpenID Connect sign-in scopes as well. Microsoft Entra treats
  `offline_access` as implied by any delegated grant. A tenant that blocks user consent might
  still need an administrator to consent `openid` and `profile` for the model client. Live
  verification must establish whether it does. The fix is documented rather than automated, so
  bootstrap still grants no Microsoft Graph permissions.
- Switching authentication methods cannot create another grant budget.
- Changing an existing publication to governed access can intentionally stop clients using
  its former generic key; that impact belongs in the reviewed plan.
- APIM remains authoritative for rotation: the next reveal sees the current key without a
  synchronization job. Already-revealed bearer keys cannot be recalled from a user's memory.
- Turning off a method or revoking a grant takes effect as APIM propagates the configuration,
  not when desired state is saved.
- The current-user APIs coexist with the portal's My access, catalog, and access-request screens.
  Portal key/connection controls, group/application-owner delegation, live analytics, and automatic
  background drift repair remain deferred.
- Approving an access request creates the requester's direct grant intent (ADR 0009) but never
  applies it. The grant joins the model's next reviewed plan like any other saved change.
- Mocked policy and API checks cannot establish live Entra/APIM interoperability. The opt-in
  live verifier and actual gateway checks must report unavailable prerequisites honestly.
