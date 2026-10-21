# Cost-center inherited access: feasibility gate

**Experimental, not enabled in MOSAIC.** This is M0 of the cost-center-group design, not an
implemented automatic-access feature or a successful live APIM verification.

The proposed direction is a managed MOSAIC group per cost center, APIM-local membership data,
and one individually owned native APIM subscription per cost center/gateway. A dedicated product
would scope that key to the selected model APIs; API policy must still enforce exact models,
membership, attribution, limits, and the cost-center budget. Existing API-scoped/pool keys remain
unchanged. Redis and Entra directory writes are not part of this proposal.

## Run the offline probe

From the repository root:

```powershell
python -m scripts.cost_center_access_probe
```

This uses fictional UUIDs and the standard library only. It makes no network requests, reads
no credentials, changes no MOSAIC data, and does not create APIM products, subscriptions, policies,
or Entra groups. The report always says `feasibilityGate: NOT_PASSED`; local serialization and
reference-model checks cannot prove native gateway behavior or latency.

The default cases are 10, 100, 1,000, and 5,000 **effective/enrolled** members, not a proposal to
raise the current cost-center limit of 500 explicit references. One Entra reference can represent
many people. Token-based group access does not require an individual APIM subscription.

Generate an optional local bundle in a fresh directory:

```powershell
python -m scripts.cost_center_access_probe --output "$env:TEMP\mosaic-cost-center-probe"
```

The bundle contains a membership fragment, a diagnostic API policy, named-value fixture data, a
native-identifier contract, and a local C# reference host with expected decisions. No subscription
keys or real directory members are included. Existing artifact files are not overwritten. The
command does not apply the bundle.

On Windows, exercise the generated membership expression in local .NET, without APIM. Use a fresh
PowerShell process for each bundle so a previously loaded reference type cannot be reused:

```powershell
$bundle = Join-Path $env:TEMP "mosaic-cost-center-probe"
Add-Type -Path (Join-Path $bundle "reference-host.cs") -ErrorAction Stop
$cases = Get-Content (Join-Path $bundle "reference-cases.json") -Raw | ConvertFrom-Json
foreach ($case in $cases) {
    $actual = [MosaicCostCenterProbe]::Evaluate($case.owner, $case.unixTime)
    if ($actual -cne $case.expected) {
        throw "Local C# membership result disagrees with the reference fixture"
    }
}
```

The host supplies an owner variable and request timestamp, not native APIM context or credentials.
This can expose C# expression/reference-model disagreements; it cannot prove APIM's expression
sandbox, named-value substitution, native authentication, or propagation.

The focused Python suite also executes a smaller generated C# fixture when PowerShell 7 is
available; otherwise it explicitly skips that local .NET case:

```powershell
python -m pytest scripts\tests\test_cost_center_access_probe.py scripts\tests\test_verify_model_access.py
```

To explore cost centers sharing one offering and account for its existing policy:

```powershell
python -m scripts.cost_center_access_probe --population 5000 --centers 3 --existing-policy-bytes 65536
```

A conservative capacity failure returns a nonzero exit code. The combined-size calculation is an
offline upper-bound screen, not a statement about how APIM counts expanded fragments or a verified
aggregate API-policy limit. In particular, multiple sibling fragments are not one fragment.
Actual native resource quotas, existing resources, policies, tier and cloud must also be checked
before implementation.

The default 64-bucket layout reserves **129 named values per cost center/gateway**, even for a
small center: two slots per bucket plus the manifest. This is a candidate to evaluate, not a frozen
production layout. Resource counts are forecasts assuming every fixture member enrolls a key; the
real feature would issue keys only on demand, independent of model count.

Named-value writes are not the entire management-plane workload. The report separately forecasts
native subscription-state changes when every fixture member has enrolled a key. A single removal
needs two data writes plus one credential-state change. Removing the fixture's Entra reference at
5,000 owners changes 65 data values and affects 4,501 Graph-only credentials; the 499 direct members
remain eligible. Those credential operations still need bounded background reconciliation, not a
burst of thousands of concurrent ARM calls. The future reconciler must publish eligibility removals
without waiting behind that suspension backlog; native key state is not a substitute for local
membership enforcement.

Include a caller-supplied resource inventory for a published-tier headroom check:

```powershell
python -m scripts.cost_center_access_probe --population 5000 --tier standard `
    --existing-subscriptions 23000 --existing-products 20 --existing-named-values 4000
```

This hypothetical inventory leaves insufficient subscription headroom and returns nonzero. All
three existing counts are required together; they are never discovered from an Azure login.
Without an inventory, the report labels the omitted counts as unprovided and leaves tier capacity
unevaluated. Published fixtures cover Developer, Basic, Standard and Premium; actual instance
limits and cloud support still need independent verification.

## Recorded offline fixture evidence

These are reproducible serialization sizes for the v1 fixture with 64 buckets, not live APIM
measurements. Both slots are included in the expanded size; existing gateway policy is not.

| Enrolled owners | Largest named value (characters) | Largest decoded bucket (records) | Expanded fixture XML (bytes) |
| --- | --- | --- | --- |
| 10 | 1,101 | 1 | 11,500 |
| 100 | 1,101 | 4 | 15,732 |
| 1,000 | 1,101 | 25 | 56,492 |
| 5,000 | 2,219 | 97 | 237,836 |

The unexpanded fragment is 15,231 bytes. The 5,000-owner bundle's generated membership expression
also agreed with the Python reference for 10,003 local .NET cases, including evidence expiry,
principal-kind mismatch and malformed owner text. This is local language/format evidence only:
APIM-native context, sandbox execution, actual Graph paging, write/propagation effects, production
scope/budget enforcement and runtime latency remain **NOT RUN / NOT MEASURED**.

## What the fixture exercises

- Exact membership uses a SHA-256-selected bucket, not a probabilistic membership filter.
- Each record is a .NET-compatible 16-byte UUID plus eligibility/principal-kind flags. Data-only
  base64 and fixed-format metadata avoid inserting arbitrary labels into a policy expression.
- Every named value is bounded at 4,096 characters. The caller selects one bucket before decoding,
  rather than decoding a 5,000-person roster.
- Two slots per bucket and a compact generation/slot manifest stage changed data before publication.
  A single removal changes one inactive bucket and the manifest, not the policy source. A freshness
  renewal changes only the manifest.
- If a new manifest propagates before its data, the affected bucket returns an explicit invalid
  decision, not a stale allow. Other unchanged buckets remain usable. This is not an atomic update
  or an immediate-revocation guarantee.
- Direct membership remains usable when simulated Graph evidence expires. Group-derived eligibility
  requires unexpired evidence. Real Graph paging/verification is not implemented by this probe.
- These are enrolled-owner eligibility fixtures. The token-only path's validated Entra group-claim
  matching is not implemented here and must not be replaced by expanding every group member.
- The Python reader checks the format and update behavior. The optional local C# host exercises the
  membership expression, but neither reader executes an APIM gateway.
- A canonical subscription identifier carries the fictional owner, but owner recovery also requires
  the exact expected native product context. This is a candidate provisioning contract, not proof
  that real APIM populates that context in every authentication configuration.

The diagnostic API policy intentionally returns `200` with **diagnostic booleans and a local
decision**, never a model response or an authorization success. It does not validate Entra tokens,
forward to a model, enforce production authorization, or echo keys/tokens/owner IDs. Never install
it on a model API or interpret its HTTP status as a passed authorization test.

## Independent Graph verification prerequisite

The read-only directory adapter has a separate internal `verify_group_members` contract for a
referenced security group and an explicit cohort of candidate owners. Unlike the intentionally
bounded directory preview, it reads every transitive-members page, including pages after every
candidate has already matched. It requests only IDs, at most 999 per page, and retains matching
candidate IDs rather than building the full directory roster in memory.

Successful immutable evidence includes the configured tenant, normalized group ID, exact candidate
scope, matched members, and the timestamp **before the first read**. Every verification request
uses a tenant-bound, app-only credential. Missing/non-security groups, invalid IDs or collections,
malformed/foreign/unexpected paging links, repeated cursors, redirects, failed pages, credential
failures, and timeouts cannot produce successful partial evidence. The entire read has a maximum
60-second deadline, including authentication and service-retry waits; callers can shorten it.
A `429` stops the read immediately and surfaces bounded retry guidance instead of retrying early
or renewing evidence.

Local mock coverage includes 6,001 transitive members and 5,000 candidate owners, a match beyond
the old 5,000-member fallback, nested/id-only fake members, late failures, changing token tenants,
Government endpoint paging, cancellation, and expired read budgets. The existing application
membership-check fallback also uses this complete read instead of a 5,000-member preview.
The ordinary directory-preview API remains bounded.

These are **local adapter checks, not a live Graph tenant verification**. Entra membership can
change while pages are read; the result is not an atomic directory snapshot or an immediate
revocation guarantee. This prerequisite does not implement a membership worker, persisted
evidence refresh, APIM freshness enforcement, or inherited access. It does not pass the APIM
feasibility gate.

## Shared functional directory check

An administrator can explicitly check one existing **Entra security-group object ID**, not a
MOSAIC group record ID:

```http
POST /api/v1/directory/group-check
Content-Type: application/json

{"groupObjectId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
```

The example is fictional. The action requires an administrator in the deployment's configured
tenant and a directory integration. It uses the complete verification contract above, including
the whole-read deadline, and returns diagnostic status without member IDs. It neither changes
Entra/MOSAIC directory records nor creates grants, keys, APIM configuration, or background work.
Invalid group GUIDs return `422`; a disabled directory integration returns `409`.
In the console, open **Identity > Entra security groups**, select a recorded group, and choose
**Verify complete group read**. The existing bounded member preview remains separate. No group is
chosen or verified automatically, and there is no new portal control.

`GET /api/v1/directory/status` includes the shared `membershipCheck` observation. Ordinary status
reads do not call Graph. `includePermissions=true` still inspects app-token consent only; it does
not start a functional group read. Both endpoints use the API's private, no-store response policy.

**Settings > Runtime configuration** shows the same diagnostic separately from token consent
and links to group selection in Identity. **Refresh group-check status** reads the observation
without starting a verification. While a read is in progress, the UI polls status only; freshness
expiry and retry boundaries also refresh status without renewing evidence or retrying Graph.
Another group's observation is explicitly labeled, failures remain visible, and a throttle
cooldown disables early manual verification.

| Result | Meaning |
| --- | --- |
| `disabled` / `notChecked` | No directory integration, or no explicit check in this process yet. |
| `checking` | A complete read is in progress. Repeating its group returns HTTP `202` and the same progress; a different group returns `409` instead of queueing more work. |
| `ready` / `stale` | The chosen group's complete read succeeded. Diagnostic freshness lasts five minutes from the oldest read-start timestamp, not from completion or a later status read. Expiry or a clock rollback reports `stale`. |
| `forbidden` / `throttled` / `unavailable` | The latest read failed. The action preserves its real error status, such as `403`, `429`, or `502`, rather than returning successful readiness. |
| `cancelled` | The read was cancelled; it is not successful evidence and its lock is released. |

Status records the latest `groupObjectId`, `checkedAt`, `completedAt`, and `message`, plus
`lastSuccessfulAt` and its **own** `lastSuccessfulGroupObjectId`. A failed check of another group
does not relabel an earlier group's success. Failures clear `freshUntil`. Throttling records
`retryAt`, returns `Retry-After`, and prevents early checks in this tenant without contacting Graph
or refreshing the observation.

This is **process-local, group-scoped operational diagnostic status**, not persisted worker
health, an authorization cache, owner eligibility, or proof of access to every group. It resets
to `notChecked` after a process restart; different API processes can have different observations.
Its five-minute window is not the future inherited-key eligibility TTL. The Identity/settings
controls are operational diagnostics, not inherited-key or authorization controls. Durable
reconciliation consumers remain pending. Local mocks exercise the contract; no live tenant check
or APIM acceptance is claimed.

## Live evidence required before APIM integration

The independent [managed-group foundation](cost-centers.md#experimental-managed-groups) can be
enabled without passing this gate: it only links MOSAIC records and shares the existing explicit
roster. It does not implement inherited authorization, projection reconciliation, or native keys.
Its availability is not evidence that the APIM contract below works.

Live testing requires an explicitly authorized APIM target and scoped test identities. Do not
infer permission from an Azure CLI login, a target manifest, or an existing model publication.

On an explicitly approved isolated target, complete the native-context, exact product/API/pool-model
scope, token/key owner-match, suspended/deleted/wrong-scope key, policy-expression and propagation
checks. Cover token-only, key-only and combined credentials with the real subscription-required
settings. An existing native key must not authorize a member whose local eligibility expired.

Measure gateway-only latency, streaming time-to-first-token, configuration write pressure, error
rates, resource counts, and mixed-version behavior under steady traffic and membership bursts.
The diagnostic bundle alone cannot measure these or replace real authorization/budget verification.
Do not use production traffic, create test directory accounts, or issue 5,000 real keys by implication.

If the native context or supported capacity does not meet the proposed contract, revise the design
before wiring it into MOSAIC. Do not silently fall back to Redis, shared keys, all-APIs subscription
scope, Graph calls from the gateway, or per-member model-policy generation.

The existing [cost-center guide](cost-centers.md) describes the behavior actually available today.
Related live-verification boundaries are in the [E2E runbook](e2e/runbook.md).

References: [APIM policy expressions and allowed types][expressions],
[named values][named-values], [policy fragments][fragments], [service limits][limits], and
[native subscription scope and validation][subscriptions].

[expressions]: https://learn.microsoft.com/en-us/azure/api-management/api-management-policy-expressions
[named-values]: https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-properties
[fragments]: https://learn.microsoft.com/en-us/azure/api-management/policy-fragments
[limits]: https://learn.microsoft.com/en-us/azure/api-management/service-limits
[subscriptions]: https://learn.microsoft.com/en-us/azure/api-management/api-management-subscriptions
