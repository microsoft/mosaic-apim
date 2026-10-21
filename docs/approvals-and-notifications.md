# Approvals and notifications

MOSAIC separates an approval, gateway enforcement, and communication about either. An approval
creates desired grant intent. An administrator still reviews and applies the resource's gateway
plan before the new access is usable. A sent notification does not change that boundary.

## Approve several requests

Select explicit requests in **Entitlements**, then review the batch. A submission contains at
most 100 requests; it is not a live "approve everything matching this filter" query. Review each
request's resource, current environment, cost center, effective limits, and blockers.

Execution validates every request again. One request's conflict does not undo another's approval.
Keep successful results and retry only unresolved items. Repeating an already approved request
converges on its existing winning grant; it must not create another grant or decision.

An environment move needs explicit human confirmation. Requests that were withdrawn, denied,
changed, or made ineligible must be reviewed, not silently replaced with a different scope.

## Approval policies

Policies are explicit administrator-authored rules, disabled by default. They combine structured
environment/resource/cost-center conditions, a bounded grant template, minimum request age, and
weekly local-time approval windows. Preview a rule against pending requests before enabling it.
The persistent evaluator starts paused. After verifying hosting and reviewing the rules, an
administrator must explicitly resume it; the environment switch never resumes it automatically.

- Choose an IANA time zone, such as `Europe/London`; the default is `UTC`. Minimum waiting time
  is elapsed time from the original UTC request timestamp, not a local clock calculation.
- Production requires explicit production opt-in. Unclassified resources, environment moves,
  unavailable eligibility evidence, and incompatible matching policies remain manual.
- A missed window does not authorize approval outside it. Disabling or revising a rule invalidates
  older evaluations. Pausing the evaluator stops future evaluations, not already committed grants.
- The decision records the policy and revision and attributes the system evaluator. It never
  impersonates the rule's author or applies a gateway plan.

## AI approval assistance

AI supplies a recommendation, not an approval or denial. An administrator chooses an existing
MOSAIC-governed model and configures MOSAIC's dedicated caller grant and cost center. Model calls
pass through that model's APIM endpoint and its existing authorization, quotas, and budget.
There is no direct-backend bypass and no reuse of a requester's runtime credential.

Use the setup checks to verify the caller identity and application role, grant, cost-center
membership, required cost-center header, applied model plan, supported operation, and response
contract. An explicit model test is an operator action with possible model cost; opening Settings
does not invoke a model. Missing prerequisites leave human review available.

Recommendations show their model, generation time, evidence, uncertainty, and deterministic
blockers. Request justification is untrusted input. A recommendation cannot invoke tools, change
limits, send messages, create a policy, or mutate a decision. Changed request/context/settings
invalidate older recommendations.

## Choose personal notifications

Administrators and portal users have separate preferences, even when the same person has both
roles. Configure request changes, access changes, operational issues where authorized, usage and
budgets, and integration health independently.

In-app notifications start on. Email, private Teams, and recurring summaries start off. For each
available external channel, choose off, immediate, or an explicitly scheduled digest. Set the
time zone, delivery time, quiet hours, enabled summary periods, thresholds, and repeat cooldown.
Changing notification thresholds never changes a grant's actual quota or a budget's amount.

The inbox's read state is separate from the underlying request or incident. Reading an approval
message does not apply access; marking a failure read does not resolve the failure. Links require
sign-in and current authorization. A removed grant must not leave access to another person's
usage through an old message.

Delivery rechecks current eligibility and preferences before sending. A changed preference can
suppress work not yet accepted by a provider; it cannot recall a message already accepted.
Unavailable channels explain their missing prerequisites rather than silently forwarding content.

## Administrator routing

Shared routes choose categories, channels, cadence, and selected administrators or all actual
MOSAIC administrators. All-admin discovery reads the API enterprise application's app-role
assignments, not just people who previously signed into MOSAIC. It needs Graph application-read
and directory/group-read permissions.

Users directly assigned to the Admin role and direct user members of a role-assigned group are
eligible. Nested-group membership does not inherit an enterprise application's group assignment.
Incomplete discovery is not a complete administrator roster: resolve the missing permissions or
explicitly configure a smaller selected-administrator route.

Personal opt-outs apply to personal delivery. They do not turn off an independently configured
shared admin Teams route. Personal request and usage details never fall back to a shared channel.
Email uses a usable directory-backed mailbox, not a fabricated address or guest UPN. Legacy
explicit budget recipient addresses remain a separate, deliberately configured audience.

## Channels and functional checks

**Settings** separates saved channel configuration, routing, delivery health, and AI setup.
Readiness reports distinguish disabled, missing configuration, needs action, checking, verified,
degraded, failed, and unknown. A saved endpoint or app ID alone is not proof of functionality.
Read-only checks do not send messages or mutate Azure, Entra, Teams, or gateway resources.

| Channel or prerequisite | Setup and evidence |
| --- | --- |
| In-app | Reachable notification storage and an authenticated, self-scoped inbox. Use Cosmos for durable records; local memory is lost at restart. |
| Email | Select an Exchange Online mailbox; an Exchange administrator grants mailbox-scoped Application Mail.Send to the API managed identity. Check runtime identity, enable the channel, and explicitly send a test. The budget-email switch is separate. See [setup and remediation](notification-setup.md#microsoft-graph-email). |
| Admin shared Teams | Save an approved Teams Workflow in Key Vault, arrange owner/co-owner and Teams connection, enable a matching admin route, then use Check access and Send test. Read-only secret/URL checks cannot verify downstream execution; inspect Workflow run history and receipt. |
| Private Teams | Configure the exact single-tenant bot identity, authenticated callback and approved personal app. Check access acquires a runtime Bot Connector token without sending; installation, persona opt-in and an explicit self-test are separate. Uninstalled or blocked apps need repair; no shared-channel fallback is used. |
| Recipients | Resolve actual role assignments and directory contacts. Grant missing read permissions externally; a health check does not grant consent. |
| Scheduling | Keep at least one API instance awake and inspect current successful worker heartbeats and backlog. App Service Always On or a minimum running replica is required for reliable in-process timing. |
| Usage | Configure gateway telemetry, Log Analytics read access, successful rollups, and pricing coverage. Use the gateway's existing Telemetry checks and explicit diagnostic/apply workflows. |

An email/Teams HTTP success means **provider accepted**, not confirmed recipient delivery or
reading. Transient and uncertain outcomes retry automatically within finite attempt limits;
delivery takes priority over avoiding duplicates. Exhausted uncertainty stays indeterminate,
and manual retry needs no duplicate-risk acknowledgement. Webhook URLs and bot credentials are
secrets, not settings to expose in screenshots, audit payloads, or browser responses.

[Notification setup](notification-setup.md) explains the administrator responsibilities,
supported deployment settings, diagnostic boundaries and corrective actions for all three paths.
Checks and explicit tests are separate actions; saving settings never sends a message.

## Usage, limits, and summaries

Daily, weekly, and monthly summaries cover completed calendar periods in the preference's time
zone. Durable UTC window identities avoid duplicate local-clock deliveries, including daylight
saving transitions. Downtime is handled as a labeled catch-up rather than one message for every
missed timer tick.

Personal summaries use the portal's own caller attribution. A shared cost-center budget or
pooled quota is labeled shared; it is not a fabricated personal dollar allowance. Unpriced usage
is unknown cost, not zero. Missing or stale telemetry is incomplete evidence, not a healthy
remaining balance.

Threshold alerts use actual supported finite counters and reset periods, normally at 80% and
100% unless changed in preferences. Incidents and cooldowns prevent repeated messages on every
poll. Delayed daily rollups cannot provide precise live RPM/TPM remaining warnings; unsupported
real-time checks must say so. Notification preferences never add daily or weekly budget
enforcement: existing dollar budgets remain monthly and reset at the UTC calendar month.

## Hosting settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `MOSAIC_NOTIFICATIONS_ENABLED` | `true` | Run shared notification processing, including migrated budget mail; this does not opt anyone into external channels. |
| `MOSAIC_NOTIFICATION_INTERVAL_SECONDS` | `30` | Notification/source polling interval, 10-3,600 seconds. |
| `MOSAIC_APPROVAL_EVALUATOR_ENABLED` | `true` | Enable evaluation of explicitly enabled approval policies. No policy is enabled by this switch alone. |
| `MOSAIC_APPROVAL_INTERVAL_SECONDS` | `60` | Policy polling interval, 10-3,600 seconds. |
| `MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON` | `false` | Deployment declaration only. Verify actual Always On/minimum replicas and heartbeat evidence. |
| `MOSAIC_FRONTEND_URL` | None | Administrator-console origin without credentials, path, query, or fragment; HTTPS in Azure. |
| `MOSAIC_PORTAL_URL` | None | Portal origin without credentials, path, query, or fragment; HTTPS in Azure. |
| `MOSAIC_TEAMS_WORKFLOW_ALLOWED_HOSTS` | `[]` | JSON array of exact approved Microsoft Workflows hostnames; no wildcards or arbitrary/internal URLs. |
| `MOSAIC_TEAMS_BOT_AUTH_MODE` | `disabled` | `managedIdentity` or `clientSecret` supplies outbound private-bot OAuth. It does not enable the channel or install an app. |
| `MOSAIC_TEAMS_BOT_APP_ID` | None | Exact bot application's or user-assigned managed identity's client GUID. |
| `MOSAIC_TEAMS_BOT_SECRET_REFERENCE` | None | HTTPS Key Vault secret URI for `clientSecret`; never the secret itself. |

The reference Bicep deployment enables App Service Always On and supplies both application
origins. It does not grant optional Graph consent, install Teams applications, enable recipient
opt-ins, or authorize live tests. Email and Teams originate from MOSAIC; APIM does not need a new
notification webhook or callback policy.

For `managedIdentity`, set the deployment parameter
`MOSAIC_TEAMS_BOT_MANAGED_IDENTITY_RESOURCE_ID` to the existing bot identity's Azure resource ID,
as well as its client GUID above. Provisioning attaches that identity to the API host; the bot
token source deliberately uses that identity, not the host's system-assigned identity. Creating
the Azure Bot, configuring its HTTPS messaging endpoint, publishing/installing the personal app,
and consent or policy changes remain explicit operator work.

See [the Teams package/setup guide](../apps/teams/README.md) for supported Workflow URL shapes,
personal installation, public-cloud limitations, OAuth, webhook rotation, owner continuity,
manifest packaging, and explicit acceptance tests.

Existing deployments also need item-level TTL enabled on `desired-state` (`defaultTtl: -1`).
This enables expiry only for documents carrying an explicit `ttl`; it does not make grants,
requests, policies, preferences, or unrelated desired state expire. Apply the updated Bicep
configuration through the normal reviewed deployment workflow.
Readiness reads the actual `desired-state` and `usage-rollups` container expiry metadata; saving
an application setting does not verify retention. A positive container default would also expire
ordinary documents, so these containers must use item-only expiry (`defaultTtl: -1`).
Test-environment hosts do not start notification background loops: tests and fictional demo
fixtures invoke reconciliation explicitly with doubles instead of probing real providers.
Turning off `MOSAIC_BUDGETS_ENABLED` also defers migrated budget-email jobs without using their
provider-attempt allowance. Their ownership and receipts remain intact for a later resume;
ordinary request, access, and operational notifications are independent.

## Upgrade, operations, and rollback

Use the existing tenant partition and additive document types. Approval source events belong in
the same desired-state transaction as the request, grant, and audit. Results from the operations
container are durably reconciled with deterministic event identities and paginated checkpoints;
an API crash must not turn a completed operation into a permanently lost notification.

Operational inbox/delivery data has bounded retention (90 days by default); source records and
audit history are not notification cleanup targets. Inspect dead letters, indeterminate sends,
worker failures, unavailable channels, and stale telemetry before declaring delivery functional.
An integration problem should not recursively generate an unlimited series of notifications
about its own notification failures.

For rollback, first disable new approval policies and pause their evaluator, then stop new
notification processing before rolling application versions back. Do not delete request/grant/
audit or legacy budget state. Already approved grants still require their normal review/apply
workflow; disabling automation does not revoke them. Preserve provider IDs and delivery records
to avoid replaying previously accepted messages during recovery.

Do not blindly downgrade a tenant with pipeline-owned budget notices to a version that does not
understand their ownership. Disable legacy budget email before such a downgrade and keep it off
until queued/unknown notices and provider receipts have been reconciled. Stopping the new worker
alone does not prevent an older inline sender from treating those notices as its own.

See [notification engine composition](notification-engine.md), [routing and digests](notification-routing.md),
and [existing budget setup](budgets-and-alerts.md) for the durable migration and operational details.
