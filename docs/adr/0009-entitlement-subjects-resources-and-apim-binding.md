# ADR 0009: Entitlement subjects, resources, and the API Management binding

**Status:** Accepted

## Context

`Entitlement` existed in the domain but had no routes, no service, and no repository. Nothing ever
wrote one. It modelled a single shape — a group granted a model deployment, with token enforcement
attached — which is narrower than what governance actually needs:

- Access is granted to a person, to a group, and to an application, not only to a group.
- What is granted is a model API, an MCP server, a product, or a deployment. The APIM developer
  portal has no concept of MCP servers at all, which is one of the reasons MOSAIC needs its own
  end-user experience.
- Restrictions are not only about tokens. A caller is commonly limited by requests per minute and
  by a monthly call quota as well as by tokens per minute and a token quota.
- Users cannot see what they are *not* entitled to but could ask for, so there is no path from
  "I need this" to "an administrator granted it".

There is also a join problem. MOSAIC will show a user their consumption, and consumption comes
from Log Analytics, where `ApiManagementGatewayLogs` and `ApiManagementGatewayLlmLog` are keyed on
`ApimSubscriptionId` — the API Management subscription's resource name. The `UserId` column in
those tables is an APIM user, not an Entra object ID. So a Cosmos entitlement cannot be joined to a
usage row unless MOSAIC knows which APIM subscription realizes it.

## Decision

**Cosmos is the source of truth for entitlement.** The portal reads MOSAIC and never queries API
Management to decide what a caller may use. MOSAIC will eventually orchestrate the APIM assignment
that makes a grant real; until then a grant is governance intent plus a recorded binding.

**A grant is a subject over a resource.** `EntitlementSubject` is a `user`, a `group`, or an
`application`; the first and last name a `Principal` and the middle names a `Group`. The subject
kind must agree with the principal's own kind, so a service principal cannot be recorded as a user.
`EntitlementResource` is a `modelApi`, `mcpServer`, `product`, or `modelDeployment`. The first two
are desired-state records that carry their own gateway. The last two are observed, which are scoped
to the gateway or model endpoint MOSAIC read them from, so they carry `scopeId`. A resource MOSAIC
does not govern is refused at creation rather than stored and discovered to be dangling later.

Entitlement IDs are deterministic on tenant, subject, and resource. Uniqueness is therefore a
property of the identifier rather than a query, and re-granting the same pair is a conflict.

**Enforcement is optional, and when present it must restrict something.** An entitlement with no
`enforcement` is a genuine unrestricted grant, which the portal reports as such rather than
rendering a limit of zero. Token limits keep the exact shape of `TokenEnforcement` because the
policy preview and the `llm-token-limit` renderer already speak it. Request limits are a separate
`RequestEnforcement` because API Management enforces them with different policies
(`rate-limit-by-key` and `quota-by-key`), and conflating the two would lose the distinction between
a short sliding window and a long accounting window.

**Each entitlement carries an `EntitlementBinding`.** It names the gateway and, where known, the
APIM product and subscription that realize the grant, along with how MOSAIC came to believe it:
`inferred`, `manual`, or `orchestrated`. Inference correlates the subject's `Principal.objectId`
to `ObservedApimUser.entraObjectId`, then to the subscriptions that user owns, and claims one only
when exactly one candidate matches. Attributing a person's consumption to the wrong subscription is
worse than reporting that MOSAIC could not determine it, so an ambiguous match yields no binding.

**Catalog visibility is administrator-authored.** `ModelApi` and `McpServer` gain a `visibility` of
`catalog` or `private`, defaulting to `catalog`, plus an optional `summary`. Because these are
authored rather than discovered, a re-import carries them forward instead of resetting them to the
default — otherwise the next gateway sync would silently republish something an administrator had
hidden.

**Access requests close the loop.** An `AccessRequest` records who asked, for what, and why, and
an administrator approves or denies it once. A decision is final; deciding twice is a conflict
rather than a silent overwrite.

## Consequences

- The old `Entitlement` shape is replaced rather than migrated. Nothing persisted it, so there is
  no data to migrate.
- `resolve_for_principal` answers "what may this person use" as direct grants plus grants to every
  MOSAIC group they belong to, and reports which path each arrived by so the portal can say
  "granted to you" or "granted through *Platform engineering*". A direct grant is never replaced by
  a group grant, because it is the more specific statement.
- Groups remain MOSAIC-managed. Entra security groups are not read, and the `groups` token claim is
  not consulted. That is a deliberate deferral, not an oversight.
- An entitlement with no binding is still a valid grant; it simply has no consumption to show yet.
  The portal must say so rather than render zero usage, and the orchestration phase will populate
  the same field with `source: orchestrated`.
- ADR 0001 is unaffected. Nothing here writes to API Management, and the Contributor role stays
  ungranted.
