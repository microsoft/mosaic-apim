# ADR 0016: Agent identities and security-group grants

**Status:** Accepted

**Amended by [ADR 0030](0030-entra-backed-cost-centers.md):** Custom-center token grants require
their dedicated center group, including individual grants. Directory lookup remains read-only;
optional group creation/member management is a separate API-identity capability. General is
exempt from the mandatory group; Government is excluded from that new workflow.

**Update:** Security-group grants can now be linked to usage. A group grant has no APIM
subscription, so its calls couldn't be told apart in the logs. The gateway now tags each call a
group grant authorizes with the grant and the caller's validated `oid`, and the grant's binding
records that it's counted per member. A member's usage report can therefore include only their own
calls. The `oid` in gateway diagnostics is personal data. See
[ADR 0015](0015-end-user-usage-report.md).

## Context

Microsoft Entra Agent ID introduces agent identities for AI agents. An agent identity is a
specialized service principal that authenticates as an application. It has an object ID and an app
ID with the same value, has no credentials of its own, and gets tokens through the agent identity
blueprint that created it. An agent's user account is a user account paired with one agent
identity. It is used when an agent needs user-like delegated context, and it can authenticate only
through its parent agent identity.

MOSAIC also needs to grant access through Microsoft Entra security groups. Customers already use
Entra groups to govern people and workload identities, and group grants must work for users,
applications, managed identities and agents without expanding group membership into per-member
MOSAIC records.

## Decision

**Principals have explicit Entra-aware kinds.** MOSAIC records `user`, `servicePrincipal`,
`managedIdentity`, `agentIdentity`, `agentUser` and `securityGroup` principals. `user` and
`agentUser` map to entitlement subject kind `user`; `servicePrincipal`, `managedIdentity` and
`agentIdentity` map to `application`; `securityGroup` maps to `securityGroup`. MOSAIC-local groups
remain the existing desired-state-only `group` subject kind.

**Directory lookup reads Microsoft Graph through the API identity, read-only.** The API managed identity
uses application permissions `User.ReadBasic.All`, `GroupMember.Read.All` and
`AgentIdentity.Read.All`. MOSAIC uses Graph to search directory objects, verify a principal when it
is added, list security-group members, resolve administrator-entered IDs, explain group access in
the console and detect overlapping grants. This lookup adapter does not write to Entra. A separate
optionally consented adapter manages verified cost-center groups and their direct membership under
ADR 0030. The API Management gateway needs no Graph permission and never calls Graph for runtime
authorization. Directory lookup is always available with Entra authentication, without a
deployment switch, and still requires tenant-administrator consent for its Graph permissions.
Local authentication has no live directory lookup; administrators type object IDs and MOSAIC
records unverified principals.

**Application lookup is an optional read-only extension.** The console can search application
service principals and Azure managed identities using the Microsoft Graph v1.0
`/servicePrincipals` API. Name queries match a display-name prefix; GUID queries first check an
object ID, then an application ID if the object is absent. Reads select only `id`, `displayName`,
`appId`, and `servicePrincipalType`, return at most 25 results, and never follow search paging.
Only `Application` and `ManagedIdentity` service principals are offered; agent identities and
blueprint principals are excluded from this picker.

The least-privileged **application** permission for this optional lookup is `Application.Read.All`.
An operator must explicitly authorize it for the API's managed identity if they want this feature.
MOSAIC does not request consent, change tenant permissions, or add this permission during deployment.
A denied read is shown with retry/configuration guidance and manual fallback. Existing manual
application registration remains supported and does not claim directory verification. Registering
a search result still saves a MOSAIC reference, not an Entra object or an application-role assignment.
Other directory searches retain their existing permissions; no write or `Directory.Read.All`
permission is required for application lookup.

**Security-group grants are enforced from the validated token's `groups` claim.** Bootstrap sets
`groupMembershipClaims: SecurityGroup` on the model-runtime app registration so runtime access
tokens carry the caller's security-group object IDs. It sets the same on the MOSAIC API
registration, so the portal can show a signed-in person the grants they hold through their groups.
`MOSAIC_ENTRA_GROUP_CLAIMS=false` records that
bootstrap did not configure these claims; MOSAIC can still store group grants, but it warns because
the gateway cannot match them from tokens.

**Group grants use Entra tokens only.** A security-group grant creates no APIM subscription, issues
no key and rejects key reveal. Callers invoke with a runtime Entra token for the model-runtime
audience. Users and agent users use delegated `Models.Invoke`; applications, managed identities and
agent identities use app-only `.default` and require `Models.Invoke.Application`. A MOSAIC grant
does not assign or consent app roles. Because Microsoft Entra does not add an app role to service
principal tokens when a service principal is in a group assigned to that role, application and agent
members still need the application role granted directly or inherited through an Agent ID
blueprint's inheritable-permissions configuration.

**Limits are per member.** A security-group grant's call and token counters are keyed by the
runtime caller's `oid`, not by the group. Every member receives the group's full allowance in the
grant's window. Direct-grant counters keep their existing entitlement key.

**Precedence is deterministic.** An enabled direct grant for the caller wins over any group grant.
If there is no enabled direct grant, the most generous enabled security-group grant wins: unlimited
beats limited, then higher token allowance, then higher call allowance, with ties going to the
lower entitlement ID. Disabled grants count as absent. MOSAIC-local groups remain informational for
runtime access and do not participate in APIM authorization.

**Overage fails closed at the gateway.** Microsoft Entra omits `groups` from JWT access tokens when
the subject is in more than 200 groups and emits overage markers such as `_claim_names` /
`_claim_sources`, or `hasgroups` for implicit-flow tokens. APIM does not call Graph, so a group grant
cannot match such a token. A direct token grant can avoid an optional resource-group check only
under General. It cannot bypass the mandatory group claim of a custom center. Resolve claim
eligibility or review a separately allowed individual-key path; never silently redirect to General.

**Overlaps are visible before apply.** The console reports overlapping grants, the winner and the
reason. Plan review also warns when direct and group grants, or multiple group grants, reach the
same principal/resource pair.

**Agent token quirks are handled explicitly.** App-only tokens normally authorize through `roles`,
not `scp`; Microsoft documents `scp` as a user-token claim. Some agent app-only tokens can carry an
empty or placeholder `scp` value, so MOSAIC treats missing, empty and `/` `scp` as not delegated and
relies on `roles` for app-only model invocation.

**MCP grants can be enforced on MOSAIC-published servers.** Grants on MCP servers may name users,
agent identities, agent users and security groups. [ADR 0017](0017-mcp-gateway-enforcement.md)
defines the gateway enforcement path for MCP servers MOSAIC publishes. Imported MCP servers remain
recorded governance intent because MOSAIC does not own their gateway policy.

## Consequences

- A token-claim check is not live directory state. Removing a user, service principal or agent from
  a group does not invalidate already-issued JWTs. Human tokens normally last 60-90 minutes from
  issuance; actual remaining `exp`, policy, and directory propagation matter. Managed-identity
  token/authorization caching can last around 24 hours per resource. This is not immediate
  revocation, a universal 90-minute bound, or a CAE promise.
- A caller with group overage cannot prove a custom center's mandatory group claim. An individual
  token grant is not a bypass, and APIM has no Graph fallback.
- Granting the Graph application permissions requires a privileged administrator.
- Security-group grants have no key path and no APIM subscription, so clients that require a
  subscription key need a direct grant.
- Agent identity blueprint inheritance is conditional: the resource app must be configured as
  inheritable and the permission must be granted on the blueprint principal. Otherwise the agent
  identity needs its own direct app-role assignment.

## Alternatives considered

- **Use API Management developer-portal groups.** Rejected. They are tied to APIM users and
  subscriptions, not to Entra access tokens, and they do not represent agent identities.
- **Expand group members into direct grants.** Rejected. It drifts from Entra, churns APIM when
  membership changes, and makes nested memberships hard to explain.
- **Call Microsoft Graph from the gateway.** Rejected. It adds latency and couples every model call
  to Graph availability and permissioning.
- **Use app roles assigned to groups for every subject.** Rejected for applications and agents.
  Microsoft Entra emits group-assigned app roles for users, but not for service principals added to
  those groups.

## Amendment 2026-10-01: Precedence is per cost center

[ADR 0022](0022-cost-centers.md) charges every grant to a cost center and makes it part of the
grant's identity. The precedence above now chooses one grant per resource and cost center: a direct
grant still wins over a group grant, and the most generous group grant over the rest, but only
among grants under the same cost center. Grants under different cost centers don't compete; a
caller picks one with the `x-mosaic-cost-center` header. Without it, the gateway tries the caller's
direct grant under their default cost center, then their other direct grants, oldest first, then
their security-group grants in the order above. The original default/listed-roster charge eligibility
is superseded by the Entra-backed membership requirement below; selection never creates eligibility.

## Amendment 2026-11-03: Entra-backed center membership and stable grants

[ADR 0030](0030-entra-backed-cost-centers.md) makes one exclusively bound Entra security group
authoritative for every custom center's membership. The center defines charging and exact-resource
policy; an entitlement still authorizes only one resource under that center. Automatic uses one
managed group grant per exact model, pool-model offering, or MCP resource. Adding a member
creates no per-member automatic entitlement and makes no APIM management write.

Every custom-center **token** path requires the center group claim, including individual grants
and reviewed quota exceptions. Direct-grant precedence does not bypass this requirement.
General's stable built-in identity is exempt. Keys remain explicit individual credentials where
resource and center allow them, with current eligibility required for creation/reveal. Membership
removal does not revoke a copied key; recheck and revoke/apply are separate work. Out-of-band
changes require an explicit eligibility recheck; there is no directory watcher.

The read-only permission baseline is unchanged. Optional `Group.Create` creates a group with the
API service principal explicitly named as owner; optional `GroupMember.ReadWrite.All` enables user
membership edits. Adding service principals additionally needs separately consented
`Application.ReadWrite.All`. A MOSAIC Admin role is not that consent, and ordinary resource
approval neither assigns invocation roles nor silently changes membership. All these are API
control-plane permissions, not APIM permissions.

The center editor's direct member list uses Graph beta as the documented workaround for v1.0
omitting service principals. That listing API is preview and not production-supported, distinct
from APIM's stable JWT group-grant path. Fictional adapter/contract tests establish no live
directory or gateway interoperability. See ADR 0030's evidence boundary.

## References

- [List service principals and least-privileged permissions](https://learn.microsoft.com/graph/api/serviceprincipal-list?view=graph-rest-1.0)
- [servicePrincipal resource type](https://learn.microsoft.com/graph/api/resources/serviceprincipal?view=graph-rest-1.0)
- [Overview of agent identities in Microsoft Entra](https://learn.microsoft.com/entra/agent-id/agent-identities)
- [Agent autonomous app OAuth flow](https://learn.microsoft.com/entra/agent-id/agent-autonomous-app-oauth-flow)
- [Agent's user account impersonation protocol](https://learn.microsoft.com/entra/agent-id/agent-user-oauth-flow)
- [Tokens in Microsoft agent identity platform](https://learn.microsoft.com/entra/agent-id/agent-tokens)
- [Inheritable permissions for Microsoft Entra Agent ID](https://learn.microsoft.com/entra/agent-id/concept-inheritable-permissions)
- [agentIdentity resource type](https://learn.microsoft.com/graph/api/resources/agentidentity)
- [Add app roles and get them from a token](https://learn.microsoft.com/entra/identity-platform/howto-add-app-roles-in-apps)
- [Access token claims reference](https://learn.microsoft.com/entra/identity-platform/access-token-claims-reference)
- [Configure group claims and app roles in tokens](https://learn.microsoft.com/security/zero-trust/develop/configure-tokens-group-claims-app-roles)
- [Group creation and owner permissions](https://learn.microsoft.com/en-us/graph/api/group-post-groups?view=graph-rest-1.0)
- [Member-write permissions](https://learn.microsoft.com/en-us/graph/api/group-post-members?view=graph-rest-1.0)
- [Access-token lifetimes](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#token-lifetime)
- [Managed-identity token caching](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identities-faq#are-managed-identities-tokens-cached)
