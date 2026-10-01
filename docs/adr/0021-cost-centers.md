# ADR 0021: Cost centers

**Status:** Accepted

**Amends:** [ADR 0011](0011-governed-model-access.md), where applies created every grant's key,
and [ADR 0016](0016-agent-identities-and-security-group-grants.md), where precedence chose one grant
per resource.

## Context

[ADR 0019](0019-usage-telemetry.md) measures usage per grant, and [ADR 0020](0020-price-list.md)
puts a cost on it. The chargeback charges a grant's calls to its subject: a person, an
application, or a security group. Organizations don't pay for AI by person. They pay by team,
project, or budget line, and one person often works for several. Administrators need to know what
each of those spends, cap what each may spend, and give people a way to say which one a call is
for.

Several things shape how MOSAIC can do that:
- The gateway decides everything from a compiled policy. It can't call MOSAIC, Graph, or Cosmos
  during a call.
- A subscription key identifies exactly one grant. An Entra token identifies a caller, who may hold
  several grants on the same model, directly and through security groups.
- API Management counts limits per gateway, and a model API or MCP server is on one gateway.
- The attribution trace names the grant the policy matched (ADR 0019). Rollups keep figures per
  grant, per API, and per caller, never per call.
- MOSAIC is a pre-release proof of concept. Dev and test tenants are re-seeded, so no data
  migration is needed.

## Decision

**A cost center is administrator-authored desired state that spans gateways.**
- It has a name, a code unique within the tenant whatever its case (`[A-Za-z0-9._-]{1,64}`), a
  description, owners (email addresses), members, a **keys allowed** switch, and limits per model
  API or MCP server.
- Members are people, applications, agents, and Entra security groups, by MOSAIC principal. A cost
  center lists at most 500; a security group stands for any number of people.
- Limits on a resource are **per-person defaults** (tokens per minute, a token quota, calls per
  minute, a call quota) and an optional **pooled monthly quota** (tokens, calls, or both). MCP
  servers carry no tokens, so theirs count calls.
- It's a `costCenter` item in `desired-state`, written with its audit event in one batch and only
  over the version that was read. A `costCenterSettings` item names the tenant's default cost
  center. Creating one, or changing its code, holds a tenant-wide lease, so two administrators
  can't take the same code at once.
- Every tenant has **General**, code `general`, built in. It can be renamed and recoded, never
  deleted, and it's the default until an administrator chooses another. A cost center can't be
  deleted while it's the tenant's default, is anyone's default, or has grants.

**A principal has a default cost center, set at onboarding.** It's the tenant's default unless the
administrator picks another. A security group has none.

**Who may charge a cost center:**
- everyone may charge the tenant's default;
- a principal may charge its own default;
- a listed member may charge it, and so may anyone in a listed security group;
- a security group may charge only the tenant's default or a cost center that lists it.

Where the evidence of group membership comes from depends on who asks:
- A person's request is checked against the groups in their own validated token, and the request
  records which of the cost center's listed groups it relied on.
- An administrator's grant is checked through Microsoft Graph when directory lookup is on, and
  through direct membership and defaults only when it's off.
- An approval checks again. Without Graph, it trusts only the groups the request recorded, and
  only while the cost center still lists them.
- MOSAIC groups never reach the gateway, so their grants aren't checked.

**Every grant carries a cost center, and it's part of the grant's identity.**
- An entitlement's ID is deterministic on tenant, subject, resource, and cost center. The same
  model under two cost centers is two grants, each with its own limits, counters, key, and usage.
- A grant made without one is charged to its subject's default. A group's grant made without one
  is charged to the tenant's default.
- "One effective grant per resource" becomes one per resource and cost center. Precedence and the
  overlap report work as before within a cost center (ADR 0016). Grants under different cost
  centers never shadow each other: the caller chooses between them.
- A person's request names a cost center from a dropdown of those they may charge, their default
  preselected. One open request per resource and cost center. An approval grants under that cost
  center, or another the administrator chooses that the requester may charge.

**Losing the right to charge a cost center revokes the grants that relied on it.**
- A revoked grant is disabled and records why, as a `revocation`. The next apply deletes its key
  rather than suspending it. An administrator can turn it back on only once its subject may charge
  the cost center again.
- Four changes can take that right away:
  - removing a member, which rechecks the member's grants, or every grant under the cost center
    when a security group leaves, since people may have charged it only through the group;
  - moving a principal's default, which rechecks its grants under the former default;
  - moving the tenant's default, which rechecks every grant under the former one;
  - turning a grant back on, which checks its subject may charge its cost center, whether it was
    revoked or turned off by hand.
- A recheck revokes each grant it covers whose subject can no longer charge the cost center, and
  keeps the rest. Without Graph, a grant keeps only the groups its approved request recorded, and
  a grant nothing proves eligible is revoked: this fails closed.
- Leaving the tenant's default revokes nothing, because everyone may charge it.

**A recheck is recorded before the change, and finished even when it's interrupted.**
- The cost center records a `pendingRecheck` before the change that may need it. A member's
  removal saves both in one write. Then the change is saved, and the recheck runs.
- Revoking a grant needs its publication's lock, which an apply holds while it runs. A grant whose
  lock is busy stays pending, so the recheck narrows to the grants it has left instead of stopping.
- Every later removal, default move, or tenant-default move runs the pending rechecks again, and so
  does **Check grants again** on the cost center (`POST /cost-centers/{id}/recheck`). Repeating the
  change that was interrupted finishes it.
- Until a recheck finishes, applies leave out every grant it covers, with a plan warning that
  says why. So no apply can give back access a change took away, however the change was cut short.
- A grant written while a change runs is checked again once it's saved, before its publication's
  lock is released. It was either checked against the change, or already saved when the recheck
  listed the grants, so no grant outlives the right it relied on.
- A principal a cost center lists can't be deleted until it's removed from the cost center.
  Onboarding a principal, changing a default, changing the tenant's default, and deleting a cost
  center hold the same tenant-wide lease. So a cost center can't be deleted while it becomes
  someone's default.

**The gateway selects a grant with the `x-mosaic-cost-center` header.**
- The policy reads the header once. It must be a single value that matches the code pattern after
  trimming, and it's compared without case.
- Without the header, the caller's direct grant under their default cost center applies, then
  their other direct grants, oldest first, then their security-group grants by precedence. The
  order is compiled from each grant's `defaultCostCenter` and `grantedAt` in the applied snapshot.
- With it, the policy considers only the caller's grants under that cost center, in the same order.
- A malformed header, or one naming a cost center the caller holds no grant under, is refused with
  403 and the denial reason `cost-center`. A key belongs to one grant, so it charges that grant's
  cost center. A key sent with a header naming another is refused with 403 and
  `cost-center-mismatch`. A token sent with a key must resolve, under the key's cost center, to the
  key's own grant, as before.
- The header is removed before the call reaches the backend.
- Right after it sets `mosaic-grant`, and before the operation guard, the attribution trace and
  any limit, the policy sets `mosaic-cost-center` to the matched grant's code, lowercased, and
  `mosaic-cost-center-id` to its ID. Later policies can rely on them.
- MCP policies select and refuse the same way.
- A cost center's code, its limits on the resource, and whether a grant is under its subject's
  default are all part of the grant's intent, and so is the keys switch for a direct model grant,
  the only kind with a key. Changing any of them marks the grant pending until its model is
  applied. Until then, the gateway keeps the policy it has.

**Personal limits stay on the grant, and a pool is a second limit.**
- A grant's own limits apply as before, on the grant's own counter. A grant that sets none takes
  its cost center's per-person defaults for the resource, compiled as if they were its own. A
  security group's grant applies them to each member, as it does its own.
- A pooled token quota is a second `llm-token-limit`, with a `Monthly` token quota keyed on the
  cost center and the publication. Every grant under the cost center on that publication draws on
  it, whoever calls. A pooled call quota is a `quota-by-key` on the calendar month, keyed the same
  way. MCP servers, and models the gateway's tier can't token-meter, are pooled by calls only; a
  token pool on them is refused when the plan is made.
- API Management counts per gateway, so a pool is per model, per gateway.
- Responses carry what's left: `x-mosaic-remaining-tokens` for the grant's tokens per minute,
  `x-mosaic-remaining-quota-tokens` for its token quota, `x-mosaic-remaining-calls` for its call
  rate, and `x-mosaic-cost-center-remaining-quota-tokens` for the cost center's pooled tokens. Call
  quotas don't report what's left.

**Keys are created on request, never by an apply.**
- An apply suspends a grant's existing key while it replaces the policy, and activates it again
  when the grant is enabled and both the publication and the cost center allow keys. It deletes
  the key of a revoked grant. It never creates one.
- A person creates, rotates, or deletes the key for their own applied direct grant in the portal.
  An administrator does the same for any direct grant, such as an application's or an agent's, on
  the Entitlements page. Security-group grants have no key, as before.
- The key's subscription keeps its deterministic name, so the policy already recognizes it and
  nothing is applied again. Its display name ends with the cost center's code. MOSAIC records the
  subscription as its own before it creates it, so a create that fails part way is still MOSAIC's
  to delete. Rotation regenerates one slot through Resource Manager while the other keeps working.
- Each change holds the publication's lock and is audited as
  `credential.{created|rotated|deleted}.{requested|succeeded|denied|failed}`. Creating or
  deleting a key changes what the publication owns, so a plan reviewed before it is stale.

**The grant decides the cost center, so attribution needs no new key.**
- The header only chooses which of the caller's grants applies. The attribution trace runs after
  that choice, so its `g=` is the chosen grant, and every grant has exactly one cost center.
  There's no case where the grant doesn't decide the cost center, so the trace and the rollups are
  unchanged.
- The attribution registry records each grant's cost center ID, code, and name, so reports name
  a grant's cost center even after it's renamed or deleted.
- Analytics groups the `grant` summary dimension by cost center when it reads.

**Spend and quotas are reported per cost center.**
- Every Analytics tab takes a cost center filter. Filtered, a report counts only the calls the
  cost center's grants carried, and rebuilds totals, APIs, models, and deployments from them, each
  grant counted against the API it grants. Refusals, unattributed calls, client applications,
  reserved capacity nobody called, and figures by the hour belong to no grant, so they're left
  out, and the report says so.
- Overview ranks cost centers, Consumers lists each with its grants, callers, and cost, Cost
  shows cost by cost center, and grants, limits, and hygiene rows name theirs.
- The chargeback CSV gains **Cost center** and **Cost center name** columns after **Object ID**.
  **Unattributed calls** and **Reserved capacity with no calls** belong to no grant, so their
  cost center is empty. A cost center's spend is what its grants' calls cost, which is the same
  figure a budget compares.
- `AnalyticsService.cost_center_spend` returns one cost center's spend this month and its
  forecast, for background checks such as budgets.
- The portal shows a person their own use, their limits, and their rate-limit use over time, as
  before. For each cost center they hold an enabled grant under, it adds the cost center's total
  this month, everyone's calls together, and the total on each resource they hold there that has a
  pooled quota, against that quota. A resource without one gets no total of its own: there's no
  quota to compare, and a model one other person uses would show their use. It never shows who
  else called, how many did, or what any one person used.

## Consequences

- Breaking for re-seeded tenants: grant IDs include the cost center, so existing grants get new
  IDs, and applies no longer create keys, so a person creates their key before they can use it.
  Grants recorded without a cost center belong to General.
- The policy grows by one code per grant, plus one line per cost center.
- A person who holds a model under two cost centers must send the header to charge the one that
  isn't first in the order. Connection details show the header and its value, with copy-ready
  samples.
- Group membership is still read from the token at call time, so removing someone from a group
  takes effect when their token expires (ADR 0016).
- A pooled quota is per gateway. The same model on two gateways has two pools.
- A cost center's totals in the portal are totals, including the person's own calls. When only
  one other grant charges the cost center, or a pooled resource under it, subtracting their own use
  shows the other's.

## Alternatives considered

- **Add a `c=` key to the trace and a cost-center rollup dimension.** Rejected: the grant always
  decides the cost center, so the key would repeat what `g=` already says, and the rollups would
  need a change for nothing.
- **Let a grant's cost center change.** Rejected. It would split one grant's history, counters,
  and key across cost centers. A new cost center is a new grant.
- **Choose by client application rather than a header.** Rejected. Keys carry no client, and one
  client often serves several projects.
- **Keep creating keys at apply time.** Rejected. Most grants never use a key, and a key nobody
  asked for is a credential nobody watches. The keys allowed switch would also have nothing to
  stop.
- **One pool across every gateway.** Not possible: API Management counts per gateway.
- **Charge unattributed calls and idle capacity to General.** Rejected. General would carry costs
  none of its grants caused, and its budget would be wrong.

## Live verification still required

The policies are tested as compiled XML, not on a gateway. On a real deployment, confirm:
- that a call through two `llm-token-limit` policies counts its tokens against both, so the pooled
  monthly quota and the grant's own limit each apply, and each returns its remaining-tokens header;
- that `x-mosaic-cost-center` selects the named grant, without case, that a missing header falls
  back to the default cost center's grant, and that an unknown code, or a key with another cost
  center's code, is refused with 403 and its `mosaic-deny` reason;
- that the backend never receives the header;
- that the pooled `quota-by-key` resets at the start of the calendar month;
- that `regeneratePrimaryKey` and `regenerateSecondaryKey` on the `2024-05-01` API change one slot
  and leave the other working.
