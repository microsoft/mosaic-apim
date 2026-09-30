# ADR 0016: Agent identities and security-group grants

**Status:** Accepted

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

**MOSAIC reads Microsoft Graph through its managed identity, read-only.** The API managed identity
uses application permissions `User.ReadBasic.All`, `GroupMember.Read.All` and
`AgentIdentity.Read.All`. MOSAIC uses Graph to search directory objects, verify a principal when it
is added, list security-group members, resolve administrator-entered IDs, explain group access in
the console and detect overlapping grants. It never writes to Entra, and the API Management gateway
never calls Graph. Operators can set `MOSAIC_ENTRA_DIRECTORY_LOOKUP=false` to disable lookup; then
administrators type object IDs and MOSAIC records unverified principals.

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
`_claim_sources`, or `hasgroups` for implicit-flow tokens. APIM cannot call Graph, so a group grant
cannot match such a token. The remedy is a direct grant for that user, service principal or agent.

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
  a group takes effect when their runtime token expires, usually within about an hour.
- A caller in too many groups needs a direct grant because APIM receives no usable group list.
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

## References

- [Overview of agent identities in Microsoft Entra](https://learn.microsoft.com/entra/agent-id/agent-identities)
- [Agent autonomous app OAuth flow](https://learn.microsoft.com/entra/agent-id/agent-autonomous-app-oauth-flow)
- [Agent's user account impersonation protocol](https://learn.microsoft.com/entra/agent-id/agent-user-oauth-flow)
- [Tokens in Microsoft agent identity platform](https://learn.microsoft.com/entra/agent-id/agent-tokens)
- [Inheritable permissions for Microsoft Entra Agent ID](https://learn.microsoft.com/entra/agent-id/concept-inheritable-permissions)
- [agentIdentity resource type](https://learn.microsoft.com/graph/api/resources/agentidentity)
- [Add app roles and get them from a token](https://learn.microsoft.com/entra/identity-platform/howto-add-app-roles-in-apps)
- [Access token claims reference](https://learn.microsoft.com/entra/identity-platform/access-token-claims-reference)
- [Configure group claims and app roles in tokens](https://learn.microsoft.com/security/zero-trust/develop/configure-tokens-group-claims-app-roles)
