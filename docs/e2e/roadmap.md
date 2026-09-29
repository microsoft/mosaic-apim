# End-to-end model import and access roadmap

This roadmap proves that MOSAIC works from start to finish **through its own UI**, against a real
deployed environment:

1. An administrator imports real Azure OpenAI and Microsoft Foundry endpoints.
2. The administrator publishes their models through Azure API Management (APIM).
3. The administrator grants those models to people and to a workload application.
4. Those callers, and only those callers, can invoke the models through the gateway.

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

Six endpoints cover both providers, all three registration paths and every model family MOSAIC
publishes today. One more endpoint is a deliberate negative case.

| Target | Kind | Registered by | Models to publish |
| --- | --- | --- | --- |
| AOAI A | Azure OpenAI | Pasting the account resource ID (first, to observe the missing-permission state) | Two chat deployments |
| AOAI B | Azure OpenAI | Discovery suggestion | A chat deployment and an o-series reasoning deployment |
| AOAI C | Azure OpenAI | Discovery suggestion, in another region | A reasoning deployment and a chat deployment |
| Foundry multi-provider | Foundry (AIServices) | Discovery suggestion | xAI Grok, Meta Llama and DeepSeek; Anthropic Claude after G5 |
| Foundry project | Foundry (AIServices) | Pasting a Foundry **project** resource ID | A small OpenAI model and Llama 4 |
| Foundry hub-connected | Foundry (AIServices) | Discovery suggestion | A chat model |
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
| G6 | Only if Grok or Llama fail on `/models/chat/completions` through APIM | Add OpenAI v1 routes | ⬜ conditional |
| G7 | When MOSAIC's identity can see no subscriptions, discovery shows nothing at all: no suggestions, no unreadable subscriptions and no hint. Found live in Phase 3 | Say how many subscriptions were scanned. When there are none, or the list fails, show the Reader command for the subscriptions MOSAIC already knows about ([#17](https://github.com/microsoft/mosaic-apim/pull/17)) | ✅ merged |
| G8 | Gateway runtime readiness can disagree with what the gateway can actually call. It accepts exactly one role, so a sufficient role such as the Cognitive Services User role it recommends before it can read the account is later reported as missing. It also checks a project-registered endpoint at the project scope, although published APIs call the parent resource, where a project-scoped grant doesn't apply | Judge readiness at the account the published API calls, and accept any role whose data actions cover the published operations. Recommend only roles the check accepts: Cognitive Services OpenAI User for Azure OpenAI, and Foundry User otherwise. A deny assignment, or disabled public access with no virtual network on the gateway, means "cannot invoke". Conditions MOSAIC can't evaluate mean "not confirmed". The endpoint's Access card shows its network, firewall and key settings. Readiness covers every API MOSAIC can publish from the endpoint, including Anthropic Messages on AI Services accounts ([#23](https://github.com/microsoft/mosaic-apim/pull/23)) | ✅ merged |
| G9 | After a redeploy, an open browser kept running the previous console build. Both web apps' nginx serve `index.html`, the SPA routes and `/config.js` with no `Cache-Control`, so browsers cache them heuristically and load the old hashed bundle. Found live in Phase 3 | Revalidate the HTML, the SPA fallback and `/config.js` on every load. Cache the hashed `/assets/` files as immutable, and return 404 for a missing asset instead of the SPA page. Keep the security headers on every response ([#29](https://github.com/microsoft/mosaic-apim/pull/29)) | ✅ merged |
| G10 | Discovery said "Scanned 1 subscription. Nothing new to register." while the subscription held 56 Azure AI accounts MOSAIC couldn't read. MOSAIC's roles were all on single resources, and ARM silently filters a subscription-wide list to what the caller can read, so the scan looked complete. Found live in Phase 3 | Check MOSAIC's own permissions at each scanned subscription. When it can read only part of one, say so and show the subscription Reader command. Also say, wherever MOSAIC asks for a role on its own identity, that a new role can take a while to apply (O7) ([#28](https://github.com/microsoft/mosaic-apim/pull/28)) | ✅ merged |
| G11 | Discovery suggested, and registration accepted, the parent account of a registered Foundry project. The second endpoint had the same URL, and syncing it listed the project's deployments again, each publishable on its own (O9). **Remove** on an endpoint deletes it and its synced models at once, and the server doesn't check publications, so a publication whose endpoint is gone can't be re-planned or applied (O10). Found live in Phase 3 | Treat each registration as covering its account: don't suggest a covered account, and refuse a registration that overlaps one, naming it. Confirm before removing an endpoint, and refuse while publications depend on it ([#27](https://github.com/microsoft/mosaic-apim/pull/27)) | ✅ merged |
| G12 | Endpoint settings leave out the Key authentication row and its note when an account doesn't set `disableLocalAuth`. Azure leaves it unset by default, which means keys are enabled, so both Azure OpenAI targets showed nothing (O8). Found live in Phase 3 | Treat an unset value on a readable account as Enabled, and add the note ([#26](https://github.com/microsoft/mosaic-apim/pull/26)) | ✅ merged |
| G13 | **No model can be published.** The default publish plan creates the policy fragment before the backend its `set-backend-service` names. APIM accepts the fragment PUT, then its validation fails it: "Backend with id '…' could not be found." The run rolls back, so API Management is left unchanged. The console shows only "The Azure operation did not succeed", because MOSAIC drops Azure's error when it polls the operation. The governed-access plan already creates the backend first. Also, the fragment PUT is long-running even when it updates an existing fragment (its 200 carries a poll header too), but MOSAIC polls only 201 and 202. So a fragment update that APIM rejects would be reported as success, which matters for governed access and every later re-apply. Found live in Phase 5 (A8) | Create the backend before the fragment, which also makes teardown remove the fragment first. Refuse to apply a plan saved in the old order, and ask for a re-plan. Show Azure's reason when an operation fails, and when a request is refused outright. Poll any write response that carries a poll header. Make the test fake of APIM validate fragments the way APIM does ([#30](https://github.com/microsoft/mosaic-apim/pull/30)) | ✅ merged |
| G14 | The console never tells someone without the Admin role that it isn't for them. For an account with only the User role, and for one with no MOSAIC role, it renders the whole admin shell with its actions, labels the account "Global Admin" (hard-coded for every Entra sign-in), calls it the administrator on Settings and the profile page, and shows "Unable to load data" in every live section. The API refuses correctly, so no admin data is shown. The portal already handles the same case with one clear denial and a sign-out button. Initials also keep punctuation, so a display name like "Name (Team)" shows "N(". Found live in A1 | A new `GET /api/v1/console/me` returns the caller's MOSAIC roles from the access token, and the console asks it before rendering. Without the Admin role it shows one card instead of the shell: no access for an account with no role, and a pointer to the end-user portal for a User, each with **Sign out**. The account label reads "MOSAIC Admin", and initials use letters and digits only. No infrastructure or app-setting change, so it ships in an image-only deploy, with the API before or together with the web app ([#31](https://github.com/microsoft/mosaic-apim/pull/31)) | ✅ merged |
| G16 | **Re-plan** says "Created a fresh publish plan. Review it before applying.", but nothing shows that plan: the console discards it, and the API can't return a saved plan. On a publication without governed access, the row's **Apply** then applies the saved plan with no review. The README says re-planning shows how API Management has diverged, and the page says changes are made only after a reviewed plan is applied. Found live in A8 | Remove the row's **Apply**. **Re-plan** makes a fresh plan and opens it in the publish dialog's review, and only **Apply plan** there applies it. When an apply is refused, the dialog says "MOSAIC didn't apply the plan you reviewed", gives the server's reason, and says it has already re-planned (O13). Web only ([#32](https://github.com/microsoft/mosaic-apim/pull/32)) | ✅ merged |
| G17 | **Governed access can't be applied.** The policy expressions MOSAIC generates for governed access use single-statement control flow, such as `if (…) return "";`. APIM rejects every such fragment: "Block statements must be enclosed in "{" and "}". You cannot use single-statement control-flow statements in CSHTML pages." The apply then falls back to its last safe snapshot, as designed, which for a publication that never had governed access denies every call. The test fake of APIM accepts any expression, so the unit tests passed. Found live in A11 | Brace every control-flow body in the generated expressions, and make the APIM fake reject unbraced control flow the way APIM does. API only, so it ships in an image-only deploy | 🔄 in progress |

There is no G15. What was first logged as G15 turned out to be APIM's own behavior, and is
recorded as O12.

G12, G9, G11, G13 and G10 are merged, in that order, and Batch 3b deployed them. G14
([#31](https://github.com/microsoft/mosaic-apim/pull/31)), a test fix
([#33](https://github.com/microsoft/mosaic-apim/pull/33)) and G16
([#32](https://github.com/microsoft/mosaic-apim/pull/32)) merged next, and Batch 3c will deploy
them. #25 now follows G16 and is ready to merge. It merged `main` without conflicts, and its test
that clicked the row's **Apply**, which G16 removed, now clicks **Re-plan**. A new test checks
that focus lands in the review **Re-plan** opens for a publication without governed access. G17
is being fixed, and Batch 3c should wait for it too, because no governed apply can succeed without
it.

The test fix is for web tests that G11 added and that failed intermittently. About 250 ms after a
Fluent dialog opens, the rest of the page becomes `aria-hidden`, and it stays hidden for about
250 ms after the dialog closes. Role queries skip hidden content, so the tests failed whenever
they queried the page inside one of those windows. #33 changes only the tests. It left two
follow-ups, neither of which blocks this plan:

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

### Phase 2: Tenant prerequisites ⏳ approval

Each batch runs only after approval and is recorded in the change ledger.

- Deploy Claude (small capacity) to the multi-provider Foundry resource, after checking
  eligibility, region, quota and marketplace terms.
- Assign the MOSAIC User role to the `user` persona. `noRole` keeps no role until Phase 7.
- Create the workload app registration and service principal, assign `Models.Invoke.Application`,
  and grant admin consent.
- Reader and Cognitive Services roles are **not** granted here. Phase 3 grants them from the
  remediation commands the UI shows.
- **Exit:** the ledger lists every change and its rollback command.

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
  - Next, Batch 3c (images only, after approval): G14
    ([#31](https://github.com/microsoft/mosaic-apim/pull/31)), G16
    ([#32](https://github.com/microsoft/mosaic-apim/pull/32)) and, once it's merged, the dialog
    fix for O4 ([#25](https://github.com/microsoft/mosaic-apim/pull/25)). G14 adds an API route,
    so the API must go out with or before the web app, which `azd deploy --all` already does.
    Since Batch 3b, `main` changes no infrastructure, Entra hook, app setting, Dockerfile, nginx
    configuration or package manifest. Batch 3c should also carry G17 once it merges, since every
    governed apply fails without it. Phase 6 started on the Batch 3b build anyway: MOSAIC UI
    actions need no approval, and the harness reaches the `aria-hidden` review (O4) by CSS
    instead of by role.
- **Exit:** the deployed build contains G1 to G5, G7 and G8, and the smoke specs pass.

### Phase 5: Live, admin publishes (A7 to A9) 🔄 every target published; the re-plan check waits for Batch 3c

- Switch the gateway to manage mode (G1). Publish each target deployment and review the plan
  steps and policy facets before applying. Every step must succeed.
- Re-planning a publication that nobody changed must plan no creates or deletes, and applying
  that plan must leave API Management as it was. A plan compares which resources exist, not what
  they contain, so every step of such a plan is an update ("Replace …"). Cross-check the APIM
  APIs, products, backends, fragments and policies with `az` before and after the apply.
- Make the published models visible in the portal catalog.

Twelve deployments are publishable now. Claude (`claude-haiku-4-5`) waits for Batch 1.

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
- ✅ A9: each published model has a row under **Imported model APIs**, and its **Catalog** select
  starts at "Discoverable". The `guest` persona's portal catalog listed all 12, each with
  **Request access**. Setting AOAI C `o4-mini` to "Entitled users only" removed it from that
  catalog after a reload, and setting it back restored it. Every entry reads "No summary
  provided." (O14).

### Phase 6: Live, admin sets identity and governed access (A10 to A12) 🔄 started early; A11 failed on G17

- Create identity entries for the `user` persona and the workload service principal. The `noRole`
  persona is left unregistered on purpose, so Phase 7 shows that approving a request from someone
  MOSAIC hasn't seen before still produces a grant.
- Enable key and Entra access with limits, grant the `user` persona and the workload, and run the
  model-wide review and apply.
- Get the workload's connection details and key handoff from the console. The key never appears
  in logs.

Progress:

- ✅ A10, `user` persona: **Add user** stores only the object ID and a local label, and the entry
  shows as Live. Adding the same object ID again is rejected with "Unable to add principal: A
  principal with this Entra object ID already exists".
- ⏳ A10, workload: waiting for the workload app registration (Phase 2).
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
  - Once G17 is deployed, retry with **Review model changes** and **Apply plan**.

### Phase 7: Live, end-user portal (P1 to P8, A13) 🔄 P0 to P2, P4 and P8 done; A13's apply waits for G17

- The `noRole` persona is denied cleanly. A persona with the User role and no grants sees an
  empty My access view and the catalog.
- That persona requests access with a justification, withdraws the request and requests again.
  The admin approves it, which creates grant intent (G2), and reviews and applies it. The persona
  then sees the grant. The `guest` persona already has the User role and isn't registered in
  MOSAIC, so it runs these steps without another tenant change.
- The `user` persona sees its grants, limits and connection details (G3). Primary and secondary
  key reveal is masked, transient and never cached.
- Users are isolated from each other: another user's entitlement ID returns 403 or 404.

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
  - Once G14 ([#31](https://github.com/microsoft/mosaic-apim/pull/31)) is deployed, A1's smoke
    spec must assert the console's new denial instead of the per-section error.
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

### Phase 8: Runtime verification (R1 to R8, A14) 🔄 verifier ready

`scripts/verify_model_access.py` now covers this phase, with unit tests against a fake gateway
that applies the governed policy. It reads each grant's connection details from MOSAIC, calls the
operation its publication exposes, and can sign callers in itself. The live run waits for the
tenant batches, the redeploy and Phases 5 to 7.

| Journey | How the verifier covers it |
| --- | --- |
| R1 | One `--user-entitlement` per model; the verifier calls whichever operation the publication exposes. Azure OpenAI deployments use `/openai/` routes with `--api-version`. Foundry deployments, such as Grok and Llama, use `/models/chat/completions` with `--models-api-version`. Claude uses `/anthropic/v1/messages` (G5) |
| R2 | `--user-token-source device-code` signs the user in through the G4 model client. `--check-ungranted-user` signs in a second person, whose token the grant lookup must refuse with 403. Every run also sends a MOSAIC control-plane token, the wrong audience, which token validation must refuse with 401 |
| R3 | `--application-entitlement` with `--application-token-source client-credentials`. The admin's control token hands off the workload's key |
| R4 | Every run: an anonymous call, an invalid key, an invalid token with a valid key, and the other subject's token with the grant's key. The end user also can't reveal an application's key |
| R5 | `--prove-shared-budget`, on fresh grants limited to 2 calls per 300 seconds. The secondary key's 429 must come from the gateway's call limit, not the deployment |
| R6 | `--prove-token-limit`, on fresh grants limited to at most 100 tokens per minute, on a model whose own limit for each grant is higher. The 429 must come from the gateway's token limit. Not Claude on the classic tier, which can't limit Anthropic tokens (G5) |
| R7 | `--watch-revocation <grant-id>` waits while the admin revokes the grant, which disables it, and applies the plan (A14). Rejections count only once MOSAIC reports the grant revoked, and must repeat |
| R8 | Manual: find the calls in Application Insights and Log Analytics |

Method toggles (A14) are checked by rerunning the verifier after each reviewed plan.

Proposed addition to R4, which needs approval because it reads keys: APIM's all-access key, the
key of the subscription APIM gave its Administrator (O12) and MOSAIC's bootstrap key must each be
refused on a governed publication.

The live driver's `verify` command runs the verifier for the personas. It signs the `user`
persona, and the `admin` persona for application grants, in to MOSAIC again, and passes their
MOSAIC API tokens to the verifier without anyone copying them. It enters the verifier's device
codes in the right persona's browser, leaving a person to confirm the sign-in and complete MFA.
The [runbook](runbook.md#verify-runtime-access) shows how to run it.

### Phase 9: Codify, document, clean up ⬜

- Turn the live run into ordered specs built on page objects.
- Record findings here and file issues for anything deferred.
- Unpublish a disposable publication (A15) and confirm that only MOSAIC-created resources are
  removed.
- Roll back test-only tenant changes from the ledger. Imported and published models stay, since
  they're the goal.

### Phase 10 (later): Gemini and AWS Bedrock ⬜

The console can already register an OpenAI-compatible endpoint, storing a Key Vault secret URI
rather than the key. MOSAIC doesn't discover that endpoint's models, though, and it refuses to
publish it because no curated API shape exists. Phase 10 is a design spike to close that gap:
discovery, a versioned shape, and backend credentials kept in Key Vault and read by the APIM
identity. Gemini would use its OpenAI-compatible endpoint or the Vertex AI API; Bedrock would use
an API key or SigV4. The environment owner writes the secrets and shares only their Key Vault URIs.

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
| A8 | Publish every target deployment; every plan step succeeds, and applying a re-plan of an unchanged publication leaves API Management as it was | 5 | 🔄 Batch 3c |
| A9 | Catalog visibility makes a published model appear in the portal | 5 | ✅ |
| A10 | Identity entries exist for the `user` persona and the workload; a duplicate is rejected | 6 | 🔄 |
| A11 | Governed access with keys, Entra and limits is reviewed and applied, and the applied state shows | 6 | ❌ G17 |
| A12 | Workload connection details and key handoff work, and the key is never logged | 6 | ⬜ |
| A13 | Approving an access request creates grant intent, which is then reviewed and applied (G2) | 7 | 🔄 G17 |
| A14 | Disable, revoke and method toggles go through review and apply | 8 | ⬜ |
| A15 | Unpublishing removes only what MOSAIC created | 9 | ⬜ |

### Portal

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| P0 | A User persona reaches My access and the catalog | 1 | ✅ |
| P1 | A persona without a MOSAIC role gets a clean denial with a sign-out option | 1, 7 | ✅ |
| P2 | With the role but no grants, My access shows its empty state and the catalog is visible | 7 | ✅ |
| P3 | My access shows applied grants, limits and attribution | 7 | ⬜ |
| P4 | A request with a justification can be withdrawn and requested again | 7 | ✅ |
| P5 | After approval and apply, the requester sees the grant | 7 | ⬜ |
| P6 | Connection details appear, and key reveal is masked, transient and uncached (G3) | 7 | ⬜ |
| P7 | Another user's entitlement ID returns 403 or 404 | 7 | ⬜ |
| P8 | The admin shows as allowed in the portal | 7 | ✅ |

### Runtime (real calls through APIM)

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| R1 | A granted user reaches every provider by key: Azure OpenAI, Foundry OpenAI, Grok, Llama, DeepSeek and Claude (after G5) | 8 | ⬜ |
| R2 | A granted user's Entra token works (G4); a token without a grant and a wrong-audience token are denied | 8 | ⬜ |
| R3 | The workload's client-credentials token and its handed-off key both work | 8 | ⬜ |
| R4 | Anonymous, invalid-key and cross-subject calls are denied | 8 | ⬜ |
| R5 | A shared budget of 2 calls per 300 seconds, spent by primary key and token, returns 429 for the secondary key | 8 | ⬜ |
| R6 | The tokens-per-minute limit returns 429 with `Retry-After` | 8 | ⬜ |
| R7 | After revocation propagates, calls fail | 8 | ⬜ |
| R8 | Calls show up in Application Insights and Log Analytics (optional) | 8 | ⬜ |

## Findings

These are smaller than the gaps above, and most came from live runs. They're recorded so they can
be confirmed, or fixed, once the journeys that exercise them have run.

| ID | Observation | Next step |
| --- | --- | --- |
| O1 | Before MOSAIC can read an account, it records a placeholder endpoint (`https://<account>.cognitiveservices.azure.com`) and the provider "Azure AI Foundry", even for an Azure OpenAI account. The UI shows these as fact | Confirmed in Phase 3: once MOSAIC can read the account, **Check access** corrects both. Still worth labeling the values as unconfirmed until the first successful read |
| O2 | A rejected duplicate registration appears under the generic title "Unable to load data". The Identity page gets this right with "Unable to add principal" | Fixed by G11, which titles every refused registration "MOSAIC didn't register this endpoint". Seen live after Batch 3b |
| O3 | The console's key reveal (`EntitlementConnectionDialog`) shows the key in an element labelled "Revealed primary key" or "Revealed secondary key", with no `data-secret` marker. The harness masks it by that label | Add `data-secret` to the revealed value when the component is next changed, so any tooling can find it |
| O4 | Opening **Review model access** from the Models or Entitlements page left keyboard focus on the page, not in the dialog. The dialog first rendered its opening step and then switched to the review in an effect, which removed the control that had focus. The same dialog also showed its first step while it closed, and an apply that finished after it closed made the next **Publish a model** open on "Step 4 of 4". Found while stabilizing the web tests ([#24](https://github.com/microsoft/mosaic-apim/pull/24)). Seen live in A8's retry, and worse: the review the console opened after the refusal was itself `aria-hidden`. Focus never entered it, so screen readers and role queries couldn't reach the dialog, while the page behind it stayed reachable. A11 saw the same on the Entitlements page: **Review model changes** opened the review `aria-hidden` | Fixed in [#25](https://github.com/microsoft/mosaic-apim/pull/25), which now follows G16 and is ready to merge. Until it's deployed, let each apply finish before closing the dialog. Once it's deployed (Batch 3c), confirm that G16's review, and the one opened after a refusal, open on "Step 3 of 4" with focus inside and aren't hidden. Closing one with **Cancel** or Escape must keep its content until it's gone, and the next opening must start clean. A11, A13 and A14 then confirm that focus starts in the review |
| O5 | After the redeploy, each endpoint kept the readiness verdict the previous build had saved, until someone ran **Check access** again. The Foundry project still recommended Foundry User at the project scope, which G8 reports as too narrow | Run **Check access** on every endpoint after a deploy that changes the readiness rules. The product could record which rules produced a verdict and flag older ones as out of date. Seen again after Batch 3b: G12's key-authentication row appeared only after **Check access** |
| O6 | The harness's unattended sign-in gave up while silent single sign-on was still redirecting. It took the first sight of the Entra sign-in page to mean a password, MFA or consent was needed | Fixed in the harness. It gives single sign-on 10 seconds to finish before it asks for a person, and still fails at once on an Entra `AADSTS` error |
| O7 | After Reader was granted to MOSAIC's managed identity on one account (01:50), **Check access** kept failing for at least 17 minutes, past the 10 minutes Microsoft documents. ARM returned 403 to MOSAIC's read, and restarting the API didn't help. The assignment was listed at once, and no deny assignment applied. A subscription-wide Reader granted at 02:12 made every account readable within 4 to 6 minutes, including one registered only after that grant | Allow for tens of minutes after granting a role, and grant the gateway's roles (A6) well before Phase 8 needs them. G10 makes the UI say that a new role can take a while |
| O8 | Both Azure OpenAI targets showed no Key authentication row or note, though keys work on them. They never set `disableLocalAuth`, and Azure leaves it out when it's unset, which means enabled. The AI Services account sets it to false explicitly and shows "Enabled" | G12 treats an unset value as enabled. After Batch 3b and **Check access**, both show "Enabled" |
| O9 | With the Foundry project registered, discovery still suggested its parent account, and registering it succeeded. The second endpoint had the same URL, and syncing it listed the project's six deployments again, each publishable on its own. Registration rejects only an exact resource ID match, and discovery compares exact IDs | G11 refuses overlapping registrations and stops suggesting covered accounts. The duplicate was removed. After Batch 3b, the parent account is no longer suggested, and pasting its ID is refused with a message naming the project |
| O10 | **Remove** on an endpoint deleted it and its synced models at once, with no confirmation. From the code: the server doesn't check publications, and a publication whose endpoint is gone can't be re-planned or applied ("Model endpoint was not found"), so its access can't change while its API keeps serving. Registering the same resource again restores the same endpoint ID | G11 confirms first and refuses while publications depend on the endpoint. After Batch 3b, **Remove** asks first, lists what goes with the endpoint, and says MOSAIC refuses while a model from it is still published |
| O11 | A published API exposes every operation of its API shape, whatever the deployment can serve. The Azure OpenAI shape gives `gpt-35-turbo` seven operations: chat completions, completions, embeddings, image generation, audio transcription and translation, and responses. MOSAIC already syncs each deployment's capabilities (A5) but doesn't use them to choose operations | In Phase 8, confirm that a call to an operation the model can't serve fails cleanly at the model. Consider publishing only the operations that match the synced capabilities |
| O12 | When MOSAIC creates a product, APIM subscribes its own Administrator to it. So each publication without governed access has a second subscription whose key can call the model, besides APIM's all-access key. The activity log shows MOSAIC wrote only its own subscription. APIM's two built-in products got the same subscription when the gateway was created | Governed access already accepts only direct-grant subscriptions, and unpublishing deletes the product with all its subscriptions. In Phase 8, check that APIM's all-access key, the Administrator's key and MOSAIC's bootstrap key are all refused on a governed publication. Reading those keys is a secret read, so it needs approval. MOSAIC could also disable the automatic subscription, or show it in the plan |
| O13 | When a plan saved in the old order is refused, the review repeats the refusal, "…Re-plan this publication and review the new order before applying.", above a plan the console has already re-planned | G16 titles the refusal "MOSAIC didn't apply the plan you reviewed", keeps the server's reason, and adds "MOSAIC has already re-planned. Review the fresh plan below before you apply it." Its tests cover this. No plan saved in the old order is left to refuse, so a live run sees it only if another refusal happens |
| O14 | Every model in the portal catalog reads "No summary provided." The API accepts a summary for each catalog entry, and the portal shows it, but the console offers only the visibility select | Let the admin write a summary in the console, or fill a default from the endpoint, model and API shape |
| O15 | The portal's My requests page heads each request "Model API" and an internal ID, where the catalog shows the model's name. Someone with several requests can't tell them apart. My access heads each grant the same way. The console's list of pending requests does show the name | A fix is ready on a branch, waiting for write access to the repository to open its PR. The API names each of the caller's own requests and grants in a new optional field, and both pages show that name, with the kind beside it and the old heading as a fallback. It changes the API and the portal only, so it ships in an image-only deploy |
| O16 | On My access, the runtime badge in each card's header wraps inside the badge's fixed height, so "Applied to APIM" shows only "to". Other labels of several words, such as "APIM changes pending", spill out of the badge. On a narrow screen, a heading that falls back to the internal ID pushes the badge out of the card, on My requests too. The same label in the card body is fine. Seen in the README's portal screenshot, and a live run would see it in P3 | Fixed on the O15 branch, which waits for the same write access. Fluent sizes the header's badge column to the label's longest word. The badge now stays on one line, and a long heading wraps instead. A check of 144 header badges at six widths, from 1440 down to 320 pixels, found 84 cut off before the fix and none after. The README's My access screenshot is refreshed. P3 confirms it live |
| O17 | An access request records only the requester's object ID. The console lists a requester MOSAIC hasn't registered by that ID, and approving registers them with no label. Their grant, and the APIM subscription the plan names after it, then carry only the ID until an admin labels the principal on the Identity page. MOSAIC has no Graph permission to look the name up. Seen in A13 | Record the requester's name and username from their token when they request access, show them in the console, and use them as the label when approval registers the requester |
| O18 | When an apply fails, the portal shows the end user APIM's raw error under "Last APIM error", including the internal fragment name and APIM's validation text. Seen in A13 after G17 | Show end users a plain status and what to do, and keep APIM's reason for the console |

The Phase 3 check on whether the gateway role recommendation narrows once the account kind is
known led to G8: it does narrow, and the check then rejects the broader role it recommended
before.

## Risks

- **Anthropic token limits on classic APIM.** A Developer-tier gateway can't run `llm-token-limit`
  or `llm-emit-token-metric` for Anthropic. G5 decided: on classic tiers, a Claude publication
  applies no token limits, and its grants use call limits instead. So R6 (the tokens-per-minute
  429) can't cover Claude here, and analytics show no token metrics for it. A v2-tier gateway would
  lift this, at extra cost.
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

MCP servers, group-based grants, analytics and chargeback, production hardening, and running the
live suite in CI (it needs interactive sign-in).
