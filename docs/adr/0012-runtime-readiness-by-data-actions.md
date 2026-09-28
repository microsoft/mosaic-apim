# ADR 0012: Judge gateway runtime readiness by data actions at the resource the published API calls

**Status:** Accepted

Amends [ADR 0006](0006-model-endpoint-onboarding.md), which introduced the runtime check.

## Context

ADR 0006 gave each model endpoint two access relationships. It verified the runtime one, whether
the gateway's managed identity can call the models, by matching **exactly one** role definition
ID, chosen by resource shape, at the **registered** scope. Driving a live deployment through the
console (roadmap gap G8) showed that both halves of that rule give wrong answers.

**False negatives from exact matching.** Any other role that grants the needed data actions was
reported as "cannot invoke". That covers Cognitive Services User or Cognitive Services OpenAI
Contributor on an Azure OpenAI account, and any custom role. The worst case contradicted MOSAIC's
own advice:

1. Before MOSAIC could read an account, it did not know the kind, so it recommended Cognitive
   Services User.
2. Once it learned `kind: OpenAI`, it required Cognitive Services OpenAI User instead.
3. An administrator who had followed the first recommendation was then told the gateway could not
   invoke.

**False positives from the project scope.** An endpoint registered by Foundry project resource ID
was checked at the project, where Foundry User was accepted. But deployments are account-level:

- Preflight replaces the endpoint with the parent account's endpoint.
- The published API's backend URL is the account host.
- Role assignments inherit downward only, so a grant on the project does not authorize calls to
  the account.

MOSAIC reported "can invoke" while real calls would fail with 401 or 403. Microsoft's
[Foundry RBAC guidance](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry) names
Foundry User on the Foundry resource as the minimum for calling its models.

**The network was never considered.** A role is not the only thing that decides whether the
gateway can call. Suppose an Azure OpenAI account has public network access disabled, and a
classic-tier API Management service with no virtual network sits in front of it. That gateway
cannot reach the account at all, whatever roles it holds.

## Decision

**The scope evaluated is the account the published API calls.** Account-registered and
project-registered endpoints are both evaluated at the parent account's scope (`account_scope`).
Role assignments are read there too. `$filter=principalId eq` returns assignments at, above, and
below that scope, and each one is placed by ancestry:

- **Direct:** on the account.
- **Inherited:** on the resource group, subscription, management group, or root. These count, and
  are labelled with their origin, as before.
- **Narrower:** below the account, such as on a project. A narrower grant that would otherwise have
  sufficed is reported with a plain explanation: models are deployed on the parent resource, and
  the published API calls that resource. It is never counted.

**Any role whose data actions cover the published API is accepted.** Each curated operation
declares the data action it needs (`OperationSpec.data_action`). The required set is derived from
the operations MOSAIC publishes, so the check cannot drift from the shape. The actions come from
`az provider operation show --namespace Microsoft.CognitiveServices`. All of them are under
`Microsoft.CognitiveServices/accounts/`:

| Shape | Data actions |
| --- | --- |
| Azure OpenAI (`/openai/...`) | `OpenAI/deployments/chat/completions/action`, `OpenAI/deployments/completions/action`, `OpenAI/deployments/embeddings/action`, `OpenAI/images/generations/action`, `OpenAI/deployments/audio/action` (transcriptions and translations), `OpenAI/responses/write` |
| Foundry Models (`/models/...`) | `MaaS/chat/completions/action`, `MaaS/embeddings/action`, `MaaS/info/read` |

The kind decides the shape. When the kind is unknown, both sets are required, because a grant
judged sufficient now must still be sufficient once the kind is known.

MOSAIC reads the role definition of every candidate assignment. Reader includes
`Microsoft.Authorization/*/read`, which covers this. Each action is then evaluated with Azure's
semantics:

- Matching is case-insensitive, and `*` is a wildcard.
- `notDataActions` subtract only from the `dataActions` of the permission block they appear in.
- Definitions are cached by ID for one check, and shared by every gateway evaluated against the
  endpoint in that check. They are never cached beyond it.
- Assignments are judged one at a time. Two partial roles that together cover the shape are
  reported as insufficient, which errs toward "cannot invoke".

Definitions are read on `2022-05-01-preview`. The stable `2022-04-01` contract drops each
permission block's `condition`, and judging a role with its conditions stripped could claim a grant
Azure would refuse.

**A role definition MOSAIC cannot read falls back to built-ins known to suffice, and only those.**
The lists were built from the real definitions (`az role definition list --name <id>`), and tests
pin them against recorded copies:

- **Foundry shape:** Foundry User, Cognitive Services User, Cognitive Services Data Contributor
  (Preview), Azure AI Developer, Foundry Project Manager, Foundry Owner.
- **Azure OpenAI shape:** all of the above, plus Cognitive Services OpenAI User and Cognitive
  Services OpenAI Contributor.

Owner and Contributor are absent because they carry no data actions. Any other unreadable role is
reported as unreadable: *not confirmed*, never a denial. If the preview version is retired, every
read fails and the check degrades to this list rather than guessing.

**A conditional assignment never earns "can invoke".** An ABAC condition, on the assignment or on a
permission block, is evaluated three-valued for each data action:

- `ActionMatches{'...'}` is the only term MOSAIC can know, because the action is the only thing it
  knows about the request.
- Every attribute test is unknown.
- `AND` mixed with `OR` at one level without parentheses is refused rather than guessed at.

A condition counts only if it is provably true for every published action. For example, Foundry
Owner's and Foundry Project Manager's delegation conditions constrain only role-assignment writes,
so they leave inference untouched. Any other conditional grant is reported as conditional. It is
*not confirmed*, not access, and not a denial, and the administrator is asked to grant the role
without a condition.

**Deny assignments are read with the opposite bias.** They are read at the account and its
ancestors (`$filter=atScope()`), and only once a role would otherwise suffice.
`doNotApplyToChildScopes` is respected. A deny applies unless its condition is provably false:

- A deny that names the gateway or All Principals, with no open condition, is a definite *cannot
  invoke*.
- A deny that depends on group membership or on a condition MOSAIC cannot evaluate is *not
  confirmed*.
- Deny assignments MOSAIC cannot read are ignored. Only Azure creates them, and "cannot read" is
  not evidence that one exists. Treating it as a block would make every check without Reader a
  false negative.

**The network path is part of the verdict.** Gateway preflight now records the service's
`virtualNetworkType`. It also records the addresses its calls to public backends come from: NAT
prefixes, or on dedicated tiers the public addresses, for every region. Endpoint preflight
records the firewall's default action, IP rules, and virtual network rule count. MOSAIC claims
only what is certain:

- **Unreachable:** public network access is disabled and the gateway has no virtual network.
- **Reachable:** public access is enabled with no firewall. Or a firewall admits every egress
  address of a gateway outside any virtual network.
- **Unverified:** anything else, such as a virtual network, or a firewall MOSAIC cannot match the
  gateway's addresses to. MOSAIC cannot see private endpoints, private DNS, or routing, so it says
  so rather than guessing.
- **Unknown:** MOSAIC cannot read the resource, so the role verdict stands alone.

The Models page's Access card shows these settings in an **Endpoint settings** section, between
MOSAIC's own access and the gateway verdicts.

**Recommendations follow the same rules, and must pass them.**

- **Azure OpenAI (`kind: OpenAI`):** Cognitive Services OpenAI User, whose data actions are limited
  to the OpenAI surface.
- **Everything else:** Foundry User, following Microsoft's Foundry guidance. This includes
  AIServices and Foundry accounts, and endpoints registered by project. Its data actions are
  identical to Cognitive Services User's: `Microsoft.CognitiveServices/*`, minus the same three
  `notDataActions`. The data actions give no reason to depart from the guidance. Both roles also
  carry `listkeys`. Cognitive Services User is no longer recommended, but it is still accepted.
- **Unknown kind:** Foundry User, because it is accepted for either kind. The message says the kind
  is unknown, and that granting MOSAIC Reader lets it recommend the exact role, which is narrower
  for Azure OpenAI.

The recommended scope is always the account. A test asserts the invariant: for every kind and
registration shape, the recommended role, granted as recommended, is accepted by the check. That
holds both when the advice was given before the kind was known and after.

**What each verdict means.** `reason` says what the check found. `evaluation` stays
`roleAssignments` for definite answers, and becomes `notEvaluated` for anything MOSAIC cannot
confirm. A consumer keyed on `evaluation` alone therefore never presents an open question as a
denial.

| `reason` | Console verdict | `evaluation` |
| --- | --- | --- |
| `granted` | can invoke | `roleAssignments` |
| `missingRole`, `narrowerScope` | cannot invoke | `roleAssignments` |
| `denyAssignment` (definite) | cannot invoke | `roleAssignments` |
| `networkUnreachable` | cannot invoke | `roleAssignments`, or `notEvaluated` if the role is also unconfirmed |
| `conditional`, `roleUnreadable`, `denyAssignment` (possible), `networkUnverified` | not confirmed | `notEvaluated` |
| `assignmentsUnreadable`, `identityNotObserved` | not confirmed | `notEvaluated` |
| `noGatewayIdentity` | cannot invoke | `noGatewayIdentity` |

Every result names the role that satisfied the check and the scope it was assigned at, or lists why
each held role fell short. MOSAIC still reports and never grants.

**Recorded results stay readable.** `requiredRoleName` and `requiredRoleDefinitionId` keep their
names and now carry the recommended role. Every new field has a default, so results recorded
before this change still load. The console falls back to `evaluation` and `canInvoke` when `reason`
is absent.

## Consequences

- An administrator who grants any sufficient role, or who follows MOSAIC's advice given before the
  kind was known, is reported as able to invoke. MOSAIC no longer contradicts itself.
- A Foundry project grant is no longer mistaken for access to the models. Endpoints registered this
  way that previously read "can invoke" on the strength of a project grant now read "cannot
  invoke" after their next check. That is a correction, not a regression.
- The check reads role definitions and deny assignments, which Reader already covers. The custom
  role ADR 0006 offers still omits the role-assignment read, so it still yields *not confirmed*.
- A private endpoint behind a gateway with no virtual network is reported as unreachable before
  anyone publishes to it.
- A new curated shape must declare a data action for every operation (`OperationSpec.data_action`
  is required), and must extend the fallback lists from real definitions. This applies to the
  Anthropic Messages shape that roadmap gap G5 is adding.

Known limitations:

- `principalId eq` returns only assignments made to the gateway's identity itself. A role reaching
  it through a group is missed and reported as "cannot invoke": a false negative, which is the safe
  direction.
- Partial roles are not combined across assignments.
- MOSAIC does not match firewall virtual network rules against the gateway's subnet, and cannot see
  private endpoints, private DNS, route tables, or network security perimeters. Those cases report
  *not confirmed*.
- Changing a resource's kind, such as upgrading Azure OpenAI to AI Services, can change the shape
  MOSAIC publishes and so the actions required. The next check reports against the new shape.
