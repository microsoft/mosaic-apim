# Cost centers

Every grant in MOSAIC, and so every call through it, is charged to a cost center: a team, project,
or budget line that pays for AI. People choose one when they ask for access, and name one on each
call when they hold several. Analytics, the chargeback, and the portal report spend and quotas per
cost center. [ADR 0021](adr/0021-cost-centers.md) records the design.

- [What a cost center holds](#what-a-cost-center-holds)
- [Set up cost centers](#set-up-cost-centers)
- [Members and who may charge a cost center](#members-and-who-may-charge-a-cost-center)
- [Limits and pooled quotas](#limits-and-pooled-quotas)
- [Grants, requests, and approvals](#grants-requests-and-approvals)
- [Choose a cost center on a call](#choose-a-cost-center-on-a-call)
- [Keys](#keys)
- [Reports](#reports)
- [API](#api)
- [Limits of the design](#limits-of-the-design)

## What a cost center holds

| Field | What it is |
| --- | --- |
| Name | What people see, such as **Customer Insights**. |
| Code | What calls name, such as `CI-204`. 1 to 64 letters, digits, dots, hyphens, and underscores, unique in the tenant whatever its case. |
| Owners | Email addresses of the people responsible for it. |
| Members | The people, applications, agents, and Entra security groups that may charge it. |
| Keys allowed | Whether its grants may use subscription keys. Off, they use Microsoft Entra tokens only. |
| Limits | For each model API or MCP server: per-person default limits, and an optional pooled monthly quota. |

A cost center spans gateways: one cost center can hold grants on models published through any of
them.

Every tenant has **General** (`general`). It's built in, so you can rename it and change its code,
but not delete it. It's the tenant's default until you choose another.

## Set up cost centers

In the console, open **Cost centers**:

1. Under **New cost center**, give it a name, a code, owners, and whether keys are allowed, and
   choose **Create cost center**.
2. Open it, and add members: people, applications, agents, or Entra security groups MOSAIC already
   knows. Add a security group rather than listing many people.
3. Under **Limits**, set per-person defaults and pooled quotas for the models and MCP servers its
   people use.
4. Under **Tenant default**, choose which cost center new principals are charged to. Everyone may
   charge the tenant's default.

When you onboard a person, application, or agent on the **Identity** page, its **Default cost
center** is the tenant's default unless you pick another. Its calls are charged there when they name
none. Security groups have no default.

A cost center can't be deleted while it's the tenant's default, while it's anyone's default, or
while grants are charged to it. Move those first. A principal can't be deleted while a cost center
lists it: remove it from the cost center first, which revokes what relied on it.

Changing a cost center's code, turning its keys off or on, changing its limits, or changing a
principal's default changes what the gateway enforces. The grants it affects show as pending until
you review and apply their models' plans. Until then, the gateway keeps enforcing what it has.

## Members and who may charge a cost center

A principal may charge a cost center when:
- it's the tenant's default, which everyone may charge;
- it's the principal's own default;
- the cost center lists the principal; or
- the cost center lists a security group the principal is in.

A security group may charge only the tenant's default and the cost centers that list it.

Removing a member stops them charging the cost center:
- Their grants under it are revoked: turned off, with the reason recorded. The next apply of each
  model deletes their keys. You can turn a revoked grant back on only once they may charge the
  cost center again.
- If it was their default, their default goes back to the tenant's.
- Removing a security group also checks every grant whose subject might have charged the cost
  center only through that group. With Microsoft Graph lookup off, a grant that came from a request
  keeps only the groups that request was made through, and a grant nothing proves eligible is
  revoked.
- Leaving the tenant's default revokes nothing.

Losing the right to charge a cost center any other way revokes grants the same way:
- **Changing a principal's default** revokes its grants under the former default, unless it may
  still charge it as a listed member or through a listed security group.
- **Changing the tenant's default** revokes the grants under the former default whose subjects may
  charge it no other way: as a listed member, through a listed security group, or as their own
  default. People onboarded while it was the tenant's default have it as their own default, so
  they keep theirs.
- **Turning a grant back on** checks again that its subject may charge its cost center, whether it
  was revoked or turned off by hand.

## Limits and pooled quotas

**Per-person defaults** apply to each grant under the cost center that sets no limits of its own:
tokens per minute, a token quota, calls per minute, and a call quota. They count on the grant's own
counter, exactly as if the grant set them. A security group's grant applies them to each member.

A **pooled monthly quota** is shared by every grant under the cost center on that model, whoever
calls:
- **Monthly tokens** is a second `llm-token-limit` in the model's policy, with a `Monthly` quota,
  counted per cost center and publication. A call has to fit both the caller's own limits and the
  pool.
- **Monthly calls** is a `quota-by-key` over the calendar month, counted the same way.
- MCP servers, and models whose gateway tier can't meter tokens, pool calls only.

API Management counts each gateway separately, so a pool is per model, per gateway.

The gateway reports what's left in response headers:

| Header | What's left |
| --- | --- |
| `x-mosaic-remaining-tokens` | The grant's tokens this minute |
| `x-mosaic-remaining-quota-tokens` | The grant's token quota |
| `x-mosaic-remaining-calls` | The grant's calls this rate window |
| `x-mosaic-cost-center-remaining-quota-tokens` | The cost center's pooled tokens this month |

Each header is present only when that limit applies. Call quotas don't report what's left.

## Grants, requests, and approvals

A grant names one cost center, and that's part of what the grant is. The same person can hold the
same model under two cost centers, as two grants, each with its own limits, key, and usage.

- **Entitlements:** a new grant charges the cost center you choose, or its subject's default. MOSAIC
  refuses a cost center the subject may not charge. The grants table shows and filters by cost
  center.
- **Portal requests:** people choose a cost center from a dropdown of those they may charge, with
  their default preselected. They can ask for the same model again under another of their cost
  centers.
- **Approvals:** the dialog shows the cost center the person chose, and you can charge another they
  may charge. Leaving limits empty applies the cost center's per-person defaults, when it has any.

## Choose a cost center on a call

A call names the cost center it charges with the `x-mosaic-cost-center` header, set to the cost
center's code. The code is compared without case. Connection details in the portal show the header
and its value for each grant.

```bash
curl "$ENDPOINT/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21" \
  -H "Authorization: Bearer $MOSAIC_ACCESS_TOKEN" \
  -H "x-mosaic-cost-center: CI-204" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Without the header, the gateway uses, in order:
1. the caller's direct grant under their default cost center;
2. their other direct grants, oldest first;
3. their security-group grants, the most generous first.

A key belongs to one grant, so it always charges that grant's cost center, and needs no header. The
gateway refuses a call with **403** when:
- the header isn't a single valid code (denial reason `cost-center`);
- the caller holds no grant under the cost center it names (`cost-center`); or
- a key comes with a header naming a different cost center (`cost-center-mismatch`).

The gateway removes the header before the call reaches the model or MCP server.

## Keys

Applies don't create keys. A grant's key exists once someone asks for it:
- In the portal, on **My access**, a person creates, rotates, or deletes the key for their own
  applied direct grant.
- In the console, on **Entitlements**, an administrator does the same for any direct grant, such as
  an application's or an agent's.

Rotating regenerates one slot, primary or secondary, while the other keeps working, so clients can
move to the new value without downtime. Deleting the key stops every client using it; a new key can
be created after. The key's name in API Management ends with the cost center's code.

Keys work only when the model's governed access accepts keys and the grant's cost center allows
them. Turning a cost center's keys off suspends its grants' keys at the next apply, and keeps them
for when keys are allowed again. Security-group grants never have keys.

## Reports

- **Analytics:** every tab has a **Cost center** filter. Filtered, it counts only calls through the
  cost center's grants. Refusals, unattributed calls, client applications, reserved capacity
  nobody called, and figures by the hour belong to no grant, so the filter leaves them out and the
  page says so. Overview ranks cost centers, Consumers lists each with its grants, callers, and cost,
  Cost shows cost by cost center, and grant, limit, and hygiene rows name theirs.
- **Chargeback:** the CSV has **Cost center** and **Cost center name** columns after **Object ID**.
  **Unattributed calls** and **Reserved capacity with no calls** belong to no grant, so their cost
  center is empty. See [Pricing](pricing.md#chargeback).
- **Portal:** **Usage & cost** shows a person their own use, limits, and rate-limit use, as before.
  For each cost center they hold a grant under, it adds the cost center's total this month, from
  everyone's calls, against its pooled quotas on the resources they hold there. It never shows who
  else called or how much any one person used.

## API

Administrators (`Admin`):

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/cost-centers` | Every cost center, with its members, grant counts, and whether it's the tenant's default |
| POST | `/cost-centers` | Create one: `name`, `code`, `description`, `owners`, `keysAllowed`; 409 `codeInUse` |
| GET | `/cost-centers/{id}` | One cost center |
| PATCH | `/cost-centers/{id}` | Change its name, code, description, owners, or keys allowed |
| DELETE | `/cost-centers/{id}` | Delete it; 409 `builtIn`, `tenantDefault`, `isDefault`, or `hasGrants` |
| PUT | `/cost-centers/{id}/members/{principalId}` | Add a member |
| DELETE | `/cost-centers/{id}/members/{principalId}` | Remove a member, and revoke their grants under it |
| PUT | `/cost-centers/{id}/limits` | Replace its per-person defaults and pooled quotas |
| GET, PUT | `/cost-center-settings` | The tenant's default cost center |
| GET | `/entitlements?costCenter={id}` | Grants charged to a cost center |
| POST, DELETE | `/entitlements/{id}/keys` | Create or delete any direct grant's key |
| POST | `/entitlements/{id}/keys/rotate` | Regenerate one slot: `{"slot":"primary"}` or `{"slot":"secondary"}` |
| GET | `/analytics/{view}?costCenterId={id}` | Any analytics view, filtered to one cost center |

People (`User`), for themselves only:

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/portal/cost-centers` | The cost centers they may charge, their default marked |
| POST | `/portal/access-requests` | Ask for access, with `costCenterId` (their default when omitted) |
| POST, DELETE | `/me/entitlements/{id}/keys` | Create or delete their key |
| POST | `/me/entitlements/{id}/keys/rotate` | Regenerate one slot of their key |
| GET | `/me/usage` | Their usage, with `costCenters`: each cost center's total this month |

## Limits of the design

- Group membership comes from the token at call time, so removing someone from a group takes effect
  when their token expires, usually within about an hour.
- A pool counts per gateway. The same model on two gateways has two pools.
- A cost center's total in the portal is a total. When only two people share a cost center, each can
  work out the other's use from it and their own.
- Calls MOSAIC couldn't attribute belong to no cost center, so a cost center's figures can be lower
  than its gateway's.
