# End-to-end model import and access roadmap

This roadmap proves that MOSAIC works from start to finish **through its own UI**, against a real
deployed environment:

1. An administrator imports real Azure OpenAI and Microsoft Foundry endpoints.
2. The administrator publishes their models through Azure API Management (APIM).
3. The administrator grants those models to people and to a workload application.
4. Those callers, and only those callers, can invoke the models through the gateway.

Phase 11 extends the same path to MCP servers: published, granted, called and measured the same
way, including an MCP server that itself calls a model through MOSAIC.

A person signs in (including MFA) and the Playwright harness in [`e2e/`](../../e2e) drives the
browser. Every phase ends at a checkpoint, so the work can pause between phases and each tenant
change can be approved on its own. How to run the harness is in the [runbook](runbook.md).

**Status key:** ✅ done · 🔄 in progress · ⏳ waiting (approval, merge, or deploy) · ⬜ not started

## Principles

- **UI first.** Every admin and end-user step goes through the web console or the portal. Direct
  API or `az` calls only verify results, or make the tenant changes the UI tells you to make.
- **Human in the loop.** People sign in to persistent browser profiles themselves. The harness
  never sees a password and can't read tokens or revealed keys.
- **Observe, then remediate.** Permissions aren't granted ahead of time. A journey first records
  what MOSAIC shows when it lacks access, including the remediation it suggests. Then it applies
  that remediation and checks again.
- **Tenant changes need approval.** Role assignments, app registrations, consent and model
  deployments happen in batches the environment owner approves. Each change goes into a local
  change ledger with its rollback command.
- **Secrets stay out of artifacts.**
  - Snapshots, logs and failure messages are redacted.
  - Snapshots and text reads work only on MOSAIC pages, never on a sign-in page or while a person
    is signing in.
  - Screenshots mask `[data-secret]` elements, revealed keys, and password and one-time-code
    fields.
  - Playwright traces, video and its own failure snapshot are off. The HTML report is opt-in,
    because it records steps before the harness can redact them.
  - Runtime credentials stay in process memory.
- **Cost is opt-in.** Flags gate writes (`MOSAIC_E2E_ALLOW_WRITES`) and inference
  (`MOSAIC_E2E_SEND_MODEL_REQUESTS`), and request payloads are small and bounded.
- **Safe to commit.** Tenant IDs, UPNs, object IDs and resource IDs live only in the gitignored
  `e2e/targets.local.json`. This document names roles, not accounts.

## Environment and personas

The target is a development deployment created with `azd up`: API, web console and portal App
Services, a Developer-tier (classic) APIM with a system-assigned identity, and Cosmos DB.

| Persona (manifest role) | Starting MOSAIC role | Purpose |
| --- | --- | --- |
| `admin` | Admin | Drives every console journey |
| `user` (member) | User, assigned in Phase 2 | Gets grants directly; calls models by key and by Entra token |
| `noRole` (member) | None | Portal denial first (P1); then requests access and is approved |
| `guest` (B2B) | User | Cross-tenant sign-in; holds only User, so the console must withhold admin data (A1). The first to request access (P2, P4, A13) |
| `outsider` (member) | None | Never granted anything; every runtime call must be denied |
| Workload application | `Models.Invoke.Application` | Client-credentials token plus an admin-handed-off key |

## Targets

Seven endpoints cover both providers, all four registration paths and every model family MOSAIC
publishes today. One more endpoint is a deliberate negative case.

| Target | Kind | Registered by | Models to publish |
| --- | --- | --- | --- |
| AOAI A | Azure OpenAI | Pasting the account resource ID (first, to observe the missing-permission state) | Two chat deployments |
| AOAI B | Azure OpenAI | Discovery suggestion | A chat deployment and an o-series reasoning deployment |
| AOAI C | Azure OpenAI | Discovery suggestion, in another region | A reasoning deployment and a chat deployment |
| Foundry multi-provider | Foundry (AIServices) | Discovery suggestion | xAI Grok, Meta Llama and DeepSeek. Azure refused Claude here (Phase 2) |
| Foundry project | Foundry (AIServices) | Pasting a Foundry **project** resource ID | A small OpenAI model and Llama 4 |
| Foundry hub-connected | Foundry (AIServices) | Discovery suggestion | A chat model |
| Foundry in another tenant | Foundry (AIServices), in another Entra tenant | Its URL and an API key pasted in the console (G18) | Anthropic Claude (G5) |
| Private AOAI (negative) | Azure OpenAI | Pasting the resource ID | None. Public network access is off, so the gateway can't reach it |

Gemini and AWS Bedrock follow in Phase 10.

## Product gaps found

Exploring the deployed build turned up the gaps below. Each gets its own pull request, with
tests and README or ADR updates wherever a decision changes.

| ID | Gap | Fix | Status |
| --- | --- | --- | --- |
| Entra fix | `main` reused one value for the runtime app role and the delegated scope. Entra rejects that, so a fresh `azd up` can't bootstrap the runtime registration | Give the application role and the delegated scope distinct values ([#15](https://github.com/microsoft/mosaic-apim/pull/15)) | ✅ merged |
| G1 | The console has no control to switch a gateway into `manage` mode, which publishing requires | Add a "Management mode" control with an explicit confirmation ([#18](https://github.com/microsoft/mosaic-apim/pull/18)) | ✅ merged |
| G2 | Approving an access request creates no grant, even though the banner says it saved grant intent | Approve opens a short dialog with limits prefilled; the server creates and links the grant intent in one step ([#20](https://github.com/microsoft/mosaic-apim/pull/20)) | ✅ merged |
| G3 | The portal can't show connection details or reveal keys, so an end user can't get a credential | Add portal connection details and masked, transient key reveal ([#21](https://github.com/microsoft/mosaic-apim/pull/21)) | ✅ merged |
| G4 | No client registration lets an end user get a `Models.Invoke` token | Optional public client registration with a tenant-wide grant for `Models.Invoke`, and its client ID in connection details. Builds on the Entra fix ([#19](https://github.com/microsoft/mosaic-apim/pull/19)) | ✅ merged |
| G5 | Anthropic deployments get the chat-completions API shape, but Claude needs the Messages API. `llm-token-limit` supports Anthropic only on APIM v2 tiers | Publish each deployment by format: Claude gets the Anthropic Messages shape. On classic tiers, Claude publications apply no token limits, and grants use call limits instead ([#22](https://github.com/microsoft/mosaic-apim/pull/22)) | ✅ merged |
| G6 | Only if Grok or Llama fail on `/models/chat/completions` through APIM | Add OpenAI v1 routes | ⬜ likely unnecessary: both reached their models there with a grant key in Phase 8; R1 confirms with tokens |
| G7 | When MOSAIC's identity can see no subscriptions, discovery shows nothing at all: no suggestions, no unreadable subscriptions and no hint. Found live in Phase 3 | Say how many subscriptions were scanned. When there are none, or the list fails, show the Reader command for the subscriptions MOSAIC already knows about ([#17](https://github.com/microsoft/mosaic-apim/pull/17)) | ✅ merged |
| G8 | Gateway runtime readiness can disagree with what the gateway can actually call. It accepts exactly one role, so a sufficient role such as the Cognitive Services User role it recommends before it can read the account is later reported as missing. It also checks a project-registered endpoint at the project scope, although published APIs call the parent resource, where a project-scoped grant doesn't apply | Judge readiness at the account the published API calls, and accept any role whose data actions cover the published operations. Recommend only roles the check accepts: Cognitive Services OpenAI User for Azure OpenAI, and Foundry User otherwise. A deny assignment, or disabled public access with no virtual network on the gateway, means "cannot invoke". Conditions MOSAIC can't evaluate mean "not confirmed". The endpoint's Access card shows its network, firewall and key settings. Readiness covers every API MOSAIC can publish from the endpoint, including Anthropic Messages on AI Services accounts ([#23](https://github.com/microsoft/mosaic-apim/pull/23)) | ✅ merged |
| G9 | After a redeploy, an open browser kept running the previous console build. Both web apps' nginx serve `index.html`, the SPA routes and `/config.js` with no `Cache-Control`, so browsers cache them heuristically and load the old hashed bundle. Found live in Phase 3 | Revalidate the HTML, the SPA fallback and `/config.js` on every load. Cache the hashed `/assets/` files as immutable, and return 404 for a missing asset instead of the SPA page. Keep the security headers on every response ([#29](https://github.com/microsoft/mosaic-apim/pull/29)) | ✅ merged |
| G10 | Discovery said "Scanned 1 subscription. Nothing new to register." while the subscription held 56 Azure AI accounts MOSAIC couldn't read. MOSAIC's roles were all on single resources, and ARM silently filters a subscription-wide list to what the caller can read, so the scan looked complete. Found live in Phase 3 | Check MOSAIC's own permissions at each scanned subscription. When it can read only part of one, say so and show the subscription Reader command. Also say, wherever MOSAIC asks for a role on its own identity, that a new role can take a while to apply (O7) ([#28](https://github.com/microsoft/mosaic-apim/pull/28)) | ✅ merged |
| G11 | Discovery suggested, and registration accepted, the parent account of a registered Foundry project. The second endpoint had the same URL, and syncing it listed the project's deployments again, each publishable on its own (O9). **Remove** on an endpoint deletes it and its synced models at once, and the server doesn't check publications, so a publication whose endpoint is gone can't be re-planned or applied (O10). Found live in Phase 3 | Treat each registration as covering its account: don't suggest a covered account, and refuse a registration that overlaps one, naming it. Confirm before removing an endpoint, and refuse while publications depend on it ([#27](https://github.com/microsoft/mosaic-apim/pull/27)) | ✅ merged |
| G12 | Endpoint settings leave out the Key authentication row and its note when an account doesn't set `disableLocalAuth`. Azure leaves it unset by default, which means keys are enabled, so both Azure OpenAI targets showed nothing (O8). Found live in Phase 3 | Treat an unset value on a readable account as Enabled, and add the note ([#26](https://github.com/microsoft/mosaic-apim/pull/26)) | ✅ merged |
| G13 | **No model can be published.** The default publish plan creates the policy fragment before the backend its `set-backend-service` names. APIM accepts the fragment PUT, then its validation fails it: "Backend with id '…' could not be found." The run rolls back, so API Management is left unchanged. The console shows only "The Azure operation did not succeed", because MOSAIC drops Azure's error when it polls the operation. The governed-access plan already creates the backend first. Also, the fragment PUT is long-running even when it updates an existing fragment (its 200 carries a poll header too), but MOSAIC polls only 201 and 202. So a fragment update that APIM rejects would be reported as success, which matters for governed access and every later re-apply. Found live in Phase 5 (A8) | Create the backend before the fragment, which also makes teardown remove the fragment first. Refuse to apply a plan saved in the old order, and ask for a re-plan. Show Azure's reason when an operation fails, and when a request is refused outright. Poll any write response that carries a poll header. Make the test fake of APIM validate fragments the way APIM does ([#30](https://github.com/microsoft/mosaic-apim/pull/30)) | ✅ merged |
| G14 | The console never tells someone without the Admin role that it isn't for them. For an account with only the User role, and for one with no MOSAIC role, it renders the whole admin shell with its actions, labels the account "Global Admin" (hard-coded for every Entra sign-in), calls it the administrator on Settings and the profile page, and shows "Unable to load data" in every live section. The API refuses correctly, so no admin data is shown. The portal already handles the same case with one clear denial and a sign-out button. Initials also keep punctuation, so a display name like "Name (Team)" shows "N(". Found live in A1 | A new `GET /api/v1/console/me` returns the caller's MOSAIC roles from the access token, and the console asks it before rendering. Without the Admin role it shows one card instead of the shell: no access for an account with no role, and a pointer to the end-user portal for a User, each with **Sign out**. The account label reads "MOSAIC Admin", and initials use letters and digits only. No infrastructure or app-setting change, so it ships in an image-only deploy, with the API before or together with the web app ([#31](https://github.com/microsoft/mosaic-apim/pull/31)) | ✅ deployed |
| G16 | **Re-plan** says "Created a fresh publish plan. Review it before applying.", but nothing shows that plan: the console discards it, and the API can't return a saved plan. On a publication without governed access, the row's **Apply** then applies the saved plan with no review. The README says re-planning shows how API Management has diverged, and the page says changes are made only after a reviewed plan is applied. Found live in A8 | Remove the row's **Apply**. **Re-plan** makes a fresh plan and opens it in the publish dialog's review, and only **Apply plan** there applies it. When an apply is refused, the dialog says "MOSAIC didn't apply the plan you reviewed", gives the server's reason, and says it has already re-planned (O13). Web only ([#32](https://github.com/microsoft/mosaic-apim/pull/32)) | ✅ deployed |
| G17 | **Governed access can't be applied.** The policy expressions MOSAIC generates for governed access use single-statement control flow, such as `if (…) return "";`. APIM rejects every such fragment: "Block statements must be enclosed in "{" and "}". You cannot use single-statement control-flow statements in CSHTML pages." The apply then falls back to its last safe snapshot, as designed, which for a publication that never had governed access denies every call. The test fake of APIM accepts any expression, so the unit tests passed. Found live in A11 | Brace every control-flow body in the generated expressions, and make the APIM fake reject unbraced control flow the way APIM does. API only, so it ships in an image-only deploy, Batch 3d | ✅ deployed ([#36](https://github.com/microsoft/mosaic-apim/pull/36)); verified live in A11 |
| G18 | **An Azure AI resource in another Entra tenant can't be published.** MOSAIC registers Azure OpenAI and Foundry endpoints only by resource ID, reads them with its managed identity, and has the gateway call them with its own. Neither identity can reach another tenant's resource: the environment owner's Claude deployment answered "Token tenant … does not match resource tenant". MOSAIC also had no way for API Management to use a key that MOSAIC never holds | Register an Azure AI endpoint by its URL and a Key Vault secret URI, never the key, and declare its deployments, because a key can't list them. API Management reads the key from Key Vault through a secret named value, with its own identity. The policy removes every credential a caller sent, then sets the backend's key header: `api-key`, or `x-api-key` for Claude. Readiness checks that the endpoint accepts the key, with a request that runs no model, and that each gateway can read the vault. The gateway needs Key Vault Secrets User on the environment vault, and MOSAIC's API needs Reader there ([#69](https://github.com/microsoft/mosaic-apim/pull/69), ADR 0018). [#77](https://github.com/microsoft/mosaic-apim/pull/77) (ADR 0021) lets the admin paste the key in the console instead: MOSAIC writes it to a new secret in the environment's vault and keeps only the secret's URI. **Replace API key** writes a new version, and removing the endpoint deletes the secret. For that, MOSAIC's API needs Key Vault Secrets Officer on the vault | ✅ deployed in Batches 3g and 3h. Verified live: the owner registered the Claude endpoint by pasting its key, and a call reached Claude through APIM (Phase 8) |
| G19 | **A model call that an MCP server makes for a person isn't attributed to that person.** MOSAIC governs and measures a person's calls to an MCP server and their calls to a model separately. When a server's tool calls a model through MOSAIC, the model call belongs to the server's identity, and nothing links it to the person who called the tool. The environment owner wants to see that consumption per person, without each person needing a grant on the model (Phase 11) | Decided by the environment owner on 2 October 2026: the server calls the model with its own application grant, which alone decides access, limits and cost center, and passes on who called it. MOSAIC accepts that only from approved intermediaries, and records the person for usage, never for access. An ADR will settle how the person is passed on and trusted: a header the gateway signs when it validates the person's token, or a reference to the person's MCP call that analytics joins to the gateway's logs. It also settles who counts as an approved intermediary, and how usage reports the person. Built as ADR 0025: the MCP server forwards a reference to the person's MCP call, which MOSAIC matches in the gateway's logs only for the application the server names as its model caller ([#90](https://github.com/microsoft/mosaic-apim/pull/90), [#91](https://github.com/microsoft/mosaic-apim/pull/91), [#92](https://github.com/microsoft/mosaic-apim/pull/92)) | ✅ deployed in Batch 3k; checked live in M9 (Phase 11) |

There is no G15. What was first logged as G15 turned out to be APIM's own behavior, and is
recorded as O12.

G12, G9, G11, G13 and G10 are merged, in that order, and Batch 3b deployed them. G14
([#31](https://github.com/microsoft/mosaic-apim/pull/31)), a test fix
([#33](https://github.com/microsoft/mosaic-apim/pull/33)) and G16
([#32](https://github.com/microsoft/mosaic-apim/pull/32)) merged next. #25, which follows G16,
merged after them. It merged `main` without conflicts, and its test that clicked the row's
**Apply**, which G16 removed, now clicks **Re-plan**. A new test checks that focus lands in the
review **Re-plan** opens for a publication without governed access. Batch 3c deployed all of
them, with the O15 and O16 fix ([#35](https://github.com/microsoft/mosaic-apim/pull/35)). G17
([#36](https://github.com/microsoft/mosaic-apim/pull/36)) merged after Batch 3c. It braces all 11
control-flow bodies in the governed policy expressions, and the APIM fake now parses every
expression the way API Management's Razor parser does, so an unbraced body fails the tests. It
ships in Batch 3d, because no governed apply can succeed without it. The O19 fix
([#37](https://github.com/microsoft/mosaic-apim/pull/37)) merged after G17. It changes only the
web app, so it can ship in the same batch. The O23 fix
([#38](https://github.com/microsoft/mosaic-apim/pull/38)), for the web app, and the O18 fix
([#39](https://github.com/microsoft/mosaic-apim/pull/39)), for the API and the portal, merged
next, so Batch 3d now deploys all three apps. Environments and the end-user usage report
([#40](https://github.com/microsoft/mosaic-apim/pull/40)) merged after them, and they join
Batch 3d too. MOSAIC now classifies gateways, endpoints and MCP servers by environment, and it
checks each gateway and endpoint pairing when a model is published. The portal gains a
**Usage & cost** page, whose figures are simulated and labeled so until MOSAIC reads Log
Analytics. #40 changes no
infrastructure or app setting and keeps its data in the existing Cosmos containers. Everything
starts Unclassified, which warns but never blocks, and a plan reviewed before the upgrade must be
planned again once (ADR 0014).

Batch 3d deployed all of them to the API, the portal and the console, image-only. A read-only
inventory showed that the deploy left API Management unchanged. A11's retry then applied governed
access, so G17's fix works against API Management, and #37, #38 and #40 were checked live
(Phases 6 and 7).

The test fix is for web tests that G11 added and that failed intermittently. About 250 ms after a
Fluent dialog opens, the rest of the page becomes `aria-hidden`, and it stays hidden for about
250 ms after the dialog closes. Role queries skip hidden content, so the tests failed whenever
they queried the page inside one of those windows. #33 changes only the tests. It left two
follow-ups, neither of which blocks this plan. Both are filed as
[#48](https://github.com/microsoft/mosaic-apim/issues/48):

- "surfaces the conflict message when removing a publication that still owns resources" would
  still pass if the dialog closed on refusal, because it can query a dialog that's no longer in
  the page.
- The web app's Vitest setup has globals turned off, so Testing Library never sets
  `IS_REACT_ACT_ENVIRONMENT`, and `act()` warnings can't appear.

Before those five merged, #25, G13 (both commits), G9, G12, G10 and G11 were merged onto `main`
locally, in that order, and combined cleanly except for two test files. G10 merged last and
resolved both by keeping both sides:

- G10 and G11 both change the import block of `apps/api/tests/test_model_endpoint_api.py`.
- G10 and G13 both add fields to the fake Azure AI service in `apps/api/tests/aoai_double.py`.

On that combined tree:

- The API passes ruff, mypy and 978 tests.
- The web app passes 213 tests, typecheck, lint and build.
- The script tests pass.

## Phases

### Phase 0: Baseline and discovery ✅

- Confirm the live build, health, readiness, Entra registrations and managed-identity roles. The
  live deployment was built from the Entra fix, not from `main`, which is why that fix lands
  first.
- Inventory the tenant's Azure OpenAI and AIServices accounts: network access, local auth,
  deployments, and whether partner models are available with quota.
- Map the personas and check their current app-role assignments. None of the MOSAIC registrations
  require assignment, so accounts without a role can still sign in and see the denial experience.
- **Exit:** targets and personas are recorded in the local manifest; this roadmap is written.

### Phase 1: Playwright harness ✅

- `e2e/` package: a persona profile per account, a local live driver for human-in-the-loop
  sessions, a sign-in helper, redaction, and read-only smoke specs (S1, S2, A0, A1, P0, P1). S2
  checks that the admin and portal APIs reject anonymous and malformed-token requests.
- **Exit:** unit tests, typecheck and lint pass, and the live driver can open every persona.

### Phase 2: Tenant prerequisites ✅ the User role, the workload, and Claude through G18

Each batch runs only after approval and is recorded in the change ledger.

- Make a Claude deployment available. The plan was to deploy one (small capacity) to the
  multi-provider Foundry resource, after checking eligibility, region, quota and marketplace terms.
  In the end it came from the owner's Foundry resource in another tenant, through G18.
- Assign the MOSAIC User role to the `user` persona. `noRole` keeps no role until Phase 7.
- Create the workload app registration and service principal, assign `Models.Invoke.Application`,
  and grant admin consent.
- Reader and Cognitive Services roles are **not** granted here. Phase 3 grants them from the
  remediation commands the UI shows.
- **Exit:** the ledger lists every change and its rollback command.

Progress (Batch 1, approved and applied):

- ✅ The `user` persona holds the MOSAIC User role.
- ✅ The workload app registration and its service principal exist. The service principal holds
  `Models.Invoke.Application`, and for an application permission that assignment is the admin
  consent. Its short-lived client secret, which R3 needed, was created in Phase 8 with the
  environment owner's approval, and is deleted once the sitting's runs are done.
- Superseded: Azure refused the Claude deployment on the multi-provider resource. An Anthropic
  deployment must carry the customer's organization name, country and industry for Anthropic's
  terms (`properties.modelProviderData`), and Azure CLI 2.83 has no option for them. The model is
  available in the region and its quota is unused. The environment owner chose to deploy it in
  the Foundry portal instead.
- ✅ Claude, 2026-10-01: the environment owner's Claude deployment is in another Entra tenant,
  which MOSAIC's and the gateway's managed identities can't reach. G18 lets MOSAIC publish it with
  an API key held in Key Vault. After Batches 3g and 3h deployed G18 and #77, the owner registered
  the endpoint in the console by pasting its key, and declared three Claude deployments. MOSAIC
  wrote the key to a new, tagged secret in the environment's vault. The endpoint's access card
  showed that the endpoint accepts the key and that the gateway can read it. It is then published
  and granted like any other model (Phase 8).

### Phase 3: Live, admin imports endpoints (A2 to A6) ✅ except seeing A2's partial-scan card

- Paste AOAI A before MOSAIC can read anything. Record "cannot read" and the exact remediation
  command, run it, and check again. Then run the subscription-scope remediation so discovery
  suggestions list the rest.
- Register the suggestions and the pasted Foundry project. A duplicate registration is rejected.
- Synced deployments must match the `az` inventory.
- Gateway runtime readiness starts at "cannot invoke" with a command. Assign the APIM identity's
  role from that command, and readiness moves to "can invoke". The private endpoint stays at
  "cannot invoke" because the gateway has no private path to it. This needs G8
  ([#23](https://github.com/microsoft/mosaic-apim/pull/23)), which also checks the network path. The
  older build checks only roles.
  - Assign gateway roles after the redeploy that ships G8, and after MOSAIC can read the account,
    so the recommendation reflects the account kind. That's Cognitive Services OpenAI User for Azure
    OpenAI, and Foundry User for AIServices accounts.
  - For the Foundry project target, assign Foundry User on the parent resource, not the project.
    G8 checks that resource, and the row says "Checked at {account}, the resource the published
    API calls." A project-scoped grant is reported as narrower than the published API needs, and
    doesn't count.
- **Exit:** every target is registered, readable and invocable, except the negative case.

Progress, before any role was granted:

- ✅ AOAI A and the Foundry project registered by pasting their resource IDs. Both show
  **Access needed**, no models, and a disabled **Sync models**.
- ✅ A4, first half: the endpoint shows "MOSAIC cannot read this endpoint" with a resource-scoped
  Reader command for MOSAIC's managed identity.
- ✅ A6, first half: after the gateway's **Check access**, the gateway card explains that MOSAIC
  can't read role assignments on the endpoint, says this is not a denial, and gives a command that
  grants the APIM identity an invoke role.
- ✅ A3, duplicates: registering the same ID again, even in lowercase, is rejected with "This
  Azure AI resource is already registered with MOSAIC".
- ⚠️ A2: with no role anywhere, discovery showed nothing at all (G7). After G7 deploys, the Models
  page should show "MOSAIC can't see any subscriptions" with a Reader command for the deployment
  subscription.
- ⏳ Next: apply the remediation commands, then run A4 and A6 again, followed by A5 and A2.

Progress on the new build (G7 and G8 deployed):

- ✅ A4 and A6, before: with no role, the endpoint shows "MOSAIC cannot read this endpoint" and the
  resource-scoped Reader command. The new **Endpoint settings** region reads "Not known yet. MOSAIC
  cannot read this resource." The gateway row reads "not confirmed", says this is not a denial, and
  recommends Foundry User on the account, noting that it's accepted for either kind until MOSAIC
  can read the resource.
- ⚠️ A2, before: discovery reads "Scanned 1 subscription. Nothing new to register." The
  subscription holds 56 Azure AI accounts, but MOSAIC could read none of them (G10).
- ✅ Reader granted to MOSAIC on AOAI A (2a), exactly as the UI's command says, and then on the
  subscription (2b).
- ✅ A4, after: AOAI A became readable, but only after 2b; 2a alone hadn't applied after 17
  minutes (O7). **Check access** corrected the provider to Azure OpenAI and the endpoint to
  `https://<account>.openai.azure.com/`, which settles O1's first question.
- ✅ A3, pasted negative: the private account registered by resource ID and was readable at once.
  It shows kind OpenAI and public network access Disabled, with the note that a gateway can only
  reach it privately. The gateway row reads "cannot invoke": no role, and MOSAIC hasn't recorded
  whether the gateway is on a virtual network, so it asks for the gateway's access check to run
  again.
- ✅ A2, after 2b: discovery reads "Scanned 1 subscription." and lists 24 suggestions, including
  the four remaining targets. Registered endpoints are left out. The message for a partly
  readable subscription waits for G10.
- ✅ A3, from suggestions: AOAI B, AOAI C, Foundry multi-provider
  and Foundry hub-connected each registered with one click on **Register**, and each was
  readable at once.
- ⚠️ O9: the Foundry project's parent account was still suggested, and registering it succeeded.
  The second endpoint had the project's URL, and syncing it listed the same six deployments
  again. **Remove** deleted it with one click and no confirmation (O10), and discovery then
  suggested it again. G11 fixes both.
- ✅ A5: **Sync models** on all eight endpoints. Every row matched
  `az cognitiveservices account deployment list` on deployment, model, version, SKU and capacity,
  and state: 51 rows, or 45 deployments without the duplicate. That covers deployments named
  differently from their model (`gpt-35-turbo` runs gpt-4.1-mini), disabled deployments, and xAI,
  DeepSeek and Meta models. Each row shows its API shape: chat completions, embeddings, image
  generation or realtime. sora-2 has none.
- ✅ A6, network: after the gateway's **Check access**, MOSAIC recorded that the gateway has no
  virtual network. The private account then read "cannot invoke": public network access is
  disabled, so the gateway has no network path to it "whatever roles it holds".
- ✅ A6, after 2c: the APIM identity got the role each gateway row showed (02:45). A minute later,
  **Check access** read "can invoke" on all six public targets, with "Satisfied by {role},
  assigned directly on {account}". The Foundry project adds "Checked at
  {account}, the resource the published API calls." The private account
  still reads "cannot invoke" for the network, and now says "The role requirement is met by
  Cognitive Services OpenAI User".
- "Can invoke" is judged from role assignments, which MOSAIC can list at once. Whether the data
  plane honors them yet is for Phase 8 to show; O7 suggests allowing tens of minutes.
- ✅ After Batch 3b deployed G10: discovery on the subscription, now readable at subscription
  scope, reports no partial scan. ⏭️ Seeing G10's card for a partly readable subscription would
  mean removing MOSAIC's subscription Reader, a tenant change, so G10's tests cover it instead.

### Phase 4: Close product gaps ✅

- ✅ Every gap PR is reviewed and merged: the Entra fix, G1, G2, G3, G4, G5, G7 and G8
  ([#23](https://github.com/microsoft/mosaic-apim/pull/23)) are on `main`. G8 requires every
  published operation to declare its data action. G5 merged first, so G8 added them for G5's
  Anthropic Messages operations.
- Merge `main` into the e2e branch and rebuild the azd environment from live values. Run
  `azd provision --preview`, and after approval run `azd up`, because G4 changes the Entra hook.
  Then re-run the smoke specs.
  - The preview is safe to run: azd skips project hooks under `--preview`. That also means it
    can't show the Entra changes the hooks make, such as creating G4's model client and its
    tenant-wide grant. Those are listed separately for approval.
  - The template resets each web app to a placeholder image, and `azd deploy` then pushes the
    real one. Always redeploy with `azd up`. Running `azd provision` on its own leaves the apps
    on placeholders.
  - What-if can't see app settings. Before approving, check that every live app setting name is
    still in the template, because provisioning replaces the whole list.
  - ✅ The environment is rebuilt, and a preview plus a full what-if against `main` show no
    creates or deletes. The preview ran again on the final `main`, after G8 merged, with the
    same result.
  - ✅ Deployed `main` (`ada6210`) with `azd up` after approval. The hooks created G4's model
    client and its tenant-wide grant, and set its client ID on the API. The API, web and portal
    images were replaced together, and `/healthz` and `/readyz` pass.
  - ✅ Smoke specs S1, S2 and A0 pass on the new build. A0 signs in without any prompt through the
    persona's saved Entra session. A1, P0 and P1 later passed live, once someone signed the
    `guest` and `noRole` personas in (see Phase 7).
  - The build context is the repository root, so `.dockerignore` excludes `e2e/`. Its local
    manifest and test results hold tenant details that must never reach an image.
  - An azd environment rebuilt from live values needs `MOSAIC_PYTHON_INDEX_URL` set, as the README
    says. Left empty, the build argument overrides the Dockerfile's default package index.
  - ✅ Batch 3b, after approval: `azd deploy` put `main` at G10's merge, which carries G9 to G13,
    on the API, web and portal (images only). Every check passed: health; G9's cache headers
    (`/config.js` returns `no-store`, which G9 intends); S1, S2 and A0; G10, with no partial scan
    reported; G11, which refused an overlapping registration by naming the project it overlaps,
    and confirms before **Remove**; G12, after **Check access** (O5); and the A8 retry (Phase 5).
  - ✅ Batch 3c, after approval: `azd deploy` put `main` with G14
    ([#31](https://github.com/microsoft/mosaic-apim/pull/31)), G16
    ([#32](https://github.com/microsoft/mosaic-apim/pull/32)), the dialog fix for O4
    ([#25](https://github.com/microsoft/mosaic-apim/pull/25)) and the O15 and O16 fix
    ([#35](https://github.com/microsoft/mosaic-apim/pull/35)) on the API, web and portal (images
    only). Since Batch 3b, `main` had changed no infrastructure, Entra hook, app setting,
    Dockerfile, nginx configuration or package manifest. The old containers kept answering for two
    to four minutes after azd finished, so checks waited until the API listed G14's route and each
    app served its new bundle. Every check passed: health; G9's cache headers; a read-only
    inventory showing the deploy left API Management alone; S1, S2, A0 with "MOSAIC Admin" and no
    "Global Admin", and A1's no-role case; G16 and O4 live (Phase 5). Phase 6 had started on the
    Batch 3b build: MOSAIC UI actions need no approval, and the harness reached the `aria-hidden`
    review (O4) by CSS instead of by role.
  - Next, Batch 3d (API, web and portal images, after approval), all merged: G17
    ([#36](https://github.com/microsoft/mosaic-apim/pull/36)) and the O18 fix
    ([#39](https://github.com/microsoft/mosaic-apim/pull/39)) on the API, the O19 and O23 fixes
    ([#37](https://github.com/microsoft/mosaic-apim/pull/37),
    [#38](https://github.com/microsoft/mosaic-apim/pull/38)) on the web app, the O18 fix on
    the portal, and environments with the usage report
    ([#40](https://github.com/microsoft/mosaic-apim/pull/40)) on all three. Since Batch 3c,
    `main` has changed no infrastructure, Entra hook, app setting, Dockerfile, nginx
    configuration or package manifest, and #40 stores its data in the existing Cosmos
    containers. The API's, the web app's and the portal's tests pass on it, and nothing has been
    deployed since Batch 3c.
- **Exit:** the deployed build contains G1 to G5, G7 and G8, and the smoke specs pass.

### Phase 5: Live, admin publishes (A7 to A9) ✅

- Switch the gateway to manage mode (G1). Publish each target deployment and review the plan
  steps and policy facets before applying. Every step must succeed.
- Re-planning a publication that nobody changed must plan no creates or deletes, and applying
  that plan must leave API Management as it was. A plan compares which resources exist, not what
  they contain, so every step of such a plan is an update ("Replace …"). Cross-check the APIM
  APIs, products, backends, fragments and policies with `az` before and after the apply.
- Make the published models visible in the portal catalog.

Twelve deployments were publishable then. Claude came later, from the endpoint in another tenant
(Phase 8).

| Endpoint | Deployments |
| --- | --- |
| AOAI A | `gpt-35-turbo`, `gpt-4o` |
| AOAI B | `gpt-4o-mini`, `o3-mini` |
| AOAI C | `o4-mini`, `gpt-4o` |
| Foundry multi-provider | `grok-4.3`, `Llama-3.3-70B-Instruct`, `DeepSeek-V4-Pro` |
| Foundry project | `gpt-5.4-nano`, `Llama-4-Maverick-17B-128E-Instruct-FP8` |
| Foundry hub-connected | `gpt-5.1-chat` |

Progress:

- ✅ A7, allowed with write access: the gateway page's **Management mode** card read "This
  gateway is in observe mode…", with write access "granted". Choosing **Manage** opened "Switch to
  manage mode?". It listed what an apply creates and what MOSAIC never changes. **Switch to
  manage** (02:55) then showed "Switched to manage mode. Nothing in API Management changed. Models
  can now be published to this gateway."
- ⏭️ A7, refused without write access: this can only be observed by removing MOSAIC's APIM write
  role, which is a tenant change. G1's unit tests cover it.
- Before the first apply, a read-only inventory of the gateway found 1 API
  (`echo-api`), 2 products, no backends or policy fragments, 3 subscriptions and 6 named values.
  The global policy's hash was also recorded.
- ✅ The publish dialog's first step lists all 45 synced deployments with their API shape,
  suggested path and runtime verdict:
  - Realtime and Sora deployments show "Not publishable".
  - The four deployments on the Private AOAI target show "Gateway may not be able to call this model".
  - All the others show "Runtime permissions observed".
  - Paths come from the endpoint and deployment names, so the `gpt-4o` deployments on six
    endpoints get six different paths.
- ✅ Plan review for AOAI A `gpt-35-turbo`, with the dialog's defaults: subscription
  required, 12,000 tokens per minute counted per subscription, and prompt tokens estimated.
  - The plan had 14 create steps: fragment, backend, API, 7 operations, API policy, product,
    product link and subscription. It had no warnings.
  - The facets showed managed-identity authentication to `https://cognitiveservices.azure.com`,
    routing, the token limit, token telemetry and the shared rule set.
- ❌ A8, first apply (03:00): step 1 failed and the run rolled back (G13). The console said only
  "The Azure operation did not succeed". The activity log showed the reason: APIM rejected the
  fragment because the backend it routes to didn't exist yet. The inventory afterwards matched the
  baseline exactly, including the global policy hash, and the publication showed "Rolled back"
  with **Re-plan** and **Apply**.
- ✅ A8, retry after Batch 3b deployed G13:
  - **Apply** on the rolled-back row was refused, because the saved plan used the old order. The
    console re-planned by itself, which took about a minute, and opened the publish dialog on its
    review step with the refusal as a warning (O13).
  - The new plan had 14 create steps: backend first, then the fragment, the API, 7 operations, the
    API policy, the product, the product link and the subscription.
  - That review dialog was hidden from assistive technology (O4), so the harness had to reach its
    buttons by CSS.
  - **Apply plan** succeeded in about 40 seconds, and the publication shows Published.
- ✅ A8, the other eleven deployments, each with the dialog's defaults: every plan put the backend
  first and the fragment second, every step was a create, and every apply succeeded, most in 11
  to 17 seconds. Azure OpenAI plans have 14 steps with 7 operations. Foundry plans have 10 steps
  with chat completions, embeddings and model info.
- ✅ A8, read-only cross-check against the baseline: 12 new APIs, products, backends and policy
  fragments, and 24 new subscriptions. The named values are unchanged, and the global policy is
  still APIM's default. Half of the new subscriptions are MOSAIC's. APIM created the other half
  for its Administrator when each product was created (O12).
- ⚠️ A8, re-plan: **Re-plan** on AOAI A `gpt-35-turbo` finished in about 10 seconds with "Created a
  fresh publish plan. Review it before applying.", but nothing showed the plan (G16). The row's
  **Apply** would have applied that unreviewed plan, so it was left unused, and G16 removes it.
  After Batch 3c, re-plan each publication and review it: every step should be an update, with
  nothing created or deleted. Then apply one and confirm with `az` that API Management's
  resources and policies are unchanged.
- ✅ A8, re-plan after Batch 3c: every row offers **Re-plan**, **Unpublish** and **Remove**, and
  no **Apply**. **Re-plan** on each of the 11 publications without governed access opened
  "Publish a model" on "Step 3 of 4" with focus inside. Each plan was all updates (14 steps for
  Azure OpenAI, 10 for Foundry), with nothing created or deleted and no warnings. **Apply plan** on
  AOAI A `gpt-35-turbo` succeeded at all 14 steps in 23 seconds. Read-only inventories before and
  after, which hash the settings of every API, operation, API policy, product, backend, fragment,
  subscription and named value (keys aren't read), were identical, and so was the global policy.
  Re-planning the other ten changed nothing in API Management either. The governed `gpt-4o-mini`
  was reviewed from the Entitlements page instead (Phase 6).
- ✅ A9: each published model has a row under **Imported model APIs**, and its **Catalog** select
  starts at "Discoverable". The `guest` persona's portal catalog listed all 12, each with
  **Request access**. Setting AOAI C `o4-mini` to "Entitled users only" removed it from that
  catalog after a reload, and setting it back restored it. Every entry reads "No summary
  provided." (O14).

### Phase 6: Live, admin sets identity and governed access (A10 to A12, A16) ✅

- Create identity entries for the `user` persona and the workload service principal. The `noRole`
  persona is left unregistered on purpose, so Phase 7 shows that approving a request from someone
  MOSAIC hasn't seen before still produces a grant.
- Enable key and Entra access with limits, grant the `user` persona and the workload, and run the
  model-wide review and apply.
- Get the workload's connection details and key handoff from the console. The key never appears
  in logs.
- After Batch 3d, check environments (A16). **Settings** lists MOSAIC's six built-in
  environments, and every gateway, endpoint and MCP server starts Unclassified. Reviewing access
  or publishing with an Unclassified pairing shows a warning and isn't blocked. Classifying the
  gateway and the endpoints as Development then clears the warning. Leave **Require
  classification** off.

Progress:

- ✅ A10, `user` persona: **Add user** stores only the object ID and a local label, and the entry
  shows as Live. Adding the same object ID again is rejected with "Unable to add principal: A
  principal with this Entra object ID already exists".
- ✅ A10, workload, after Batch 1: **Add workload identity** stores the service principal's object
  ID, a local label and the type Service principal, and the entry shows as Live. Adding it again
  is refused with "A principal with this Entra object ID already exists". Focus is lost after
  **Save**, the pattern [#49](https://github.com/microsoft/mosaic-apim/issues/49) covers.
- ❌ A11, on the Batch 3b build, for AOAI B `gpt-4o-mini`, a publication without governed access
  until then. It failed on G17.
  - **Save access settings**, with keys and Entra both on, said "Saved governed-access intent
    only. Review and apply this model's plan to change API Management." The badge read "Access:
    pending". A read-only inventory then showed API Management unchanged, as the page promises.
  - **Add direct grant** gave the `user` persona 2,000 tokens per minute, 100,000 tokens a month
    and 60 calls a minute. The row read "Saved, not applied" and "Not bound".
  - **Review model changes** opened "Review model access" on "Step 3 of 4", but `aria-hidden`
    (O4). The plan had 20 steps in three stages. It retires the legacy subscription, creates a
    subscription for each grant, and replaces the fragment, operations and product. It then
    installs the governed policy and turns access on.
  - **Apply plan** failed at the fragment step after 23 seconds (G17). The dialog said "Apply
    failed. Do not assume the target access or revocation is active." and "Access was restricted
    to the last safe snapshot. Subscriptions were retained for a reviewed retry; suspension
    failures are listed above. No keys were rotated."
  - A read-only inventory confirmed the fallback: the fragment now denies every call, the API
    requires a subscription, and the legacy subscription and both grant subscriptions are
    suspended. The global policy is unchanged. Recovery may restrict access but never grant it,
    and this publication had no governed access to fall back to, so it serves no one until an
    apply succeeds. Nothing depended on it.
  - The console reports the failure everywhere. The publication reads Failed on Models. On
    Entitlements, the model reads "Access: failed" with APIM's reason and "Last applied methods:
    Deny all — both methods disabled", and each grant row reads "Apply failed".
  - Once G17 is deployed (Batch 3d), retry with **Review model changes** and **Apply plan**. The
    retry is also the first live check of what the APIM fake can't model, such as C# compile
    errors and the bare `catch { }` in the check of the request's model. A refusal fails closed
    again, as the first attempt did. No planned grant has a weekly call quota, the only
    expression that uses `DayOfWeek`, a type API Management's list of allowed types doesn't
    name. A Phase 8 grant can add one.
- ✅ O4 on the Batch 3c build: **Review model changes** for AOAI B `gpt-4o-mini` opened "Review
  model access" on "Step 3 of 4" with focus inside, and nothing hid it. The plan had A11's shape.
  It was closed without applying, so the model still reads "Access: failed" until Batch 3d.
- ✅ A11, retried on the Batch 3d build. **Review model changes** made a fresh plan: 20 steps, with
  the `user` and `guest` personas' grants and the publication's 12,000 tokens per minute. #40
  added a warning that both resources were unclassified. **Apply plan** succeeded in 26 seconds,
  every step included, so API Management accepted the governed fragment (G17). The model reads
  "Access: applied", with "Subscription key OR Entra token", at access version 2.
  - A read-only inventory showed that only this model's API and fragment, and the two grants'
    subscriptions, changed. Both grant subscriptions are active. The legacy product subscription
    stays suspended, and the API no longer requires a subscription, so the governed policy decides.
  - #37 held: while the plan applied, focus stayed on the busy **Applying…** button, then moved to
    "Step 4 of 4" and to the result. Escape closed the dialog at once, without Tab.
- ✅ The workload's grant: **Add direct grant** for the workload, with 1,000 tokens per minute,
  50,000 tokens a month and 30 calls a minute. The row read "Saved, not applied", in Development.
  The review had 22 steps and three grants. It creates the workload's subscription, updates the
  other two, and moves access version 2 to 3. **Apply plan** succeeded in 32 seconds. API
  Management gained one subscription, active, scoped to this model's API, with no owner and
  tracing off, and only the model's fragment changed.
- ✅ A12: **Connection info** on the workload's row opens "Model connection and keys". It shows
  the endpoint, deployment, tenant, runtime state, methods, the model-runtime audience and scope,
  the operations and the limits, with placeholders in its examples. **Reveal primary key**
  showed the key in the only `data-secret` element, and the harness masked it. Focus stayed on
  the button, the status read "Primary key revealed.", and the key never took focus (#38, O3).
  Closing the dialog cleared the key, and reopening showed none. **Copy** wasn't pressed, so the
  key never reached the clipboard. Handing the key to the verifier is R3, in Phase 8.
- ✅ A16 on the Batch 3d build. **Settings** lists the six built-in environments, only Production
  marked production-class, each with no resources, and says 8 resources need classification: 1
  gateway and 7 endpoints. **Require classification before publishing** is off and stays off.
  **Review suggestions** had "No suggestion" for every row, since nothing in the tenant hints at
  an environment. Choosing Development doesn't tick a row, so each was ticked too, and **Submit
  selections** applied all 8. A new review of the model then had no unclassified warning.
  - O25: submitting drops focus, and nothing announces the result.
- #38 on the import dialog: **Import from gateway** lists only Echo API, which isn't a model.
  Ticking it and pressing **Clear** left focus on **Clear**, now `aria-disabled`, and Escape
  closed the dialog. Nothing was imported. A failed import's focus stays covered by unit tests.
  The approval dialog was checked with the `user` persona's request, in P5 (Phase 7).
- Two findings came from these steps. O26: closing a dialog usually drops focus, or moves it to
  an unrelated button. O27: the grant table's Binding column overlaps **Revoke**.

### Phase 7: Live, end-user portal (P1 to P9, A13) ✅

- The `noRole` persona is denied cleanly. A persona with the User role and no grants sees an
  empty My access view and the catalog.
- That persona requests access with a justification, withdraws the request and requests again.
  The admin approves it, which creates grant intent (G2), and reviews and applies it. The persona
  then sees the grant. The `guest` persona already has the User role and isn't registered in
  MOSAIC, so it runs these steps without another tenant change.
- The `user` persona sees its grants, limits and connection details (G3). Primary and secondary
  key reveal is masked, transient and never cached.
- Users are isolated from each other: another user's entitlement ID returns 403 or 404. The
  portal has no page for a single grant, so the verifier checks this at MOSAIC's API with
  `--foreign-user-entitlement`. The admin first confirms that someone else holds the grant. Then
  the grant must stay out of the user's lists and usage report, and its connection details and
  key are refused. The check sends no model requests.
- After Batch 3d, the portal's **Usage & cost** page lists only the caller's own grants, one row
  per resource, and says its figures are simulated, because MOSAIC doesn't read Log Analytics yet
  (ADR 0015). Its route, `GET /api/v1/me/usage`, is scoped to the caller the way the portal's
  entitlements are, so P7's foreign grants must not appear in it either (P9). The verifier's
  `--foreign-user-entitlement` check reads the route's 90-day report and fails if a foreign grant
  is in its rows or timeline. Against a MOSAIC without the route it says it skipped the check.
  [ADR 0019](../adr/0019-usage-telemetry.md) has since replaced the simulation in Azure with
  figures measured from the gateway's logs. A deployment with it shows **Measured figures** and
  how current they are instead, and the verifier's check is unchanged.

Progress:

- ✅ P8: the admin reaches the portal with single sign-on and no second MFA prompt. The header
  shows "Admin allowed", and My access, Catalog and My requests show their empty states without
  errors.
- ✅ P1: the `noRole` persona signed in to the portal and saw only "You do not have access to the
  portal yet. An administrator must grant you the MOSAIC User role before catalog or entitlement
  data can be shown.", with a **Sign out** button. Entra let it sign in, because none of MOSAIC's
  app registrations requires user assignment, so the denial comes from MOSAIC.
- ✅ P0: the `guest` persona reached the portal through single sign-on, with no prompt. My access
  shows "No access granted yet" and says the account has the portal role. Catalog shows "No
  catalog entries", because nothing is published yet, and My requests shows "No requests opened".
  None of them shows an error.
- ✅ A1: the `guest` persona holds only the User role, and the console showed it no admin data.
  Every live section on every page said "Unable to load data" and "The Admin app role is
  required". Settings, Support and the profile page showed only the account's own sign-in
  details and the console's public runtime settings.
  - It found G14: the console still rendered the whole admin shell with its actions, labelled the
    account "Global Admin", and called it the administrator. The `noRole` persona got the same
    shell and label in the console, with "A MOSAIC app role is required: Admin, User" in each
    section.
  - ✅ G14 ([#31](https://github.com/microsoft/mosaic-apim/pull/31)) is deployed (Batch 3c). The
    console now shows the `guest` persona one card, "This console is for MOSAIC administrators",
    which says the User role opens the end-user portal and offers **Sign out**. There's no
    navigation, no "Global Admin" and no "Unable to load data". The card names the portal but
    can't link to it (O20). The `noRole` persona gets "You do not have access to MOSAIC yet". A1's
    smoke spec now checks both cards, and A0's checks the "MOSAIC Admin" label.
- ✅ P2: after A8 and A9, the `guest` persona's My access still shows "No access granted yet",
  and its catalog lists every Discoverable model with **Request access**.
- ✅ P4: the `guest` persona requested AOAI B `gpt-4o-mini` with a justification. The card
  switched to "A request is already open." with **Withdraw**, and My requests listed the request
  as Pending with its justification. **Withdraw** there marked it Withdrawn and removed the
  button, and the catalog card offered **Request access** again. A second request, with a new
  justification, is Pending above the withdrawn one. No step logged a browser error.
  - My requests names both requests "Model API" followed by an internal ID, not the model's name
    (O15).
- 🔄 A13: the console's Entitlements page counted one pending request and listed it with
  **Approve** and **Deny**. The resource shows the endpoint and deployment names followed by
  "(model API)", and the requester shows as an object ID, because MOSAIC hasn't registered the
  `guest` persona (O17).
  - Once A11 had saved governed access, **Approve** opened "Approve access request". It showed
    "Not registered in MOSAIC yet" for the requester and prefilled the publication's 12,000
    tokens per minute. With 1,000 tokens per minute, 50,000 tokens a month and a note, **Approve
    and create grant** registered the requester as a user and created its grant intent. The
    banner said API Management is unchanged and linked to the model's review. The page then
    showed no pending requests.
  - In the portal, the `guest` persona's My requests showed the request Approved with the note,
    and "Approval created your grant. It may not work until an administrator applies it.", with
    a link that opens My access. My access read "APIM changes pending", and its connection
    details showed the endpoint and operations. The key buttons were disabled, with "Keys become
    available after an administrator applies governed access for this model."
  - The persona was labelled "E2E guest persona" on the Identity page before the review, so the
    plan names its subscription with that label.
  - The apply then failed on G17 (A11). My access now reads "APIM apply failed", shows APIM's
    reason under "Last APIM error" (O18), keeps the key buttons disabled, and says "Both methods
    are turned off, so APIM denies every call to this model."
- ✅ O15 and O16 on the Batch 3c build: the `guest` persona's My access heads its grant with the
  model's name and "Model API · Granted directly to you", and the header badge, "APIM apply
  failed", reads in full on one line. My requests heads both requests with the name and "Model
  API · Opened" with the date. P3 itself waits for G17, because the grant hasn't been applied.
- ✅ P7: the verifier's `--foreign-user-entitlement` check first passed with the admin persona in
  the user's place, naming the grants the `user` and `guest` personas hold. Neither grant appeared
  in the admin's own grant lists, and MOSAIC refused its connection details and its key. The
  refused key reveals are in MOSAIC's audit log, as requested and then denied. A made-up grant ID
  failed at the admin's first check, as it should. The `guest` persona's own run stopped at its
  home tenant's MFA prompt, with no one there to approve it. After Batch 1, the `user` persona,
  which holds only the User role, ran it against the `guest` persona's grant and passed: the grant
  stayed out of its lists and its usage report, and MOSAIC refused the grant's connection details
  and key.
- ✅ A13: A11's retry applied the `guest` persona's approved grant with the others, so approval,
  review and apply work end to end. The persona's own view of it needs its home tenant's MFA, so
  the `user` persona covered P3, P5 and P6 instead.
- ✅ P9, after Batch 3d: the anonymous usage route answers 401 instead of 404, so it's deployed.
  The verifier's `--foreign-user-entitlement` run, with the admin persona in the user's place, said
  "the user's usage report leaves out 2 grant(s) held by someone else" where it had skipped the
  check before, and the `user` persona's run said the same of the `guest` persona's grant. The
  `user` persona's **Usage & cost** page shows a "Sample data" badge, says its usage figures are
  simulated and its estimated costs are not a bill, and lists one row, for the one grant it held
  then. Its resource filter offers only that model.
- ✅ P3, after the environment owner signed the `user` persona in again: My access lists its
  AOAI B `gpt-4o-mini` grant with "Model API · Granted directly to you", "Applied to APIM" and
  Development. It lists the grant's limits, 2,000 tokens per minute, 100,000 tokens per month and
  60 calls per 60 seconds, and under "Usage attribution" its APIM subscription and the gateway.
  The header counts "1 entitlements" (O29).
- ✅ P6: the grant's **Connection details** shows the base URL, deployment and key header, the chat
  completions and responses operations, both methods as Accepted, the Entra tenant, client ID,
  scope and audience, the limits, and code samples with placeholders. **Show primary key**
  showed the key in the only `data-secret` element, and the harness masked it. Focus stayed on
  the button, and a status said "Primary key shown. It hides automatically after 60 seconds." A
  minute later the key was gone, and the status said "Key hidden automatically after 60
  seconds." The secondary key showed the same way, and **Hide key** removed it at once, said "Key
  hidden." and moved focus to **Show secondary key**. After a reload, no key was on the page.
  **Copy** wasn't pressed.
- ✅ P5, with a requester MOSAIC had registered: the `user` persona asked for Foundry
  multi-provider `grok-4.3` with a justification, and the card said "A request is already open."
  The console listed the request under the persona's object ID, though MOSAIC knows its label
  (O30). The admin saved governed access for the model, with both methods and the publication's
  12,000 tokens per minute.
  - **Approve** named the requester by label and ID, prefilled 12,000 tokens per minute, and
    Fluent put focus in the first field. With the same limits as the persona's `gpt-4o-mini`
    grant and a note, **Approve and create grant** kept focus while it worked, and was
    `aria-disabled` (#38). The dialog then closed with its opener gone, and nothing had focus
    (O26). The banner said API Management is unchanged and linked to the model's review.
  - Before the apply, My requests showed the request Approved, with the note and "Approval
    created your grant. It may not work until an administrator applies it.", and My access
    showed the grant as "APIM changes pending".
  - The review had 14 steps and moved the model from legacy access to access version 1.
    **Apply plan** succeeded at all 14 steps in 15 seconds. Focus moved from **Applying…** to
    the result, and Escape closed the dialog. My access then read "Applied to APIM", with the
    grant's limits, and its connection details listed one operation: chat completions, at
    `/models/chat/completions`.
  - This was the first governed apply on a Foundry model. API Management gained the grant's
    subscription, and the model's bootstrap subscription was suspended. Only the model's API,
    policy fragment and that subscription changed; the global policy didn't. The governed policy
    allows only chat completions on these publications, so their embeddings and model-info
    operations are denied by design.

### Phase 8: Runtime verification (R1 to R14, A14, A17, A18) 🔄 R1 to R9, A14, A17 and A18 pass; R10 to R14 remain

`scripts/verify_model_access.py` now covers this phase, with unit tests against a fake gateway
that applies the governed policy. It reads each grant's connection details from MOSAIC, calls the
operation its publication exposes, and can sign callers in itself. The tenant batches, the
redeploy and Phases 5 to 7 are done, and what remains runs at the environment owner's sitting,
described below.

| Journey | How the verifier covers it |
| --- | --- |
| R1 | One `--user-entitlement` per model; the verifier calls whichever operation the publication exposes. Azure OpenAI deployments use `/openai/` routes with `--api-version`. Foundry deployments, such as Grok and Llama, use `/models/chat/completions` with `--models-api-version`. Claude uses `/anthropic/v1/messages` (G5) |
| R2 | `--user-token-source device-code` signs the user in through the G4 model client. `--check-ungranted-user` signs in a second person, whose token the grant lookup must refuse with 403. Every run also sends a MOSAIC control-plane token, the wrong audience, which token validation must refuse with 401 |
| R3 | `--application-entitlement` with `--application-token-source client-credentials`. The admin's control token hands off the workload's key |
| R4 | Every run: an anonymous call, an invalid key, an invalid token with a valid key, and the other subject's token with the grant's key. The end user also can't reveal an application's key |
| R5 | `--prove-shared-budget`, on fresh grants limited to 2 calls per 300 seconds. The secondary key's 429 must come from the gateway's call limit, not the deployment |
| R6 | `--prove-token-limit`, on fresh grants limited to at most 100 tokens per minute, on a model whose own limit for each grant is higher. The 429 must come from the gateway's token limit. Not Claude on the classic tier, which can't limit Anthropic tokens (G5) |
| R7 | `--watch-revocation <grant-id>` waits while the admin revokes the grant, which disables it, and applies the plan (A14). Rejections count only once MOSAIC reports the grant revoked, and must repeat |
| R8 | Manual: find the calls in Application Insights and Log Analytics. With [ADR 0019](../adr/0019-usage-telemetry.md) deployed, the gateway's **Telemetry** section must pass its checks, and within about 20 minutes the portal's **Usage & cost** and the console's **Analytics** must count the run's calls against the right grant and client, and its refusals under their reasons |
| R9 | Manual: after R8, with [ADR 0020](../adr/0020-price-list.md) deployed, **Analytics > Cost** must price each deployment the run called at its seeded list price, its tokens times the price per million, and the portal's **Usage & cost** must show the user only their own cost. Then check that **Pricing > Unpriced deployments** lists the declared Claude deployment until its type is set, and compare a month's estimate for one pay-as-you-go deployment with its Cost Management line at list price |
| R10 | Manual, with [ADR 0022](../adr/0022-cost-centers.md) deployed: grant the `user` persona one model under two cost centers, apply, and create a key under one. With an Entra token, a call with `x-mosaic-cost-center` set to either code, in any letter case, must be attributed to that cost center's grant in **Analytics** (filter by cost center); a call without the header must go to the grant under the persona's default; a call naming an unknown code must return 403 with `mosaic-deny v=1 r=cost-center`. The key with the other cost center's code must return 403 with `r=cost-center-mismatch`, and with its own code or none must work. Capture a backend request, for example with a test MCP server or the deployment's diagnostic logs, to confirm the header never reaches the backend |
| R11 | Manual, with ADR 0022: give a cost center a pooled monthly token quota on a model, smaller than the grant's own limits, a few hundred tokens. Spend it with two different grants under that cost center. Each response must carry `x-mosaic-cost-center-remaining-quota-tokens`, falling across both grants, and once it's spent both grants must get 429 from the pool's `llm-token-limit` while a grant under another cost center still works. Check that `x-mosaic-remaining-tokens` and `x-mosaic-remaining-quota-tokens` report the grant's own limits on the same responses. Repeat with a pooled call quota on an MCP server, which must return 403 or 429 from `quota-by-key` once spent |
| R12 | Manual, with [ADR 0023](../adr/0023-budgets-and-notifications.md) deployed: time how long API Management takes to apply a changed named value. Re-apply a publication so its gateway has `mosaic-blocked-cost-centers`, and check that its ARM `GET` returns the value. Then, while calling a model under one cost center every few seconds, set that cost center a blocking budget below its spend and note when MOSAIC audits `gateway.blockedCostCentersUpdated` and when the first 403 arrives. Raise the budget and time the first call that works again. Record both on a classic tier and, if one is available, a v2 tier |
| R13 | Manual, with ADR 0023: in an Azure Government subscription, deploy with `MOSAIC_DEPLOY_EMAIL=true`, or create Communication Services with an Azure-managed email domain by hand. Record whether the resource and domain deploy, then save the `.communication.azure.us` endpoint and sender in **Settings > Email** and send a test email. MOSAIC asks for a token for `https://communication.azure.us/.default`; the test must be accepted and arrive. In either cloud, send the same notification twice with one `Operation-Id`, for example by retrying a refused email, and confirm only one email arrives and what status the second send returns |
| R14 | Manual, with ADR 0023 and email set up: the block and unblock round trip. Give the `user` persona's cost center a blocking budget a little above its spend, with an address you can read. Spend past it. Within about 30 minutes: one 80% email, one 100% email and one block email arrive; calls charged to the cost center get 403 naming it, with `mosaic-deny v=1 r=budget` in the gateway's trace and **Analytics > Reliability**; a call charged to another of the persona's cost centers still works; and the portal's **My access** shows the blocked banner. Raise the budget: calls work again within the time R12 measured, and one unblock email arrives. Checking again sends nothing more |

Since [ADR 0022](../adr/0022-cost-centers.md), applies don't create keys. When a grant the
verifier reads has none, it creates the key through MOSAIC first, as its holder or the
administrator would, and says so.

Method toggles (A14) are checked by rerunning the verifier after each reviewed plan. The verifier
reveals a grant's keys only while keys are on, so it can't show that a key stops working once keys
are turned off. That check reads the grant's key beforehand, through Azure Resource Manager, and
calls the model with it after the plan is applied. Its check that a token is refused while Entra
tokens are off needs the user's token in the same run, so that run also names a second grant, for
the same user, with Entra tokens on.

R4's addition reads keys, so it needed approval. It checks that APIM's all-access key, the key of
the subscription APIM gave its Administrator (O12) and MOSAIC's bootstrap key are each refused on
a governed publication.

The live driver's `verify` command runs the verifier for the personas. It signs the `user`
persona, and the `admin` persona for application grants, in to MOSAIC again, and passes their
MOSAIC API tokens to the verifier without anyone copying them. It enters the verifier's device
codes in the right persona's browser, leaving a person to confirm the sign-in and complete MFA.
The [runbook](runbook.md#verify-runtime-access) shows how to run it.

The environment owner decided on 2026-09-30:

- Approved: real, billed model calls, and device-code sign-ins for the `user` persona and for an
  `outsider` as the ungranted user; a short-lived client secret for the workload; reading the
  privileged keys for R4's addition; fresh grants for R5 and R6; and revoking, disabling and
  toggling access methods, only on those two grants and their models (A14, R7).
- Claude was to be deployed in the Foundry portal at the start of the sitting. That's superseded:
  on 2026-10-01 the owner registered a Claude deployment in another tenant through G18 instead
  (Phase 2), so the sitting has no Claude step.
- The `user` persona's User role is removed once the runtime tests are done.
- Phase 10 is deferred.

Progress:

- ✅ **R3**: the workload's handed-off key and its client-credentials token both reached AOAI B
  `gpt-4o-mini`. The end user couldn't retrieve the workload's key. An anonymous call, an invalid
  key, a MOSAIC control-plane token and an invalid token with a valid key were refused. The check
  that the user's token fails with the workload's key needs a user token, so it runs in R1's run.
- ✅ **R4's addition**: on AOAI B `gpt-4o-mini`, a governed publication, APIM's all-access key and
  the Administrator's product subscription were refused with 403, and the suspended bootstrap
  subscription with 401. Nothing reached the model. So O12's extra subscription doesn't bypass
  governed access.
- ✅ **R5's and R6's grants**, made in the console for the `user` persona on two models it didn't
  hold: AOAI A `gpt-4o` with 2 calls per 300 seconds and no token limit, and AOAI C `gpt-4o` with
  100 tokens per minute and no call limit, under the model's 12,000. They were each model's first
  governed apply: 18 steps each, as an Azure OpenAI publication has 7 operations, and all
  succeeded. Each suspended the model's bootstrap subscription. With **Calls** left empty, the
  form sends no call limit, so R6's grant has only its token limit.
- A grant's ID isn't shown in the grants table, but **Review model changes** lists
  "Grant: entitlement_…" for every grant in the plan.
- Output-token parameter: `gpt-5.1-chat` on `/models/chat/completions` refuses `max_tokens` with
  400 "Use 'max_completion_tokens' instead", the verifier's default for `/models/` routes. It
  accepts `max_completion_tokens`, and so do `grok-4.3`, `DeepSeek-V4-Pro` and
  `Llama-4-Maverick`, checked with each grant's key. R1's run passes
  `--chat-token-parameter max_completion_tokens`. The portal's samples don't set the parameter
  for chat completions, so they aren't affected.
- The same key calls show Grok and Llama reach their models through `/models/chat/completions`,
  so G6 looks unnecessary. R1 confirms it with Entra tokens.
- ✅ **Batch 3e**, 2026-09-30: the environment now runs main as of #61 plus PyJWT 2.14.0
  ([#63](https://github.com/microsoft/mosaic-apim/pull/63)). It's an image-only deploy, with
  directory lookup and group claims turned off, because #51's tenant changes haven't been made.
  #51 changed how a governed policy finds a token's grant, so all seven governed models were
  re-planned and applied: 14 steps for each Foundry model, 18 for each Azure OpenAI model with
  one grant, and 22 for AOAI B `gpt-4o-mini`, which has three. Every step updated the model's own
  resources, and all succeeded. In API Management, only those seven policy fragments changed.
  R3 passed again on the new build, and every governed model answered its grant's key.
  [#62](https://github.com/microsoft/mosaic-apim/pull/62), merged after the approval, needs its
  own, because it adds a trace to every governed policy.
- ✅ **Batch 3f**, 2026-09-30: the environment now runs main as of #66, which adds #62 and
  [#65](https://github.com/microsoft/mosaic-apim/pull/65) (O33, O34) to Batch 3e. The seven
  governed models were re-planned and applied again for #62's trace, with the same step counts,
  and again only their policy fragments changed. R3 passed, and every governed model answered its
  grant's key.
- 🔄 **R8**: every call so far shows up in the gateway's resource logs in Log Analytics, with its
  status: the models' 200s, and R3's refusals, with `validate-azure-ad-token` as the reason for
  the two bad tokens. Application Insights has each model's token counts, by deployment and
  publication. Since Batch 3f, each authorized call to a governed model also writes one trace,
  `mosaic-attribution v=1`, that names its grant by a stable hash, both in Application Insights
  and in the resource logs. Refused calls and calls to publications without governed access
  write none. The 429s wait for R5's and R6's runs. After Batch 3i, the resource logs also keep
  the trace's properties. A key call's trace records `mosaic-client` as `-`, as #81 intends, both
  there and in Application Insights. On Batch 3i's build, within 15 minutes, the console's
  **Analytics** and the `user` persona's **Usage & cost** counted Batch 3i's key calls against
  the right grants. Each grant showed "Linked from gateway log traces", and all of them were
  charged to General. The console also listed the workload's earlier calls under its own name, and
  the gateway's telemetry was current. Each total was split into prompt and completion tokens, but
  grok's parts don't add up to its total (O40). Prices are missing for Claude, which has no
  deployment type yet, and for the Foundry models. That is R9's to check.
- ✅ **Batch 3g**, 2026-10-01: the environment runs main as of
  [#74](https://github.com/microsoft/mosaic-apim/pull/74), which brings G18
  ([#69](https://github.com/microsoft/mosaic-apim/pull/69)), the usage analytics of
  [#71](https://github.com/microsoft/mosaic-apim/pull/71) and the price list of
  [#73](https://github.com/microsoft/mosaic-apim/pull/73). `azd provision` wasn't run, because its
  preprovision hook would make #51's Entra changes, which aren't approved. A template with only the
  new resources added three role assignments instead: Monitoring Reader on the gateway for MOSAIC's
  API (#71), and on the environment's vault, Key Vault Secrets User for the gateway and Reader for
  MOSAIC's API (G18). It also added the usage rollups container, and the API got four new app
  settings. The gateway's **Telemetry** section reports it's ready. Turning on its API diagnostics,
  a refresh and a backfill wait for R8.
- ✅ **Batch 3h**, 2026-10-01: main as of [#77](https://github.com/microsoft/mosaic-apim/pull/77),
  plus Key Vault Secrets Officer on the vault for MOSAIC's API, which #77 needs to write keys.
  [#78](https://github.com/microsoft/mosaic-apim/pull/78) and
  [#79](https://github.com/microsoft/mosaic-apim/pull/79), cost centers and budgets, were left out
  for Batch 3i, because every governed model must be re-applied after them.
- ✅ **Batch 3i**, 2026-10-02: main as of [#82](https://github.com/microsoft/mosaic-apim/pull/82),
  which adds cost centers (#78), budgets (#79) and O37's fix
  ([#81](https://github.com/microsoft/mosaic-apim/pull/81)). No infrastructure or settings changed:
  email stays off, and budgets need no new setting. Existing grants kept their IDs and keys. A
  grant made before cost centers is charged to the built-in **General**, the tenant default, which
  everyone may charge and which allows keys. #78's advice to re-seed applies to grants made again,
  because a new grant's ID includes its cost center. At startup, MOSAIC's budget check created
  the gateway's list of blocked cost centers, a plain named value holding `-`. Each of the eight
  governed models was then re-planned and applied in the console. In each plan the list's step
  said "No change", and every other step updated the model's own resources: 15 to 23 steps per
  model, and all succeeded. In API Management, only the eight policy fragments, the grants'
  subscriptions, whose display names now name the cost center, and the new named value changed.
  Every grant's key reached its model, Claude's included, and the portal's **My access** shows
  each grant under General. The new API took about seven minutes to replace the old one (O38).
- ✅ **Batch 3j**, 2026-10-02: MOSAIC's API only, from main as of
  [#84](https://github.com/microsoft/mosaic-apim/pull/84), O39's fix. Nothing else changed, and
  API Management was untouched. The new API took over about three and a half minutes after the
  deploy finished. In the ten minutes before, the API sent about 2,430 records of the Azure SDK's
  calls and the exporter's uploads to Application Insights. After the switch, it sent none. Its
  first rollup on the new build still logged its token requests, outbound HTTP calls and rollup
  results, and Cosmos DB calls are still recorded as dependencies. Checking this showed that the
  API had never recorded its incoming requests (O41).
- ✅ **Batch 3k**, 2026-10-03: the API, console and portal, from main as of
  [#92](https://github.com/microsoft/mosaic-apim/pull/92). It brings request telemetry with query
  values redacted ([#86](https://github.com/microsoft/mosaic-apim/pull/86),
  [#87](https://github.com/microsoft/mosaic-apim/pull/87)), tokens only for calls the model served
  ([#89](https://github.com/microsoft/mosaic-apim/pull/89)), and G19
  ([#90](https://github.com/microsoft/mosaic-apim/pull/90) to #92). No infrastructure or settings
  changed. The API went first, and the console and portal followed once it had taken over, so the
  console never called routes the API didn't have yet (O38). The new API's warm-up took 32 seconds.
  The console asked the admin to sign in again once its new build loaded. The eight governed models
  were re-planned and applied, and every step succeeded. Each fragment now removes the on-behalf
  header before the model and records a reference from an application's call, which stays empty
  until an MCP server calls a model (M9). In API Management only those fragments, their grant
  subscriptions and the Claude key's named value changed. The five active grants' keys and Claude's
  reached their models, and R5's and R6's revoked grants got 401. A backfill of three days then
  recomputed the stored usage (O43).
- ✅ **Claude**, 2026-10-01: from the endpoint the owner registered (Phase 2), `claude-opus-4-6`
  was published in the console in 11 steps, among them a Key Vault named value for the key, and
  all succeeded. A Messages call with the bootstrap key reached Claude. Its governed access, a
  direct grant for the `user` persona with 60 calls per 60 seconds and no token limit (G5), applied
  in 14 steps, all succeeded, and the bootstrap key is now refused with 401. In API Management,
  only this publication's resources and the grant's new subscription changed. The portal's
  **My access** lists the grant as applied, and its connection details explain the Messages route:
  the base URL for an Anthropic SDK, and a key in `Ocp-Apim-Subscription-Key`, because the gateway
  removes `x-api-key`, or a token. The grant's key got 500 (O37) until Batch 3i deployed its fix,
  [#81](https://github.com/microsoft/mosaic-apim/pull/81). Since then the key reaches Claude.

- ✅ **The sitting**, 2 October 2026, about an hour on Batch 3j's build, with the environment owner
  confirming six device-code sign-ins:
  - **R1, R2 and R4**: each of the `user` persona's six grants reached its model with the grant's
    key and with the persona's Entra token: AOAI B `gpt-4o-mini`, `grok-4.3`, `DeepSeek-V4-Pro`,
    `Llama-4-Maverick-17B-128E-Instruct-FP8`, `gpt-5.1-chat` and Claude `claude-opus-4-6`. Each
    refused an anonymous call, an invalid key, a MOSAIC control-plane token, an invalid token with
    a valid key, and an `outsider`'s token, which has no grant. The persona couldn't list, read or
    retrieve the key of the `guest` persona's grant, and its usage report left that grant out.
    Grok and Llama answered tokens on `/models/chat/completions`, so G6 isn't needed. The workload
    was left out, because its client secret had expired; R3 passed on Batch 3f's build.
  - **R5**: the primary key, the Entra token and the secondary key shared one budget of 2 calls
    per 300 seconds, and the gateway's call limit returned the third call's 429.
  - **R6**: the grant's 100 tokens per minute returned 429 with `Retry-After` after 7 calls. The
    first try failed on the verifier. API Management words a token-limit 429 two ways: "Token
    limit is exceeded" once the window is spent, and "Token limit will exceed" when it refuses a
    prompt that would spend more than is left. The verifier knew only the first, so it blamed the
    deployment. It accepts both now, and a test covers the second.
  - **A14**: with keys turned off on R6's model, its grant's key was refused with 401. With Entra
    tokens turned off on R5's model, the persona's token was refused there, while the key still
    worked, and R6's model still took tokens.
  - **R7**: R6's grant was revoked in the console while the verifier watched. MOSAIC reported it
    applying, then revoked, and the persona's token was refused from then on.
  - **R8**: every call showed up in the gateway's resource logs with its status, and each refusal
    with its reason. The 429s named their limit: `RateLimitExceeded` for R5,
    `TokenLimitExceededAfterPrompt` and `OpenAITokenLimitExceeded` for R6. Each authorized call's
    attribution trace was in Application Insights and the resource logs. The token metrics split
    grok's tokens into prompt, completion and reasoning tokens (O40).
  - Afterwards R5's grant was revoked too, and both models' methods were restored. In API
    Management, only those two models' policy fragments and grant subscriptions changed, and both
    subscriptions are suspended.

The `user` persona keeps its User role for now, because Phase 11 needs it. The environment owner
had decided to remove it after Phase 8.

R9 to R14 and A17 check pricing, cost centers and budgets, which Batches 3g and 3i deployed.

- ✅ **A17**, 2 and 3 October. **Pricing** lists its prices with their source: seeded from the
  Azure Retail Prices API on 30 September, 25 of 48 deployments priced. Each price row has
  **Override** and its history. **Unpriced deployments** lists the other 23, each with its reason,
  such as "No price for grok-4.3 1 (GlobalStandard) in eastus2 in Azure Commercial", and a fix.
  **Clouds and endpoints** reads each endpoint's cloud from its host. Each Azure endpoint showed
  Azure Commercial and its region; the Claude endpoint, registered by URL, has no region. On 3
  October, with priced usage on two days, the regional `gpt-4.1-mini` price that AOAI B's
  `gpt-4o-mini` deployment uses was overridden from that day at 10,000 times its list price.
  **Analytics > Cost** then priced the deployment's 13 tokens on 3 October at $0.08, and left its
  52 tokens on 2 October at $0.00. Saving the list price again with the same date corrected it,
  and 3 October went back to $0.00. The price's history keeps all three versions, each with its
  note. It names whoever recorded a version by object ID, not by name.
- ✅ **R9**, 2 and 3 October. **Analytics > Cost** priced the day's calls to priced deployments, and
  left out, by name, the 848 tokens with no price. The `user` persona's **Usage & cost** shows only
  their own cost. **Unpriced deployments** lists the three declared Claude deployments as
  "Deployment type unknown", with **Set facts**. Two problems came up: cost by model and cost by
  API disagreed (O43, fixed in Batch 3k), and the price list has no price for most models this
  environment runs (O42). The comparison with Cost Management can't run here: Cost Management
  returns no cost rows for this subscription, for any service, from 25 September to 3 October.

R10 to R14 need new grants, and R13 and R14 need email set up, so each waits for the owner's
approval.

A call quota (O28) can't be set in the console, so these grants have none. A weekly one adds a
policy expression that API Management hasn't compiled yet.

### Phase 9: Codify, document, clean up 🔄 ordered specs and A15 done; deferred findings filed as issues

- ✅ The live run is now ordered specs built on page objects
  ([#68](https://github.com/microsoft/mosaic-apim/pull/68)): `00-smoke` through `90-cleanup`, 50
  tests in 8 files. They repeat against the shared environment: journeys over existing state only
  verify it, writes need `MOSAIC_E2E_ALLOW_WRITES=1` and act only on disposable targets the
  manifest names, and billed calls need `MOSAIC_E2E_SEND_MODEL_REQUESTS=1`. The
  [runbook](runbook.md) explains how to run them and how to recover. The harness's unit tests had
  failed since #51, because the harness didn't know the verifier's `--agent-entitlement` and
  `--group-entitlement`. It knows them now, and the plan checks accept a key-authenticated
  publication's own named value (G18).
- 🔄 The ordered specs' first live run, read-only, on 2026-09-30: 24 passed. The 3 failures were
  the `guest` persona's MFA prompt, which needs a person, and the 2 skips need billed calls. The
  run found two harness problems, both fixed. The specs looked for the workload's MOSAIC identity
  by its app registration's name, which an admin can relabel; they now use its service principal's
  object ID when the manifest gives one. And they checked the console's tables before those tables
  had loaded their data; the page objects now wait for each page's loading indicators to clear.
  The write and runtime specs wait for a sitting.
- Record findings here and file issues for anything deferred. Filed so far:
  - O14 as [#42](https://github.com/microsoft/mosaic-apim/issues/42), O22 as
    [#43](https://github.com/microsoft/mosaic-apim/issues/43), O20 as
    [#44](https://github.com/microsoft/mosaic-apim/issues/44), O17 as
    [#45](https://github.com/microsoft/mosaic-apim/issues/45), O24 as
    [#46](https://github.com/microsoft/mosaic-apim/issues/46) and O28 as
    [#52](https://github.com/microsoft/mosaic-apim/issues/52).
  - O27 as [#53](https://github.com/microsoft/mosaic-apim/issues/53) and O29 as
    [#55](https://github.com/microsoft/mosaic-apim/issues/55), both fixed since, in
    [#58](https://github.com/microsoft/mosaic-apim/pull/58) and
    [#57](https://github.com/microsoft/mosaic-apim/pull/57).
  - O30 as a [comment](https://github.com/microsoft/mosaic-apim/issues/45#issuecomment-5902625270)
    on O17's issue, fixed since in [#58](https://github.com/microsoft/mosaic-apim/pull/58).
  - O1 and O5 together as [#47](https://github.com/microsoft/mosaic-apim/issues/47).
  - #33's two test follow-ups as [#48](https://github.com/microsoft/mosaic-apim/issues/48).
  - The follow-ups to O19 and O23 as [#49](https://github.com/microsoft/mosaic-apim/issues/49),
    with O25 and O26 added in a
    [comment](https://github.com/microsoft/mosaic-apim/issues/49#issuecomment-5901935028).

  O11's product suggestion waits for its Phase 8 check. O12's check passed (Phase 8), and its
  product suggestion stands for publications without governed access. R1 confirmed that G6 isn't
  needed.
- The `outsider` persona's browser profile holds a session for the `user` persona: its portal
  opens as the `user` persona. The verifier wasn't fooled, because it refuses an "ungranted" token
  that belongs to the granted user. But a portal check run as the `outsider` would see the wrong
  person. Sign that profile out of the `user` persona before running one.
- ✅ A15, on AOAI C `o4-mini`, a publication without governed access. **Unpublish** removed its
  API, with the API's operations and policy, and its product, backend, policy fragment and
  bootstrap subscription. Nothing else in API Management changed, apart from the subscription
  API Management had given its Administrator on that product (O12), which it deleted with the
  product. **Re-plan** on the publication, now a draft, planned 14 creates. Applying them
  restored every resource with the same content, except the new subscriptions' keys, and the
  model answered its bootstrap key. Unpublishing took one click, with no confirmation or plan
  (O33), and the portal kept offering the model while it was unpublished (O34). A15 ran again on
  the Batch 3f build, which has the fix for both. **Unpublish** opened a review listing the same
  resources, deleted in order, and said that without governed access MOSAIC can't list who calls
  the model. Only **Unpublish model** ran it. The publication then showed as **Unpublished**, with
  when, and the portal's catalog left the model out. A request from a catalog page loaded before
  the unpublish was refused, with the reason. **Re-plan** and **Apply plan** published it again.
- Roll back test-only tenant changes from the ledger. Imported and published models stay, since
  they're the goal.

### Phase 10 (later): Gemini and AWS Bedrock ⬜ deferred by the environment owner (2026-09-30)

The console can already register an OpenAI-compatible endpoint, storing a Key Vault secret URI
rather than the key. MOSAIC doesn't discover that endpoint's models, though, and it refuses to
publish it because no curated API shape exists. Phase 10 is a design spike to close that gap:
discovery, a versioned shape, and backend credentials kept in Key Vault and read by the APIM
identity. Gemini would use its OpenAI-compatible endpoint or the Vertex AI API; Bedrock would use
an API key or SigV4. The environment owner writes the secrets and shares only their Key Vault URIs.
G18 has since built the backend-credential part for Azure AI endpoints, a Key Vault-backed named
value that the gateway reads with its own identity, and Phase 10 would reuse it.

### Phase 11 (next): MCP servers end to end ⬜ requested by the environment owner (2026-10-02)

MOSAIC publishes MCP servers through API Management and governs them with grants, as it does
models, but no journey has exercised that yet. Phase 11 deploys a few generic MCP servers and
takes them through the whole path: registration, publication, access requests and grants, calls
from a real MCP client, and usage. One of the servers calls a model through MOSAIC, so the phase
also shows how a model call made by an MCP server is attributed to the person who called the
server (G19). It runs after Phase 9's cleanup, with the same environment, harness, personas and
ledger.

**Prerequisites**, each needing the environment owner's approval:

- **Batch 5a, Entra.** MCP runtime tokens use the `Mcp.Invoke` delegated scope and the
  `Mcp.Invoke.Application` app role on MOSAIC's runtime registration
  ([connect-to-mcp-servers.md](../connect-to-mcp-servers.md)). The registration has only
  `Models.Invoke` and `Models.Invoke.Application` today, because #51's Entra changes haven't been
  made. Add the MCP scope and role. Consent the MCP test client for the scope. Create an app
  registration for the agent server's workload identity, and an audience for the server that
  accepts only the gateway's managed identity.
- **Batch 5b, Azure.** Deploy the servers, all with streamable HTTP, public network access, the
  smallest scale, and logs to the environment's workspace:
  - **M-tools**, on Container Apps: a few deterministic tools, such as echo, the time and adding
    two numbers. Upstream authentication **None**.
  - **M-protected**, on Azure Functions with its MCP extension: the same kind of tools, accepting
    only a token from the gateway's managed identity for its audience. Upstream authentication
    **Managed identity**.
  - **M-agent**, on Container Apps: a tool that answers by calling a governed model through
    MOSAIC with the agent's own application grant, so a call to it leads to a second, governed
    call (G19).
  - Optionally, an SSE-only server as a negative case, because MOSAIC doesn't publish one.
- **The gateway's diagnostics** must log 0 bytes of response bodies for MCP APIs, or streaming
  breaks. MOSAIC warns about this when it plans a publication.

**Journeys** (see the MCP table under the journey matrix): M1 registers each server and syncs its
tools; M2 publishes it after a reviewed plan; M3 sets governed access for people and the agent's
identity; M4 is a person's request and its approval in the portal; M5 is calls from a real MCP
client; M6 is call limits and pooled quotas; M7 is revocation; M8 is usage; M9 is the agent
server's governed model call; and M10 is unpublishing. A scripted MCP client, built on the MCP
Python SDK, signs in with a device code as the verifier does, and the live driver enters the codes.

**Product decisions before M9:**

- **G19**: how a model call made by an MCP server, on a person's behalf, is attributed to that
  person. The environment owner decided on 2 October 2026: the server calls with its own
  application grant and passes on who called it, and MOSAIC records that person for usage only,
  trusting the value only from approved intermediaries. A person needs no grant on the model.
  The ADR settles the mechanism before M9 runs.
- **Which tool was called**: MOSAIC's MCP traces read only policy variables, never a body, so
  usage counts calls to a server but not to each tool. Reading the JSON-RPC request's method and
  tool name in the policy would add that. M8 shows how much is missing.

## Journey matrix

A journey passes only when the stated observable outcome happens in the UI, or at the gateway for
runtime journeys. **Status** is the result of the latest live run. 🔄 means part of the journey
has passed, and ❌ means the latest run failed on the product gap named.

### Admin console

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| A0 | The admin reaches the console; the model endpoints page loads without errors | 1 | ✅ |
| A1 | A User-only account signs in but sees no admin data | 1 | ✅ |
| A2 | Discovery suggestions list the target accounts; unreadable subscriptions show a remediation command | 3 | 🔄 |
| A3 | Register from a suggestion, a pasted account ID and a pasted Foundry project ID; a duplicate is rejected | 3 | ✅ |
| A4 | An unreadable endpoint shows "cannot read" and the exact command; after running it, MOSAIC can read it | 3 | ✅ |
| A5 | Synced deployments match the Azure inventory | 3 | ✅ |
| A6 | Gateway runtime readiness moves from "cannot invoke" with a command to "can invoke" | 3 | ✅ |
| A7 | Manage mode is refused without APIM write access and allowed with it (G1) | 5 | 🔄 |
| A8 | Publish every target deployment; every plan step succeeds, and applying a re-plan of an unchanged publication leaves API Management as it was | 5 | ✅ |
| A9 | Catalog visibility makes a published model appear in the portal | 5 | ✅ |
| A10 | Identity entries exist for the `user` persona and the workload; a duplicate is rejected | 6 | ✅ |
| A11 | Governed access with keys, Entra and limits is reviewed and applied, and the applied state shows | 6 | ✅ |
| A12 | Workload connection details and key handoff work, and the key is never logged | 6 | ✅ |
| A13 | Approving an access request creates grant intent, which is then reviewed and applied (G2) | 7 | ✅ |
| A14 | Disable, revoke and method toggles go through review and apply | 8 | ✅ |
| A15 | Unpublishing removes only what MOSAIC created | 9 | ✅ |
| A16 | Settings lists the built-in environments; an Unclassified pairing warns but isn't blocked, and classifying both sides clears the warning (#40) | 6 | ✅ |
| A17 | **Pricing** lists the seeded price of each target deployment with its source, detects each endpoint's cloud from its host, and says why any deployment has no price; an override from a date prices only the days from then (ADR 0020) | 8 | ✅ |
| A18 | An endpoint in another Entra tenant is registered by its URL and a pasted API key (G18, ADR 0021); its access card confirms the endpoint accepts the key and the gateway can read it; its Claude deployment is published and granted, the bootstrap key is refused once governed access applies, and the grant's key and token reach Claude | 8 | ✅ |

### Portal

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| P0 | A User persona reaches My access and the catalog | 1 | ✅ |
| P1 | A persona without a MOSAIC role gets a clean denial with a sign-out option | 1, 7 | ✅ |
| P2 | With the role but no grants, My access shows its empty state and the catalog is visible | 7 | ✅ |
| P3 | My access shows applied grants, limits and attribution | 7 | ✅ |
| P4 | A request with a justification can be withdrawn and requested again | 7 | ✅ |
| P5 | After approval and apply, the requester sees the grant | 7 | ✅ |
| P6 | Connection details appear, and key reveal is masked, transient and uncached (G3) | 7 | ✅ |
| P7 | Another user's entitlement ID returns 403 or 404 | 7 | ✅ |
| P8 | The admin shows as allowed in the portal | 7 | ✅ |
| P9 | **Usage & cost** lists only the caller's grants and labels its figures as simulated, until ADR 0019 measures them; the verifier checks its route leaves out other people's grants (#40) | 7 | ✅ |

### Runtime (real calls through APIM)

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| R1 | A granted user reaches every provider by key: Azure OpenAI, Foundry OpenAI, Grok, Llama, DeepSeek and Claude (G5, G18) | 8 | ✅ |
| R2 | A granted user's Entra token works (G4); a token without a grant and a wrong-audience token are denied | 8 | ✅ |
| R3 | The workload's client-credentials token and its handed-off key both work | 8 | ✅ |
| R4 | Anonymous, invalid-key and cross-subject calls are denied | 8 | ✅ |
| R5 | A shared budget of 2 calls per 300 seconds, spent by primary key and token, returns 429 for the secondary key | 8 | ✅ |
| R6 | The tokens-per-minute limit returns 429 with `Retry-After` | 8 | ✅ |
| R7 | After revocation propagates, calls fail | 8 | ✅ |
| R8 | Calls show up in Application Insights and Log Analytics, and with ADR 0019, in MOSAIC's usage and analytics (optional) | 8 | ✅ |
| R9 | With ADR 0020, the run's calls are priced at list price in Analytics, the Dashboard, and the user's own portal page (optional) | 8 | ✅ |
| R10 | With ADR 0022, `x-mosaic-cost-center` selects the grant, a missing header falls back to the default cost center, and an unknown code or a key with another cost center's code is refused with 403; the backend never sees the header | 8 | ⬜ |
| R11 | With ADR 0022, a cost center's pooled `llm-token-limit` is shared by its grants, reports `x-mosaic-cost-center-remaining-quota-tokens`, and returns 429 once spent, alongside each grant's own limits | 8 | ⬜ |
| R12 | With ADR 0023, how long a changed `mosaic-blocked-cost-centers` named value takes to reach the gateway, blocking and unblocking | 8 | ⬜ |
| R13 | With ADR 0023, Communication Services Email works in Azure Government with MOSAIC's managed identity, and a repeated `Operation-Id` sends one email | 8 | ⬜ |
| R14 | With ADR 0023, a blocking budget's round trip: each email once, 403 with `r=budget` for that cost center only, the portal banner, and calls working again once the budget is raised | 8 | ⬜ |

### MCP servers (Phase 11)

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| M1 | The admin registers each MCP server by its URL, and MOSAIC syncs its tools; an SSE-only server is refused | 11 | ⬜ |
| M2 | Publishing a server through the gateway shows a reviewed plan, including the diagnostics warning and the environment verdict; every step succeeds; the server serves its protected resource metadata, and the portal's catalog lists it | 11 | ⬜ |
| M3 | Governed access for an MCP server: a direct grant for the `user` persona and an application grant for the agent's identity are reviewed and applied | 11 | ⬜ |
| M4 | In the portal, a person requests access to an MCP server, an admin approves it, and the person's connection details give the server URL, the metadata URL and the scope, but never a token | 11 | ⬜ |
| M5 | A real MCP client, signed in with the `Mcp.Invoke` scope, lists and calls tools through the gateway; an anonymous call gets 401 with the metadata URL, and an ungranted person's token gets 403 | 11 | ⬜ |
| M6 | An MCP grant's call limit, and a cost center's pooled call quota on the server, refuse calls once spent | 11 | ⬜ |
| M7 | After an MCP grant is revoked and its plan applied, its calls are refused | 11 | ⬜ |
| M8 | **Analytics** and **Usage & cost** count each person's calls to each MCP server under their grant and cost center; the gap: which tool was called | 11 | ⬜ |
| M9 | A call to the agent server's tool leads to a governed model call, which usage attributes to the agent's grant and, by G19's design, to the person who called the tool | 11 | ⬜ |
| M10 | Unpublishing an MCP server removes only what MOSAIC created | 11 | ⬜ |

## Findings

These are smaller than the gaps above, and most came from live runs. They're recorded so they can
be confirmed, or fixed, once the journeys that exercise them have run.

| ID | Observation | Next step |
| --- | --- | --- |
| O1 | Before MOSAIC can read an account, it records a placeholder endpoint (`https://<account>.cognitiveservices.azure.com`) and the provider "Azure AI Foundry", even for an Azure OpenAI account. The UI shows these as fact | Confirmed in Phase 3: once MOSAIC can read the account, **Check access** corrects both. Still worth labeling the values as unconfirmed until the first successful read. Filed with O5 as [#47](https://github.com/microsoft/mosaic-apim/issues/47) |
| O2 | A rejected duplicate registration appears under the generic title "Unable to load data". The Identity page gets this right with "Unable to add principal" | Fixed by G11, which titles every refused registration "MOSAIC didn't register this endpoint". Seen live after Batch 3b |
| O3 | The console's key reveal (`EntitlementConnectionDialog`) shows the key in an element labelled "Revealed primary key" or "Revealed secondary key", with no `data-secret` marker. The harness masks it by that label | Fixed in [#38](https://github.com/microsoft/mosaic-apim/pull/38) with O23, and deployed with Batch 3d. The revealed value carries `data-secret`, as the portal's key reveal does, so any tooling can find it. In A12 the key was the only `data-secret` element, and the harness masked it. The harness still masks the label too, for builds from before it |
| O4 | Opening **Review model access** from the Models or Entitlements page left keyboard focus on the page, not in the dialog. The dialog first rendered its opening step and then switched to the review in an effect, which removed the control that had focus. The same dialog also showed its first step while it closed, and an apply that finished after it closed made the next **Publish a model** open on "Step 4 of 4". Found while stabilizing the web tests ([#24](https://github.com/microsoft/mosaic-apim/pull/24)). Seen live in A8's retry, and worse: the review the console opened after the refusal was itself `aria-hidden`. Focus never entered it, so screen readers and role queries couldn't reach the dialog, while the page behind it stayed reachable. A11 saw the same on the Entitlements page: **Review model changes** opened the review `aria-hidden` | Fixed in [#25](https://github.com/microsoft/mosaic-apim/pull/25), merged after G16. Batch 3c deployed it, and a live check passed: **Re-plan** on each of the 11 publications without governed access, and **Review model changes** on the Entitlements page, opened on "Step 3 of 4" with focus inside, and neither review was hidden. Closing with **Close** or Escape never showed "Step 1 of 4", and the next **Publish a model** opened clean on "Step 1 of 4". The review the console opens after a refusal hasn't been seen live yet |
| O5 | After the redeploy, each endpoint kept the readiness verdict the previous build had saved, until someone ran **Check access** again. The Foundry project still recommended Foundry User at the project scope, which G8 reports as too narrow | Run **Check access** on every endpoint after a deploy that changes the readiness rules. The product could record which rules produced a verdict and flag older ones as out of date. Seen again after Batch 3b: G12's key-authentication row appeared only after **Check access**. Filed with O1 as [#47](https://github.com/microsoft/mosaic-apim/issues/47) |
| O6 | The harness's unattended sign-in gave up while silent single sign-on was still redirecting. It took the first sight of the Entra sign-in page to mean a password, MFA or consent was needed | Fixed in the harness. It gives single sign-on time to finish before it asks for a person, and still fails at once on an Entra `AADSTS` error. The first window, 10 seconds, proved too short after Batch 3c: the `guest` persona's silent sign-in to the console took longer, and finished on its own after the harness had given up. It's now 30 seconds |
| O7 | After Reader was granted to MOSAIC's managed identity on one account (01:50), **Check access** kept failing for at least 17 minutes, past the 10 minutes Microsoft documents. ARM returned 403 to MOSAIC's read, and restarting the API didn't help. The assignment was listed at once, and no deny assignment applied. A subscription-wide Reader granted at 02:12 made every account readable within 4 to 6 minutes, including one registered only after that grant | Allow for tens of minutes after granting a role, and grant the gateway's roles (A6) well before Phase 8 needs them. G10 makes the UI say that a new role can take a while |
| O8 | Both Azure OpenAI targets showed no Key authentication row or note, though keys work on them. They never set `disableLocalAuth`, and Azure leaves it out when it's unset, which means enabled. The AI Services account sets it to false explicitly and shows "Enabled" | G12 treats an unset value as enabled. After Batch 3b and **Check access**, both show "Enabled" |
| O9 | With the Foundry project registered, discovery still suggested its parent account, and registering it succeeded. The second endpoint had the same URL, and syncing it listed the project's six deployments again, each publishable on its own. Registration rejects only an exact resource ID match, and discovery compares exact IDs | G11 refuses overlapping registrations and stops suggesting covered accounts. The duplicate was removed. After Batch 3b, the parent account is no longer suggested, and pasting its ID is refused with a message naming the project |
| O10 | **Remove** on an endpoint deleted it and its synced models at once, with no confirmation. From the code: the server doesn't check publications, and a publication whose endpoint is gone can't be re-planned or applied ("Model endpoint was not found"), so its access can't change while its API keeps serving. Registering the same resource again restores the same endpoint ID | G11 confirms first and refuses while publications depend on the endpoint. After Batch 3b, **Remove** asks first, lists what goes with the endpoint, and says MOSAIC refuses while a model from it is still published |
| O11 | A published API exposes every operation of its API shape, whatever the deployment can serve. The Azure OpenAI shape gives `gpt-35-turbo` seven operations: chat completions, completions, embeddings, image generation, audio transcription and translation, and responses. MOSAIC already syncs each deployment's capabilities (A5) but doesn't use them to choose operations | In Phase 8, confirm that a call to an operation the model can't serve fails cleanly at the model. Consider publishing only the operations that match the synced capabilities |
| O12 | When MOSAIC creates a product, APIM subscribes its own Administrator to it. So each publication without governed access has a second subscription whose key can call the model, besides APIM's all-access key. The activity log shows MOSAIC wrote only its own subscription. APIM's two built-in products got the same subscription when the gateway was created | Checked in Phase 8, with approval to read the keys: on a governed publication, APIM's all-access key and the Administrator's subscription are refused with 403, and MOSAIC's suspended bootstrap subscription with 401. Governed access accepts only direct-grant subscriptions, and unpublishing deletes the product with all its subscriptions. The Administrator's key still works on a publication without governed access, so MOSAIC could disable the automatic subscription, or show it in the plan |
| O13 | When a plan saved in the old order is refused, the review repeats the refusal, "…Re-plan this publication and review the new order before applying.", above a plan the console has already re-planned | G16 titles the refusal "MOSAIC didn't apply the plan you reviewed", keeps the server's reason, and adds "MOSAIC has already re-planned. Review the fresh plan below before you apply it." Its tests cover this. No plan saved in the old order is left to refuse, so a live run sees it only if another refusal happens |
| O14 | Every model in the portal catalog reads "No summary provided." The API accepts a summary for each catalog entry, and the portal shows it, but the console offers only the visibility select | Let the admin write a summary in the console, or fill a default from the endpoint, model and API shape. Filed as [#42](https://github.com/microsoft/mosaic-apim/issues/42) |
| O15 | The portal's My requests page heads each request "Model API" and an internal ID, where the catalog shows the model's name. Someone with several requests can't tell them apart. My access heads each grant the same way. The console's list of pending requests does show the name | Fixed in [#35](https://github.com/microsoft/mosaic-apim/pull/35), merged and deployed in Batch 3c. The API names each of the caller's own requests and grants in a new optional field, and both pages show that name, with the kind beside it and the old heading as a fallback. It changes the API and the portal only, so it shipped in an image-only deploy |
| O16 | On My access, the runtime badge in each card's header wraps inside the badge's fixed height, so "Applied to APIM" shows only "to". Other labels of several words, such as "APIM changes pending", spill out of the badge. On a narrow screen, a heading that falls back to the internal ID pushes the badge out of the card, on My requests too. The same label in the card body is fine. Seen in the README's portal screenshot, and a live run would see it in P3 | Fixed in [#35](https://github.com/microsoft/mosaic-apim/pull/35), with O15, and deployed in Batch 3c. Fluent sizes the header's badge column to the label's longest word. The badge now stays on one line, and a long heading wraps instead. A check of 144 header badges at six widths, from 1440 down to 320 pixels, found 84 cut off before the fix and none after. The README's My access screenshot is refreshed. P3 confirms it live |
| O17 | An access request records only the requester's object ID. The console lists a requester MOSAIC hasn't registered by that ID, and approving registers them with no label. Their grant, and the APIM subscription the plan names after it, then carry only the ID until an admin labels the principal on the Identity page. MOSAIC has no Graph permission to look the name up. Seen in A13 | Record the requester's name and username from their token when they request access, show them in the console, and use them as the label when approval registers the requester. Filed as [#45](https://github.com/microsoft/mosaic-apim/issues/45) |
| O18 | When an apply fails, the portal shows the end user APIM's raw error under "Last APIM error", including the internal fragment name and APIM's validation text. Seen in A13 after G17. MOSAIC keeps one error per publication and copies it to every grant, so each grantee sees the same text, which can describe other people's grants. Three end-user routes return it: the portal's grant list, and the list and connection details under `/me` | Fixed in [#39](https://github.com/microsoft/mosaic-apim/pull/39), and deployed with Batch 3d (API, then portal). Those three routes now leave the error out for every caller, including an admin reading their own grant there, and the portal no longer shows it. The portal already says in plain words that the apply failed and to ask an administrator to retry it. The console's routes keep APIM's reason. A test fails if any other end-user route starts returning a grant's runtime state. Reverting the API change fails 7 of the new tests, and restoring the portal's block fails 1. Other APIM details that end-user routes still return are O24 |
| O19 | When **Apply plan** finishes, the publish dialog removes the button that had focus, and nothing on the page has focus. Escape then doesn't close the dialog until Tab brings focus back inside. Tabbing works because the dialog still traps focus, but a screen reader isn't taken to the result. #25 handles focus only when the dialog opens. Seen live after Batch 3c, applying a re-plan of AOAI A `gpt-35-turbo`. The code does the same at every step: each step change removes the button that had focus, and **Review plan** and **Apply plan** disable themselves while they work | Fixed in [#37](https://github.com/microsoft/mosaic-apim/pull/37), and deployed with Batch 3d. When the step changes, focus moves to the step's outcome if it has one (the result, the refusal or the error), and otherwise to "Step N of 4". **Review plan** and **Apply plan** keep focus while they work, and can't be pressed again. Reverting the component fails 15 of its tests. Seen live in A11's retry and the workload's apply: focus stayed on **Applying…**, then moved to "Step 4 of 4" and to the result, and Escape closed the dialog at once. The review's follow-ups are filed with O23's as [#49](https://github.com/microsoft/mosaic-apim/issues/49) |
| O20 | The console's card for an account with only the User role says that role opens the MOSAIC end-user portal, but it can't link there, because the console isn't configured with the portal's address. Someone who opens the console by mistake has to find the portal on their own. Seen in A1 after Batch 3c | Add the portal's address to the console's runtime configuration, and link to it from the card. Filed as [#44](https://github.com/microsoft/mosaic-apim/issues/44) |
| O21 | After Batch 3c, P0 failed within milliseconds, before it opened a page: "Failed to open a new tab", then "Target page, context or browser has been closed". The harness closes every page when a test ends. A headed Chromium quits about a quarter of a second after its last tab closes, so the `guest` persona's browser was gone by the time P0 wanted it. A1 now checks a second persona between the `guest` persona's two smoke tests, so there was time for the browser to quit. Before that, P0 had passed only by winning the race. A throwaway profile shows the same thing every time | Fixed in the harness. Each persona's browser keeps one blank tab open between tests. With that tab, a new page opens after the browser sits idle for 9 seconds; without it, the browser is gone. P0 still needs a live run while someone can answer the `guest` persona's MFA |
| O22 | MOSAIC lets an admin save limits that API Management may not support on the gateway's tier. The `quota-by-key` page's tier banner lists only the classic tiers, though its usage section also names v2. `rate-limit-by-key` and `llm-token-limit` list the classic and v2 tiers. None lists Consumption. MOSAIC checks the tier only for Anthropic token limits, which need v2. From G17's review of the policy pages, not seen live: this deployment runs the Developer tier, which supports all three | Check the gateway's tier when limits are saved or planned, and say which limit it can't enforce. A follow-up gap, not blocking this plan. Filed as [#43](https://github.com/microsoft/mosaic-apim/issues/43) |
| O23 | Other dialogs lose focus the way O19's did. The key reveal in the console's connection details, **Approve** on an access request, and **Import** from a gateway are `disabled` while they work, so the button that had focus loses it, and a revealed key or an error doesn't take focus when it appears. The import dialog's **Clear** disables itself too. From the O19 fix's review. A local probe in current Chromium confirms the mechanism: a focused button that becomes `disabled` hands focus to the page, Escape then doesn't reach the dialog, and focus doesn't come back when the button is enabled again. With `aria-disabled`, which the publish and removal dialogs already use, focus stays and Escape closes the dialog. About 45 buttons on the console's and portal's pages also disable themselves while they work. Their impact is lower, because no dialog is trapping Escape there | Fixed in [#38](https://github.com/microsoft/mosaic-apim/pull/38), and deployed with Batch 3d (web). A busy button keeps focus and can't be pressed again. A failed reveal, approval or import moves focus to its reason. A failed copy leaves focus on **Copy** and is announced as an alert. A reveal that succeeds leaves focus on its button, and a status says which key it revealed, never the key. **Clear** stays focusable. Reverting each dialog fails 7, 4 and 6 of its tests. Seen live in A12's key reveal, on the import dialog's **Clear**, and in P5's approval, where **Approve and create grant** kept focus while it worked. P6 saw the portal's key reveal keep focus too. Two small gaps remain, neither blocking: a copy that fails because the browser has no clipboard API isn't announced again when pressed again, and a copy failure stays on screen if the grant stops qualifying and the key is hidden. The page buttons are left for a separate decision. The gaps, the page buttons, and other dialogs whose buttons still disable themselves are filed as [#49](https://github.com/microsoft/mosaic-apim/issues/49) |
| O24 | End-user routes still return other API Management details. My access shows each grant's APIM product and subscription names under "Usage attribution". Since #40 it no longer shows MOSAIC's gateway record ID there, but the route still returns it. The grant and its connection details also return counter-key policy expressions and APIM subscription names that the portal doesn't show. A failed key reveal's error body carries the full Azure Resource Manager URL, which names the Azure subscription, resource group, APIM service and APIM subscription. When APIM refuses to list the key, it adds the service's resource ID. The portal shows its own text for these, but they're visible in the browser's network tools. From the O18 fix's review, not seen live | Decide what an end user needs to identify their usage. Return APIM names to end-user routes only where the portal uses them, and give those routes problem details without upstream URLs, resource IDs or Azure's raw text. The console keeps them. A follow-up gap, not blocking this plan. Filed as [#46](https://github.com/microsoft/mosaic-apim/issues/46) |
| O25 | More dialogs disable their buttons while they work, as O23's did. In **Review environment suggestions**, **Close** and **Submit selections** are `disabled` while the submission runs, and **Submit selections** stays disabled once it succeeds, because no rows are left. Seen live in A16 after submitting 8 rows: nothing had focus, and Escape didn't close the dialog until Tab brought focus back to **Close**. Nothing announces the success message either, because nothing provides an `AnnounceProvider`. Settings' add, edit and delete environment dialogs do the same in the code; not seen live | O23's fix applies: a busy button keeps focus and is `aria-disabled`, and the outcome takes focus. Added to [#49](https://github.com/microsoft/mosaic-apim/issues/49#issuecomment-5901935028) |
| O26 | Closing a dialog doesn't return focus to the button that opened it. Console dialogs open from page state, and none uses a `DialogTrigger`, so Fluent returns focus only to an element marked with `useRestoreFocusTarget()`. Only 2 of the console's 18 dialogs have marked openers: the Models page's removal confirmations and the gateway's management-mode dialog. Seen live on the Batch 3d build: closing **Connection info** on Entitlements leaves nothing focused. Closing **Change environment** on an endpoint, or **Import from gateway**, moves focus to **Remove** on the first published model, in another section, because it was the last marked element that had focus. In P5, approving an access request closed the dialog with its opener gone, and nothing had focus. The portal has no dialogs | Mark each opener with `useRestoreFocusTarget()`, and give focus a sensible place when the dialog's action removes its opener, as approving an access request or removing a published model does. Landing on a destructive button nobody chose is worse than losing focus. Added to [#49](https://github.com/microsoft/mosaic-apim/issues/49#issuecomment-5901935028) |
| O27 | In the Grants table on Entitlements, the Binding badge, a `mosaic-grant-…` subscription name and its source, doesn't wrap. It runs under **Revoke** in the next column. Seen live at 1440 pixels wide with three applied grants, and the README's `console-entitlements` screenshot shows it in its last row | Fixed in [#58](https://github.com/microsoft/mosaic-apim/pull/58), and deployed with Batch 3e. The badge holds only the subscription name and wraps inside its column, with the source on a line of its own beneath. Every binding stays inside its cell at 1280, 1440 and 1920 pixels, in both themes. The regenerated `console-entitlements` screenshot is tall enough to show a bound grant. Seen live after Batch 3e at 1440 pixels: the badge wraps onto three lines inside its column. Filed as [#53](https://github.com/microsoft/mosaic-apim/issues/53) |
| O28 | A grant's call quota, a number of calls per hour, day, week, month or year, can be set only through the API. The console and the portal display it, but neither **Add entitlement** nor **Approve access request** has a field for it. A weekly call quota's counter key finds the start of the week with `(int)now.DayOfWeek`. API Management's list of types allowed in policy expressions includes the `DateTime.DayOfWeek` property but doesn't name the `System.DayOfWeek` enum it returns, and the APIM fake in the tests doesn't check types. If API Management refuses it, every apply for that model fails and rolls back, as G17's did. From the code and the policy docs, not seen live | Add the call quota to the console's forms, or say there that it's set through the API. Apply a weekly call quota once on a disposable publication, and if API Management refuses it, find the start of the week without `DayOfWeek`. Filed as [#52](https://github.com/microsoft/mosaic-apim/issues/52) |
| O29 | The portal's header counts the caller's access as "1 entitlements · 0 pending requests", with no singular, and one open request reads "1 pending requests". It's also the only visible place in the portal that says "entitlements"; every page says "grant". Seen in P3 and P5 | Fixed in [#57](https://github.com/microsoft/mosaic-apim/pull/57), and deployed with Batch 3e. The header says "grant" and "request", in the singular for one, such as "1 grant · 1 pending request". The portal's screenshots are regenerated. Seen live after Batch 3e: the `user` persona's header reads "7 grants · 0 pending requests". Filed as [#55](https://github.com/microsoft/mosaic-apim/issues/55) |
| O30 | The console's **Pending access requests** table lists each requester by object ID, even one MOSAIC has registered with a label. **Approve** and the banner after it look the requester up and show the label. Seen in P5 | Fixed in [#58](https://github.com/microsoft/mosaic-apim/pull/58), and deployed with Batch 3e. The table uses Approve's lookup and shows a registered requester's label, with the object ID beneath. Unregistered requesters still need O17's fix. Not seen live yet, as no request is pending. Added to [#45](https://github.com/microsoft/mosaic-apim/issues/45#issuecomment-5902625270) |
| O31 | In the console's **Pending access requests** table, **Approve** and **Deny** stick out about 15 pixels past the table's right edge at 1280 pixels wide. Found in #58's width checks on the demo estate | Fixed in [#60](https://github.com/microsoft/mosaic-apim/pull/60), and deployed with Batch 3e. The table sits in a two-thirds-width card, so at 1280 pixels its Actions column is narrower than a medium button's minimum width. Both buttons are now small, as on Settings → Environments, and stay inside their cell at 1280, 1440 and 1920 pixels, in both themes, without taking width from the other columns. Between 1080 pixels, where the cards stack, and about 1150 pixels, the buttons still pass their cell's edge. The environment badges beside them overflow their columns at those widths, and by about 7 pixels at 1280, as they did before. Not seen live yet, as no request is pending |
| O32 | **Approve access request** shows the requester's object ID twice when the matching principal has no label and its recorded object ID differs in letter case from the request's, because the dialog compares the two case-sensitively. Found in #58's review | Fixed in [#60](https://github.com/microsoft/mosaic-apim/pull/60), and deployed with Batch 3e. With no label, or a blank one, the dialog names the requester by the object ID the request recorded, and compares label and ID without regard to letter case, so the ID appears once. Not seen live yet, as no request is pending |
| O33 | **Unpublish** in the Models page's Published models table acts at once, with no confirmation and no plan to review, though the page's design is that the administrator sees every plan before it runs. In A15, one click removed the publication's API, product, backend, policy fragment and bootstrap subscription. On a governed publication, the same click also removes every grant's subscription, so every grantee loses access. MCP servers unpublish the same way | Fixed in [#65](https://github.com/microsoft/mosaic-apim/pull/65), and deployed with Batch 3f. **Unpublish** now asks MOSAIC for an unpublish plan, which deletes nothing, and opens it in a review: who loses access and what stops working for each grant, and every resource MOSAIC deletes, in order. Only the review's **Unpublish model** runs it. MOSAIC runs exactly the reviewed steps, and refuses a plan the publication has since outgrown, or an unpublish with no plan. MCP servers work the same way. Seen live in A15's rerun |
| O34 | After unpublishing, the publication shows **Draft** with its old **Last applied** time. Its entry under **Imported model APIs** stays discoverable, and the portal's catalog still lists the model with **Request access**, though the gateway no longer serves it. Seen in A15 | Fixed in [#65](https://github.com/microsoft/mosaic-apim/pull/65), and deployed with Batch 3f. While a model or MCP server MOSAIC publishes has no API in API Management, the portal leaves it out of the catalog, refuses a new request for it with a `409`, marks grants and requests for it as no longer available, and gives no connection details for it. The console shows the publication as **Unpublished**, with when. Model APIs imported from a gateway are unaffected. Seen live in A15's rerun: the catalog dropped the model, and a request from a page loaded earlier got the reason |
| O35 | With directory lookup turned off, **Overlapping grants** warns that principal membership overlaps weren't checked, and the warning ends in two periods: the console adds one after the API's reason, which already ends with one. Seen after Batch 3e | Add the period only when the reason lacks one. Cosmetic |
| O36 | A key-authenticated endpoint (G18) may name any secret in any Key Vault that MOSAIC's identity and the gateway's can read. Today both can read only the environment's vault, and it holds no other secrets. But once the gateway holds Key Vault Secrets User there, a key endpoint could point at a secret stored for another purpose, and the gateway would send it to that endpoint. From #69's review | Accept only the environment's vault, or only secrets marked for MOSAIC, for example by a name prefix or content type. A follow-up, not blocking |
| O37 | **A grant's key gets HTTP 500 from a governed model.** Since [#71](https://github.com/microsoft/mosaic-apim/pull/71), the attribution trace in every governed policy also records the caller's client ID as trace metadata. A caller with a key has no client ID, so the value is empty, and API Management refuses a trace metadata element with no value: "Expression value is invalid. The value field is required." The call fails before it reaches the model. Seen live on Claude's grant key after its first governed apply on Batch 3h's build, and the gateway's resource log names the trace as the source. Tokens carry a client ID, so they're not affected. The seven governed models applied in Batch 3f record only the grant, so their keys still work until they're re-applied. [#62](https://github.com/microsoft/mosaic-apim/pull/62)'s member metadata, added when a publication has a group grant, is empty in the same way for every caller of a direct grant on that publication, for models and MCP servers | Fixed in [#81](https://github.com/microsoft/mosaic-apim/pull/81): every trace property records `-` for a value the call doesn't have, the message the usage queries read is unchanged, and a test fails if code adds trace metadata any other way. Deployed in Batch 3i and verified live: after the re-applies, every governed model's grant key reaches its model, Claude's included, and a key call's trace records `-` as its client, both in the resource logs and in Application Insights. The case of a direct grant beside a group grant wasn't seen live, because no publication here has a group grant |
| O38 | After `azd deploy api` finished, the old API container kept answering for about seven minutes, until the new one passed its warm-up probe, which took 204 seconds. The console, deployed meanwhile, already called the new API routes, so **Cost centers** said "Unable to load data: Not Found" until the new API took over. Nothing the API returns says which build it runs, so only the missing routes showed it hadn't switched. Seen in Batch 3i. In Batch 3j, the warm-up took 69 seconds and the new API took over about three and a half minutes after `azd deploy api` finished | Report the build, such as the commit or image tag, on `/healthz` or a version route, and have deployments wait for it before they deploy the console and the portal. Find out why the warm-up varies from about one minute to over three. A follow-up, not blocking |
| O39 | MOSAIC's API sends the Azure SDK's log of every HTTP request it makes, and the Azure Monitor exporter's log of each of its own uploads, to Application Insights. A quiet dev deployment logged about 330,000 such entries in a day, against about 4,500 others. Each upload logs more entries to upload. No secret is in them: credential headers don't appear, and bodies are only noted as present. But they cost ingestion and bury MOSAIC's own logs. The cause is the root logger's INFO level, from which the exporter collects. Found while checking R8 after Batch 3i | Fixed in [#84](https://github.com/microsoft/mosaic-apim/pull/84), merged: it raises three loggers to at least `WARNING`, azure-core's HTTP logging policy, Cosmos DB's own HTTP logging policy and the exporter's. MOSAIC's logs, other libraries' records, and every warning and error still reach Application Insights. [#85](https://github.com/microsoft/mosaic-apim/pull/85), merged, adds a test that other Azure SDK loggers still export at INFO, so the fix can't widen unnoticed. Deployed in Batch 3j and verified live: from about 2,430 such records in ten minutes to none, while MOSAIC's own logs, other libraries' records and Cosmos DB dependencies still arrive |
| O41 | MOSAIC's API has never recorded its incoming requests in Application Insights: there were none in 48 hours, while its traces and Cosmos DB dependencies arrive. Reproduced locally. The Azure Monitor distro instruments FastAPI by replacing `fastapi.FastAPI` with an instrumented subclass, but `main.py` imports the class before that, so the app it builds isn't instrumented. Operators can't see the API's request rates, failures or latency there. MOSAIC's outbound calls through httpx, to Azure Resource Manager, Log Analytics and API Management, aren't recorded as dependencies either, because nothing instruments httpx. Found while verifying Batch 3j | Fixed in [#86](https://github.com/microsoft/mosaic-apim/pull/86), merged: the API instruments its app explicitly once it's created, leaves out the health probes, and never records headers or bodies. Its review found that the recorded URL keeps query values, and the directory search's `q` holds names and email addresses, so [#87](https://github.com/microsoft/mosaic-apim/pull/87) redacts them. Both deployed in Batch 3k and verified live: in the first half hour the API recorded 142 requests, none of them a health probe, and every query value read REDACTED. Tracing httpx calls would add a dependency, so it's a separate decision |
| O42 | MOSAIC's price list has no price for most models this environment runs, though the Azure Retail Prices API lists some of them. Its seed is a curated list of models, each mapped to its meters, which covers GPT-4o, GPT-4.1, GPT-5 and the o-series, Llama 3.3, DeepSeek-R1, Phi-4 and embeddings. In eastus2 the API has global prices for `Llama-4-Maverick-17B-128E-Instruct-FP8`, as "Llama 4 Maverick 17B" ($0.25 in, $1.00 out per million tokens), and for GPT-5.1 chat. The seed has neither. Azure reports the `gpt-5.1-chat` deployment's model by its alias, `gpt-chat-latest`, so no curated name would match it anyway. DeepSeek-V4-Pro has only Data Zone prices, under Fireworks, and Grok 4.3 has none yet, so those are correctly unpriced. Seen in R9 | Add the models with retail meters to the seed: Llama 4, GPT-5.1 and its chat model, GPT-5.4 and GPT-5.4-nano. Decide how an alias such as `gpt-chat-latest` maps to a price, for example by its version date. **Add price** covers the rest. A follow-up, not blocking |
| O43 | Usage and cost count tokens for calls the gateway refused before they reached the model. When a grant's token limit refused two calls with 429 (`TokenLimitExceededAfterPrompt`), the gateway's LLM log still recorded its prompt estimate for them, 22 tokens with no model name. MOSAIC counted and priced those tokens, though Azure doesn't bill a call that never reached it. Because the rows have no model name, **Cost by model** left them out: its shares summed to 96.2%, and it gave the model 473 tokens where **Cost by API** gave its two APIs 495. Seen in R9 after the sitting's R6 run | Count and price tokens only for calls that reached the model, and attribute a counted row with no model name to its deployment's model, so every breakdown adds up to the total. Fixed in [#89](https://github.com/microsoft/mosaic-apim/pull/89), deployed in Batch 3k and verified live after a three-day backfill: the training account's gpt-4o API went from 445 tokens to 423, and the tokens by model and by API both came to 1,667 |
| O44 | In **Analytics > Cost**, the cost shares of a breakdown can add up to more than 100%. After Batch 3k, both the cost by model and the cost by API added up to 103.1%. Each part's share divides its full-precision cost by a total that `cost_report.py` has already rounded to $0.0001. At this environment's sub-cent totals, about $0.0014, the rounding is about 3% of the total. At real spend it's negligible. Seen in Batch 3k's check of O43 | Divide by the unrounded total, and round only what's shown. A follow-up, not blocking |
| O45 | Since the API records its requests (O41), App Service's ping of `/` every 5 minutes is recorded as a failed request: `GET /` gets 404, about 288 times a day. That inflates the API's failure rate in Application Insights. Seen in Batch 3k | Serve `/` with 200, or point App Service's health check at `/healthz`, or leave `/` out of the recorded requests as the probes are. A follow-up, not blocking |
| O40 | For `grok-4.3`, **Usage & cost** and **Analytics** show 196 tokens for one call, broken down as "Prompt 8 · Completion 2". The gateway's LLM log recorded a total of 196, but only 8 prompt and 2 completion tokens. The other 186 are probably the model's reasoning tokens, which the breakdown doesn't name. The grant's tokens-per-minute limit counts all 196, so a person whose calls are refused sees parts that don't add up to what was counted. Seen in R8 after Batch 3i. The sitting's token metrics confirm they're reasoning tokens: its two grok calls' metrics read 16 prompt, 4 completion and 456 reasoning tokens, 476 in all | Show reasoning tokens as their own part wherever a total is broken down, as the gateway's metrics already do, and check whether the LLM log names them. A follow-up, not blocking |
The Phase 3 check on whether the gateway role recommendation narrows once the account kind is
known led to G8: it does narrow, and the check then rejects the broader role it recommended
before.

## Risks

- **Anthropic token limits on classic APIM.** A Developer-tier gateway can't run `llm-token-limit`
  or `llm-emit-token-metric` for Anthropic. G5 decided: on classic tiers, a Claude publication
  applies no token limits, and its grants use call limits instead. So R6 (the tokens-per-minute
  429) can't cover Claude here, and Application Insights gets no token metric for it. MOSAIC's
  analytics still count its tokens. They read the gateway's LLM log, which recorded Claude's 14
  input and 4 output tokens on this tier after Batch 3i. A v2-tier gateway would lift the limits'
  restriction, at extra cost.
- **Model availability and terms.** Claude needs an eligible subscription and region, available
  quota and accepted marketplace terms. Accepting terms may need someone in the Azure portal.
- **Network reach.** A Developer-tier gateway without a virtual network can't reach private
  endpoints. That's why the private account is only a negative case.
- **Sign-in friction.** Conditional Access and MFA are handled by a person in persistent profiles.
  A guest whose home tenant requires a managed browser can switch that persona to Edge.
- **Propagation delays.** RBAC, Entra consent and APIM policy all take time to apply, so journeys
  poll with explicit timeouts instead of fixed sleeps. A role granted to MOSAIC's managed identity
  took more than 17 minutes to apply (O7), and restarting the API didn't help. So the gateway's
  roles (Batch 2c) go in as soon as the UI shows them, well before Phase 8 needs them.
- **Secret leakage.** Handled by the redaction and artifact rules above. Browser profiles hold
  refresh tokens, so they live under the user's local app data folder, never in the repository.
- **Redeploying from a new checkout.** Rebuild the azd environment from live values and confirm
  with `azd provision --preview` that it targets the existing resources before running `azd up`.
- **Gateway maintenance.** A Developer-tier gateway has no SLA and goes offline for platform
  updates. During one, ARM answered requests for it with 422 `ManagementApiRequestFailed` for about
  7 minutes. Check that the gateway answers before a publish batch, and treat that error as
  transient.

## Out of scope

Group-based grants, analytics beyond R8's check, chargeback, production hardening, and running
the live suite in CI (it needs interactive sign-in). MCP servers were out of scope until the
environment owner asked for them on 2 October 2026; Phase 11 covers them.
