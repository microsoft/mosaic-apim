# End-to-end harness runbook

How to run the live, human-in-the-loop Playwright harness in [`e2e/`](../../e2e) against a deployed
MOSAIC environment. The phases and journeys it serves are in the [roadmap](roadmap.md).

The harness has three parts:

- **Persona profiles:** one persistent Chromium profile per test account. A person signs in once
  (password, MFA, "Stay signed in?"), and later runs reuse that Entra session.
- **The live driver** (`npm run live`): keeps the persona browsers open and takes one action at a
  time from `tools/drive.ts`. It's used for exploratory, UI-first runs, including by an agent.
- **The suite** (`npm test`): ordered Playwright specs that codify journeys once they pass live.

## Prerequisites

- Node.js 24 or later. The tools run TypeScript directly, with no build step.
- A deployed MOSAIC environment (`azd up`) and the accounts you want to test with.
- The Azure CLI, for verification and approved tenant changes. The harness never calls it.

## Setup

```powershell
Set-Location e2e
npm ci
npx playwright install chromium
Copy-Item targets.example.json targets.local.json
```

Edit `targets.local.json`, which git ignores:

- `origins`: the web, portal, API and gateway origins, as bare `https://` origins.
- `personas`: one entry per account, with its UPN, expected MOSAIC role (`Admin`, `User`, or
  `None`), and optionally `guest: true` or a browser `channel` (`chromium`, `msedge`, or `chrome`).
- `roles`: maps the journey roles (`admin`, `user`, `guest`, `noRole`, `outsider`) to persona keys.
- `endpoints` and `negatives`: resource IDs for Azure OpenAI or Foundry accounts, how each one is
  registered (`paste` or `suggestion`), and the deployments to publish.
- `suite` (optional): what the ordered specs act on, described in
  [Run the ordered suite](#run-the-ordered-suite).
- `mcp` (optional): Phase 11's MCP servers and the grants on them, described in
  [Verify MCP access](#verify-mcp-access).

The manifest is checked on load. The error names the field to fix if an origin isn't a bare
origin, a persona key is invalid, a role points to a missing persona, or a resource ID isn't a
Cognitive Services account.

Local state is kept outside the repository, in `%LOCALAPPDATA%\mosaic-e2e` (or
`MOSAIC_E2E_STATE_DIR`):

| Path | Contents |
| --- | --- |
| `profiles\<persona>\` | Persistent browser profile. **It holds refresh tokens, so treat it as a credential** |
| `live.json` | Live driver process ID, port and per-run token. Deleted when the driver stops |
| `artifacts\` | Masked screenshots and reports (or `MOSAIC_E2E_ARTIFACTS_DIR`) |

## Sign in each persona

Run this while the live driver is **not** running, because the driver locks the profiles:

```powershell
npm run login -- admin user-a --app both
```

A browser opens for each persona. Complete the password and MFA prompts. The tool fills in the
username and accepts "Stay signed in?", and continues once the app shell or the portal's
no-access card appears. A sign-in error (an `AADSTS` code) fails that persona and moves on to the
next one.

If a guest's home tenant requires a managed browser, set `"channel": "msedge"` on that persona.
Use a different profile for each persona, and never sign one profile in as someone else.

## Drive the UI live

Start the driver in its own terminal and leave it running:

```powershell
npm run live
```

Then send one action at a time from another terminal:

```powershell
node tools/drive.ts status
node tools/drive.ts admin signin web /models
node tools/drive.ts admin snapshot
node tools/drive.ts admin click "role:button:Add model endpoint"
node tools/drive.ts admin fill "label:Azure resource ID" "@target:endpoints.aoai-east.resourceId"
node tools/drive.ts admin click "role:button:Register" --in "role:dialog" --exact
node tools/drive.ts admin wait "role:table:Registered model endpoints" --timeout 120000
node tools/drive.ts admin shot after-register
node tools/drive.ts admin logs
node tools/drive.ts shutdown
```

- **Targets:**
  - `role:<role>[:<name>]`, `label:`, `text:`, `placeholder:`, `title:` and `alt:` accept a
    `/regular expression/flags`.
  - `testid:` and `css:` are literal.
  - Scope a target with `--in <target>` or `--row <text>`, narrow it with `--has <text>`, pick a
    match with `--nth <n>` (counting from 0) or `--nth last`, and add `--exact` for an exact name.
    Negative numbers need an equals sign (`--nth=-1`), because the argument parser reads
    `--nth -1` as two options.
- **Dialogs:** about 250 ms after a console dialog opens, Fluent UI marks the rest of the page
  `aria-hidden`, and it stays hidden for about 250 ms after the dialog closes. Role targets and
  `snapshot` skip hidden content. `click`, `wait` and `text` wait for their target, but `count`
  doesn't, so `wait` for the target before you count right after a dialog closes. Before
  [#25](https://github.com/microsoft/mosaic-apim/pull/25) was deployed, a dialog opened on its
  review step could itself be hidden (O4 in the [roadmap](roadmap.md#findings)); on such a build,
  reach its controls with `css:` targets.
- **Values:** `@target:<dot.path>` reads a value from the manifest, so identifiers don't end up in
  shell history. `fill` never echoes the value.
- **Sign-in:** `signin` waits for a person to finish MFA; the default timeout is 10 minutes.
- **Logs:** `logs` returns redacted console errors, page errors, dialogs, API responses at 400 or
  above, and failed requests. Dialogs are dismissed unless you run `dialogs accept`.
- **Navigation:** the driver only goes to the web and portal origins in the manifest. `signin`
  also refuses a path that would leave that app's own origin.
- **Reading pages:** `snapshot` and `text` work only on MOSAIC web and portal pages, and not while
  `signin` is waiting for a person. On a sign-in page or any other site they refuse and report the
  URL, the title and any `AADSTS` code instead. Use `url`, `shot` or `logs` there.

The driver listens on `127.0.0.1` on a random port. It requires the per-run token from `live.json`
and rejects any request that carries browser `Origin` or `Sec-Fetch-Site` headers, so web pages
can't drive it.

## Run the suite

Stop the live driver first (`node tools/drive.ts shutdown`) so the suite can open the profiles:

```powershell
npm test                                   # every spec; journeys that write or call models skip themselves
npm test -- --grep "@smoke"                # or pick journeys by title
$env:MOSAIC_E2E_ALLOW_WRITES = '1'; npm test
```

| Variable | Effect |
| --- | --- |
| `MOSAIC_E2E_INTERACTIVE=1` | Wait for a person when sign-in needs a password or MFA, instead of failing |
| `MOSAIC_E2E_ALLOW_WRITES=1` | Run journeys that change MOSAIC or Azure state. Otherwise they're skipped |
| `MOSAIC_E2E_SEND_MODEL_REQUESTS=1` | Run runtime journeys that send billable model requests |
| `MOSAIC_E2E_HEADLESS=1` | Run headless. This only works once every profile is already signed in |
| `MOSAIC_E2E_HTML_REPORT=1` | Also write an HTML report to `playwright-report\` (open it with `npx playwright show-report`). It records step titles and step errors before the harness can redact them, so use it only for runs that don't reveal keys, and delete it afterwards |
| `MOSAIC_E2E_BROWSER_CHANNEL` | Default browser channel for personas that don't set one |
| `MOSAIC_E2E_TARGETS` | Path to a manifest other than `targets.local.json` |
| `MOSAIC_E2E_STATE_DIR`, `MOSAIC_E2E_ARTIFACTS_DIR` | Move profiles, driver state or artifacts |
| `MOSAIC_E2E_PYTHON` | The Python that runs the verifiers, for the live driver's `verify` and `verify-mcp` and the suite's runtime journeys. Defaults to `python`, which needs `httpx` |

Specs run serially with a single worker, because journeys build on each other and share
profiles. Traces and video are off. On failure, the suite attaches a masked screenshot of each
open persona page and, for MOSAIC pages, a redacted accessibility snapshot. It also redacts the
test's errors before Playwright writes them to `error-context.md` and the console. Playwright's
own page snapshot is turned off (`PLAYWRIGHT_NO_COPY_PROMPT`), because it isn't redacted.

## Run the ordered suite

The specs in `specs/` turn the live run's journeys into tests that can run again and again against
the same environment. Playwright runs them in file order. The journey IDs come from the
[journey matrix](roadmap.md#journey-matrix):

| Spec | Journeys | Tags |
| --- | --- | --- |
| `00-smoke` | S1, S2, A0, A1, P0 and P1: the apps and the API answer, and turn away who they should | `@smoke` |
| `10-endpoints` | A2 to A6: discovery, registration, synced models, and whether MOSAIC and the gateway can reach each endpoint | `@console` |
| `20-publish` | A7 to A9: the managed gateway, publishing, and catalog visibility | `@console` |
| `30-access` | A10 to A12 and A16: identities, governed access, the workload's key handoff, and environments | `@console` |
| `40-portal` | P0 to P9 and A13: the catalog, My access, keys, requests, isolation between people, and usage | `@portal` |
| `50-runtime` | R1 to R6, through the runtime verifier | `@runtime` |
| `60-lifecycle` | A14 and R7: turning a method off and on, revoking a grant and enabling it again, and the gateway refusing a revoked grant | `@console`, `@writes` |
| `90-cleanup` | A15, and putting back everything else the suite changed | `@console`, `@writes`, `@cleanup` |

A test that changes MOSAIC or Azure is also tagged `@writes`, and one that runs the runtime
verifier `@runtime`, whichever spec it's in. Pick what to run with a path, `--grep` or
`--grep-invert`:

```powershell
npm test -- --grep-invert "@writes"         # only reads; changes nothing
npm test -- --grep-invert "@runtime"        # runs no verifier and sends no model requests
npm test -- --grep "@portal"
npm test -- specs/60-lifecycle.spec.ts
```

**Gates.** A test that isn't allowed to run skips, and names the variable it needs:

- Writes need `MOSAIC_E2E_ALLOW_WRITES=1`.
- `@runtime` tests run `scripts\verify_model_access.py` the way the live driver's `verify` does (see
  [Verify runtime access](#verify-runtime-access)): the MOSAIC API tokens come from the personas'
  browsers, device codes are entered in the right persona's browser, and every line is redacted.
  All of them except P7's isolation check send billed model requests, so they need
  `MOSAIC_E2E_SEND_MODEL_REQUESTS=1`.
- A grant that accepts Microsoft Entra tokens needs its holder's model token. With
  `MOSAIC_E2E_INTERACTIVE=1` the verifier signs the holder in with a device code, and a person
  confirms it there. Otherwise set `MOSAIC_SMOKE_USER_RUNTIME_TOKEN` to the holder's token; a token
  that belongs to someone else isn't passed on. R1's ungranted-user check signs in `roles.outsider`,
  or `roles.noRole`, the same way, or uses `MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN`. A check the
  verifier can't make is left out and noted in the report, never counted as passed.
- R3 needs the workload's token when its grant accepts Entra tokens: set
  `MOSAIC_SMOKE_APPLICATION_CLIENT_ID` and `MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET`, or
  `MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN`.

**The manifest's `suite` section** names what the specs act on, and `targets.example.json` shows
every field. Each part is optional: a journey whose part is missing skips, and names the field.

| Field | Journeys | What it names |
| --- | --- | --- |
| `gateway` | Most tests in 10 to 90 | The MOSAIC gateway, by name, that the suite publishes through |
| `runtime.userGrants` | A11, P3, P6, R1, R2, R4 | Models on which `roles.user` already holds an applied grant. Only read |
| `runtime.applicationGrant` | A11, A12, R3 | The model on which the workload in `targets.workload` holds its grant. Only read. Set `targets.workload.objectId` to the workload's service principal object ID, so the specs find its MOSAIC identity whatever an admin labelled it; without it, the label must equal `targets.workload.displayName` |
| `runtime.foreignGrant` | A11, P7 | A grant that a persona other than `roles.user` holds, and the user must not be able to reach. Only read |
| `runtime.apiVersion`, `runtime.modelsApiVersion`, `runtime.chatTokenParameter` | R1 to R7, P7 | The verifier's flags of the same names |
| `disposable.publication` | The `@writes` tests of 20 to 60, and A15 | A deployment that is **not** in its endpoint's `publish` list, with a `requester` persona and the `limits` of the grant they're given |
| `disposable.sharedBudget` | R5 | A persona and a published model for a throwaway grant of exactly 2 calls per 300 seconds |
| `disposable.tokenLimit` | R6 | A persona and a published model for a throwaway grant of at most 100 tokens per minute |

The manifest is checked on load. `sharedBudget` and `tokenLimit` must be on different models, and
on neither of the `runtime` models, because each run applies their models' access plans, and every
apply briefly refuses calls to the model.

**How a run stays repeatable:**

- Tests over what the environment keeps, such as the registered endpoints, the models in each
  endpoint's `publish` list and the grants in `runtime`, only read. The suite never re-plans or
  unpublishes a kept model, and never deletes an identity.
- Write tests act only on the disposable publication and the throwaway grants. Each one checks the
  state first, and either resets what an earlier run left half done, or skips and says to run
  `90-cleanup` first.
- Every change goes through "Review model changes" or the publish plan, and "Apply plan". The test
  stops before applying if the plan, or the console's list of steps, reaches beyond the model and
  grant being changed. Unpublishing goes through its own review in the same way, and stops before
  "Unpublish model" unless the plan deletes exactly what MOSAIC created for the disposable model. A
  call that should be refused but gets a 2xx response fails the test.
- The disposable model is published by 20, governed by 30, requested and granted in 40, changed
  and revoked in 60, and unpublished by 90.
- The throwaway grants for R5 and R6 are made once and reused. Each run enables the grant and
  applies it, runs the proof, then revokes the grant and applies that, even if the proof failed.
  Between runs they stay disabled and revoked.
- What the suite makes is named or noted so that cleanup can find it: requests start with
  `E2E suite` and end with "Safe to deny.", grants carry a note naming the suite, and the
  disposable publication and its model API are named `E2E suite …`. Access requests stay in
  MOSAIC's history.

**Cleanup and recovery.** `90-cleanup` puts back what the suite changed. It denies the suite's
open requests and unpublishes the disposable model (A15). Before it confirms with "Unpublish
model", the unpublish review must list only the API Management resources MOSAIC created for that
model, and afterwards the model's row must read "Unpublished". Cleanup then removes its
publication, grants and model API. It also revokes any suite grant that isn't at rest, and
removes any endpoint registration named `E2E suite …`. Two things stay by design: the access
requests in MOSAIC's history, and the R5 and R6 grants, disabled and revoked, because MOSAIC keeps
a disabled grant until its model is unpublished. The next run enables them again. Cleanup fails,
rather than deleting, if an identity named `E2E suite …` is left: that one shares its object ID
with a real person, so delete it on the Identity page by hand. Its tests don't depend on each
other, so it can run on its own after a failed run:

```powershell
$env:MOSAIC_E2E_ALLOW_WRITES = '1'
npm test -- specs/90-cleanup.spec.ts        # or: npm test -- --grep "@cleanup"
```

If a model's access is still applying, cleanup stops and says so. Let the apply finish, or
recover it on the Entitlements page, then run it again.

**Timing:**

- The suite waits up to 20 minutes for MOSAIC to apply a plan, and up to 5 more for the model's
  access and each grant to settle. A test that applies has 45 minutes in all, and a runtime test
  adds the verifier's own time limit to that.
- R5's budget is per 300 seconds, so leave 5 minutes before running R5 again.
- R7 starts the verifier's revocation watch, then revokes the grant and applies the revocation.
  The watch lasts 15 minutes, so the apply must finish within that.

**Left for a person:**

- R8, the calls in Application Insights and Log Analytics.
- The parts of A4, A6 and A7 that need an Azure role to be missing first, and A16's step of
  classifying both sides of a pairing. The suite checks the state after them.
- P2 needs a persona with the User role, other than `roles.user`, who holds no grant. In a steady
  environment each one holds some, so P2 usually skips. P4 checks the same empty state on the
  disposable model before its requester asks for it.

## Verify runtime access

Phase 8 calls the gateway directly with `scripts\verify_model_access.py`, outside the browser.
[Verifying a real gateway](../../README.md#verifying-a-real-gateway) explains its checks, flags
and variables. The live driver's `verify` command runs it for the personas, so nobody copies a
token by hand:

```powershell
node tools/drive.ts verify -- --user-entitlement <grant-id> --send-model-requests
node tools/drive.ts verify --user user-a -- `
  --user-entitlement <grant-id> --user-entitlement <another-grant-id> `
  --api-version <azure-openai-api-version> --models-api-version <foundry-models-api-version> `
  --user-token-source device-code --check-ungranted-user --send-model-requests
node tools/drive.ts verify --user user-a -- --foreign-user-entitlement <someone-else's-grant-id>
```

Everything after `--` goes to the verifier. The driver adds `--api-base-url` and
`--gateway-origin` from the manifest's `api` and `gateway` origins, so it refuses them after `--`.
It also refuses unknown or abbreviated flags, and checks the flags the way the verifier does
before anyone signs in.

- **People:**
  - `--user` holds the user grants. It defaults to `roles.user`.
  - `--admin` hands off application keys, confirms that each `--foreign-user-entitlement` is a real
    grant that someone other than the user holds, and reads the connection details of
    `--agent-entitlement` and `--group-entitlement` grants. It's only used with those four flags,
    and defaults to `roles.admin`. With `--application-entitlement` it must be a different account
    from the user. A run with only grants held by someone else may name the admin as the user:
    the driver then signs in once and uses that token for both.
  - `--stranger` holds no grant for the models in the run. It's only used with
    `--check-ungranted-user` and `--user-token-source device-code`, and defaults to
    `roles.outsider`, then `roles.noRole`.
- **MOSAIC API tokens:** the driver signs the user in to MOSAIC again in a new tab, and the admin
  too when the run needs one. It uses the web console for a persona whose expected role is
  `Admin`, and the portal otherwise. It takes the token from the app's first API call and checks
  that it belongs to that persona, comes from the manifest's tenant, and lasts the run. Then it
  closes the tab and passes the token to the verifier in its environment. If a profile's Entra
  session has expired, the driver waits for a person to sign in, as `signin` does.
- **Device codes:** when the verifier prints a code, the driver opens the Microsoft sign-in page
  in the right persona's browser: the user's, or the stranger's for `--check-ungranted-user`. It
  enters the code and picks that persona's account. A person confirms the sign-in and completes
  MFA there; the driver never confirms it. If the driver can't enter the code, it says so, and you
  enter it yourself at the address the verifier printed.
- **Workloads and other variables:** when `MOSAIC_SMOKE_APPLICATION_CLIENT_ID`,
  `MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET`, `MOSAIC_SMOKE_PAYLOAD` or one of the five
  `MOSAIC_SMOKE_*_RUNTIME_TOKEN` variables is set in drive.ts's shell, drive.ts passes it to that
  run only. Use a short-lived client secret and delete it afterward. The two control-token
  variables are never passed on: those tokens always come from the personas' browsers.
- **Agent and security-group grants:** `--agent-entitlement` and `--group-entitlement` call the
  model with an Entra Agent ID's token or a group member's token. The driver can't sign either in,
  so set `MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN` or `MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN` in
  drive.ts's shell. Both flags need `--send-model-requests`, and neither can carry a proof or the
  revocation watch.
- **Output:** the verifier's lines appear in the live driver's terminal as they happen, prefixed
  `[verify]`. drive.ts prints them when the run ends, and exits with the verifier's exit code.
  The tokens and secrets the driver passed on are redacted by value, as well as by pattern.
- **Limits:** one run at a time. The driver stops the verifier after 45 minutes, plus the
  revocation watch's timeout and interval, and stopping drive.ts (Ctrl+C) stops it too.
  `MOSAIC_E2E_PYTHON` picks the Python to run it with (`python` by default); it needs `httpx`.

Grant IDs appear in the console's model access review, as `Grant: <id>` on each row. Proofs need
fresh grants with no other callers. Create them for the run in the console, with the limits the
README names for each proof. Sign-in failures print only their `AADSTS` codes. The
[troubleshooting table](../call-models-with-entra-tokens.md#troubleshooting) explains them.

`--foreign-user-entitlement` needs no fresh grants and sends no model requests: name any user
grant that someone other than `--user` holds. It checks journey P7 at MOSAIC's API, because the
portal has no page for a single grant that a changed ID could reach. It also checks P9's route:
the grant must stay out of the user's usage report, which **Usage & cost** shows. A MOSAIC from
before that report skips this part and says so.

For `--watch-revocation <grant-id>`, leave `verify` running and drive the admin from another
terminal. Revoke the grant (this disables it; don't delete it), then review and apply its model's
access plan. The driver serves other actions while a verification runs. If a token the watch uses
won't last until the timeout, the run stops before waiting and names the token, and a shorter
`--revocation-timeout` fixes it.

## Verify MCP access

Phase 11 checks the MCP servers MOSAIC publishes with `scripts\verify_mcp_access.py`, a scripted
MCP client that needs only `httpx`. It reads each grant's MCP connection details from MOSAIC, then
calls the gateway as a real client over streamable HTTP:

- `initialize`, offering protocol revision 2025-11-25, then the `notifications/initialized`
  notification, `tools/list` and `tools/call`.
- `MCP-Protocol-Version` on every later request, from revision 2025-06-18 on, and `Mcp-Session-Id`
  when the server returns one. Each session the server opens requires confirmed `DELETE` cleanup,
  or an explicit 405 indicating unsupported deletion, before the proof passes.
- JSON and event-stream responses alike. It refuses redirects, and any server or metadata URL off
  the manifest's gateway origin.

The live driver's `verify-mcp` runs it the way `verify` runs the model verifier (see
[Verify runtime access](#verify-runtime-access)). It signs the personas in to MOSAIC for their API
tokens, enters the verifier's device codes in the right persona's browser, and redacts every line.

### Before you run it

- **Servers:** M-tools and M-protected, with the `echo`, `utc_now` and `add` tools, and M-agent,
  with `ask_model`, are registered, published and applied (M1 and M2). M-agent's application is
  named under **Calls models as** and applied, and holds a grant on the model its tool calls.
  M-agent's upstream is public, so keep that grant revoked except for M9's call (see
  [Open M-agent's model grant only for M9's call](#open-m-agents-model-grant-only-for-m9s-call)).
- **Grants**, applied (M3):
  - `roles.user` holds a grant on each tools server, and on M-agent for M9.
  - An application holds a direct grant on a tools server, and has the `Mcp.Invoke.Application`
    app role. The administrator reads the publication's applied grant so its application object
    ID can be matched to the runtime token's `oid` before any MCP probe. Missing identity data,
    another application's token, and security-group entitlements fail closed. A group's object
    ID is not an application identity; selecting a group cannot prove which grant wins.
  - For M6's call limit: a fresh grant limited to 6 to 30 calls per 60 to 300 seconds, with no call
    quota and no other callers. Nothing may call it in the window before the proof, so after a
    run, wait out the window before the next.
  - For M6's pooled quota: a grant with no limits of its own, under a cost center whose pool on the
    server allows at most 50 calls a month, and another grant on the same server under a different
    cost center. **The proof spends the pool for the rest of the month**, so use a cost center made
    for it. Use a stateless server for a proof that can finish: on a stateful server the spent
    pool also denies session DELETE, making the run incomplete even if the quota check worked.
  - For M7: a grant you can revoke. A person holds one grant on a server under each cost center, so
    put it under a cost center of its own.
- **Consent:** MOSAIC's model client, which connection details name as `clientId`, is consented for
  `api://<runtime-client-id>/Mcp.Invoke`, so people sign in to it with a device code.
- **The manifest's `mcp` section** names each server and grant. A grant's ID can then be passed as
  `@target:mcp.grants.<key>.id`. `targets.example.json` shows every field. The manifest is checked
  on load, and a call-limit or pooled grant must be within the proofs' bounds.

| Field | What it names |
| --- | --- |
| `servers.<key>.url` | The server's own streamable HTTP URL, which the admin registers in M1. HTTPS, with no query, so no key |
| `servers.<key>.displayName` | The name it's published under, which the verifier's output shows |
| `servers.<key>.upstreamAuth`, `audience` | `none`, or `managed-identity` with the audience the gateway's managed identity asks for |
| `servers.<key>.tools` | The tools it declares, which M1's sync must show |
| `servers.<key>.modelCaller` | For M-agent: the `application` it calls models as, and that application's `modelGrant` |
| `grants.<key>.server`, `id` | The server, and the grant's ID |
| `grants.<key>.persona` or `application` | Who holds it |
| `grants.<key>.costCenter` | The code of its cost center |
| `grants.<key>.callLimit` | For M6: `calls` per `perSeconds`, the limit `--prove-call-limit` spends |
| `grants.<key>.pooledCalls` | For M6: the monthly calls of its cost center's pool on the server, which `--prove-pooled-quota` spends |

### Run it

```powershell
# M5: discovery, refusals, and tools, for people and an application
node tools/drive.ts verify-mcp -- `
  --user-entitlement @target:mcp.grants.tools-user.id `
  --user-entitlement @target:mcp.grants.protected-user.id `
  --application-entitlement @target:mcp.grants.tools-agent.id `
  --user-token-source device-code --application-token-source client-credentials `
  --check-ungranted-user --check-missing-scope

# M6: a grant's call limit, then a cost center's pooled quota
node tools/drive.ts verify-mcp -- --user-token-source device-code `
  --user-entitlement @target:mcp.grants.tools-limited.id `
  --prove-call-limit @target:mcp.grants.tools-limited.id
node tools/drive.ts verify-mcp -- --user-token-source device-code `
  --user-entitlement @target:mcp.grants.tools-user.id `
  --user-entitlement @target:mcp.grants.tools-pooled.id `
  --prove-pooled-quota @target:mcp.grants.tools-pooled.id

# M7: revocation
node tools/drive.ts verify-mcp -- --user-token-source device-code `
  --user-entitlement @target:mcp.grants.tools-revocable.id `
  --watch-revocation @target:mcp.grants.tools-revocable.id

# M9: a model call made on the person's behalf, and its attribution
node tools/drive.ts verify-mcp -- --user-token-source device-code `
  --on-behalf-entitlement @target:mcp.grants.agent-user.id --send-model-requests `
  --await-attribution --model-caller-entitlement @target:mcp.servers.m-agent.modelCaller.modelGrant `
  --await-model-grant
```

Everything after `--` goes to the verifier. The driver adds the manifest's origins, resolves
`@target:` references, and checks the flags as the verifier does, before anyone signs in. Each
run can make one proof, and either watch a revocation or wait around M9's call: for the model
caller's grant before it, with `--await-model-grant`, and for its attribution after it.

Session cleanup uses the session's original credentials and cost center, with no quota bypass.
A gateway call-limit 429 on DELETE permits one retry after a numeric `Retry-After` of 1–300 seconds;
each DELETE has a 30-second HTTP timeout. Cleanup waiting is excluded from the rate-proof timing.
A second refusal, a quota/budget/auth denial, other unexpected status, or transport failure retains
the session identity in memory and fails the run as **unresolved session cleanup**. It is not a
PASS, and the script does not save session IDs or retry after exit. The server must eventually
expire an unresolved session or an operator must arrange legitimate cleanup separately. A 405 is
reported as unsupported deletion, not as a confirmed server-side termination.

- **People:** as for `verify`, with these differences.
  - `--user` holds the user grants and the on-behalf grant. It defaults to `roles.user`.
  - `--admin` reads an application grant's connection details and applied application identity,
    what a server's last apply compiled for `--prove-pooled-quota`, whether the on-behalf
    server passes references on, and the `--model-caller-entitlement` grant, which
    `--await-model-grant` reads until it's applied. It's only
    used with `--application-entitlement`, `--prove-pooled-quota` and `--on-behalf-entitlement`,
    and may be the same person as `--user`.
  - `--stranger` holds no grant on these MCP servers. It's only used with `--check-ungranted-user`
    and `--user-token-source device-code`.
- **Variables** drive.ts passes on when they're set in its shell:
  - `MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN`, `MOSAIC_SMOKE_MCP_APPLICATION_RUNTIME_TOKEN` and
    `MOSAIC_SMOKE_MCP_UNGRANTED_USER_RUNTIME_TOKEN`: MCP tokens to use instead of signing in.
  - `MOSAIC_SMOKE_APPLICATION_CLIENT_ID` and `MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET`, for
    `--application-token-source client-credentials`.
  - `MOSAIC_SMOKE_USER_RUNTIME_TOKEN`, the user's model token, which `--check-missing-scope` sends.
    Entra puts every scope a client is consented for into each token it issues, so once the model
    client is consented for `Mcp.Invoke`, its model tokens carry that too, and the verifier refuses
    to use them. Take this token from a client that isn't consented for `Mcp.Invoke`.
- **Bills:** only `ask_model` calls a model, so only `--on-behalf-entitlement` needs
  `--send-model-requests`. The call is charged to M-agent's model grant. M-agent's upstream is
  public, so while that grant is open, anyone who reaches M-agent can spend it.
  `--await-model-grant` keeps it open only for M9's call.
- **Time:** the driver stops the verifier after 45 minutes, plus the timeout and interval of each
  wait: the revocation watch, or M9's waits for the model caller's grant and for the attribution.
  The MOSAIC API tokens must last 20 minutes, plus those timeouts and intervals and a minute. With
  M9's defaults that's about 62 minutes, longer than a token issued for an hour lasts, so if the
  driver says a token won't last, lower `--attribution-timeout`. For `--watch-revocation`, drive
  the admin from another terminal, as for `verify`: revoke the grant, then plan and apply its MCP
  server's access. For `--await-model-grant`, see the next section.

### Open M-agent's model grant only for M9's call

Keep M-agent's model grant, which the manifest names as
`mcp.servers.m-agent.modelCaller.modelGrant`, revoked except while M9 makes its call. With
`--await-model-grant`, the verifier waits for the grant after the person's sign-in, so it isn't
open while the person signs in, which can take most of a device code's 15 minutes. Drive the admin
from another terminal:

1. Start the run with the grant revoked and its model's access plan applied.
2. The person confirms the device-code sign-in. After the sign-in and the other checks, the
   verifier asks for the grant:

   ```text
   WAIT: re-enable the model caller's grant in MOSAIC's console and apply its model's access plan. Checking every 15 seconds for up to 600 seconds
   ```

3. Re-enable the grant on the Entitlements page, then review and apply its model's access plan.
   The verifier reads the grant every `--model-grant-interval` seconds (15 by default, 10 to 120)
   for up to `--model-grant-timeout` seconds (600 by default, 60 to 1800), and prints a `WAIT` line
   whenever its status changes. It calls `ask_model` as soon as MOSAIC reports the grant enabled
   and applied.
4. Straight after the call, it asks you to close the grant again. Revoke it and apply its model's
   access plan while the attribution wait goes on:

   ```text
   INFO: On-behalf grant (M-agent) made its model call. Revoke the model caller's grant now and apply its model's access plan; the attribution wait goes on
   ```

   A run that stops after the grant was applied asks the same, in a line that starts
   `INFO: On-behalf grant (M-agent) stopped after the model caller's grant was applied`. One that
   stops while waiting for the grant says to revoke it if you re-enabled it.

If the grant isn't enabled and applied in time, the run fails before any model call. If it's
already enabled and applied when the checks finish, the run says so and calls `ask_model` straight
away. With the grant open, the verifier gives up on the call 11 minutes after it starts. A read
already waiting can take 2 more minutes, and ending the session up to 6 more, so the run says to
revoke the grant at most 19 minutes after the call starts. The admin's
MOSAIC API token reads the grant, so it must last the wait, and the user's must last both waits.
The person's MCP token makes the call, so it must last the wait and those 19 minutes: about 30
minutes with the defaults. Each is checked before the verifier asks for the grant. Whenever a run
ends with the grant re-enabled, however it ends, revoke the grant and apply its model's access plan.

### What each check proves

| Journey | Check | What it proves |
| --- | --- | --- |
| M5 | An anonymous call gets 401, and its `WWW-Authenticate` names the `resource_metadata` URL the connection details give | The gateway refuses a call without a token, and tells clients where to find out how to sign in |
| M5 | That metadata names the server's URL, `https://login.microsoftonline.com/<tenant>/v2.0`, and `api://<runtime-client-id>/Mcp.Invoke` | A client such as VS Code signs in to the right tenant, for the right scope (RFC 9728) |
| M5 | MOSAIC's control-plane token gets 401 | Token validation checks the audience, so a MOSAIC API token never reaches the grant lookup. An `INFO` line says whether that 401 names the metadata, which ADR 0017 leaves to live verification |
| M5 | An `x-mosaic-cost-center` that isn't a code, and a code the caller holds no grant under, get the cost-center rule's 403 | The header can't select a grant the caller doesn't hold |
| M5 | An ungranted person's token gets 403 with `insufficient_scope` | A valid token isn't enough: the caller needs a grant |
| M5 | The user's own token without `Mcp.Invoke`, such as a model token, gets 403 with `insufficient_scope` | Model and MCP permissions never open each other |
| M5 | The person's token, and the application's `.default` token with `Mcp.Invoke.Application`, list the tools, and `echo` and `add` return the text and the sum | The gateway admits each kind of holder, and passes the streamable HTTP conversation through to the server |
| M5 | Those calls name the grant's cost center in `x-mosaic-cost-center`: as MOSAIC gives the code when the session opens, then in the other case | The header selects the grant, compared without case |
| M6 | `--prove-call-limit`: the first call leaves one fewer than the grant's limit in `x-mosaic-remaining-calls`, which falls on each successful call to 0, then the gateway's own 429 comes, with `Retry-After` in seconds | The gateway enforces the grant's own call limit, not a smaller or larger one, and reports it as it's spent. A 429 from the server, a quota's 403, or a refusal while calls are left fails the proof |
| M6 | `--prove-pooled-quota`: calls under the pooled cost center reach the gateway's quota 403 ("Out of call volume quota"), then the other grant on the server, under another cost center, still reaches its tools | The cost center's pool is counted across its grants and refuses once spent, while other cost centers are unaffected. A call limit's 429 fails the proof |
| M7 | `--watch-revocation`: once MOSAIC reports the grant revoked, two calls in a row that name its cost center get the grant lookup's 403, from the cost-center rule | The revocation reached the gateway. Refusals while the plan applies, a 401, or a quota's or budget's 403, which means the gateway still found the grant, don't count |
| M9 | M-agent's last apply names its model caller, and `ask_model` answers the person. With `--await-model-grant`, the call comes only after the sign-in and the other checks, once MOSAIC reports the model caller's grant enabled and applied | The server receives each call's reference, and its tool called a governed model as its application. The model caller's grant, which anyone reaching M-agent could spend, was open for the call, not for the sign-in |
| M9 | `--await-attribution`: the person's usage report, which the portal's **Usage & cost** shows, gains model use through M-agent. With `--model-caller-entitlement`, on that grant's model and under its cost center | MOSAIC attributed the model call to the person and charged the agent's grant (ADR 0025) |

### Find M9's attribution

After the call, the verifier prints what finds it in the gateway's logs, and never a token:

```text
INFO: On-behalf grant (M-agent) called ask_model between <start> and <end> UTC
INFO: On-behalf grant (M-agent) was called by object ID <person>, through client <client ID>
INFO: On-behalf grant (M-agent)'s MCP call trace reads: mosaic-attribution v=1 g=<grant key> m= a=<client ID> r=<request ID> i=<model caller>
```

`g=` is the key MOSAIC compiles into the policy for the grant. Search `TraceRecords` for it in that
window to find the MCP call; the model call's trace repeats the MCP call's `r=`. MOSAIC attributes
the call when its usage rollup reads the logs, usually minutes later. `--await-attribution` reads
the person's usage report every `--attribution-interval` seconds (60 by default), for up to
`--attribution-timeout` seconds (1800). It skips the wait, and says why, when MOSAIC's usage is
simulated or it has no usage report.

## Secret hygiene

- Never commit `targets.local.json`, profiles, artifacts or reports. `e2e/.gitignore` covers them.
- Key reveal UIs must mark revealed values with `data-secret`, which keeps them out of snapshots,
  text reads, screenshots and failure messages. The console's reveal dialog has done so since
  [#38](https://github.com/microsoft/mosaic-apim/pull/38). For builds deployed before it, the
  harness also treats elements labelled `Revealed … key` as secret, along with password and
  one-time-code fields.
- Don't point text or value assertions (`toHaveText`, `toHaveValue`) at a secret element. Failure
  messages are redacted using the values still on the page when the test ends, and a transient
  reveal may be gone by then.
- Redaction removes:
  - JWTs, `Bearer` values, subscription and API key headers, and cookies.
  - OAuth `code`, `state` and `sig` parameters, and URL fragments.
  - Connection-string keys, Entra client secrets, and anything that looks like an APIM or
    Cognitive Services key.
- The runtime verifiers keep keys and tokens in process memory. They print pass, skip, wait, info
  and failure lines, never credentials, model output or tool output.
- To reset a persona, stop the driver and delete `profiles\<persona>`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `needs a password, MFA, or consent` | Run `npm run login -- <persona>`, or set `MOSAIC_E2E_INTERACTIVE=1`. Before it fails, the harness gives silent sign-in 30 seconds on a Microsoft sign-in page with nothing to automate. A B2B guest's home tenant can ask for MFA again within 20 minutes, so run the guest's specs interactively while someone can answer |
| The profile is already in use | Stop the live driver or the other Playwright run using it |
| `AADSTS53003` or another Conditional Access code | Use `"channel": "msedge"` for that persona, or sign in on a compliant device |
| `<app> is signed in as X, not persona Y` | Delete that persona's profile and sign in again as the right account |
| `The live driver is not running` | Run `npm run live` |
| `Could not reach the live driver` | The driver stopped without cleaning up. Delete the stale `live.json` and restart it |
| `the browser hadn't finished exiting, so the harness moved on` | Nothing to fix. On a busy Windows machine a browser can take a minute or more to leave the process table after it has saved its profile. The live driver and `login` wait up to 20 seconds and then continue, and the profile can be reopened straight away |
| `Failed to open a new tab`, or `Target page, context or browser has been closed`, before a test opens its first page | That persona's browser has quit. A headed Chromium quits once its last tab closes, so the harness keeps one blank tab open in each persona's browser between tests. Don't close that tab or the browser window while a run is going. If it happens anyway, rerun |
| `Worker teardown timeout of 180000ms exceeded` after the tests finished | Playwright waits for every browser it launched to exit before a worker stops, and on a busy machine that can outlast the timeout. The test results reported before it still stand. Rerun when the machine is less loaded if you need a clean exit code |
| `Could not run the verifier with "python"` | Install `httpx` for that Python, or set `MOSAIC_E2E_PYTHON` to one that has it |
| `AADSTS65001` when `verify-mcp` signs someone in | The model client isn't consented for `api://<runtime-client-id>/Mcp.Invoke`. An administrator consents it; see [Connect to MCP servers](../connect-to-mcp-servers.md#troubleshooting) |
| `Signing in … failed:` HTTP 500, 502, 503 or 504, `server_error` or `temporarily_unavailable` | The Microsoft sign-in service failed for a moment. While the code is still valid, the verifier keeps polling through a few of these in a row, with an `INFO` line for each, and the sign-in page stays open. It fails only when they keep coming, so rerun |
| `MOSAIC_SMOKE_USER_RUNTIME_TOKEN carries Mcp.Invoke` | That model token came from a client consented for both scopes, so the gateway would accept it. Take it from a client that isn't consented for `Mcp.Invoke`, or leave out `--check-missing-scope` |
| `its server's last apply names no model caller` | Name M-agent's application under **Calls models as** on the MCP servers page, then plan and apply the server, before M9 |
| `The model caller's grant: after N seconds, MOSAIC reports it as <status>, not applied` | Nobody re-enabled M-agent's model grant and applied its model's access plan within `--model-grant-timeout`, or the apply failed or hadn't finished. The run made no model call. If you re-enabled the grant, revoke it and apply the plan again, then rerun with it revoked. If applies take longer, raise `--model-grant-timeout`, up to 1800 |
| `its first call left N of its M calls, not M-1` | Calls from the last window still count, or the gateway enforces a smaller limit than MOSAIC applied. Wait out the window with nothing calling the grant, then rerun; if it repeats, plan and apply the server's access |
| `The MOSAIC API token <persona>'s browser sent is for a different account` | That profile is signed in as someone else. Delete its profile and sign in again as the right account |
| `The MOSAIC API token … expires in N seconds` | Entra issues the token when the driver signs the persona in again, so this comes from a short token lifetime policy or a long wait. Lower the timeout the message names: `--revocation-timeout`, or for M9 `--attribution-timeout` or `--model-grant-timeout` |
| `Stopped before Apply on …` or `Stopped before Unpublish model on …` | The plan, or the review dialog, reached beyond the model and grant the test was changing, so the suite closed it without running the plan. Before Apply, something else is saved on that model and not yet applied. Review it on the Entitlements page, and apply or undo it by hand before running the test again. Before Unpublish model, the unpublish review didn't match what MOSAIC created for the disposable model. Open Unpublish on its row on the Models page, read the review and cancel it, and don't run `90-cleanup` again until the review lists only that model's resources |

## Quality checks

```powershell
npm run test:unit
npm run typecheck
npm run lint
```
