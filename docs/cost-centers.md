# Cost centers

A cost center is the team, project, or budget line that pays for AI. It defines charging, resource
availability, default limits, and pooled quotas. For every **custom** center, one dedicated Entra
security group defines membership. An **entitlement** is still a grant to one exact resource under
one center: membership alone is not permission to call every model or MCP server.

The built-in **General** center is exempt from the mandatory Entra group. Its exemption follows
its built-in identity, not its editable name or code. Government offerings are not available in
the new Entra-backed setup and access-mode workflow; their existing workflow is separate.

[ADR 0030](adr/0030-entra-backed-cost-centers.md) records this design and supersedes the earlier
local-group experiment. See [workflow diagrams](workflows.md) for setup, access, and removal.

- [What a cost center holds](#what-a-cost-center-holds)
- [Set up cost centers](#set-up-cost-centers)
- [My cost centers and self-service settings](#my-cost-centers-and-self-service-settings)
- [Members and who may charge a cost center](#members-and-who-may-charge-a-cost-center)
- [Resource access modes](#resource-access-modes)
- [Grants, requests, and approvals](#grants-requests-and-approvals)
- [Removing a member](#removing-a-member)
- [Limits and pooled quotas](#limits-and-pooled-quotas)
- [Choose a cost center on a call](#choose-a-cost-center-on-a-call)
- [Keys](#keys)
- [Reports](#reports)
- [API](#api)
- [Earlier MVP records and historical proposals](#earlier-mvp-records-and-historical-proposals)
- [Limits of the design](#limits-of-the-design)

## What a cost center holds

| Item | What it means |
| --- | --- |
| Name and code | A readable name and a code calls can select, such as **Customer Insights** and `CI-204`. Codes are 1 to 64 letters, digits, dots, hyphens, and underscores, unique in the tenant regardless of case. |
| Owners/contact details | People responsible for spend and notifications. An email address here does not assign Entra group ownership or add a member. |
| Entra binding | The configured directory and verified group object ID, with setup status and whether MOSAIC created or attached it. Required for custom centers. |
| Resource access | Automatic, Request-only, or Not available for each exact published model, pool-model offering, or MCP server. |
| Keys allowed | Whether an otherwise eligible individual grant may use a subscription key. This does not enable keys on the resource or create a group key. |
| Limits | Per-person defaults and optional pooled monthly quotas for each exact resource. |

A center spans gateways. Its group can be bound to **only one** MOSAIC cost center; a similar
display name is not an identity match. Membership lives in Entra, not in an editable MOSAIC
shadow roster, and the centers list does not download the full membership of every group.

Every tenant has General, initially code `general`. You may rename it and change its code, but
not delete it. Tenant and personal defaults are preferences for selecting among eligible grants;
making a custom center a default does not give anyone membership or resource permission.

## Set up cost centers

Open a center's workspace in the administrator console. Configure its details, group setup,
initial membership, and resource access together:

1. Save the center's name, code, responsible contacts, and key restrictions.
2. Attach an eligible existing Entra group, or explicitly create one through MOSAIC.
3. Verify the directory and group object ID. An existing group must be an ordinary
   **cloud-managed, assigned-membership, security-enabled, mail-disabled, non-role-assignable**
   group. Dynamic, on-premises-managed, mail-enabled, and role-assignable groups are unsupported
   in this editor.
4. Choose initial members explicitly, including whether to add the person who requested the
   center. Neither ownership/contact metadata nor approval adds anyone silently.
5. Set access modes and limits for exact resources. Review and apply the affected gateway plans
   before treating new resource grants as active.

**Attaching a group affects the real directory group.** Adding or removing members through MOSAIC
can also change their access to other applications that use it. Attaching requires an explicit
acknowledgment of this effect; prefer a group dedicated to the center.

Directory lookup and verification use the API identity's read-only baseline. Optional
`Group.Create` permits creation; the API service principal is explicitly assigned as an owner
when MOSAIC creates a group. Optional `GroupMember.ReadWrite.All` permits user membership edits.
Adding service principals, including managed identities and app-only agent identities, additionally
requires separately consented `Application.ReadWrite.All`. These application permissions can be
tenant-wide even though MOSAIC restricts mutations to a center's verified bound group. A MOSAIC
Admin role is not directory consent. See [deployment and consent](deployment.md#optional-cost-center-group-management).

### Incomplete setup and recovery

The center is saved **before** external group creation. If consent is missing, Graph refuses the
operation, or creation fails, keep the center and finish setup later. It is not usable for custom
center grants, defaults-based eligibility, or credentials until its binding is verified.

A timed-out creation has an **unknown outcome**, not permission to create another group. Inspect
the retained operation and reconcile the actual object identity or use administrator-assisted
recovery. Retrying approval or recovery must not create another center or group. A matching
display name is not sufficient recovery evidence.

A verified attached group can support runtime access without membership-write consent. Its
membership view is then read-only, not an incomplete group binding. Capability checks distinguish
creation, user membership, and workload membership; they do not create or delete test groups.

Keep these states separate:

| State | What it establishes |
| --- | --- |
| Center setup | Whether the required group is verified and exclusively bound |
| Directory operation | Whether a create/add/remove succeeded, failed, or has an unknown outcome |
| Membership propagation | Whether refreshed tokens can be expected to reflect the directory change |
| Gateway apply | Which resource/gateway changes are pending, applied, or failed |
| Individual key revocation | Which existing credentials still need revocation and gateway apply |

Deleting or disabling a center does not delete its Entra group. Deletion remains blocked while
references such as grants or defaults require the center. Rebinding or releasing an applied
group association requires explicit revocation/reconfiguration; changing a stored association
does not remove access already applied to APIM.

## Members and who may charge a cost center

The Members view reads the actual Entra membership with server-side paging, including supported
workload identities. With the appropriate consent, administrators add or remove **direct**
relationships in the bound group. A successful local record edit is never reported as a successful
directory change. Permission denial, throttling, replication delay, and unknown outcomes remain
visible with recovery guidance.

**Preview dependency:** direct member listing currently uses Microsoft Graph **beta**, following
Microsoft's [documented workaround for v1.0 omitting service principals](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-1.0).
Graph beta APIs are subject to change and
[are not supported for production use](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-beta).
This is a control-plane roster-listing limitation, separate from the stable JWT group-claim
authorization path at APIM, which does not call Graph. Neither listing nor the new write adapter
has proved live interoperability in this change.

Direct and inherited membership are distinct. An inherited member cannot be removed by pretending
to delete a direct relationship; its parent-group relationship must be reviewed separately.
Removing a direct relationship also does not prove that no other membership path remains.

For a custom center, current eligibility depends on its verified Entra group, not a local roster,
an owner email address, a personal default, or the tenant default. Requests, approvals, individual
grant creation/re-enabling, and key creation/reveal check eligibility. Failed or incomplete
directory verification is not successful membership evidence.

At runtime, **every custom-center token path** requires the center's group ID in the validated
token, including an approved individual grant. A more-specific direct grant can choose different
limits but cannot bypass that requirement. General retains its existing resource/credential rules
without a new mandatory center-group check.

## Resource access modes

| Mode | What members receive |
| --- | --- |
| **Automatic** | One managed Entra group grant for this center and exact resource. Eligible members use it with a runtime JWT after gateway apply. No per-member automatic entitlements or keys are created. |
| **Request-only** | Eligible members may request an individual resource grant. Approval and gateway apply remain separate. |
| **Not available** | The resource cannot be newly requested or granted under this center. This is enforced by the API, not just hidden in the portal. |

Modes apply to an **exact offering**, not to the catalog's visual grouping of similar models.
A pool-model offering includes both the model identity and the pool scope; the same model in
another pool or publication is a different resource. Model and MCP grants are independent.

Automatic requires **caller-side Entra token support** on that publication. MOSAIC does not
silently enable tokens, weaken the resource's authentication settings, or manufacture individual
keys when tokens are disabled. A backend reached with an API key can still accept caller JWTs at
APIM; backend authentication and caller authentication are different.

Configure Automatic once and apply the group grant through the reviewed gateway plan. Adding
members afterward performs Graph work, with **no per-person automatic grant generation and no
APIM management writes**. Resource modes, grants, quotas, key restrictions, and other policy
changes still use review/apply and can leave pending work on several gateways. Inspect partial
failures per resource rather than treating one successful apply as completion everywhere.

Automatic itself provides no personal key. Where resource keys and center keys are enabled,
an explicit eligible individual grant can still use the individual-key workflow. Reviewed
individual quota exceptions on available resources remain membership-gated. Neither an exception
nor a key request can bypass Not available or incomplete center setup. Changing a mode is desired
intent; inspect and apply the resulting grant/revocation changes before claiming existing runtime
access has changed.

## Grants, requests, and approvals

A grant names one cost center as part of its identity. The same person can hold the same model
under two centers as two grants, each with its own limits, counters, and usage.

- **Administrators** choose an eligible center and available exact resource. A managed Automatic
  grant derives from the center's configuration; it is not an individual request approval.
- **Portal users** see the distinction between *Automatic through your cost center* and *Approved
  for you*. Inherited access does not appear as a personal key or create a personal grant record
  for every group member.
- **Resource requests** name an eligible center and a Request-only offering. Approval rechecks
  membership, mode, availability, and limits, and saves an individual grant. Scheduled approval
  policies do not turn Request-only into Automatic.
- **Gateway apply** makes the approved grant effective. Approved but unapplied access is waiting
  for apply, not Active.
- **Model pools** grant one exact pool model at a time. Two models in one pool require two grants,
  even where eligible direct grants share one individual pool-and-center key.

### Request a cost center

A portal user opens **My cost centers > Request a cost center** to submit a proposed name,
purpose/justification, and an optional existing Entra
group object ID. Submitting a suggestion gives the user no directory-wide group search or write
permission.

This is a distinct request kind, not a fabricated model or MCP entitlement. It has its own status
and appears in the administrator queue, counts, notifications, and request links. The administrator
uses the normal center setup workspace, may use or ignore the group suggestion, and explicitly
chooses initial membership.

Approval links the request to exactly one created center, even if directory setup remains
incomplete. An approved center request is not evidence that the requester is a member, a resource
grant exists, or a gateway apply succeeded. See [approvals](approvals-and-notifications.md).

## My cost centers and self-service settings

The portal's **My cost centers** area separates current membership from the resources in
**My access**. **My memberships** shows the caller's own centers, including private ones.
**Find cost centers** searches only centers an administrator explicitly published for discovery.
General is shown as a built-in charging option, not as an Entra group membership to request.

Administrators configure **Settings > Cost centers**:

| Setting | Default | Effect |
| --- | --- | --- |
| Cost-center creation requests | On | Users may propose a new center for administrator review. |
| Cost-center membership requests | Off | Users may request to join an administrator-published center. |

These are persisted tenant settings, not environment variables or browser preferences. Turning
one off prevents new submissions; it does not remove memberships, delete centers, cancel existing
requests, change grants, or change the tenant/personal default charging preference. Existing
reviews and recovery remain available to administrators.

Each custom center starts private. After group setup is ready, an administrator can publish its
name, code, and public purpose from its editor, acknowledging that adding members changes the
real Entra group and may affect other applications that use it. Search does not expose private
centers, directory-wide group lookup, owner email addresses, member rosters, directory IDs,
credentials, or spending. Unpublishing stops new discovery/submission, not existing membership.

### Request to join

Find a published center, open its details, and choose **Request to join**. Explain why you need
membership; submitting does not add you. **My requests** tracks resource access, center creation,
and membership as distinct request kinds. There is at most one active join request per person
and center, including submissions retried with another key. Administrators may still add someone
directly through the center's Members view without requiring a request.

An administrator reviews the requesting person, center, group readiness, and the real membership
effect, then approves or denies. Approval uses MOSAIC's existing consented direct Entra addition,
not a local roster or a fabricated resource grant. If the person already has effective membership,
including through a nested group, the request can complete without another direct addition.
Ordinary users request only their own membership; workload-identity joins are not offered here.

Approval, the directory operation, and usable resource access have separate states. Missing
consent or a failed write leaves **administrator action needed**. An uncertain outcome requires
reconciliation of the retained operation, not a new blind addition. The portal distinguishes
directory-confirmed membership from what the current token proves, and reports failed membership
verification rather than claiming that the caller has no memberships.

Joining can unlock already-applied **Automatic** offerings once a fresh runtime token includes
the group. **Request-only** offerings still require individual approval; **Not available** remains
unavailable. Joining creates no key, increases no budget or pooled quota, and changes no default.
Graph membership reads in the portal are not a runtime authorization fallback, and do not bypass
token group overage or gateway apply. The existing supported-cloud restriction remains in effect.

## Removing a member

Removing someone in MOSAIC changes their real **direct Entra relationship**. It does not recall a
token, invalidate a previously copied key, or rewrite the stable Automatic grant.

| Credential/access path | Consequence and required follow-up |
| --- | --- |
| Automatic JWT | Once directory propagation and token refresh remove the center group claim, that token cannot match the center's group grant. The existing grant/policy does not need rebuilding for each member. |
| Individual JWT | A refreshed token without the center group also fails, even if an individual grant remains. Direct-grant precedence does not bypass membership. |
| Existing individual key | The key is independent of JWT membership. Review affected individual grants/keys, revoke or disable the affected access, and apply the affected gateway plans. A directory write alone does not revoke it. |
| Another inherited membership path | Review the remaining relationship; removing one direct relationship does not necessarily remove effective membership. |

Already-issued JWTs retain their claims until expiry. Human access tokens normally have
**60-90 minutes total lifetime from issuance**, not another fixed 60-90 minutes after removal;
the token's actual remaining `exp` and configured token policy matter. Directory propagation can
also delay new claims. Managed identities can cache tokens/authorization per resource for
**around 24 hours**, and membership changes can take several hours. MOSAIC does not promise a
universal 90-minute bound, immediate JWT revocation, or CAE for this APIM path.

After removal, distinguish directory success, propagation/token refresh pending, and individual
key revocation pending. MOSAIC identifies affected direct/key access for eligibility recheck and
review/apply. If a publication lock prevents completion, the work remains explicitly pending;
retry the recheck rather than interpreting the membership edit as a completed gateway revocation.

For changes made **outside MOSAIC**, an administrator must explicitly recheck individual-key
eligibility and finish any revoke/apply work. Complete, tenant-bound verification is required;
failed or partial checks stay visible. There is no continuous directory watcher or guaranteed
automatic key revocation. See [removal workflow](workflows.md#remove-a-member-and-finish-credential-revocation).

## Limits and pooled quotas

**Per-person defaults** apply to a grant that sets no limits of its own: tokens per minute, a
token quota, calls per minute, and a call quota. They use that grant's counter. A security-group
grant applies its allowance to each member, keyed by the runtime caller's object ID.

A **pooled monthly quota** is shared by every grant under the center on that resource:

- Monthly tokens use a second `llm-token-limit`, with a `Monthly` quota, counted per cost center
  and publication. A call must fit both its own limits and the pooled quota.
- Monthly calls use `quota-by-key` over the calendar month, counted the same way.
- MCP servers, and models whose gateway tier cannot meter tokens, have a pooled call quota only.

APIM counts each gateway separately. The same model on two gateways has two pooled quotas.
A [model pool](model-pools.md)'s quota counts every call to its exact pool model, whichever member
deployment served it, per center and pool model.

| Response header | What's left |
| --- | --- |
| `x-mosaic-remaining-tokens` | The grant's tokens this minute |
| `x-mosaic-remaining-quota-tokens` | The grant's token quota |
| `x-mosaic-remaining-calls` | The grant's calls this rate window |
| `x-mosaic-cost-center-remaining-quota-tokens` | The center's pooled tokens this month |

Each header appears only when that limit applies. Call quotas do not report what's left.

## Choose a cost center on a call

`x-mosaic-cost-center` optionally selects the code of a center under which the caller **already
has an authorized grant**. Codes are compared without case. The header grants neither membership
nor resource access; connection details show the appropriate value.

```bash
curl "$ENDPOINT/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21" \
  -H "Authorization: ******" \
  -H "x-mosaic-cost-center: CI-204" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Without a header, APIM considers eligible grants in this order:

1. the caller's direct grant under their default center;
2. other direct grants, oldest first;
3. matching security-group grants, most generous first.

These are selection preferences, not ways around a custom center's group requirement.
A key belongs to one grant, or one pool and center, and always charges that center. APIM rejects
an invalid/unauthorized code, a key with a conflicting center header, or a key/token combination
that does not validate to the same grant. It strips the header before forwarding to the backend.

## Keys

Keys remain **individual credentials**, not inherited group credentials. They work only when the
resource accepts keys, the center allows them, and the individual grant is eligible and applied.
New key creation and reveal require current eligibility. A key is transferable bearer material,
not proof that its named holder physically made the call.

For commercial model grants, apply does not create a key. The holder explicitly creates, rotates,
reveals, or deletes their applied direct grant's key in the portal; an administrator does so for
an application or agent. Rotation replaces one slot while the other can remain in use. Explicit
key deletion is separate from changing Entra membership.

Turning center keys off suspends affected keys at the next apply. Revoking a grant ends its
access through the reviewed revocation/apply workflow. Existing copied keys do not expire because
a directory relationship was removed; inspect pending key work before declaring access ended.
Security-group grants have no key path.

A model pool provides one individual key per subject, pool, and center for eligible direct grants
on its models. Revoking one model grant removes only that model at the pool's next apply; the key
is deleted when no eligible grants it serves remain. Automatic group access does not enlarge the
set of models that such a key authorizes.

## Reports

- **Analytics:** the Cost center filter includes attributed calls under that center's grants.
  Unattributed calls, refusals, client-application summaries, idle reserved capacity, and hourly
  figures without grant attribution are excluded and labeled. Grant, limit, and hygiene rows
  identify their center.
- **Chargeback:** the CSV includes Cost center and Cost center name after Object ID.
  Unattributed calls and reserved capacity with no calls have no center. See [pricing](pricing.md#chargeback).
- **Model pools:** attributed calls use the selected grant's center and the price of the deployment
  that served the call.
- **Portal:** Usage & cost shows the caller's own attributed use. Shared center totals and pooled
  quotas are labeled shared and do not disclose the roster or a per-person spending breakdown.
  A shared total can still allow inference about another person's use in a very small cohort.
- **Budgets:** a center's monthly budget can notify its responsible contacts and block calls
  asynchronously. See [budgets and alerts](budgets-and-alerts.md).

## API

The deployed OpenAPI document is the reference for request bodies. Use the center's Entra workflow,
not the superseded local managed-group membership routes. Creating or updating a center accepts
`groupSetup` (attach, create, or leave setup for later) and exact `resourceAccess` definitions.
Directory member writes require an `Idempotency-Key`; an HTTP response alone is not proof of
success. Inspect the returned operation state, propagation warnings, and pending key work.

| Method | Route under `/api/v1` | Purpose |
| --- | --- | --- |
| GET, POST | `/cost-centers` | List centers and setup state, or save a center |
| GET, PATCH, DELETE | `/cost-centers/{id}` | Read/update a center, or delete it subject to reference guards |
| GET | `/cost-centers/group-capabilities` | Read the separately consented directory capabilities |
| GET | `/cost-centers/{id}/directory-members` | Page through the real group's direct Entra members |
| PUT, DELETE | `/cost-centers/{id}/directory-members/{objectId}` | Add/remove a direct relationship through a durable directory operation |
| GET | `/cost-centers/{id}/directory-operations` | Inspect group and membership operation outcomes |
| GET | `/cost-centers/{id}/directory-operations/{operationId}` | Read an operation and its permitted recovery actions |
| POST | `/cost-centers/{id}/directory-operations/{operationId}/retry` | Explicitly retry a known-safe failure |
| POST | `/cost-centers/{id}/directory-operations/{operationId}/recover` | Reconcile an uncertain external outcome rather than blindly replay it |
| POST | `/cost-centers/{id}/recheck` | Explicitly recheck affected individual-grant/key eligibility |
| PUT | `/cost-centers/{id}/limits` | Replace default limits and pooled quotas for exact resources |
| GET, PUT, DELETE | `/cost-centers/{id}/budget` | Manage the monthly budget |
| GET, PUT | `/cost-center-settings` | Read/update the default selection preference and independent self-service switches |
| GET | `/entitlements?costCenter={id}` | List grants charged to a center |
| GET | `/portal/cost-centers` | Caller-scoped eligible centers |
| POST | `/portal/access-requests` | Request an available Request-only resource under an eligible center |
| POST, DELETE | `/me/entitlements/{id}/keys` | Explicitly create/delete the caller's individual key |
| POST | `/me/entitlements/{id}/keys/reveal` | Reveal an eligible applied individual key |
| GET | `/analytics/{view}?costCenterId={id}` | Read filtered analytics |

Cost-center creation requests have their own routes and never stand in for resource grants:

| Method | Route under `/api/v1` | Purpose |
| --- | --- | --- |
| GET | `/request-counts` | Read separate resource, creation, and membership counts and their total |
| GET | `/cost-center-requests` | Review cost-center proposals, optionally filtered by state |
| GET | `/cost-center-requests/{id}` | Read the proposal, linked center, and setup attention state |
| POST | `/cost-center-requests/{id}/approve` | Review the center configuration and explicitly choose whether to add the requester |
| POST | `/cost-center-requests/{id}/deny` | Decline a pending proposal |
| GET | `/portal/request-counts` | Read the caller's separate request counts |
| GET, POST | `/portal/cost-center-requests` | List the caller's proposals or submit one with an optional group suggestion |
| GET | `/portal/cost-center-requests/{id}` | Track the caller's own proposal and incomplete setup |
| POST | `/portal/cost-center-requests/{id}/withdraw` | Withdraw the caller's pending proposal |

Membership discovery and requests use separate, owner-scoped routes:

| Method | Route under `/api/v1` | Purpose |
| --- | --- | --- |
| GET | `/portal/cost-center-self-service-settings` | Read the two saved self-service switches without exposing administrator settings |
| GET | `/portal/my-cost-centers` | Page/search the caller's current memberships with separate token eligibility |
| GET | `/portal/cost-center-directory` | Page/search administrator-published centers using safe public fields |
| GET | `/portal/cost-center-directory/{id}` | Read a published center or the caller's own private membership |
| GET, POST | `/portal/cost-center-membership-requests` | List the caller's joins or submit a justified, idempotent request |
| GET | `/portal/cost-center-membership-requests/{id}` | Track the caller's own decision and directory outcome |
| POST | `/portal/cost-center-membership-requests/{id}/withdraw` | Withdraw the caller's pending join |
| GET | `/cost-center-membership-requests` | Review joins, optionally filtered by decision state |
| GET | `/cost-center-membership-requests/{id}` | Read the administrator view, including retained operation and recovery context |
| POST | `/cost-center-membership-requests/{id}/approve` | Approve with explicit membership-change acknowledgment |
| POST | `/cost-center-membership-requests/{id}/deny` | Decline a pending join |
| POST | `/cost-center-membership-requests/{id}/resume` | Resume a recorded approval using its retained directory operation |

Discovery lists accept `q`, `cursor`, and `limit`; membership-check failure is explicit rather
than an authoritative empty roster. Submission uses an `idempotencyKey` UUID in the body.
Approval and resume require `acknowledgeMembershipChanges: true`.

Administrators use the non-`/me` entitlement credential routes for authorized workload handoff.
Knowing a center, principal, group, request, or entitlement ID is not authorization to mutate it.
Creation, membership, and resource access requests are distinct request kinds.

<a id="experimental-managed-groups"></a>

## Earlier MVP records and historical proposals

[ADR 0029](adr/0029-managed-cost-center-groups.md) described a default-off MOSAIC-local managed
group and shared internal roster. That experiment is superseded, not an authorization fallback.
Its old local group IDs must never be treated as Entra object IDs.

Old MVP centers without a verified new binding need explicit reconfiguration or recreation.
There is no production migration, upgrade wizard, automatic member conversion, or historical
backfill. Reconfigure deliberately: MOSAIC does not silently copy old members into Entra, broaden
individual grants into group grants, delete existing data, or redirect charges to General.
Generic MOSAIC-local groups remain unrelated desired-state records, not runtime groups.

The [APIM-local membership/inherited-key feasibility proposal](cost-center-access-feasibility.md)
is **historical and unimplemented**. Its offline probe is not the design for Entra-backed centers,
evidence of live interoperability, or a prerequisite for the JWT group-grant path.

## Limits of the design

- Group-overage tokens cannot prove the mandatory group claim for a custom center. An individual
  token grant cannot bypass it; APIM has no Graph fallback. An administrator must resolve claim
  eligibility or review a separately allowed individual-key path.
- JWT expiry, directory propagation, managed-identity caching, and key revocation have different
  timing. Removal is not an instant kill switch for all credentials.
- Stable Automatic group grants avoid per-member policy churn; they do not establish production
  throughput or the scale of arbitrary individual grants/keys.
- Direct member listing depends on the preview Graph beta API, not a production-supported
  directory-listing contract. The runtime's local JWT group-grant check is independent of it.
- Reports omit calls MOSAIC cannot attribute. Dollar-budget feedback is delayed, not a hard cap.
- Fictional adapters, policy tests, and screenshots do not prove live Entra/APIM behavior. Live
  group mutation and gateway checks require a separately authorized non-production target.

References: [Graph creation and ownership](https://learn.microsoft.com/en-us/graph/api/group-post-groups?view=graph-rest-1.0),
[member-write permissions](https://learn.microsoft.com/en-us/graph/api/group-post-members?view=graph-rest-1.0),
[safe relationship removal](https://learn.microsoft.com/en-us/graph/api/group-delete-members?view=graph-rest-1.0),
[access-token lifetime](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#token-lifetime),
and [managed-identity caching](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identities-faq#are-managed-identities-tokens-cached).
