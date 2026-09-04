# ADR 0010: Publish model deployments by completing the APIM reconciliation loop

**Status:** Accepted

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
order, a `mosaic-*` policy fragment, a backend, an API, its operations, the API policy document, a
product, a product/API link, and a subscription. ADR 0001's read-only APIM boundary is therefore
partially superseded. APIM remains the runtime plane and Cosmos remains desired state, but the
control plane now performs explicit APIM writes when publishing is applied.

**The reconciliation loop is completed rather than bypassed.** ADR 0001 described desired state ->
observed state -> deterministic plan -> explicit apply -> audited result, and stopped one step
short. A `Publication` record in `desired-state` produces a persisted, deterministic `PublishPlan`.
Apply runs against that specific plan and rejects a stale plan digest. A `PublishRun` records the
result of each step. Saving a publication is never the operation that mutates APIM.

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

**APIM writes are treated as asynchronous when Azure says they are.** Some API Management writes
return `202 Accepted` with `Azure-AsyncOperation` or `Location`. The transport polls those
operations to completion. Reporting success for a resource that is still provisioning would make the
next step's failure look inexplicable, so the step is not successful until Azure's long-running
operation has settled.

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
- Nothing detects drift in the background. Re-planning shows it, consistent with the gap ADR 0005
  already acknowledged.
