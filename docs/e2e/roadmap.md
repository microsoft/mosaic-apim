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
  - Snapshots, logs and errors are redacted.
  - Screenshots mask `[data-secret]` elements and password fields.
  - Playwright traces and video are off.
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
| Entra fix | `main` reused one value for the runtime app role and the delegated scope. Entra rejects that, so a fresh `azd up` can't bootstrap the runtime registration | Give the application role and the delegated scope distinct values ([#15](https://github.com/microsoft/mosaic-apim/pull/15)) | ⏳ review |
| G1 | The console has no control to switch a gateway into `manage` mode, which publishing requires | Add a "Management mode" control with an explicit confirmation | 🔄 |
| G2 | Approving an access request creates no grant, even though the banner says it saved grant intent | Approve opens a short dialog with limits prefilled; the server creates and links the grant intent in one step | 🔄 |
| G3 | The portal can't show connection details or reveal keys, so an end user can't get a credential | Add portal connection details and masked, transient key reveal | 🔄 |
| G4 | No client registration lets an end user get a `Models.Invoke` token | Optional public client registration with consent, and its client ID in connection details. Stacked on the Entra fix | 🔄 |
| G5 | Anthropic deployments get the chat-completions API shape, but Claude needs the Messages API. `llm-token-limit` supports Anthropic only on APIM v2 tiers | Publish Anthropic with the Messages shape, and decide how to limit tokens on classic tiers | 🔄 |
| G6 | Only if Grok or Llama fail on `/models/chat/completions` through APIM | Add OpenAI v1 routes | ⬜ conditional |

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
  sessions, a sign-in helper, redaction, and read-only smoke specs (S1, A0, A1, P0, P1).
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

### Phase 3: Live, admin imports endpoints (A2 to A6) ⬜

- Paste AOAI A before MOSAIC can read anything. Record "cannot read" and the exact remediation
  command, run it, and check again. Then run the subscription-scope remediation so discovery
  suggestions list the rest.
- Register the suggestions and the pasted Foundry project. A duplicate registration is rejected.
- Synced deployments must match the `az` inventory.
- Gateway runtime readiness starts at "cannot invoke" with a command. Assign the APIM identity's
  role from that command, and readiness moves to "can invoke". The private endpoint stays at
  "cannot invoke".
- **Exit:** every target is registered, readable and invocable, except the negative case.

### Phase 4: Close product gaps ⏳ review and merge

- Every gap PR is reviewed and merged. G4 merges after the Entra fix.
- Merge `main` into the e2e branch and rebuild the azd environment from live values. Run
  `azd provision --preview`, and after approval run `azd up`, because G4 changes the Entra hook.
  Then re-run the smoke specs.
- **Exit:** the deployed build contains G1 to G5, and the smoke specs pass.

### Phase 5: Live, admin publishes (A7 to A9) ⬜

- Switch the gateway to manage mode (G1). Publish each target deployment and review the plan
  steps and policy facets before applying. Every step must succeed.
- Re-planning must be a no-op. Cross-check the APIM APIs, products and policies with `az`.
- Make the published models visible in the portal catalog.

### Phase 6: Live, admin sets identity and governed access (A10 to A12) ⬜

- Create identity entries for the `user` and `noRole` personas and the workload service principal.
- Enable key and Entra access with limits, grant the `user` persona and the workload, and run the
  model-wide review and apply.
- Get the workload's connection details and key handoff from the console. The key never appears
  in logs.

### Phase 7: Live, end-user portal (P1 to P8, A13) ⬜

- The `noRole` persona is denied cleanly. After getting the User role it sees an empty My access
  view and the catalog.
- That persona requests access with a justification, withdraws the request and requests again.
  The admin approves it, which creates grant intent (G2), and reviews and applies it. The persona
  then sees the grant.
- The `user` persona sees its grants, limits and connection details (G3). Primary and secondary
  key reveal is masked, transient and never cached.
- Users are isolated from each other: another user's entitlement ID returns 403 or 404.

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
runtime journeys.

### Admin console

| ID | Journey | Phase |
| --- | --- | --- |
| A0 | The admin reaches the console; the model endpoints page loads without errors | 1 |
| A1 | A User-only account signs in but sees no admin data | 1 |
| A2 | Discovery suggestions list the target accounts; unreadable subscriptions show a remediation command | 3 |
| A3 | Register from a suggestion, a pasted account ID and a pasted Foundry project ID; a duplicate is rejected | 3 |
| A4 | An unreadable endpoint shows "cannot read" and the exact command; after running it, MOSAIC can read it | 3 |
| A5 | Synced deployments match the Azure inventory | 3 |
| A6 | Gateway runtime readiness moves from "cannot invoke" with a command to "can invoke" | 3 |
| A7 | Manage mode is refused without APIM write access and allowed with it (G1) | 5 |
| A8 | Publish every target deployment; every plan step succeeds, and re-planning is a no-op | 5 |
| A9 | Catalog visibility makes a published model appear in the portal | 5 |
| A10 | Identity entries exist for both member personas and the workload | 6 |
| A11 | Governed access with keys, Entra and limits is reviewed and applied, and the applied state shows | 6 |
| A12 | Workload connection details and key handoff work, and the key is never logged | 6 |
| A13 | Approving an access request creates grant intent, which is then reviewed and applied (G2) | 7 |
| A14 | Disable, revoke and method toggles go through review and apply | 8 |
| A15 | Unpublishing removes only what MOSAIC created | 9 |

### Portal

| ID | Journey | Phase |
| --- | --- | --- |
| P0 | A User persona reaches My access and the catalog | 1 |
| P1 | A persona without a MOSAIC role gets a clean denial with a sign-out option | 1, 7 |
| P2 | With the role but no grants, My access shows its empty state and the catalog is visible | 7 |
| P3 | My access shows applied grants, limits and attribution | 7 |
| P4 | A request with a justification can be withdrawn and requested again | 7 |
| P5 | After approval and apply, the requester sees the grant | 7 |
| P6 | Connection details appear, and key reveal is masked, transient and uncached (G3) | 7 |
| P7 | Another user's entitlement ID returns 403 or 404 | 7 |
| P8 | The admin shows as allowed in the portal | 7 |

### Runtime (real calls through APIM)

| ID | Journey | Phase |
| --- | --- | --- |
| R1 | A granted user reaches every provider by key: Azure OpenAI, Foundry OpenAI, Grok, Llama, DeepSeek and Claude (after G5) | 8 |
| R2 | A granted user's Entra token works (G4); a token without a grant and a wrong-audience token are denied | 8 |
| R3 | The workload's client-credentials token and its handed-off key both work | 8 |
| R4 | Anonymous, invalid-key and cross-subject calls are denied | 8 |
| R5 | A shared budget of 2 calls per 300 seconds, spent by primary key and token, returns 429 for the secondary key | 8 |
| R6 | The tokens-per-minute limit returns 429 with `Retry-After` | 8 |
| R7 | After revocation propagates, calls fail | 8 |
| R8 | Calls show up in Application Insights and Log Analytics (optional) | 8 |

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
