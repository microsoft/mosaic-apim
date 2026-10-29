# Federal demonstration estate

The opt-in federal estate is an unofficial, persistent customer demonstration, not an agency
deployment, government-cloud deployment, or compliance certification. It uses fictional people
and public/synthetic reference material. Its Production environment is a demo classification:
Developer-SKU API Management has no production SLA.

## Inventory

One MOSAIC control plane governs three separate gateways, with a core resource group and one
resource group per environment. Five usable AI resources provide Luna in each environment and
Terra/GPT-5.1 alternatives in Production. Existing reused AI accounts remain in their original
groups, keep their original workloads, and are not moved, resized, opened to the public, or
subjected to key resets.

Eight MCP designs have separate Development, Staging, and Production replicas: 24 services.
Five manual Production workload jobs have distinct managed identities, grants, and attribution.
No traffic-generation schedule is enabled.

| Manual application | Cost center | Production model | Public evidence |
| --- | --- | --- | --- |
| NASA Mission Briefing (`nasa-brief`) | NASA-MISSION | GPT-5.6 Terra | Historical Artemis I media metadata |
| FAA Airspace Briefing (`faa-airspace`) | FAA-OPS | GPT-5.6 Luna | Dated airport reference, never operational guidance |
| FAA Flight Standards Review (`faa-standards`) | FAA-RND | GPT-5.6 Luna | Dated Title 14 educational reference |
| DOE Lab Research (`doe-research`) | DOE-LABS | GPT-5.1 | OSTI research-discovery metadata, not full papers |
| USDA Field Insights (`usda-field`) | USDA-DATA | GPT-5.6 Luna | Published national crop estimates, not farm-specific advice |

Each ordinary run retrieves real public snapshot evidence through its published MCP route and
makes one actual model request through Production APIM. It keeps only status/counts and answer
length in stdout, never the prompt, answer, credential, or endpoint. Reasoning is disabled for
these short briefs so the unchanged 256-token output cap produces answer text.

| Agency | MCP services |
| --- | --- |
| DOE | Energy Data Desk; Lab Research Library |
| FAA | Airport Reference; Regulatory Reference |
| USDA | Crop Insights; Conservation Guide |
| NASA | Mission Archive; Earthdata Explorer |

Reference data must identify its source, vintage, and snapshot/live mode. A failed live lookup
must not silently become a successful snapshot lookup. FAA examples are not flight-planning,
navigation, operational, or safety-critical guidance.

## Teams and fictional people

| Cost center | People | Environments | Monthly model budget |
| --- | --- | --- | ---: |
| General | All 20 | All three | USD 25 |
| NASA Mission Systems (`NASA-MISSION`) | Daniel Brooks, Miguel Santos, Ethan Park, Aisha Carter, Priya Shah | All three | USD 100 |
| FAA Airspace Operations (`FAA-OPS`) | James Wilson, Carlos Rivera, Marcus Bennett | Production | USD 150 |
| FAA Flight Systems Research (`FAA-RND`) | Carlos Rivera, Emily Chen, Fatima Hassan | All three | USD 75 |
| DOE National Lab Research (`DOE-LABS`) | Michael Reed, David Nguyen, Omar Rahman, Sofia Martinez, Grace Kim | All three | USD 150 |
| USDA Agricultural Insights (`USDA-DATA`) | William Turner, Andre Johnson, Noah Patel, Sarah Thompson, Elena Garcia | All three | USD 100 |

UPNs are `mosaic.demo.<agency>.<first>.<last>@<verified-domain>`. They are real, enabled cloud-only
accounts marked as demo identities, with the portal User role, not MOSAIC Admin or Azure RBAC.
Each agency cohort has three male and two female fictional personas. No name represents an
actual agency employee.

The initial password for each account is unique and stored only in an operator-only onboarding
Key Vault, not a vault that MOSAIC or APIM can read. Retrieve the selected account's initial
password privately, change it on first sign-in, and complete tenant-required MFA. Entra cannot
reveal current passwords later; the vault's initial password stops being valid after it changes.
Rerunning setup never resets an existing account's password.

The remaining accounts can stay unused. Enabled does not mean first sign-in or MFA has been
completed. Do not weaken Conditional Access/security defaults, or claim all accounts have passed
portal testing. Creating a UPN does not create an Exchange mailbox or purchase a license.

## Operator commands

Run from the repository root. Start with a private target file and a persistent ownership ledger:

```powershell
Copy-Item scripts\federal_demo\target.example.json federal-target.local.json
# Replace the example tenant, subscription, and verified domain before executing.
python -m scripts.federal_demo plan --target federal-target.local.json --state federal-state.local.json
python -m scripts.federal_demo preflight --target federal-target.local.json --state federal-state.local.json
python -m scripts.federal_demo prices --target federal-target.local.json --state federal-state.local.json
```

These commands perform no cloud writes. Preflight refuses a different Azure CLI tenant,
subscription, or cloud, an unverified UPN domain, or insufficient native deployment quota.
`prices` resolves exact public USD input/output/cache meters; it does not claim that a gateway
has proven its pricing envelope or that a budget has blocked calls.

Review inherited organizational policies before provisioning. A management-group Modify policy
can replace `publicNetworkAccess: Enabled` with `Disabled` even when ARM reports successful
deployment. A healthy API process is not proof that its Cosmos backend is reachable: check
`/readyz` and actual vault access separately. `ForbiddenByConnection` is a network/policy denial,
not evidence that the operator needs broader secret permissions. Do not add bypass tags,
exemptions, or public-access updates to defeat such a policy automatically. Obtain an explicit
policy-owner decision or review a private-network design, including the operator's secure path
to the onboarding vault, before creating account credentials.

For App Service quota constraints, `appServiceLocation` may place only the B1 plan and its three
sites in another reviewed US region. The remaining core resources, gateways, models and MCP
hosting retain the profile's East US 2 location. The live regional quota API is
`Microsoft.Web/locations/<region>/usages`; model quota and generic VM quota do not establish
App Service B1 availability.

The demo profile sets MCP discovery's read timeout to 60 seconds for consumption-hosted cold
starts; the normal deployment keeps its 20-second default. This changes neither authentication
nor scale-to-zero. Before a demonstration, preflight/synchronize the protected endpoints and
confirm authorized discovery, rather than treating an anonymous refusal as a warm-up.

Provisioning must use the opt-in `infra\federal-demo` entry point, not an unmodified `azd up`
that would create a fourth, co-located APIM. Review current costs and ARM what-if first. Keep
actual parameter files, object/resource IDs, private bindings, and the ledger outside Git.
The normal single-environment MOSAIC deployment retains its own defaults.

`prepare --apply` reuses the existing Entra bootstrap and writes private ARM parameters.
`what-if` records the actual plan and refuses deletions/out-of-estate writes. `bind-core --apply`
can bind ready core outputs while APIM is still activating; `bind --apply` binds the completed
estate. Azure CLI may camel-case acronym output names; the binding code normalizes output keys.
Persist actual digest-pinned `apiPlaceholderImage`, `webPlaceholderImage`, and
`portalPlaceholderImage` overrides on every later root deployment so the real applications
are not replaced with bootstrap images. After root succeeds, use the separate `runtime-only.bicep`
and `inference-only.bicep` stages, rather than re-PUTing APIM, AI accounts, or the core sites.
The [deployment contract](../infra/federal-demo/deployment-contract.json) defines their exact
private parameters and outputs.

If a policy owner publishes an explicitly eligible, time-limited per-resource self-service
exception, record the original authorization and fixed expiry privately. Preserve its tags only
on the reviewed backing resources; never spread them to resource groups or subscriptions,
renew them automatically, or exempt cost controls. A submitted longer-term request is not an
approval. Plan the private-network transition or obtain approval before the temporary window ends.

If a previously selected reused account disappears externally, stop rather than recreating or
modifying its resource group. The optional `provisionProductionLuna` mode creates a fourth
owned account instead, retaining five usable endpoints with the remaining reused Terra account.
Select its permitted SKU only after reviewing fresh native quota; do not reclaim other workloads.

On Windows, a local Azure CLI Unicode log-streaming failure does not establish that an ACR build
failed. Inspect the existing run status and pushed manifest digest before retrying; do not
rebuild a successful remote image just to recover terminal output.

After provisioning, extend the private target with `onboardingVault`,
`apiServicePrincipalId`, and `userRoleId` from the actual core/Entra outputs:

```powershell
python -m scripts.federal_demo directory --target federal-target.local.json --state federal-state.local.json --apply
```

Directory setup is operator-run Graph administration. MOSAIC's API itself retains read-only Graph
permissions. Any account/group name collision without the estate's ownership marker stops setup
rather than adopting another person's object.

Model seeding additionally needs `mosaicApiUrl`, `apiAppClientId`, `gateways` (an environment-to-ARM-ID
map), and five `endpoints` bindings (`environment`, `resourceId`, `name`, `deploymentName`).
Use an explicitly consented delegated operator session with the MOSAIC Admin role; an ARM token
does not authorize the MOSAIC API.

```powershell
python -m scripts.federal_demo seed-models --target federal-target.local.json --state federal-state.local.json --apply
```

Seeding calls the audited MOSAIC APIs, synchronizes inventory, records grants, and applies exact
saved gateway plans. It never writes Cosmos documents directly. Failed or uncertain applies stop
for diagnosis/recovery instead of being retried as if no write happened.

Deploy the protected runtime initially with `backendAuthVerified=false`. Then run
`verify-backends --apply` and `verify-wrong-client --apply`. The second probe obtains valid
right-audience tokens as an unapproved application, requires all 24 hosts to return HTTP 403,
and removes its temporary backend roles before recording success. Network failures and HTTP 401
are not valid-token authorization proof; a 23-of-24 result does not pass.

Only after that proof, use `authorize-runtime --apply`, `prepare-inference --apply`, and the
inference-only deployment to grant the five exact gateway/account inference pairs. Use
`prepare-verified-runtime --apply` and the runtime-only deployment to enable helpers. Neither
stage grants the workload identities direct model-account access.

`seed-tools --apply` synchronizes and publishes the 24 exact protected endpoints, creates agency
MCP grants, and binds each summary server's independent model identity. A failed initial
registration gets one explicit preflight before synchronization, to accommodate cold starts;
if discovery remains refused, onboarding stops with the saved access verdict. It never assumes
that a deployed container or an anonymous HTTP 401 proves successful authorized discovery.
`seed-jobs --apply` applies the five Production applications' separate model and MCP grants.
Enable and inspect gateway telemetry after all publications exist, keeping all request/response
body logging at zero bytes.

For the bounded real-model check, first configure `operator-model-consent --apply`, then run:

```powershell
python -m scripts.federal_demo prepare-model-verification --target federal-target.local.json --state federal-state.local.json --apply
python -m scripts.federal_demo verify-models --target federal-target.local.json --state federal-state.local.json --apply --invoke-paid
python -m scripts.federal_demo cleanup-model-verification --target federal-target.local.json --state federal-state.local.json --apply
```

Use the installed workspace Python environment for these commands. Verification grants belong
only to the real deploying operator, never impersonated demo people. The verifier reserves at
most USD 0.25 cumulatively, makes at most ten gateway requests per attempt with a 256-token
output cap, and requires real native metering and pre-backend oversized-prompt denial. Failed
or uncertain requests retain their spend reservation; never clear the ledger to retry.
Successful verification installs deployment-scoped prices and the six budgets before cleanup
reapplies the publications. Retire temporary grants and delegated model consent even after a
failed attempt; inspect uncertain writes before resuming. Applies wait for the exact reviewed
run to succeed, not merely an already-applied publication while its new run is still finishing.
Disabled applied snapshots retain historical principal references: cleanup preserves an
ungranted operator record when the API requires it for that history, rather than unpublishing
live models to erase it. No enabled temporary grant or delegated model consent is kept.

## Access and cost boundaries

Team membership gives token-based Luna access through supported Entra group grants. The baseline
does not issue personal keys. Other model access is separately requested, approved/denied, and
applied. Group changes take effect through fresh tokens, not instantaneous gateway Graph reads.

FAA-OPS has Production-only grants; this is a configured grant/approval boundary, not a new
product-level restriction on which cost center a future administrator could choose.
Carlos's two FAA memberships demonstrate the `x-mosaic-cost-center` header. General remains a
small legitimate fallback grant: blocking one team does not block a different authorized center.

The six monthly allowances total USD 600 in model use, not hosting or unrelated existing
workloads. Alerts are at 80% and 100%, with cost-center blocking at 100%. Dollar enforcement
follows delayed gateway logs/rollups and propagation, so it is not an exact cash cutoff.
Token/call pools are per publication/gateway, whereas dollar budgets aggregate gateways.
An organization budget can warn but cannot block.

Do not install short-context prices as universal model aliases: these models also have
long-context/cache-write meters. Scope prices to verified deployments and prove that supported
text operations enforce their bounded pricing envelope before accepting budget readiness.
Unpriced usage stays visible and must never be treated as zero.

MCP summary tools call models as their own application identity through the same gateway.
Their model grants, runtime app roles, and cost center are independent of the person's MCP grant.
Opaque on-behalf-of references provide supported attribution, not delegated model authorization.

## Verification and lifecycle

Use the existing model/MCP verifier and human-in-the-loop portal patterns for live evidence.
Exercise actual workload identities, protected-backend refusals, group access, pooled quotas,
cross-environment denials, and a reversible budget block/unblock using bounded real usage.
Do not fabricate logs or spend, and do not exhaust USD 600 to demonstrate a budget threshold.
Record pending human sign-in gates separately.

An operator validation can use an existing workload identity to prove that both its model and
MCP calls to Development are refused, without granting that identity Development access.
Exercise an MCP summary separately: the MCP server must call as its own model-enabled managed
identity, not borrow the caller's model authorization. An application-driven check does not
count as a completed human portal or human on-behalf-of journey.

For a budget demonstration, first wait for real priced usage to reach the rollups. Temporarily
lower only the selected center's allowance, confirm its key appears in all three actual gateway
blocked lists and that model/MCP calls return budget-specific HTTP 403 responses, then restore
the approved amount in a `finally` cleanup. Confirm every gateway clears the key and an ordinary
workload succeeds again. Keep a cumulative small spend reservation for validation; never remove
that reservation after an uncertain request or manufacture usage to cross a threshold.

To pause demonstration traffic, do not start manual jobs or invoke tools; consumption-hosted
MCP replicas scale to zero. This does not stop fixed hosting charges for APIM or App Service.
Before resuming, check API readiness, the original exception deadline, actual gateway telemetry,
budget states, and authenticated MCP discovery. Removing resources or stopping shared services
requires a separate ownership-scoped decision, not an automatic pause or test teardown.

Email and Teams remain disabled until a real sender and authorized destinations are selected.
GCP requires a provider compatibility assessment; generic OpenAI compatibility is not publishing
support. Bedrock uses the supported Anthropic Messages pool path after separate credential and
cross-cloud data-transfer approval.

Cleanup requires a separately reviewed plan and the ownership ledger. Never use broad
subscription cleanup, delete reused endpoint groups, or run the E2E teardown suite against
this persistent estate. README screenshots must still come only from the screenshot script's
fictional local estate, never this actual tenant.
