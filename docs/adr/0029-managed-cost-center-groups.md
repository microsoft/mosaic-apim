# ADR 0029: Managed cost-center group foundation

**Status:** Experimental, disabled by default

**Extends:** [ADR 0022](0022-cost-centers.md), without changing gateway authorization.

## Context

Cost-center membership currently establishes who may charge a center, not who may use a model.
Generic MOSAIC groups have separate membership documents, reject Entra security-group references,
and do not reach the gateway. Reusing one of those groups by name would mix unrelated ownership
and membership semantics.

The proposed automatic-access path needs one stable MOSAIC-owned group per center, containing
individual principals and references to existing Entra security groups. The APIM-native owner,
scope, local-data, and propagation contract still requires
[isolated live verification](../cost-center-access-feasibility.md). Group ownership and shared
membership can be established independently without enabling that path.

## Decision

`MOSAIC_MANAGED_COST_CENTER_GROUPS=false` is the compatibility default. When enabled, new cost
centers receive one managed group, with a deterministic ID derived from the tenant and immutable
cost-center ID, never from its name. The center records `groupId`; the group records
`purpose: "costCenter"` and `costCenterId`. An existing local group is never adopted, even if its
display name matches.

The cost-center repository owns creation, metadata updates, and deletion of this group. Cosmos
commits the center, group, and audit outbox event in one three-operation tenant-partition batch.
Replacements/deletes use the ETags that were read. The in-memory implementation performs the
corresponding validated mutation without an intervening await. A collision, stale write, missing
group, or ownership mismatch changes neither record nor records a successful mutation audit.
Existing audit projection/retry behavior remains unchanged.

**There is one canonical roster, not two independently editable lists.** This compatibility
foundation deliberately retains the existing embedded cost-center `members` array as its physical
storage. The managed-group membership API delegates to the cost-center service and projects the
existing `GroupMembership` response shape; it writes no additional membership documents.
This avoids a partially migrated authorization source and keeps a 500-reference migration in
three batch operations rather than exceeding Cosmos's 100-operation batch limit.

Both API surfaces therefore share principal validation, Entra-reference support, the existing
500-reference bound, audit, ETag concurrency and existing leases, charge-eligibility rechecks,
and personal-default
reset on removal. Direct users and security-group references remain explicit references; directory
members are not expanded. Billing defaults are not group membership.

`membershipRevision` starts at one on linkage and advances for explicit roster changes. It does
not advance for idempotent adds/linkage, renames, limits, or recheck-marker cleanup. It is a durable
membership version, **not** a gateway work queue, a freshness timestamp, or synchronization success.
Group ownership/revision fields do not enter existing grant policy-intent digests.

Administrators can opt an existing center into this foundation with
`POST /api/v1/cost-centers/{id}/group`. It preserves members and their original metadata, center
IDs/timestamps, grants, keys, and defaults. Repeating it verifies the existing link and returns it
without another migration audit. Conflicting or missing owned metadata is an actionable conflict,
not permission to adopt or silently recreate a record.

Generic group services and repository writes cannot independently edit/delete a managed group or
persist its memberships. Rename and delete through the owning center. Center deletion also checks
for group-subject grants under any center before atomically removing owned metadata.

Managed groups are not legacy group-entitlement subjects: creation is explicitly refused rather
than producing incomplete membership resolution or notifications through generic membership
documents. Their future inherited model bindings need a distinct reviewed implementation.
Generic groups and their grant/membership behavior remain unchanged.

## Boundaries and consequences

- The experiment creates no model grants, APIM products/subscriptions/policies/named values,
  Entra groups, Graph writes, Redis dependency, or inference-time membership lookup.
- Existing direct, Entra-group, pool, MCP, key, budget, and delayed apply/revocation behavior is
  unchanged. A new member still needs the existing model-access workflow.
- Turning the flag off prevents new linkage but preserves the shared roster and guards of groups
  already created. It does not erase data or downgrade ownership.
- The console shows managed ownership, shares explicit member editing between the linked records,
  and excludes managed groups from legacy grant selection. Name/description editing and deletion
  remain cost-center operations. Neither app has new automatic-access controls.
- The fictional screenshot estate explicitly opts into this record foundation; its managed-group
  view is not an inherited-access or gateway-synchronization demonstration. Normal deployments
  retain the default-off setting.
- This is an intermediate storage bridge, not a claim that the eventual managed-membership
  migration, effective-member paging, Graph freshness, durable gateway reconciliation, access
  modes/model selection, native owner contract, or inherited keys are complete.
- A 5,000-person Entra group can occupy one reference, but this foundation does not prove a
  5,000-user APIM scale or latency envelope. The offline probe and live gate remain separate.

## Alternatives considered

- **Copy members into generic group membership documents.** Rejected: two editable rosters can
  diverge, generic groups have different semantics, and a 500-member atomic backfill would exceed
  a single batch's operation count.
- **Keep per-member policy edits as the group implementation.** Rejected: the goal is stable
  bindings with incremental data updates, subject to the APIM feasibility gate, not larger policies
  or a manual apply for each join/leave.
- **Create or manage Entra groups.** Rejected: existing Entra groups remain read-only references;
  this feature must not require Graph write permissions.
