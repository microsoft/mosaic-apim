# Verify a real deployment

[Documentation](README.md) | [MOSAIC](../README.md)

- [Verifying a real gateway](#verifying-a-real-gateway)
- [Entra-backed center evidence](#entra-backed-center-evidence)
- [End-to-end UI testing](#end-to-end-ui-testing)

## Verifying a real gateway

Automated unit tests and policy snapshots do not prove that a real gateway accepts a caller's
token or can reach its model. `scripts\verify_model_access.py` is an opt-in verification client:
it calls the actual APIM endpoint and does not proxy through MOSAIC, provision resources, change
grants, or save credentials.

Deploy the feature and bootstrap its runtime registration. Then publish the models to check, in a
non-production environment, and apply grants for them: any number of user grants held by one
person, and application grants held by one workload. The script reads each grant's connection
details from MOSAIC and calls the operation its publication exposes:

- Azure OpenAI chat completions under `/openai/`, with `--api-version`.
- Foundry Models chat completions under `/models/`, with `--models-api-version`.
- Anthropic Messages at `/anthropic/v1/messages`, which takes no API version.

Credentials come from process environment variables, not source files:

- `MOSAIC_SMOKE_USER_CONTROL_TOKEN`: the granted user's token for the MOSAIC API, with `User`.
  Needed for user grants and for grants held by someone else. For application grants, it also
  lets the script check that the user can't reveal the application's key.
- `MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN`: an administrator's MOSAIC API token for application-key
  handoff. Needed for application, agent identity, and security-group grants, and to confirm whose
  grants the user must not reach.
- `MOSAIC_SMOKE_USER_RUNTIME_TOKEN`: that user's delegated model-runtime token. The user can get
  one by signing in with the model client, as in
  [Call a published model with an Entra token](call-models-with-entra-tokens.md).
- `MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN`: the granted application's model-runtime token.
- `MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN`: for `--check-ungranted-user`, a model-runtime token
  for a different user who holds none of the grants.
- `MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN`: for `--agent-entitlement`, an app-only model-runtime token
  for the granted agent identity.
- `MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN`: for `--group-entitlement`, a model-runtime token for a
  user, application or agent that is a member of the granted Entra security group.

The script can sign in instead of reading runtime tokens. With `--user-token-source device-code`
it uses the model client that MOSAIC names in the connection details, and prints a code for the
user to enter at the sign-in page. With `--application-token-source client-credentials` it reads
the workload's `MOSAIC_SMOKE_APPLICATION_CLIENT_ID` and `MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET`.
Use a short-lived secret and delete it afterward.

```powershell
python scripts\verify_model_access.py `
  --api-base-url https://<mosaic-api-host> `
  --gateway-origin https://<approved-apim-host> `
  --user-entitlement <user-grant-id> `
  --user-entitlement <another-user-grant-id> `
  --application-entitlement <application-grant-id> `
  --agent-entitlement <agent-grant-id> `
  --group-entitlement <security-group-grant-id> `
  --api-version <azure-openai-api-version> `
  --models-api-version <foundry-models-api-version> `
  --user-token-source device-code `
  --check-ungranted-user `
  --send-model-requests
```

The explicit flag acknowledges actual inference requests and their consumption; each asks for at
most 8 output tokens. Every sign-in happens before the first model call. For each grant, the
gateway must first reject an anonymous call and an invalid key. Token validation must refuse a
MOSAIC control-plane token, and an invalid token sent with a valid key, with 401. When the run has
a token for the other kind of subject, the gateway must refuse it with the grant's key with 403,
because the two name different grants. With `--check-ungranted-user`, the grant lookup must also
refuse the ungranted user's token with 403. When Entra tokens are off, every token must get 401. A
rejection with the other status came from a different rule, so the check fails. Then the grant
must reach its model with its own key and its own Entra token, whichever methods are applied.

The script refuses redirects or an unexpected gateway origin. Before using a token, it checks the
token's audience, permission and expiry, and that Entra issued it in the version 2.0 format the
gateway accepts. The run fails if the grant subject's own token or the ungranted user's token
doesn't pass, or expires within a minute. A check that only borrows a token, such as the other
subject's token with this grant's key, is skipped with the reason instead, because token
validation would refuse that token before the rule under test. The script prints neither keys,
tokens, nor model output. Sign-in failures show only their error and `AADSTS` codes, which the
[troubleshooting table](call-models-with-entra-tokens.md#troubleshooting) explains. Chat
requests cap output with `max_completion_tokens` on Azure OpenAI routes and `max_tokens` on
Foundry Models routes; `--chat-token-parameter` overrides that for API versions that differ. Set
`MOSAIC_SMOKE_PAYLOAD` to a bounded request JSON object to send instead of the default; the script
still sets its `model` to each grant's deployment.

`--agent-entitlement` and `--group-entitlement` each name a grant to check with the
administrator's token, after the user and application grants. Repeat either flag for each such
grant. An agent identity grant's connection details must name an agent identity, a `.default`
runtime scope, the `Models.Invoke.Application` app role, and Entra tokens applied with nothing
pending. A security-group grant must report that it has no keys, and MOSAIC must refuse to reveal
one with 409. The script warns when the group member's token has no `groups` claim or signals
group overage, because the gateway can't then match the caller to the group. Then each grant must
reach its model with `MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN` or
`MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN`, which the script reads rather than signing in. These
grants don't take part in the rejection checks, the proofs, or `--watch-revocation`.

Each run can add one proof, using fresh isolated grants with no other callers. Only the gateway's
own limit counts: a 429 from the model deployment, or from a different limit, fails the proof as
inconclusive.

- `--prove-shared-budget`: for grants limited to **2 requests per 300 seconds**, with keys and Entra
  tokens applied. Two successful calls using the primary key and Entra token must exhaust the
  budget for the secondary key too, which then gets the gateway's call-limit 429.
- `--prove-token-limit`: for grants limited to at most **100 tokens per minute**, without call
  limits. The model's own token limit for each grant must be higher than the grant's. Further
  calls must reach the gateway's token-limit 429 with `Retry-After`. Classic-tier gateways can't
  limit an Anthropic model's tokens, so this proof doesn't apply to Claude there, and the script
  refuses it before calling the model.

`--watch-revocation <grant-id>` ends the run by waiting while you revoke that grant in the console,
which disables it, and apply its model's access plan. Don't delete the grant: the script follows
its status in MOSAIC. Every apply briefly refuses all calls to the model, so rejections count only
after MOSAIC reports the grant revoked. Then the gateway must reject the grant's key, and its Entra
token with 403 from the grant lookup (401 if the plan also turned Entra tokens off), twice in a
row. The watch polls every `--revocation-interval` seconds (30 by default) and fails after
`--revocation-timeout` seconds (900 by default). The tokens it uses must stay valid for the whole
watch plus a minute; otherwise it stops before waiting.

`--foreign-user-entitlement <grant-id>` checks that the user can't reach a grant someone else
holds. Repeat it for each such grant. With the administrator's token, the script first confirms
that the grant exists and that a different user holds it, so a mistyped ID or one of the user's
own grants can't pass as a refusal. Then, with the user's token, MOSAIC must leave the grant out
of the user's lists, including the portal's **My access**, and out of the user's 90-day usage
report, both its rows and its timeline. It must also refuse the grant's connection details and
its key with 403 or 404. A MOSAIC from before the usage report (ADR 0015) answers its route with
404, and the script says it skipped that part. These checks call only MOSAIC's API, before any
model call, and each refused key request is recorded in MOSAIC's audit log. A run that names only
grants held by someone else sends no model requests, so it doesn't need `--send-model-requests`.

Separately exercise method toggles through reviewed plans, allowing APIM to propagate each change
before rerunning the script. Verify rotation by changing a test subscription key directly in APIM
and revealing it again: no MOSAIC synchronization should be needed. Do not report these live
scenarios as passed when deployment, consent, credentials, or a test gateway are unavailable.

For published MCP servers, `scripts\verify_mcp_access.py` is the same kind of opt-in client, and
needs only `httpx`. It reads each MCP grant's connection details from MOSAIC, then speaks
streamable HTTP to the gateway as a real MCP client: `initialize`, `notifications/initialized`,
`tools/list` and `tools/call`, with `MCP-Protocol-Version` and `Mcp-Session-Id`, JSON or
event-stream responses, and `DELETE` to end each session. Prepare a non-production published
server whose tools include `echo` and `add`, then apply grants on it:

```powershell
python scripts\verify_mcp_access.py `
  --api-base-url https://<mosaic-api-host> `
  --gateway-origin https://<approved-apim-host> `
  --user-entitlement <mcp-grant-id> `
  --application-entitlement <application-mcp-grant-id> `
  --user-token-source device-code `
  --application-token-source client-credentials `
  --check-ungranted-user `
  --check-missing-scope
```

For each grant, an anonymous call must get `401` naming the protected resource metadata, which
must name the server URL, the tenant's authorization server and
`api://<runtime-client-id>/Mcp.Invoke`. MOSAIC's control-plane token must get `401`, and an
`x-mosaic-cost-center` that isn't a code, or names a cost center the caller holds no grant under,
must get the cost-center rule's `403`. With the checks requested, an ungranted user's token and the
user's own token without `Mcp.Invoke` must get `403` with `insufficient_scope`. That token is
`MOSAIC_SMOKE_USER_RUNTIME_TOKEN`, unless `--missing-scope-client-id <client-id>` names a public
client consented only for `Models.Invoke`: then the verifier signs the user in again through it.
Then the grant's own token must list the tools, and `echo` and `add` must return the text and the
sum, with the grant's cost center named in either case.

People sign in through the model client that connection details name, which needs consent for
`Mcp.Invoke`, or supply `MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN` and, for the ungranted user,
`MOSAIC_SMOKE_MCP_UNGRANTED_USER_RUNTIME_TOKEN`. An application signs in with
`MOSAIC_SMOKE_APPLICATION_CLIENT_ID` and `MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET`, or supplies
`MOSAIC_SMOKE_MCP_APPLICATION_RUNTIME_TOKEN` (including a supplied managed-identity token). Before
any MCP probe, the verifier reads the selected application's applied publication grant and requires
its object ID to match the token's `oid`; another application's valid token cannot prove that grant.
Application checks require a direct application entitlement, not a security-group entitlement.
The control-plane tokens are the model verifier's.
Optional flags prove a grant's call limit (`--prove-call-limit`) or a cost center's pooled call
quota (`--prove-pooled-quota`, which spends the pool for the month), watch a revocation take effect
(`--watch-revocation`), and call a server whose `ask_model` tool calls a governed model on the
person's behalf (`--on-behalf-entitlement` with `--send-model-requests`, and `--await-attribution`
to wait for the person's usage report to attribute it). With `--model-caller-entitlement`,
`--await-model-grant` makes that call wait until the server's model grant is enabled and applied,
so a grant on a public server is open only for the call. The
[end-to-end runbook](e2e/runbook.md#verify-mcp-access) covers each one, what it proves, and
its prerequisites. Session cleanup is required before a tool or limit proof passes: DELETE uses
the same credentials and cost center, with at most one gateway rate-limit retry after a
`Retry-After` of 1–300 seconds. A denied or failed cleanup makes the run incomplete, not passed;
only successful DELETE or explicit unsupported deletion (405) releases the session identity.
On a stateful server, a pooled-quota proof spends the pool so that the session's DELETE is the last
call it allows, then checks the refusal with requests that open no session. So it needs a cost
center made for it, with a fresh pool: one that something else had spent part of refuses the
DELETE too, and the run is incomplete.
The script prints neither tokens nor any tool's output. Do not report live MCP
interoperability as passed when the APIM preview contract, consent, credentials or a test server
are unavailable.

## Entra-backed center evidence

The [Entra-backed center workflow](cost-centers.md) has additional acceptance boundaries beyond
the original grant-verifier commands above. Fictional adapters, API/policy tests, and screenshots
do not establish live directory mutation, group claims, APIM propagation, or production performance.
The direct Members list uses the preview Graph beta workaround for service principals omitted
by v1.0; this is not a production-supported listing API, even though runtime group-grant matching
is a separate stable JWT-claim path.
An Azure CLI sign-in or an existing publication is not authorization to create a group, edit
membership, issue keys, or call a model. Obtain a separately authorized non-production target and
scoped test identities first.

Record which checks actually ran:

- Attach an eligible exclusive group and create an explicitly API-owned group through the
  separately consented API identity. Verify read-only attachment without write consent and
  truthful setup-required/error/unknown states after missing consent, denial, throttling,
  replication delay, or uncertain creation. Retry without duplicate centers/groups.
- Add/remove actual direct relationships, distinguish inherited membership, and verify removal
  uses `/$ref` only. Check workload-member permissions separately from user-member permissions.
- Apply one Automatic exact-resource group grant, then add a member with no per-member automatic
  entitlements or APIM management writes. Record the unchanged Automatic-only policy identity/hash;
  that local check alone is not a live gateway authorization test.
- Exercise Automatic, Request-only approval/apply, and Not available for model, exact pool-model,
  and MCP offerings. Confirm General's exemption, Government exclusion, token/key settings,
  defaults/header selection, group overage, and independent model/MCP permissions.
- After removal, test existing versus refreshed JWTs, including individual token grants that
  must still require the center group. Record actual expiry/propagation/cache conditions rather
  than assuming immediate revocation or a universal 90-minute bound.
- Test individual-key eligibility checks and explicit revoke/apply. Change membership outside
  MOSAIC and run the explicit eligibility recheck; failed/incomplete verification must not become
  a successful result. A directory write alone does not establish that a copied key stopped working.
- Submit a portal center request without added directory privileges, review its optional group
  suggestion, and distinguish approved-but-incomplete setup from active resource access.

Keep consent, directory operations, token observations, gateway apply, and runtime outcomes as
separate evidence. Do not mark unavailable live checks passed or use the historical APIM-local
feasibility probe as evidence for this design. See [ADR 0030](adr/0030-entra-backed-cost-centers.md)
and the [E2E runbook](e2e/runbook.md).

## End-to-end UI testing

[`e2e/`](../e2e) holds a live, human-in-the-loop Playwright harness. People sign in their own test
accounts, and the harness drives the web console and portal to:

- Import Azure OpenAI and Foundry endpoints, publish their models, and grant them.
- Check that each end user or workload can call its model, and that others are denied.

The [roadmap](e2e/roadmap.md) tracks the phases and the journey matrix. The
[runbook](e2e/runbook.md) covers setup, personas, flags and secret hygiene. Its `verify`
command runs `scripts\verify_model_access.py` with the personas' own MOSAIC API tokens, and
enters its device codes in their browsers. Its `verify-mcp` command runs
`scripts\verify_mcp_access.py` the same way.
