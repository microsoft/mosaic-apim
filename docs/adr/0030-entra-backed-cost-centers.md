# ADR 0030: Entra-backed cost centers and exact-resource access

**Status:** Accepted, 2026-11-03

**Supersedes:** [ADR 0029](0029-managed-cost-center-groups.md).

**Amends:** [ADR 0022](0022-cost-centers.md) membership/default eligibility,
[ADR 0016](0016-agent-identities-and-security-group-grants.md) directory-write and group requirements,
and the custom-center authorization paths in [ADR 0011](0011-governed-model-access.md) and
[ADR 0017](0017-mcp-gateway-enforcement.md). The read-only lookup baseline in
[ADR 0003](0003-entra-applications-and-local-auth.md) remains; group management is a separate,
explicitly consented control-plane capability.

## Context

The local managed-group experiment established ownership of MOSAIC records but left administrators
with a roster that was neither an Entra group nor an implemented gateway permission boundary.
The APIM-local membership/native inherited-key feasibility proposal did not establish live gateway
behavior and is not the chosen implementation.

Administrators need one understandable membership source and a deliberate choice of what members
receive automatically versus may request. End users need usable connection details and a way to
request a center without directory administration privileges. MOSAIC must retain individual keys
where enabled without implying that removing a group member revokes those independent credentials.

This is an MVP redesign. It does not include production migration or a Government extension.

## Decision

### Separate charging, membership, and resource authorization

- A cost center defines charging and exact-resource availability, default limits, pooled quotas,
  and key restrictions.
- Every custom center has a verified binding to one dedicated Entra security group in the
  configured directory. The directory/group identity can bind to only one center, enforced
  through conditional reservation and idempotent linking, including across concurrent retries.
- The group is ordinary, cloud-managed, assigned-membership, security-enabled, mail-disabled, and
  non-role-assignable. Unsupported dynamic, synchronized, mail-enabled, and role-assignable groups
  are rejected rather than presented as normally editable.
- Entra is authoritative for membership. There is no competing editable local center roster.
  Generic MOSAIC groups remain unrelated desired-state records.
- An entitlement authorizes **one exact resource under one center**. Pool-model identity includes
  the model and pool scope. Portal model grouping is not a permission boundary.
- Built-in General is exempt from the required group, by stable built-in identity. Tenant and
  personal defaults select among eligible grants; they do not create custom-center membership.
- Government offerings are unavailable in this new workflow. Existing Government behavior is not
  redesigned here.

### Save setup before making an external change

Administrators may attach an eligible existing group or create one through MOSAIC. Attachment
requires acknowledgment that membership edits change the real group and can affect other apps.
Group creation explicitly assigns the API service principal as owner. Contact email addresses
neither assign human Entra owners nor add initial members; each is an explicit choice.

Save the center before creation. Missing consent or failed creation leaves a recoverable,
setup-required center, not a lost record or a usable binding. A verified attachment can operate
with a read-only membership view if write consent is absent.

Graph and Cosmos are not one transaction. Retain a small operation record for provisioning and
membership changes, use conditional writes for intent/results/audit, and keep external I/O outside
short Cosmos-only leases. A timed-out create has an unknown outcome: reconcile its actual object
identity or require administrator-assisted recovery, rather than issuing another create.
A display-name match or a Graph correlation ID is not idempotency evidence.

Deleting/disabling a center never automatically deletes its Entra group. Releasing or rebinding
an applied association requires explicit access revocation/reconfiguration.

### Consent only the capabilities the operator chooses

Read-only directory lookup remains separate from a narrow group-management adapter. Both use the
API identity's tenant-bound app-only credential and server-configured Graph host. Only administrators
may mutate the verified bound group; request-supplied mutation URLs and arbitrary targets are refused.

| Capability | Permission/setup |
| --- | --- |
| Baseline directory lookup and verification | Existing `User.ReadBasic.All`, `GroupMember.Read.All`, and `AgentIdentity.Read.All`; optional lookup/photo extensions remain separate |
| Group creation | Explicit optional `Group.Create`, with the API service principal set as owner |
| User membership edits | Explicit optional `GroupMember.ReadWrite.All` |
| Add service principals, including managed identities and agent identities | Additional separately consented `Application.ReadWrite.All`, per the Graph add-member contract |

Human-owner assignment needs the appropriate additional lookup permission and is not inferred from
cost-center contacts. Optional application permissions may be tenant-wide even though MOSAIC
constrains its targets. Normal deployment and resource approval do not silently consent writes,
create app registrations for callers, or assign runtime/Azure roles.

Membership reads are paginated and must include supported workload identities. A bounded transitive
preview is not a complete roster or successful eligibility proof. Add/remove actions change direct
relationships; inherited relationships are labeled, not deceptively offered as direct removals.
Removal always uses `DELETE /groups/{group-id}/members/{object-id}/$ref`, never directory-object
deletion. Denials, throttling, replication delay, and unknown outcomes stay truthful.

The direct member-list implementation uses Graph **beta**, following Microsoft's documented
workaround for v1.0 omitting service principals. That listing dependency is preview, subject to
change, and **not supported for production use**. It is not a beta dependency of APIM's stable
JWT group-claim authorization, and no live interoperability is claimed for either the new directory
adapter or its gateway integration.

### Define availability on each exact offering

| Mode | Contract |
| --- | --- |
| Automatic | One managed grant from the verified center group to the exact resource, using caller JWTs |
| Request-only | Eligible members may request approved individual access |
| Not available | No new grants or requests under this center, enforced server-side |

Automatic requires caller-side Entra support on the publication. Backend key authentication does
not prevent caller JWTs. MOSAIC must not silently enable tokens or generate per-member keys when
tokens are unavailable. Resource token/key enablement and the center's key restrictions all apply.
Explicit individual quota exceptions remain reviewed and membership-gated on available resources.

Configure and apply a stable Automatic group grant once. Adding a member performs Graph work with
zero per-member automatic entitlements and zero APIM management writes. The binding/resource
configuration participates in policy intent; membership-list changes do not change the Automatic
policy identity or hash.

APIM authorizes locally with validated tokens and applied configuration. It requires no Graph
permissions and makes no inference-time Graph, MOSAIC, Cosmos, or Log Analytics authorization call.
For a custom center, both Automatic **and individual token grants** require the center group claim,
in addition to the invocation scope/app role and exact-resource grant. Direct-grant precedence
cannot bypass membership. Group overage fails closed without a gateway directory fallback.

Approval saves grant intent; a reviewed gateway apply makes it effective. Resource-mode, grant,
quota, and key changes can require apply; a directory membership edit is not itself a gateway plan.
Keep setup, directory-operation, propagation, per-resource apply, and key-revocation states separate.

### Keep credentials and their revocation semantics separate

Automatic access creates no shared key and no personal grant record per member. Individual keys
remain available only through explicit eligible individual grants where both resource and center
permit them. New key creation/reveal checks current center eligibility.

Removing membership does not revoke an already-issued key. MOSAIC identifies affected direct/key
access and uses explicit eligibility recheck, revocation-pending, and reviewed revoke/apply paths.
Out-of-band directory changes require an explicit eligibility recheck with complete, tenant-bound
evidence; no continuous directory watcher or automatic instant key revocation is promised.

Already-issued JWTs retain their claims until expiry. Human tokens normally last 60-90 minutes
from issuance; the actual remaining `exp` and configured policy matter after removal. Directory
propagation affects new claims. Managed identities can cache per-resource tokens/authorization for
around 24 hours, with membership changes taking several hours. There is no universal 90-minute
revocation bound, immediate JWT invalidation, or CAE promise for this APIM path.

`x-mosaic-cost-center` optionally selects among authorized grants, never membership. Keys stay
bound to their center; conflicting headers and incompatible key/token grant selections are rejected.
The header is stripped before forwarding. Model and MCP grants and invocation permissions remain
independent, including when an MCP server calls a model as its own application.

### Give center requests their own workflow

Portal users submit a name, purpose/justification, and optional existing group object ID. This
does not require directory-wide search/write permission. Administrators may use or ignore the
suggestion, review in the normal setup workspace, and explicitly choose initial members.

A distinct request kind flows through status, queue counts, notifications, and deep links; it is
not a fabricated model/MCP resource. Approval links to exactly one created center, even if setup
remains incomplete. Retries converge on that center rather than creating duplicate centers/groups.
An approved center request does not itself create resource access.

### Reconfigure old MVP data explicitly

Old local group IDs are never Entra object IDs. Centers without verified bindings remain
setup-required or must be explicitly reconfigured/recreated. There is no upgrade wizard,
automatic member copy, conversion of individual grants into group grants, historical backfill,
silent deletion, or silent charge fallback to General. Preserve data until the operator explicitly
chooses what to reconfigure/reset.

## Consequences and evidence boundary

- Administrators have one real membership source, with an explicit optional directory-write blast
  radius. MOSAIC Admin is not tenant consent.
- Stable group grants avoid per-member policy churn. Paginated membership and bounded operations
  avoid embedding large rosters, but neither establishes production performance or arbitrary
  individual-key scale.
- Directory, token, storage, and gateway changes have separate failure/propagation boundaries.
  Saving intent or a successful directory write cannot be shown as universal active/revoked access.
- Development, tests, and screenshots use fictional adapters and make no live directory writes.
  Mock/contract tests and offline policy hashes do not prove live interoperability.
- The Graph beta direct-listing workaround is not a production-supported roster API. Its preview
  status is a control-plane limitation, distinct from stable group-grant runtime authorization.
- Live Entra mutation and APIM checks need a separately authorized non-production target.
  Unavailable live checks must be reported, not marked passed. The historical APIM-local probe
  remains unimplemented and supplies no live evidence for this design.

## Alternatives considered

- **Continue the local roster/managed-group foundation.** Rejected: it creates a competing
  membership authority without the desired end-to-end permission boundary.
- **APIM-local membership and inherited personal keys.** Not adopted. The historical feasibility
  proposal did not prove its native gateway contract; it is not a hidden fallback.
- **Expand every member into Automatic personal grants.** Rejected: joins/leaves churn policy
  and control-plane state instead of using a stable Entra group grant.
- **Require tokens only for every custom center.** Rejected: retain explicit individual keys where
  supported, with honest eligibility and revocation/apply requirements.
- **Resolve overage or removal through Graph during inference.** Rejected: it breaks the runtime
  boundary. Neither key revocation nor stale-token handling justifies an inference-time callback.
- **Production migration and Government expansion.** Out of scope for this MVP redesign.

## References

- [Current cost-center guide](../cost-centers.md)
- [Setup, access, and removal diagrams](../workflows.md)
- [Historical APIM-local feasibility proposal](../cost-center-access-feasibility.md)
- [Microsoft Graph: create group and ownership permissions](https://learn.microsoft.com/en-us/graph/api/group-post-groups?view=graph-rest-1.0)
- [Microsoft Graph: add members and workload permissions](https://learn.microsoft.com/en-us/graph/api/group-post-members?view=graph-rest-1.0)
- [Microsoft Graph: v1.0 service-principal listing omission and workarounds](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-1.0)
- [Microsoft Graph beta listing and production-support warning](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-beta)
- [Microsoft Graph: remove the membership relationship](https://learn.microsoft.com/en-us/graph/api/group-delete-members?view=graph-rest-1.0)
- [Entra access-token lifetime](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#token-lifetime)
- [Managed-identity token caching](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identities-faq#are-managed-identities-tokens-cached)
