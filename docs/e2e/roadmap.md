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
| `guest` (B2B) | User | Cross-tenant sign-in; holds only User, so the console must withhold admin data (A1) |
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
| G2 | Approving an access request creates no grant, even though the banner says it saved grant intent | Approve opens a short dialog with limits prefilled; the server creates and links the grant intent in one step | 🔄 |
| G3 | The portal can't show connection details or reveal keys, so an end user can't get a credential | Add portal connection details and masked, transient key reveal | 🔄 |
| G4 | No client registration lets an end user get a `Models.Invoke` token | Optional public client registration with a tenant-wide grant for `Models.Invoke`, and its client ID in connection details. Builds on the Entra fix ([#19](https://github.com/microsoft/mosaic-apim/pull/19)) | ✅ merged |
| G5 | Anthropic deployments get the chat-completions API shape, but Claude needs the Messages API. `llm-token-limit` supports Anthropic only on APIM v2 tiers | Publish Anthropic with the Messages shape, and decide how to limit tokens on classic tiers | 🔄 |
| G6 | Only if Grok or Llama fail on `/models/chat/completions` through APIM | Add OpenAI v1 routes | ⬜ conditional |
| G7 | When MOSAIC's identity can see no subscriptions, discovery shows nothing at all: no suggestions, no unreadable subscriptions and no hint. Found live in Phase 3 | Say how many subscriptions were scanned. When there are none, or the list fails, show the Reader command for the subscriptions MOSAIC already knows about ([#17](https://github.com/microsoft/mosaic-apim/pull/17)) | ✅ merged |
| G8 | Gateway runtime readiness can disagree with what the gateway can actually call. It accepts exactly one role, so a sufficient role such as the Cognitive Services User role it recommends before it can read the account is later reported as missing. It also checks a project-registered endpoint at the project scope, although published APIs call the parent resource, where a project-scoped grant doesn't apply | Evaluate at the resource scope the published API calls, accept any role whose data actions are sufficient, and recommend only roles the check will accept. Also account for network reachability: the console never shows an endpoint's "public network access is disabled" note, and a private endpoint shows "can invoke" as soon as the role exists | 🔄 |

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

### Phase 3: Live, admin imports endpoints (A2 to A6) 🔄 first half done; remediation ⏳ approval

- Paste AOAI A before MOSAIC can read anything. Record "cannot read" and the exact remediation
  command, run it, and check again. Then run the subscription-scope remediation so discovery
  suggestions list the rest.
- Register the suggestions and the pasted Foundry project. A duplicate registration is rejected.
- Synced deployments must match the `az` inventory.
- Gateway runtime readiness starts at "cannot invoke" with a command. Assign the APIM identity's
  role from that command, and readiness moves to "can invoke". The private endpoint stays at
  "cannot invoke" because the gateway has no private path to it. Before G8, readiness checks only
  roles, so this expectation needs G8.
  - Until G8 is deployed, assign gateway roles only after MOSAIC can read the account, so the
    recommendation reflects the account kind.
  - For the Foundry project target, assign the role on the parent resource rather than the project.
    Readiness reports that grant as inherited, and runtime calls need it.
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

### Phase 4: Close product gaps ⏳ review and merge

- Every gap PR is reviewed and merged. The Entra fix, G1, G4 and G7 are on `main`; G2, G3, G5
  and G8 are in progress.
- Merge `main` into the e2e branch and rebuild the azd environment from live values. Run
  `azd provision --preview`, and after approval run `azd up`, because G4 changes the Entra hook.
  Then re-run the smoke specs.
- **Exit:** the deployed build contains G1 to G5, G7 and G8, and the smoke specs pass.

### Phase 5: Live, admin publishes (A7 to A9) ⬜

- Switch the gateway to manage mode (G1). Publish each target deployment and review the plan
  steps and policy facets before applying. Every step must succeed.
- Re-planning must be a no-op. Cross-check the APIM APIs, products and policies with `az`.
- Make the published models visible in the portal catalog.

### Phase 6: Live, admin sets identity and governed access (A10 to A12) 🔄 started early

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

### Phase 7: Live, end-user portal (P1 to P8, A13) 🔄 P8 done

- The `noRole` persona is denied cleanly. After getting the User role it sees an empty My access
  view and the catalog.
- That persona requests access with a justification, withdraws the request and requests again.
  The admin approves it, which creates grant intent (G2), and reviews and applies it. The persona
  then sees the grant.
- The `user` persona sees its grants, limits and connection details (G3). Primary and secondary
  key reveal is masked, transient and never cached.
- Users are isolated from each other: another user's entitlement ID returns 403 or 404.

Progress:

- ✅ P8: the admin reaches the portal with single sign-on and no second MFA prompt. The header
  shows "Admin allowed", and My access, Catalog and My requests show their empty states without
  errors.

### Phase 8: Runtime verification (R1 to R8, A14) ⬜

- Extend `scripts/verify_model_access.py` in the e2e PR:
  - Acquire tokens with MSAL: user tokens through the G4 client, workload tokens with client
    credentials.
  - Add the Anthropic Messages payload.
  - Add per-provider targets.
  - Add a tokens-per-minute 429 check.
- Call every provider by key and by token, check that every denial case is denied, prove the
  shared budget, and verify that revocation and method toggles take effect.

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
runtime journeys. **Status** is the result of the latest live run; 🔄 means part of the journey has passed.

### Admin console

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| A0 | The admin reaches the console; the model endpoints page loads without errors | 1 | ✅ |
| A1 | A User-only account signs in but sees no admin data | 1 | ⬜ |
| A2 | Discovery suggestions list the target accounts; unreadable subscriptions show a remediation command | 3 | 🔄 |
| A3 | Register from a suggestion, a pasted account ID and a pasted Foundry project ID; a duplicate is rejected | 3 | 🔄 |
| A4 | An unreadable endpoint shows "cannot read" and the exact command; after running it, MOSAIC can read it | 3 | 🔄 |
| A5 | Synced deployments match the Azure inventory | 3 | ⬜ |
| A6 | Gateway runtime readiness moves from "cannot invoke" with a command to "can invoke" | 3 | 🔄 |
| A7 | Manage mode is refused without APIM write access and allowed with it (G1) | 5 | ⬜ |
| A8 | Publish every target deployment; every plan step succeeds, and re-planning is a no-op | 5 | ⬜ |
| A9 | Catalog visibility makes a published model appear in the portal | 5 | ⬜ |
| A10 | Identity entries exist for the `user` persona and the workload; a duplicate is rejected | 6 | 🔄 |
| A11 | Governed access with keys, Entra and limits is reviewed and applied, and the applied state shows | 6 | ⬜ |
| A12 | Workload connection details and key handoff work, and the key is never logged | 6 | ⬜ |
| A13 | Approving an access request creates grant intent, which is then reviewed and applied (G2) | 7 | ⬜ |
| A14 | Disable, revoke and method toggles go through review and apply | 8 | ⬜ |
| A15 | Unpublishing removes only what MOSAIC created | 9 | ⬜ |

### Portal

| ID | Journey | Phase | Status |
| --- | --- | --- | --- |
| P0 | A User persona reaches My access and the catalog | 1 | ⬜ |
| P1 | A persona without a MOSAIC role gets a clean denial with a sign-out option | 1, 7 | ⬜ |
| P2 | With the role but no grants, My access shows its empty state and the catalog is visible | 7 | ⬜ |
| P3 | My access shows applied grants, limits and attribution | 7 | ⬜ |
| P4 | A request with a justification can be withdrawn and requested again | 7 | ⬜ |
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

## Findings from live runs

These are smaller than the gaps above. They're recorded so they can be confirmed, or fixed, once
the journeys that exercise them have run.

| ID | Observation | Next step |
| --- | --- | --- |
| O1 | Before MOSAIC can read an account, it records a placeholder endpoint (`https://<account>.cognitiveservices.azure.com`) and the provider "Azure AI Foundry", even for an Azure OpenAI account. The UI shows these as fact | After Reader is granted, confirm that **Check access** corrects the provider and endpoint. If it does, label the values as unconfirmed until the first successful read |
| O2 | A rejected duplicate registration appears under the generic title "Unable to load data". The Identity page gets this right with "Unable to add principal" | Use a title that fits a failed registration |
| O3 | The console's key reveal (`EntitlementConnectionDialog`) shows the key in an element labelled "Revealed primary key" or "Revealed secondary key", with no `data-secret` marker. The harness masks it by that label | Add `data-secret` to the revealed value when the component is next changed, so any tooling can find it |

The Phase 3 check on whether the gateway role recommendation narrows once the account kind is
known led to G8: it does narrow, and the check then rejects the broader role it recommended
before.

## Risks

- **Anthropic token limits on classic APIM.** A Developer-tier gateway can't run `llm-token-limit`
  for Anthropic. G5 decides between request-rate limits only and adding a v2-tier gateway, which
  costs more.
- **Model availability and terms.** Claude needs an eligible subscription and region, available
  quota and accepted marketplace terms. Accepting terms may need someone in the Azure portal.
- **Network reach.** A Developer-tier gateway without a virtual network can't reach private
  endpoints. That's why the private account is only a negative case.
- **Sign-in friction.** Conditional Access and MFA are handled by a person in persistent profiles.
  A guest whose home tenant requires a managed browser can switch that persona to Edge.
- **Propagation delays.** RBAC, Entra consent and APIM policy all take time to apply, so journeys
  poll with explicit timeouts instead of fixed sleeps.
- **Secret leakage.** Handled by the redaction and artifact rules above. Browser profiles hold
  refresh tokens, so they live under the user's local app data folder, never in the repository.
- **Redeploying from a new checkout.** Rebuild the azd environment from live values and confirm
  with `azd provision --preview` that it targets the existing resources before running `azd up`.

## Out of scope

MCP servers, group-based grants, analytics and chargeback, production hardening, and running the
live suite in CI (it needs interactive sign-in).
