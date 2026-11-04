# Development and quality checks

[Documentation](README.md) | [MOSAIC](../README.md)

- [Prerequisites](#prerequisites)
- [Local development](#local-development)
- [Cost-center development and old MVP data](#cost-center-development-and-old-mvp-data)
- [Quality checks](#quality-checks)
- [Merging into `main`](#merging-into-main)

## Prerequisites

For an offline local run, Python, uv, Node.js, and npm are sufficient; Azure identity and
subscription permissions below apply when connecting to Azure or [deploying](deployment.md).

- Azure subscription where the deployer can create resources and role assignments
- Microsoft Entra role capable of managing app registrations and service principals (Application
  Administrator or broader), assigning the initial app role, and granting the model client's
  tenant-wide consent. Agent identity and security-group lookup also need a privileged
  administrator to consent the API managed identity's Microsoft Graph application permissions:
  `User.ReadBasic.All`, `GroupMember.Read.All` and `AgentIdentity.Read.All`. Application and
  managed-identity search optionally uses `Application.Read.All`; person thumbnails optionally
  use separately consented `ProfilePhoto.Read.All`. Neither optional permission is required for
  onboarding people or agents. See [Optional Entra person photos](identity-photos.md).
  Notification routing to all actual administrators additionally requires `Application.Read.All`
  and complete direct app-role/group-member discovery; signed-in principal inventory is not a
  substitute. See [Notification routing](notification-routing.md).
- Cost-center group creation/member editing optionally needs additional **API-identity** consent:
  `Group.Create` for creation, `GroupMember.ReadWrite.All` for user membership writes, and
  separately consented `Application.ReadWrite.All` to add service principals. None is required
  for the directory-read baseline or APIM runtime checks. See
  [optional group-management setup](deployment.md#optional-cost-center-group-management).
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 24 and npm
- Docker for local container validation

The reference development target is region `eastus2` with environment name `mosaic-dev`. Supply
your own subscription when deploying.

## Local development

From the repository root, install both applications' dependencies:

```powershell
uv sync --all-packages --group dev
npm ci --prefix apps\web
npm ci --prefix apps\portal
```

Run the API from the repository root with the opt-in local modes:

```powershell
$env:MOSAIC_ENVIRONMENT = "local"
$env:MOSAIC_AUTH_MODE = "local"
$env:MOSAIC_REPOSITORY_BACKEND = "memory"
$env:MOSAIC_TENANT_ID = "local-development"
uv run mosaic-api
```

Local authentication grants both the `Admin` and `User` app roles by default. Set
`MOSAIC_LOCAL_ROLES` to narrow it — `'["User"]'` simulates an end user who must not reach an
administrative route. The setting is rejected outside local and test environments.

Directory lookup is available automatically with Entra authentication; no environment-variable
switch is required or supported. The API reads Microsoft Graph through its Azure credential,
using its managed identity in Azure, and still needs tenant-administrator consent for the
permissions listed above. Local authentication has no live directory lookup, so administrators
use manual entry instead.

Group-claim enforcement has its own setting:

| Setting | Default | Purpose |
| --- | --- | --- |
| `MOSAIC_ENTRA_GROUP_CLAIMS` | `true` | Records whether bootstrap configures `groupMembershipClaims: SecurityGroup` on the runtime/API registrations. Without usable group claims, custom-center Automatic and individual token paths cannot prove membership; a direct token grant is not a bypass. |

Usage figures have their own settings. See [Usage analytics](usage-analytics.md).

| Setting | Default | Purpose |
| --- | --- | --- |
| `MOSAIC_USAGE_SOURCE` | `auto` | `rollups` reads usage rolled up from gateway logs. `simulated` makes the portal's figures up from the caller's real grants, and Azure refuses it. `auto` means `rollups` in Azure and `simulated` elsewhere. |
| `MOSAIC_USAGE_ROLLUP_ENABLED` | `true` | Runs the job that rolls gateway logs up into Cosmos. |
| `MOSAIC_USAGE_ROLLUP_INTERVAL_SECONDS` | `900` | How often the job runs, from 60 to 86,400 seconds. |
| `MOSAIC_USAGE_ROLLUP_RETENTION_DAYS` | `400` | How long daily figures are kept, from 62 to 3,650 days. Monthly figures are kept for good. |
| `MOSAIC_USAGE_ROLLUP_BACKFILL_MAX_DAYS` | `90` | How far back a gateway's first rollup, a catch-up after downtime, and a backfill without `days` read, from 1 to 730 days. |
| `MOSAIC_LOG_ANALYTICS_ENDPOINT` | `https://api.loganalytics.azure.com` | The host/default Log Analytics query endpoint. External resources use their connection's cloud profile; this setting does not redirect Government queries or select their credentials. |

Budgets and their email have these. See [Budgets and alerts](budgets-and-alerts.md#settings).

| Setting | Default | Purpose |
| --- | --- | --- |
| `MOSAIC_BUDGETS_ENABLED` | `true` | Runs the background check that judges budgets, emails at thresholds, and blocks cost centers. It runs only where usage comes from rollups. Off, budgets are kept but never judged. |
| `MOSAIC_BUDGET_INTERVAL_SECONDS` | `900` | How often every budget is checked, from 60 to 86,400 seconds. |
| `MOSAIC_BUDGET_FAST_INTERVAL_SECONDS` | `300` | How often a budget at 90% or more, with a threshold or a block to come, is checked, from 60 to 3,600 seconds. |

The Graph sender mailbox and separate budget-email switch are saved in
**Settings > Notifications > Email**, not deployment environment variables. The removed
`MOSAIC_DEPLOY_EMAIL` and `MOSAIC_EMAIL_SUGGESTED_*` settings no longer provision or configure email.

Approval and notification worker settings, channel prerequisites, and delivery semantics are
documented in [Approvals and notifications](approvals-and-notifications.md#hosting-settings).
In-app notifications default on; general email, Teams, and recurring summaries remain opt-in.
Saved configuration is not proof that a worker stays awake or a provider delivered a message.

Logging has one setting.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MOSAIC_LOG_LEVEL` | `INFO` | The least severe level the API logs, to stdout and, when `MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING` is set as `azd` sets it, to Application Insights. Libraries log at this level too, but the loggers for the Azure SDK's HTTP calls and the Azure Monitor exporter's uploads never go below `WARNING`. |

With `MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING` set, the API also records each request it
serves in Application Insights, as a row in `AppRequests` named for its method and route, such as
`GET /api/v1/principals/{principal_id}`, with its status code and duration. Its URL keeps the
query's parameter names, but each value is recorded as `REDACTED`, so the text someone types into
the directory search isn't kept, and a query that can't be read that way is left out. If redaction
fails, the request is recorded without its URL and the API logs a `request_query_redaction_failed`
warning. The health probes `/healthz` and `/readyz` aren't recorded, because App Service's health
check and the deployment's smoke checks call them often enough to bury the rest. Nor is the root,
`/`, which App Service's Always On pings every five minutes: it answers `GET` and `HEAD` with an
empty `200` that needs no sign-in. A request to any other path is recorded, even one that gets
`404`. No header or body of a request or response is recorded, so no bearer token or API key
reaches Application Insights; don't set OpenTelemetry's
`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` variables, which would record the headers they name.
Without the connection string, nothing is instrumented.

In a second terminal, from the repository root:

```powershell
Set-Location apps\web
npm run dev
```

For the portal, use a third terminal from the repository root:

```powershell
Set-Location apps\portal
npm run dev
```

The console serves on `http://localhost:5173` and the portal on `http://localhost:5174`.
The console's runtime browser configuration is described in the [web application reference](../apps/web/README.md).

`apps/web/public/config.js` selects local auth and `http://localhost:8000`. Do not deploy that file
as configuration: the web container regenerates it from App Service environment variables on
startup.

## Cost-center development and old MVP data

Custom centers use real Entra membership in connected deployments, through a separately consented
group-management adapter rather than the read-only lookup adapter. Local/demo/test runs must use
fictional directory and group-management doubles for creation, direct-member operations, and
complete eligibility verification. Local manual principal entry is not permission to pretend that
a local roster edit succeeded in Entra.

The direct member-list adapter uses Graph beta to include service principals omitted by v1.0.
That preview listing dependency is not supported for production use and is distinct from APIM's
stable JWT group-claim authorization. Keep beta-list paging and failure cases in fictional tests;
neither they nor policy tests prove live interoperability. See the
[listing limitation](cost-centers.md#members-and-who-may-charge-a-cost-center).

Fixtures should make the center's verified group, exact-resource modes, membership capabilities,
directory-operation state, and per-resource apply state explicit. Include setup-required centers,
read-only verified attachments, successful writes, denied/throttled operations, replication delay,
unknown create outcomes, inherited membership, and key-revocation-pending cases. Keep Automatic
inheritance distinct from personal entitlements; adding a fictional member must not create
per-person automatic grants or change the Automatic-only policy hash.

Keep General's stable built-in exemption in fixtures. Do not use a custom center's name, code,
tenant default, or personal default to bypass membership. Pool-model fixtures must retain both
model identity and pool scope; MCP and model grants remain independent. Government offerings
stay out of the new workflow.

Earlier MVP records based on internal managed groups are not migrated automatically. Explicitly
reconfigure/recreate custom centers and seed a verified fictional binding when resetting a local
estate. Never reinterpret a local group ID as an Entra object ID, silently copy old members to
Entra, broaden individual grants, delete existing data, or redirect charges to General. This is
an MVP reconfiguration boundary, not a production migration/upgrade wizard.

Run focused domain, directory, request, policy, and UI tests with doubles before broader checks.
Do not grant live consent or mutate a tenant merely to make local tests pass. Mocked Graph results,
policy snapshots, and fictional screenshots do not prove live Entra/APIM interoperability or
production scale. Record separately authorized live evidence, and say when it was unavailable.
See [cost centers](cost-centers.md), [verification](verification.md), and
[ADR 0030](adr/0030-entra-backed-cost-centers.md).

## Quality checks

Run from the repository root after installing the application dependencies:

```powershell
uv run ruff check apps/api e2e/mcp-servers
uv run mypy apps/api/src
uv run pytest apps/api/tests

Set-Location apps\web
npm run typecheck
npm run lint
npm run test
npm run build

Set-Location ..\portal
npm run typecheck
npm run lint
npm run test
npm run build

Set-Location ..\..\e2e
npm ci
npm run test:unit
npm run typecheck
npm run lint
npx playwright install chromium
npm run test:offline
Set-Location ..

az bicep build --file infra\main.bicep
az bicep build --file e2e\mcp-servers\deploy\main.bicep
uv run python -m unittest discover -s scripts/tests -t .
uv run ruff check scripts/federal_demo `
  scripts/tests/test_federal_demo.py scripts/tests/test_federal_model_verifier.py `
  scripts/tests/test_federal_access_lifecycle.py scripts/tests/test_federal_job_cleanup.py
az bicep build --file infra\federal-demo\main.bicep
az bicep build --file infra\federal-demo\runtime-only.bicep
az bicep build --file infra\federal-demo\inference-only.bicep
python -m unittest discover -s infra\federal-demo\tests -v
```

[CI](../.github/workflows/ci.yml) runs these checks on every pull request to `main` and every push to
it. It additionally runs the MCP fixture servers/deployment kit's isolated pinned-dependency tests
and an M-protected Docker smoke check; their commands live in the
[MCP fixture guide](../e2e/mcp-servers/README.md). The E2E browser checks use mocked traffic and
temporary contexts, not live fixtures or persona profiles; CI installs Chromium's Linux system
dependencies as well.

The [federal demo runtime](../demo/federal/README.md#run-and-validate) has its own pinned environment
and isolated test/lint commands. Its CI job does not make live directory, gateway, or model calls.

## Merging into `main`

Changes reach `main` only through a pull request. Two rulesets protect the branch:

- **Protect main** applies to everyone, administrators included. A pull request must pass the
  `CI result` check and the Microsoft CLA check (`license/cla`), be up to date with `main`, and
  have every review conversation resolved. It merges by squash or rebase, so the history stays
  linear. Nobody can force-push to `main` or delete it. Copilot reviews each new pull request.
- **Require review on main** adds one approval from someone with write access other than the last
  person to push. A new push dismisses earlier approvals. [Code owners](../.github/CODEOWNERS) are
  asked to review each pull request. Repository administrators can bypass this ruleset, and only
  this one, when merging a pull request. That lets a sole maintainer merge their own work, and
  GitHub records each bypass.

CodeQL scans `main` on every push and weekly, and also scans pull requests opened from branches in
this repository. It doesn't gate merging, because it doesn't scan pull requests from forks.
