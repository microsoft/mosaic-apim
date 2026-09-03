# ADR 0004: Onboard gateways as records, and never show policy markup

**Status:** Accepted

## Context

MOSAIC deployed one API Management instance and pointed at it with three environment variables. That
is configuration, not management: there was no registry, no verification that MOSAIC could read the
service, no inventory, and nothing an administrator could look at. The intended destination is
enrollment — adding a user, application, group, or project to a model and having MOSAIC translate
that into gateway enforcement — and none of it is possible until MOSAIC can hold a gateway and
describe what is inside it.

Administrators onboarding MOSAIC also should not have to think in API Management terms. The point of
the product is that nobody reads policy XML or navigates the portal to understand who can call which
model and how hard.

## Decision

**Gateways are registered records, not environment variables.** A `Gateway` entity in Cosmos holds an
Azure resource ID, access state, observed capabilities, and an inventory summary. MOSAIC supports
many gateways across subscriptions and environments. `MOSAIC_APIM_SUBSCRIPTION_ID`,
`MOSAIC_APIM_RESOURCE_GROUP`, and `MOSAIC_APIM_SERVICE_NAME` are demoted to an onboarding *hint* for
the instance MOSAIC deploys, and are not read at runtime for anything else.

**Managed identity is the only credential.** MOSAIC never stores a per-gateway secret. When it cannot
read a service it says so precisely — naming the role, the role definition ID, the scope, and a
ready-to-run `az role assignment create` command — because granting that role requires more privilege
than MOSAIC has. The principal ID in that command is read from the `oid` claim of MOSAIC's own ARM
token rather than injected by Bicep, because the API's app settings are an input to the web app that
owns the identity and injecting it there would be circular.

**Read access is verified, write access is only reported.** Preflight reads effective permissions at
the resource scope and falls back to probing when even that is denied. `canWrite` is reported so the
UI can explain what enrollment will later require, but no write is attempted and no write role is
granted. ADR 0001 stands.

**Observed state lives in its own container.** `observed-state` is partitioned by `/tenantId` and
holds APIs, operations, products, subscriptions, gateway users and groups, backends, named value
metadata, and policy analyses. It is disposable and rebuilt on every sync, unlike `desired-state`,
which is administrator-authored and audited. Documents use deterministic IDs so a re-sync upserts in
place, and anything absent from the new snapshot is swept.

**A failed read is never mistaken for a deletion.** Once a failed collection falls back to an empty
list it is indistinguishable from a genuinely empty one, so a snapshot records which entity types
MOSAIC could not read. Those types are exempt from the sweep and keep their previous documents until
a clean sync supersedes them. Otherwise a transient throttling response would turn "MOSAIC could not
read subscriptions" into "this gateway has no subscriptions", which for a governance control plane is
worse than an outright error.

**Observation never overwrites intent.** A sync re-reads the gateway before writing its results. If an
administrator removed the gateway while ARM was being read, the snapshot is discarded rather than
resurrecting the record; if they renamed or relabelled it, the observation fields are applied to the
current document rather than the copy captured when the sync started.

**Sync admission is claimed synchronously.** A gateway is added to an in-flight set before the first
`await` in `start_sync`. An `asyncio.Lock` cannot guard admission, because the run must be persisted
before the task starts and that await lets a second request pass between checking the lock and
acquiring it.

**Raw policy XML is never persisted and never rendered.** Policy documents are fetched, parsed in
memory, and reduced to a SHA-256 digest plus redacted semantic facets. The source markup is
discarded. This is both the product promise and a security property: policy documents routinely carry
inline credentials in `set-header` and backend authentication. Drift detection works off the digest,
so nothing is lost by not keeping the text. URLs are stripped of query and fragment before they enter
a facet or an observed record, because backend URLs commonly carry Azure Functions keys and storage
SAS tokens as query parameters.

The rule applies to policy MOSAIC *authors* as well as policy it reads. `POST /policies/preview`
returns facets and a digest; the generated XML is kept in process for a future apply phase but is
excluded from serialisation, so markup never crosses the API boundary into a browser at all.

**Unrecognised configuration is reported, not hidden.** Every policy element MOSAIC cannot interpret
becomes a facet marked *externally authored* and is counted by name. Implying that MOSAIC governs
configuration it cannot read would be worse than admitting the gap.

**Policy fragments are the future write boundary.** When MOSAIC starts writing, it will author named
`mosaic-*` policy fragments that customer policies include, rather than rewriting whole policy
documents. Onboarding inventories fragments now and flags which are MOSAIC-shaped, so the write phase
has a clean ownership seam and cannot clobber customer-authored policy.

**The bundled gateway seeds itself from the API.** The APIM that `azd` deploys is registered
automatically on first startup, in the background and idempotently, with a `system:bootstrap` audit
actor. An `azd` hook was rejected: it would need a token for MOSAIC's own API, which would mean
pre-authorising the Azure CLI against the API registration and widening the authentication surface
for a convenience. The UI also surfaces the detected gateway as a one-click suggestion if seeding did
not run or did not succeed.

## Consequences

- Administrators onboard any existing API Management service without redeploying MOSAIC.
- A missing role assignment produces an exact remediation instead of an empty inventory.
- MOSAIC cannot show a policy it does not understand, so the parser's coverage is a visible,
  measurable product surface rather than a hidden one.
- Because raw policy is not stored, any future feature that needs the original text must re-read it
  from Azure. That is the intended trade.
- Inventory sync is in-process and not durable across restarts. Runs orphaned by a restart are reaped
  to `failed` rather than left pending, and sync is idempotent and re-runnable.
- APIM users and groups are shown as gateway-local identities. Governance still anchors on Entra
  principals and subscriptions, so the two identity models must stay visibly distinct in the UI.
