# ADR 0010: Publish model deployments by completing the APIM reconciliation loop

**Status:** Accepted

**Update:** [ADR 0011](0011-governed-model-access.md) joins opted-in publications to direct
entitlements and supersedes this record's "never calls listSecrets" product policy with
authorized, audited, on-demand key reveal. The historical tradeoffs below describe the initial
publishing release; legacy publications retain their behavior until explicitly opted in.
[ADR 0012](0012-format-aware-model-publishing.md) chooses the curated operation set from each
deployment's format and capability instead of from its provider alone. It adds an Anthropic
Messages shape for Claude on Foundry and lists deployments no shape can serve as not publishable.

## Context

ADR 0001 made APIM the runtime plane and MOSAIC the control plane, but deliberately stopped
short of writing to API Management. The backend could read APIM, preview policy, and explain a
future apply, but it could not create or change runtime resources. That was the right boundary while
apply, failure recovery, and rollback did not exist. It is no longer an honest description of the
system.

ADR 0004 then defined the policy ownership seam: MOSAIC would own `mosaic-*` policy fragments
and keep customer-authored policy documents out of its write path. ADR 0005 made adoption a
Cosmos-only import, and acknowledged that drift is only exposed when an administrator re-plans.
ADR 0006 kept model endpoint onboarding read-only by choosing `Reader`
(`acdd72a7-3385-48ef-bd42-f606fba81ae7`) rather than any role that could infer, invoke, or read
keys. Publishing model deployments into gateways crosses a different boundary. It needs APIM
writes, and pretending otherwise would hide the main operational risk of the feature.

ADR 0009 named this phase without implementing it: it recorded that "MOSAIC will eventually
orchestrate the APIM assignment that makes a grant real", left `EntitlementBinding.source` with an
`orchestrated` value nothing produces, and stated that ADR 0001 was unaffected because nothing there
wrote to API Management. That was true of entitlements. It stops being true here, and this record
is where the Contributor role is actually granted.

## Decision

**MOSAIC writes to API Management.** Publishing a model deployment into a gateway creates, in
order, a backend, a `mosaic-*` policy fragment that routes to it, an API, its operations, the API
policy document, a product, a product/API link, and a subscription. ADR 0001's read-only APIM
boundary is therefore partially superseded. APIM remains the runtime plane and Cosmos remains
desired state, but the control plane now performs explicit APIM writes when publishing is applied.

**Every resource is created after the resources it names.** API Management accepts a policy
fragment and only then validates its `set-backend-service`, failing the write when the backend it
names does not exist yet, so the backend is created before the fragment. Likewise, the API policy's
`include-fragment` names the fragment; the operations and the API policy belong to the API; the
product/API link joins the product and the API; and a subscription's scope names the product. Each
is created after what it names. Governed access keeps the same dependency: its prepare stage writes
the backend, and the fragment follows in its policy stage. A rollback walks the order backwards and
unpublish reverses it, so teardown removes the fragment before the backend it routes to. The first
release created the fragment first, which failed every publish on a real gateway at its first step.
A plan saved in that order is refused at apply and must be planned again, because apply runs a
plan's steps in the order they were saved and the plan digest covers intent rather than order.

**The reconciliation loop is completed rather than bypassed.** ADR 0001 described desired state ->
observed state -> deterministic plan -> explicit apply -> audited result, and stopped one step
short. A `Publication` record in `desired-state` produces a persisted, deterministic `PublishPlan`.
Apply runs against that specific plan and rejects a stale plan digest. A `PublishRun` records the
result of each step. Saving a publication is never the operation that mutates APIM. The admin
console applies a plan only from a review of that plan, so an administrator sees the steps that
will run before they run; re-planning opens its fresh plan in the same review.

**Rollback deletes only resources this failing apply created.** Each step records whether it created
the resource or found it already present. On failure MOSAIC reverses the completed steps and deletes
only resources tracked as created by that run. Ownership is never inferred from naming, so a product
that merely happens to match a MOSAIC name is not destroyed. Rollback failures are reported, not
swallowed; the honest result of a partial rollback is "here is exactly what is left behind".

**Existence is read at write time, not at plan time, and ownership accumulates.** The gap between
planning and applying is a human review window and can be arbitrarily long, so what the plan saw is
evidence rather than fact by the time the write happens. Each step re-reads the resource immediately
before writing it, and the case the plan expected to create but which now exists is refused outright
rather than overwritten: MOSAIC will not take over a resource that appeared while an administrator
was reading the plan, and will not later delete it during a rollback believing it was its own.
Ownership is also merged across applies rather than replaced, because a second apply legitimately
observes everything already present, and recording that as "MOSAIC created none of this" would
quietly disarm rollback, unpublish, and the guards that stop a publication or a gateway being
deleted while its resources are still running.

**`management_mode` is the write gate, not the role assignment.** `Gateway.management_mode` is
`observe` or `manage`. It already existed and was inert; publishing makes it meaningful. A gateway
in `observe` mode is never written to even when Azure RBAC would permit the write, and a gateway
cannot move to `manage` until preflight has actually confirmed `canWrite`. Both conditions must
hold: the operator's MOSAIC intent and Azure's permission model.

**MOSAIC asks for `API Management Service Contributor`.** The requested built-in role is
`API Management Service Contributor` (`312a565d-c81f-4fd8-895a-4e21e48d571c`). This is the weakest
part of the design. The role carries
`Microsoft.ApiManagement/service/subscriptions/listSecrets/action`, so MOSAIC could read APIM
subscription keys. It does not call that action: a published subscription is created, its name and
scope are shown, and the operator retrieves the key from Azure. That is a product policy, not a
permission boundary. ADR 0006 chose `Reader` precisely so inference and key access were impossible
rather than merely declined. A narrower custom role could restore that property; it was not chosen,
in favour of one built-in role name operators can grant.

**Published APIs use curated operation sets, not fetched specs.** MOSAIC ships versioned operation
sets per provider. Azure OpenAI includes chat completions, completions, embeddings, image
generations, audio transcriptions and translations, and responses. Azure AI Services inference
includes `/models/chat/completions`, `/models/embeddings`, and `/models/info`. A published API
records the shape version that produced it. Publishing therefore does not depend on fetching a
provider OpenAPI document at apply time, which would make the plan non-deterministic and couple an
APIM write to a third-party document's availability. The cost is that a provider adding an operation
needs a MOSAIC release.

**ADR 0004's policy ownership seam holds.** Enforcement lives in a `mosaic-*` policy fragment. The
API policy document is a thin `<include-fragment>`, and MOSAIC owns outright only the APIs it
created. It still never rewrites a policy document authored by someone else. Raw policy XML still
never crosses the API boundary: the plan carries semantic facets and a SHA-256 digest, not markup.

**APIM writes are treated as asynchronous when Azure says they are.** Azure says a write is still
running by answering it with `Azure-AsyncOperation` or `Location`. The transport polls any `200`,
`201` or `202` write response that carries either header to completion, because an update can be
answered `200` and still be finished later. A `200` or `201` without either header finished
synchronously and costs no further request. Reporting success for a resource that is still
provisioning would make the next step's failure look inexplicable, and reporting an update as
applied while API Management still enforces the previous content would make a runtime failure
inexplicable, so a step is not successful until Azure's long-running operation has settled. API
Management also validates some writes only after accepting them, so an accepted write can still
fail. A failed update leaves the previous content in place.

The 2024-05-01 API Management contract MOSAIC writes against marks these of its writes as
long-running (`x-ms-long-running-operation`, final state via `location`):

| Write | Long-running | Responses |
| --- | --- | --- |
| Policy fragment PUT | Yes | `200` (update) and `201` (create), both with `Azure-AsyncOperation` and `Location` |
| API PUT | Yes | `200` (update) and `201` (create), both with `Azure-AsyncOperation` and `Location` |
| API DELETE | Yes | `202` with `Azure-AsyncOperation` and `Location`, or `204` |
| Backend, operation, API policy, product and subscription PUT | No | `200` or `201` with an ETag |
| Product/API link PUT | No | `200` or `201` |
| Every other DELETE | No | `200` or `204` |

So a publish costs a poll for its fragment and for its API, whether it creates or updates them, and
a governed apply costs one for each fragment and API write in its stages. The rule in the transport
is general rather than a list: any write Azure answers with a poll header is waited for.

**A failed operation says why.** When the settled operation reports an `error`, at the top level or
under `properties`, the failed step's error includes Azure's code and message and the most specific
nested detail. The publish dialog shows it on the step's row in the run table, and the run's errors,
which the dialog shows as banners, name the step's resource kind and name before it, so a fragment
that names a missing backend fails as:

```text
policyFragment mosaic-retroburn-aoai-east-gpt-35-turbo: The Azure operation did not succeed (Failed). ValidationError: One or more fields contain incorrect values. Detail: Error in element 'set-backend-service' on line 3, column 4: Backend with id 'mosaic-retroburn-aoai-east-gpt-35-turbo' could not be found.
```

The reason is bounded in length. Any policy markup or policy expression Azure echoes is cut off, so
ADR 0004's rule that MOSAIC-authored policy never crosses the API boundary still holds; an element
name, a line and a column are not markup. A failed operation with no error body is reported by its
terminal state, and says to check the Azure activity log for the resource. MOSAIC does not read
again to find a reason: after a failed create the resource does not exist, after a failed update it
still holds its previous content, and the activity log needs subscription-scope access MOSAIC is not
granted.

**A refused request says why too.** A request Azure refuses outright with a `4xx`, or that still
gets a `429` or `5xx` after the transport's retries, fails with its HTTP status and Azure's reason,
taken, bounded and stripped of policy markup exactly as an operation's is. An API policy Azure
refused with a validation error, for example, would fail as:

```text
apiPolicy policy: Azure Resource Manager rejected the request (HTTP 400). ValidationError: One or more fields contain incorrect values. Detail: Error in element 'include-fragment' on line 3, column 6: Fragment with id 'mosaic-contoso-aoai-gpt-4o-prod' could not be found.
```

A request that keeps failing reads, for example, `Azure Resource Manager did not return a usable
response (HTTP 503). ServiceUnavailable: The service is temporarily unavailable.`, and one whose
body gives no reason ends at the status. Three kinds of request are exceptions. A credential
request's message never carries Azure's text, because a credential response must never be reflected
in an error. The `401`/`403`, `404` and `412` refusals keep their fixed messages, because the
console keys its remediation off them. And the subscription scan behind endpoint suggestions keeps
returning MOSAIC's own wording without Azure's text, as it always has.

## Consequences

- Administrators publish a model into a gateway without leaving MOSAIC or writing policy XML.
- MOSAIC is now capable of destructive action in APIM. `observe` mode and tracked-creation rollback
  bound it, but the blast radius is no longer zero, and is bounded by MOSAIC's correctness rather
  than by Azure's permission model.
- The contributor role means MOSAIC's inability to read subscription keys is now a promise rather
  than a guarantee.
- Existing deployments need `azd provision` or a manual role grant before publishing works. Until
  then preflight reports missing write permissions precisely rather than failing during apply.
- Curated provider shapes need a MOSAIC release to track provider changes.
- A partial rollback failure can leave orphaned APIM resources. They are named and reported, but
  nothing reconciles them automatically yet.
- A publication and an entitlement both describe an API Management product and subscription, and
  they are not yet joined. An entitlement's `EntitlementBinding` still reaches `inferred` or
  `manual` only; making a publication populate it as `orchestrated` is the obvious next step and is
  deliberately not taken here, because ADR 0009's binding landed while this was being built.
  Enforcement is specified per publication in the meantime.
- Nothing detects drift in the background. An administrator sees it by re-planning, and the console
  opens every fresh plan for review before anything is applied. A plan compares existence rather
  than content, so a missing resource shows as a create, and a changed one shows as an update, the
  same as one nobody touched. This is consistent with the gap ADR 0005 already acknowledged.
- Every fragment and API write waits for its operation, so a publish or a governed apply makes a
  few more reads than it writes, and takes as long as API Management takes to validate the policy.
