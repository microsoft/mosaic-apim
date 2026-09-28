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
    match with `--nth <n>` (`-1` is the last), and add `--exact` for an exact name.
- **Values:** `@target:<dot.path>` reads a value from the manifest, so identifiers don't end up in
  shell history. `fill` never echoes the value.
- **Sign-in:** `signin` waits for a person to finish MFA; the default timeout is 10 minutes.
- **Logs:** `logs` returns redacted console errors, page errors, dialogs, API responses at 400 or
  above, and failed requests. Dialogs are dismissed unless you run `dialogs accept`.
- **Navigation:** the driver only goes to the web and portal origins in the manifest.

The driver listens on `127.0.0.1` on a random port. It requires the per-run token from `live.json`
and rejects any request that carries browser `Origin` or `Sec-Fetch-Site` headers, so web pages
can't drive it.

## Run the suite

Stop the live driver first (`node tools/drive.ts shutdown`) so the suite can open the profiles:

```powershell
npm test                                   # every spec; journeys that write or call models skip themselves
npm test -- --grep "@smoke"                # or pick journeys by title
$env:MOSAIC_E2E_ALLOW_WRITES = '1'; npm test
npx playwright show-report                 # open the HTML report afterwards
```

| Variable | Effect |
| --- | --- |
| `MOSAIC_E2E_INTERACTIVE=1` | Wait for a person when sign-in needs a password or MFA, instead of failing |
| `MOSAIC_E2E_ALLOW_WRITES=1` | Run journeys that change MOSAIC or Azure state. Otherwise they're skipped |
| `MOSAIC_E2E_SEND_MODEL_REQUESTS=1` | Run runtime journeys that send billable model requests |
| `MOSAIC_E2E_HEADLESS=1` | Run headless. This only works once every profile is already signed in |
| `MOSAIC_E2E_BROWSER_CHANNEL` | Default browser channel for personas that don't set one |
| `MOSAIC_E2E_TARGETS` | Path to a manifest other than `targets.local.json` |
| `MOSAIC_E2E_STATE_DIR`, `MOSAIC_E2E_ARTIFACTS_DIR` | Move profiles, driver state or artifacts |

Specs run serially with a single worker, because journeys build on each other and share
profiles. Traces and video are off. On failure, the suite attaches a screenshot with secrets
masked.

## Secret hygiene

- Never commit `targets.local.json`, profiles, artifacts or reports. `e2e/.gitignore` covers them.
- Key reveal UIs must mark revealed values with `data-secret`, which keeps them out of snapshots,
  text reads and screenshots.
- Redaction removes:
  - JWTs, `Bearer` values, subscription and API key headers, and cookies.
  - OAuth `code`, `state` and `sig` parameters, and URL fragments.
  - Connection-string keys, Entra client secrets, and anything that looks like an APIM or
    Cognitive Services key.
- Runtime verification keeps keys and tokens in process memory and prints only status codes.
- To reset a persona, stop the driver and delete `profiles\<persona>`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `needs a password, MFA, or consent` | Run `npm run login -- <persona>`, or set `MOSAIC_E2E_INTERACTIVE=1` |
| The profile is already in use | Stop the live driver or the other Playwright run using it |
| `AADSTS53003` or another Conditional Access code | Use `"channel": "msedge"` for that persona, or sign in on a compliant device |
| `<app> is signed in as X, not persona Y` | Delete that persona's profile and sign in again as the right account |
| `The live driver is not running` | Run `npm run live` |
| `Could not reach the live driver` | The driver stopped without cleaning up. Delete the stale `live.json` and restart it |

## Quality checks

```powershell
npm run test:unit
npm run typecheck
npm run lint
```
