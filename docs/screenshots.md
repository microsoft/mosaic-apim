# README screenshots

The [Screenshots](../README.md#screenshots) section of the README shows the administrator console
and the end-user portal. The images are generated, not taken by hand. `scripts/screenshots` runs
the real MOSAIC API and both apps against a fictional Contoso estate, drives them in Chromium with
Playwright, and blurs anything that looks like an endpoint, identifier, address, or secret before
it saves the PNGs to `docs/images/screenshots/`.

The demo signs in locally and uses fake Graph adapters to illustrate Entra-backed directory
workflows without a real sign-in or directory request. Directory records, permission claims,
and complete-group-read results are simulated, not evidence of live Graph connectivity.
This is a screenshot-only fixture arrangement: normal local-authentication deployments disable
Graph lookup and complete group checks.

The fictional estate also explicitly enables managed cost-center record ownership. Its linked
MOSAIC groups share the existing explicit rosters; this does not enable inherited model access,
gateway synchronization, or new keys. Normal deployments keep that experimental setting off.

## When to update them

Regenerate the affected shots, and reread their README descriptions, in the same change as any of
these:

- A pictured page changes its layout, wording, navigation, or the data it shows. The
  [inventory](#shot-inventory) lists the pictured pages.
- A new capability deserves a place in the README.
- A pictured page is renamed, moved, or removed.

Each README description is one or two sentences: what the view is, and what it lets someone see or
do. Describe what the image shows today, not what a later release will add.

## Regenerate the screenshots

You need the same tools as for [local development](../README.md#local-development), plus the
portal's packages:

```powershell
uv sync --all-packages --group dev
npm ci --prefix apps\web
npm ci --prefix apps\portal
```

Then, from the repository root:

```powershell
uv run --with "playwright>=1.58,<2" python -m scripts.screenshots.capture
```

The script starts the demo API and both apps, installs Playwright's Chromium if it is missing,
captures every shot, and stops the servers again. A full run takes several minutes; seeding,
browser readiness, and the optional navigation checks affect its duration.
Playwright is needed only here, so `uv run --with` supplies it for the run rather than adding it
to `uv.lock`. Pillow is an API dependency used for thumbnail validation and screenshot redaction.

If `uv` is available only as a Python module, use `python -m uv run ...` with the same arguments.

An existing Python environment with the API dependencies and Playwright installed also works;
this does not require the workspace's `.venv`:

```powershell
$env:PYTHONPATH = "apps\api\src;apps\api\tests"
python -m scripts.screenshots.capture
```

| Option | Effect |
| --- | --- |
| `--list` | Print each shot's name, app, theme, and route, then exit |
| `--only NAME` | Capture only that shot. Repeat it to capture several |
| `--output DIR` | Write the PNGs somewhere else, for example to compare before and after |
| `--logs-dir DIR` | Use a new dedicated project-local directory for server logs and unredacted failure captures, outside the PNG output directory. The default is an ignored `scripts\screenshots\__pycache__\capture-*` cache; a successful run removes it |
| `--headed` | Show the browser while it captures |
| `--preview` | Serve production assets after building both apps with `npm run build --prefix apps\web` and `npm run build --prefix apps\portal`; avoids cold dev-server dependency bundling |
| `--check-navigation` | Also verify all four administrator rail combinations in both themes, full width recovery, single-line controls and document reflow, every Pool section and Pricing at exactly 1440px document width with local table scrolling and `scrollIntoView`, mobile focus/inert behavior, workspace drafts and section-change discard decisions, forced colors, usage-first portal landing, portal primary/section rail width recovery, expanded/compact appearance controls, unchanged portal list geometry, and drawer/credential teardown against the fictional estate. For each captured portal theme, check all four pages at desktop/mobile widths, glass chrome, opaque panels/drawers, panel spacing, and reduced-transparency/forced-colors/unsupported-blur fallbacks |
| `--reuse-servers` | Use a demo API and apps that are already running instead of starting them |
| `--api-port`, `--console-port`, `--portal-port` | Change the ports, which default to 18000, 15173, and 15174 |

If `uv` isn't on your `PATH`, run it as `python -m uv`. On a network that can't reach PyPI's file
host, point uv at a mirror for the run and leave the lock file alone:

```powershell
$env:UV_INDEX_URL = "https://<your-mirror>/simple/"
uv run --frozen --with "playwright>=1.58,<2" python -m scripts.screenshots.capture
```

If Playwright can't download Chromium, set `PLAYWRIGHT_DOWNLOAD_HOST` to a mirror as well.

### Iterate on one shot

Starting the servers takes most of a run. To iterate on one shot, leave them running in three
terminals:

```powershell
uv run python -m scripts.screenshots.demo_api --port 18000 --console-port 15173 --portal-port 15174
```

```powershell
Set-Location apps\web
npm run dev -- --port 15173 --strictPort --host 127.0.0.1
```

```powershell
Set-Location apps\portal
npm run dev -- --port 15174 --strictPort --host 127.0.0.1
```

Then capture against them, as often as you like:

```powershell
uv run --with "playwright>=1.58,<2" python -m scripts.screenshots.capture --reuse-servers --only console-models --headed
```

The demo API keeps its state in memory. If a shot's actions submit anything, restart the API to
get the original estate back. Before committing, refresh every affected shot without
`--reuse-servers`; changes to shared navigation require the full gallery.

### Browse the demo estate

To find a view worth capturing, run the demo API on its default ports and start each app with
`npm run dev`:

```powershell
uv run python -m scripts.screenshots.demo_api
```

The console at `http://localhost:5173` signs you in as the demo administrator, Adele Vance. The
portal at `http://localhost:5174` signs you in as the demo end user, Megan Bowen.

## How it works

- `demo_fakes.py` is the fictional Azure side. It extends the API's test doubles in
  `apps/api/tests` into a Contoso estate: a production gateway MOSAIC can read, a development copy
  of it whose APIs still point at the production accounts, a partner gateway that answers `403` so
  it shows **Access needed**, Azure OpenAI and Foundry accounts, two more Foundry accounts in other
  regions that host Claude, a Key Vault that MOSAIC keeps a partner's Foundry key in and the
  gateways can read it from, and MCP servers. MOSAIC reaches them through `httpx` mock transports,
  so nothing leaves the machine. A fake Log Analytics workspace answers MOSAIC's usage queries from
  the gateways' generated logs, a day at a time.
- The same estate includes a fictional Government management connection in a separate fake
  credentials vault, a Government gateway and Azure OpenAI backend, and Government GPT-4o mini
  and MCP offerings under the same Customer Insights cost center. Both cloud labels and environment
  classification come from real resource contracts; all Government authority/ARM traffic remains
  on mock transports. This illustrates the UI, not live Government service availability.
- `demo_api.py` runs the production FastAPI app with in-memory repositories and swaps its
  Azure-facing services for the fakes. It then seeds the estate through MOSAIC's own services, in
  the order an operator would: add a custom Partner environment, create cost centers with their
  owners, members, per-person limits, and pooled quotas, onboard people and workloads with their
  default cost centers, register, classify, and sync gateways, register a partner's Foundry
  resource by URL with a fictional API key, which MOSAIC stores in the Key Vault double, and
  declare its deployments, then import, publish, grant under cost centers, and request, approve,
  and deny access. It builds two model pools on the production gateway: **Anthropic Claude**, a
  published breaker pool that serves Claude Opus 4.5 from three regions and Claude Sonnet 4.5 from
  two, and **Claude evaluation**, a hidden linear draft. Both use the Sonnet deployments, so each
  warns that it shares their capacity with the other. Claude Haiku 4.5 is deployed in two of those
  regions but served by no pool, so the **Pools** page suggests one for it. Anthropic Claude is
  then governed: Megan Bowen holds both its models under Customer Insights, the Market Research
  Agent holds Opus, and the Agent Builders group holds Sonnet. Customer Insights sets a per-person
  Sonnet limit and a pooled quota that counts every region, and Isaiah Langer's request for Opus is
  pending. Commercial grants' subscription keys exist only where someone asked for one: the support copilot's,
  the one Megan Bowen's notebook uses, and the one she calls both Claude models with. Eligible
  Government model and MCP grants receive their initial keys automatically after reviewed apply.
  One MCP
  server keeps only a legacy label, so Settings has something to classify, and the development
  gateway's routes to production produce environment findings. Grants and decided requests are then
  dated back as far as 120 days. Last, it enables API diagnostics on the production gateway and
  generates a quarter of gateway traffic from the estate's people and workloads, including
  throttled and refused calls and calls to adopted APIs. Each call to the Claude pool lands on one
  of its deployments, drawn by weight, and Johanna Lorenz's calls to it are refused because she
  holds no grant. Pool calls also leave the attempt traces the pool's policy writes. North Central
  US runs short of Opus capacity, so some Opus calls are throttled there and retried in another
  region, which the pool's **Health** card shows. The published Docs Search MCP server calls GPT-4o
  mini as its own managed identity, so many of its tool calls make model calls that pass on the
  gateway's reference, and a few pass on one MOSAIC can't use. MOSAIC's rollup job reads it all
  back, so the Dashboard, Analytics, and the portal's **Usage & cost** page show measured figures.
  They are priced from the price list MOSAIC ships, plus what the seed sets through the pricing
  service: the partner's declared GPT-4.1 mini deployment's type, a negotiated GPT-4o rate from
  next month, which the **Pricing** page shows as scheduled, a fictional Government-only contract
  rate, and what Contoso pays for Claude Opus 4.5, Claude Sonnet 4.5, and Claude Haiku 4.5, which
  prices each pool call at the deployment that
  served it. The partner's Claude, Mistral Large, Cohere, and realtime deployments and the adopted
  APIs stay unpriced, so the **Unpriced deployments** tab has something to show. Every page
  therefore renders what the product would show for that estate. Budget email is turned on against
  a Graph email double that accepts every message and delivers none, and the seed sets
  budgets from this month's spend so far: Customer Insights near its limit, Customer Support past
  its own and blocked at the gateways, Finance on track, and an organization budget that only
  warns. Sizing them from the month's spend keeps those levels whatever the date, so in a capture's
  first UTC day the amounts are only a few hours' worth. The demo runs no rollup or budget loop
  afterwards, so every shot in a run shows the same figures. Approval evaluation and notification
  workers are also disabled; the seed explicitly runs notification processing against local provider
  and directory doubles, which accept fictional messages but deliver none. A disabled development
  office-hours policy exercises environment scope, local-time windows, waiting periods, and bounded
  limits without approving requests. The administrator and portal user have independent example
  preferences, including daily, weekly, and monthly email summaries and immediate private Teams.
  Shared and private Teams use the production services over local token, vault and HTTP doubles.
  A locally signed Connector lifecycle binds each fictional person's personal installation;
  explicit synthetic tests persist accepted results without reaching Microsoft. One shared
  Workflow deliberately has denied vault access, so the gallery shows actionable diagnostics.
  The AI adapter is a deterministic local double, never a paid model call, and readiness honestly
  reports local memory, disabled workers, and missing deployment prerequisites.
  The traffic and timestamps follow the clock, though, so they shift a little from one run to the
  next. Requests from the portal's origin
  are answered as Megan Bowen with the `User` role, and all others as Adele Vance with `Admin` and
  `User`. Local authentication is refused outside local and test environments, so the demo can't be
  pointed at a deployed MOSAIC.
- `capture.py` opens each shot in a fresh Chromium context 1440 pixels wide, with the shot's theme
  set both as the operating-system preference and as MOSAIC's stored preference. It waits for
  every spinner to clear and for the shot's ready text, runs the shot's actions, captures the
  viewport, and blurs every sensitive region before it saves the PNG. Administrator shots fail
  rather than save clipped content if the document overflows horizontally. Dates are rendered in UTC,
  and they show the day of the capture.
- Grant-selection actions use the dropdown's full cloud label (for example, **Azure Government**,
  not the **AzGov** badge), exact cost center, direct attribution, and either **Ready** or
  **Applied to APIM** status. This keeps mixed-cloud grants from selecting the wrong offering.

## Redaction

Before each capture, the script checks every visible text node, form value, and selected option
against these rules. Anything that matches is pixelated and then blurred, so no letter shape
survives to be read back.

- `SENSITIVE_LITERALS` in `demo_api.py`: the demo tenant, client, and object IDs.
- `RESOURCE_NAMES` in `capture.py`: the demo's gateway, resource group, account, vault, MCP, and
  local-group names, including their hyphenated aliases wherever they appear in an API path.
- The shared MCP metadata API, readable MOSAIC-owned APIM resource names and account-derived
  API paths, product names, and fictional person/workload/gateway names and directory aliases are
  redacted too.
- Exact offering names in `.resource-offering` text are blurred, independently of literal matches.
- Deployment account names and identifiers marked by `data-deployment-account` and
  `data-deployment-identifier` are blurred as complete regions, including reviewed model sources.
- `REDACTION_PATTERNS` in `capture.py`: GUIDs, email addresses, URLs and URIs, Azure resource IDs,
  Azure, Graph, Workflows, Bot Connector and Contoso hostnames, IPv4 addresses, MOSAIC record IDs and the
  APIM names derived from them, and the demo's placeholder keys.

Fictional people, workload identities, directory results, and gateway/account display names are
also blurred. Public model names, API paths, deployment names, limits, and dates stay readable
where they do not contain an endpoint, account name, or identifier.

People avatars, including profile images and initials, are blurred as whole regions. Demo photos
are synthetic fixtures; the capture never loads a real directory image.

Runtime configuration also blurs the administrator context, tenant ID, and API base URL values
by their field labels, even when the demo shows generic placeholders rather than GUIDs.

Redaction reads the page's text. It can't see text drawn in an image or a canvas. If a shot shows
something identifying that isn't text, or the rules miss something, add a literal or a pattern
rather than editing the image by hand. Then capture the shot again and check it.

## Add, change, or remove a shot

1. Find the view in the demo estate. If the estate lacks the data the view needs, seed it in
   `seed_estate` in `demo_api.py` through MOSAIC's services. Data that comes from Azure belongs in
   `demo_fakes.py`.
2. Add a `Shot` to `SHOTS` in `capture.py`.
   - `name` is the PNG's file name, such as `console-models`. A light and dark pair of one view
     ends in `-light` and `-dark`.
   - `app` is `"console"` or `"portal"`.
   - `path` is the route to open, including any query string.
   - `theme` is `"light"` or `"dark"`.
   - `ready` is visible text that appears only once the page's data has loaded. A heading that
     shows while the page is still loading doesn't work.
   - `actions` are the steps that reach the pictured state. Build them from `click_link`,
     `click_tab`, `click_button`, `select_option`, `fill_text`, `wait_for_text`, and
     `scroll_to_text`.
   - `height` and `width` are the viewport dimensions in pixels, defaulting to 900 and 1440.
3. Capture it with `--only`, open the PNG, and check that everything identifying is blurred.
4. Embed it in the README with a one- or two-sentence description, and add it to the
   [inventory](#shot-inventory).

To remove a shot, delete its `Shot`, its PNG, its README entry, and its inventory row. After a run,
the script warns about any PNG in the output folder that no `Shot` produces.

The README's **Light and dark** table shows one view in both themes, side by side. The galleries
are two-column tables with light shots on the left and dark ones on the right, so each gallery
shows both themes. Keep new shots in that pattern.

## Troubleshooting

- **A port is in use.** Another run or dev server is still up. Stop it, or choose other ports.
- **The API exits early.** Seeding failed, usually because an API service changed, or one of the
  test doubles in `apps/api/tests` that `demo_fakes.py` builds on did. The API's log, in the
  project-local cache the script prints, has the traceback. Fix the demo in the same change.
- **A shot fails.** The script carries on with the other shots and exits with an error at the end.
  For each failure it prints the browser's errors and failed requests, and the path of a
  full-page `<name>.failed.png`. Most failures are ready text or an action target that no longer
  appears on the page, so update the shot to match.
- **Debug captures aren't redacted.** They stay in the ignored project-local cache and must never
  be copied into the documentation or committed. The cache and its server logs are deleted after
  a successful run and kept after a failed one.

## Shot inventory

The portal shots use the console's cool light/dark palette and glass navigation, with opaque
resource lists, forms, and connection drawers. The Usage & cost overview uses a taller viewport
to include both the daily trend and the complete last-24-hours chart.

| Shot | App | Route and state | Theme | README entry |
| --- | --- | --- | --- | --- |
| `console-dashboard-light`, `console-dashboard-dark` | Console | `/dashboard` | Both | Light and dark: console overview |
| `portal-catalog-light`, `portal-catalog-dark` | Portal | `/catalog`, full-width model-grouped rows with filters, blue-tinted glass navigation, and appearance controls | Both | Light and dark: portal catalog |
| `console-gateways` | Console | `/gateways`, compact list with selected gateway overview | Light | Gateways |
| `console-government-gateway` | Console | `/gateways`, fictional Government gateway selected with AzGov and Production context | Light | Government gateway |
| `console-gateway-overview` | Console | Contoso AI Gateway, **Overview** section | Dark | Gateway overview |
| `console-gateway-apis` | Console | Contoso AI Gateway, **APIs and endpoints** section | Light | APIs and endpoints |
| `console-models` | Console | `/models`, compact list with selected publication detail | Dark | Models |
| `console-register-key-endpoint` | Console | `/models?register=1`, **Azure AI with an API key** tab with a pasted, masked key, filled in and never submitted | Light | Register with an API key |
| `console-key-endpoint` | Console | `/models?tab=endpoints`, Fabrikam partner Foundry selected, scrolled to its **Access** card with **Replace API key** | Dark | Endpoint reached with an API key |
| `console-deploy-model` | Console | `/models?tab=endpoints`, **Deploy model** on the fictional Azure OpenAI account; explicit deployment-management opt-in, fresh version/SKU/quota configuration, no Azure create | Light | Deploy an Azure model |
| `console-deploy-model-review` | Console | The same workflow's **Review deployment** step with exact target, native capacity, quota, billing/residency warnings, and unsubmitted **Deploy to Azure** action | Dark | Review before Azure creation |
| `console-pools` | Console | `/pools`, compact list with Anthropic Claude selected, **Overview** detail and separate Suggestions navigation | Light | Model pools |
| `console-pool-detail` | Console | Anthropic Claude detail, **Models and deployments** section with both navigation rails compact | Dark | A model pool |
| `console-pool-access` | Console | Anthropic Claude detail, **Access and connection** section with both navigation rails compact | Light | Who can call a pool |
| `console-pool-health` | Console | Anthropic Claude detail, **Health** section for the last 24 hours with both navigation rails compact | Dark | Pool health |
| `console-pool-editor` | Console | `/pools?tab=suggestions`, **Create pool** on the Claude Haiku 4.5 suggestion, **3. Models** step, never saved | Light | Create a pool |
| `console-endpoint-pools` | Console | `/models?tab=endpoints`, Contoso AI Foundry selected, scrolled to **Used by pools** dependencies | Dark | Pools on an endpoint |
| `console-mcps` | Console | `/mcps`, selected published server with its model-calling identity | Light | MCP servers |
| `console-identity` | Console | `/identity?tab=agents`, selected agent detail beside the list | Dark | Identity |
| `console-people-search` | Console | `/identity?tab=users`, **Add person**, selected result, never added | Light | Find a person |
| `console-identity-detail` | Console | `/identity?tab=agents`, selected agent overview workspace | Dark | Identity details |
| `console-directory-picker` | Console | `/identity?tab=agents`, **Add agent** workspace with query `agent`, selected result, never added | Light | Directory picker |
| `console-security-group-members` | Console | `/identity?tab=security-groups`, **AI Model Users** selected, scrolled to read-only **Security group members** | Dark | Security group members |
| `console-directory-verification` | Console | `/identity?tab=security-groups`, **AI Model Users** selected, explicit complete-read check against the fictional directory, scrolled to its scoped diagnostic status | Light | Complete group-read verification |
| `console-entitlements` | Console | `/entitlements`, selected grant with detailed state and limits | Light | Entitlements |
| `console-access-requests` | Console | `/entitlements?tab=requests`, pending queue with the first request selected in **Selected access request** detail | Light | Access requests |
| `console-model-access-settings` | Console | `/entitlements?tab=settings`, GPT-4o selected | Dark | Model access settings |
| `console-overlapping-grants` | Console | `/entitlements?tab=effective`, **Overlapping grants** | Dark | Overlapping grants |
| `console-cost-centers` | Console | `/cost-centers`, selected detail beside the compact list | Light | Cost centers |
| `console-new-cost-center` | Console | `/cost-centers`, **New cost center** workspace, never saved | Dark | New cost center |
| `console-cost-center-detail` | Console | Customer Insights detail, **Members** section | Dark | Cost center members |
| `console-cost-center-group` | Console | `/identity?tab=groups`, managed Customer Insights group selected, explicit roster and owning-center navigation | Light | Managed cost-center group |
| `console-mcp-publish` | Console | `/mcps`, full-workspace **Review changes** for the published Docs MCP server, intended changes and impact with collapsed execution, never applied | Light | MCP publish review |
| `console-model-change-review` | Console | `/entitlements?tab=settings`, GPT-4o key setting saved but never applied, current edit separated from an earlier pending grant | Light | Model change review |
| `console-unpublish-review` | Console | `/models`, GPT-4o selected, detail **More actions > Unpublish**, recorded removals, impact and callers losing access, never confirmed | Light | Unpublish review |
| `console-budgets` | Console | Customer Support detail, **Budget** section | Dark | Budgets |
| `console-analytics` | Console | `/analytics`, **Overview** section | Dark | Analytics |
| `console-analytics-filters` | Console | `/analytics`, grouped draft **Filters** open, never applied | Light | Analytics filters |
| `console-request-notifications` | Console | `/dashboard`, pending-approvals popover open, separate from unread inbox bell | Light | Pending approvals |
| `console-bulk-approval` | Console | `/entitlements?tab=requests`, two explicit pending requests in **Review bulk approvals**, never submitted | Dark | Bulk approval review |
| `console-approval-policy` | Console | `/entitlements?tab=approvalPolicies`, disabled **Development office hours** policy in the full-workspace editor, never saved or enabled | Light | Scheduled approval policy |
| `console-notification-inbox` | Console | `/notifications`, category/unread filters and selected exact notification | Dark | Administrator inbox |
| `console-notification-preferences` | Console | `/profile`, **Edit preferences** workspace with independent administrator channel/cadence and digest controls, never saved | Light | Administrator preferences |
| `console-notification-readiness` | Console | `/settings?tab=notifications&notificationView=health`, evidence and remediation for storage, hosting, email, public links, and disabled workers | Dark | Notification readiness |
| `console-analytics-cost` | Console | `/analytics?tab=cost` | Light | Cost |
| `console-pricing` | Console | `/pricing`, **Prices** section filtered to `2024-11-20` | Dark | Pricing |
| `console-analytics-consumers` | Console | `/analytics?tab=consumers` | Light | Consumers |
| `console-analytics-mcp-model-use` | Console | `/analytics?tab=consumers`, scrolled to **Model use through MCP servers** | Dark | Model use through MCP servers |
| `console-analytics-limits` | Console | `/analytics?tab=limits` | Dark | Grant limits |
| `console-gateway-telemetry` | Console | Contoso AI Gateway, **Overview** section, scrolled to **Telemetry** | Light | Gateway telemetry |
| `console-analytics-reliability` | Console | `/analytics?tab=reliability` | Dark | Reliability |
| `console-environments` | Console | `/settings` | Light | Environments |
| `console-azure-connections` | Console | `/settings?tab=connections`, commercial host identity and fictional Government service principal | Dark | Azure connections |
| `console-environment-findings` | Console | `/settings?tab=findings` | Dark | Environment findings |
| `console-runtime-configuration` | Console | `/settings?tab=runtime`, read-only deployment details, Entra identity and app-registration references, directory availability, granted/missing Graph consent with role IDs, and separate complete-group-read status; local demo authentication with a fictional directory double | Light | Runtime configuration |
| `console-email-settings` | Console | `/settings?tab=notifications&notificationView=email`, saved Graph mailbox, read-only access checks and last explicit test | Light | Email setup |
| `console-teams-shared` | Console | `/settings?tab=notifications&notificationView=teams`, selected fictional Workflow with denied vault access, correction guidance and saved test | Light | Shared Teams access |
| `console-teams-private` | Console | `/settings?tab=notifications&notificationView=personalTeams`, saved bot registration, caller-scoped runtime access check, installation, opt-in and test | Dark | Private Teams access |
| `portal-request-access` | Portal | `/catalog`, GPT-4o mini's exact Commercial offering selected in the access drawer, never submitted | Light | Request access |
| `portal-access` | Portal | `/access`, grouped model rows and an exact grant's **Connection details** open in a drawer | Light | My access |
| `portal-connection-cost-center` | Portal | `/access`, the GPT-4o grant under General with **Connection details** open, scrolled to **Cost center header** | Light | Choosing a cost center |
| `portal-pool-connection` | Portal | `/access`, Claude Sonnet 4.5 selected, **Connection details** drawer scrolled to **Authentication** and the shared-key note | Dark | Models that share a key |
| `portal-mcp-connection` | Portal | `/access`, with an enforced MCP grant's **Connection details** open | Dark | MCP connection |
| `portal-government-connection` | Portal | `/access`, exact AzGov GPT-4o mini direct grant under Customer Insights, **Connection details** open | Light | Government key access |
| `portal-government-mcp-connection` | Portal | `/access`, exact AzGov MCP direct grant under Customer Insights, custom-header **Connection details** open | Dark | Government MCP connection |
| `portal-requests` | Portal | `/requests`, model-first cards with offering and version context | Dark | My requests |
| `portal-usage` | Portal | `/usage`, the default portal landing page with icon-based, collapsible section navigation | Light | Usage & cost |
| `portal-usage-resources` | Portal | `/usage?tab=resources`, **By resource** with exact offering context | Dark | Usage by resource |
| `portal-usage-breakdowns` | Portal | `/usage?tab=breakdowns`, **Cost centers** and pooled quotas | Light | Usage breakdowns |
| `portal-budget-banner` | Portal | `/access`, with the budget banner for Customer Insights | Light | Budget banner |
| `portal-notifications` | Portal | `/notifications`, personal request/usage updates, category/unread filters and exact links | Light | Personal inbox |
| `portal-notification-preferences` | Portal | `/notification-preferences`, per-category channels/cadence; example saved preferences, never submitted during capture | Dark | Personal notification preferences |
| `portal-notification-digests` | Portal | `/notification-preferences`, scrolled to daily/weekly/monthly schedules, quiet hours and threshold reminder controls | Light | Usage summaries and alert timing |
