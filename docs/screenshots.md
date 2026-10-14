# README screenshots

The [Screenshots](../README.md#screenshots) section of the README shows the administrator console
and the end-user portal. The images are generated, not taken by hand. `scripts/screenshots` runs
the real MOSAIC API and both apps against a fictional Contoso estate, drives them in Chromium with
Playwright, and blurs anything that looks like an endpoint, identifier, address, or secret before
it saves the PNGs to `docs/images/screenshots/`.

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
uv run --with "playwright>=1.58,<2" --with "pillow>=11,<12" python -m scripts.screenshots.capture
```

The script starts the demo API and both apps, installs Playwright's Chromium if it is missing,
captures every shot, and stops the servers again. A full run takes about a minute and a half.
Playwright and Pillow are needed only here, so `uv run --with` supplies them for the run rather
than adding them to `uv.lock`.

If `uv` is available only as a Python module, use `python -m uv run ...` with the same arguments.

| Option | Effect |
| --- | --- |
| `--list` | Print each shot's name, app, theme, and route, then exit |
| `--only NAME` | Capture only that shot. Repeat it to capture several |
| `--output DIR` | Write the PNGs somewhere else, for example to compare before and after |
| `--headed` | Show the browser while it captures |
| `--preview` | Serve production assets after building both apps with `npm run build --prefix apps\web` and `npm run build --prefix apps\portal`; avoids cold dev-server dependency bundling |
| `--check-navigation` | Also verify desktop collapse, mobile focus/inert behavior, usage-first landing, unchanged list geometry, and drawer/credential teardown against the fictional estate |
| `--reuse-servers` | Use a demo API and apps that are already running instead of starting them |
| `--api-port`, `--console-port`, `--portal-port` | Change the ports, which default to 18000, 15173, and 15174 |

If `uv` isn't on your `PATH`, run it as `python -m uv`. On a network that can't reach PyPI's file
host, point uv at a mirror for the run and leave the lock file alone:

```powershell
$env:UV_INDEX_URL = "https://<your-mirror>/simple/"
uv run --frozen --with "playwright>=1.58,<2" --with "pillow>=11,<12" python -m scripts.screenshots.capture
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
uv run --with "playwright>=1.58,<2" --with "pillow>=11,<12" python -m scripts.screenshots.capture --reuse-servers --only console-models --headed
```

The demo API keeps its state in memory. If a shot's actions submit anything, restart the API to
get the original estate back, and commit only images from a full run without `--reuse-servers`.

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
  pending. Grants' subscription keys exist only where someone asked for one: the support copilot's,
  the one Megan Bowen's notebook uses, and the one she calls both Claude models with. One MCP
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
  next month, which the **Pricing** page shows as scheduled, and what Contoso pays for Claude Opus
  4.5, Claude Sonnet 4.5, and Claude Haiku 4.5, which prices each pool call at the deployment that
  served it. The partner's Claude, Mistral Large, Cohere, and realtime deployments and the adopted
  APIs stay unpriced, so the **Unpriced deployments** tab has something to show. Every page
  therefore renders what the product would show for that estate. Budget email is turned on against
  a Communication Services double that accepts every message and delivers none, and the seed sets
  budgets from this month's spend so far: Customer Insights near its limit, Customer Support past
  its own and blocked at the gateways, Finance on track, and an organization budget that only
  warns. Sizing them from the month's spend keeps those levels whatever the date, so in a capture's
  first UTC day the amounts are only a few hours' worth. The demo runs no rollup or budget loop
  afterwards, so every shot in a run shows the same figures. The traffic and timestamps follow the
  clock, though, so they shift a little from one run to the next. Requests from the portal's origin
  are answered as Megan Bowen with the `User` role, and all others as Adele Vance with `Admin` and
  `User`. Local authentication is refused outside local and test environments, so the demo can't be
  pointed at a deployed MOSAIC.
- `capture.py` opens each shot in a fresh Chromium context 1440 pixels wide, with the shot's theme
  set both as the operating-system preference and as MOSAIC's stored preference. It waits for
  every spinner to clear and for the shot's ready text, runs the shot's actions, captures the
  viewport, and blurs every sensitive region before it saves the PNG. Dates are rendered in UTC,
  and they show the day of the capture.

## Redaction

Before each capture, the script checks every visible text node, form value, and selected option
against these rules. Anything that matches is pixelated and then blurred, so no letter shape
survives to be read back.

- `SENSITIVE_LITERALS` in `demo_api.py`: the demo tenant, client, and object IDs.
- `RESOURCE_NAMES` in `capture.py`: the demo's gateway, resource group, account, and vault names.
- The shared MCP metadata API, readable MOSAIC-owned APIM resource names, product names, and
  fictional person/workload/gateway names and directory aliases are redacted too.
- Exact offering names in `.resource-offering` text are blurred, independently of literal matches.
- `REDACTION_PATTERNS` in `capture.py`: GUIDs, email addresses, URLs and URIs, Azure resource IDs,
  Azure, Communication Services, and Contoso hostnames, IPv4 addresses, MOSAIC record IDs and the
  APIM names derived from them, and the demo's placeholder keys.

Fictional people, workload identities, directory results, and gateway/account display names are
also blurred. Public model names, API paths, deployment names, limits, and dates stay readable
where they do not contain an endpoint, account name, or identifier.

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
  temporary folder the script prints, has the traceback. Fix the demo in the same change.
- **A shot fails.** The script carries on with the other shots and exits with an error at the end.
  For each failure it prints the browser's errors and failed requests, and the path of a
  full-page `<name>.failed.png`. Most failures are ready text or an action target that no longer
  appears on the page, so update the shot to match.
- **Debug captures aren't redacted.** They stay in the temporary folder and must never be copied
  into the repository. The folder and its server logs are deleted after a successful run and kept
  after a failed one.

## Shot inventory

| Shot | App | Route and state | Theme | README entry |
| --- | --- | --- | --- | --- |
| `console-dashboard-light`, `console-dashboard-dark` | Console | `/dashboard` | Both | Light and dark: console overview |
| `portal-catalog-light`, `portal-catalog-dark` | Portal | `/catalog`, model-grouped rows with environment and access filters | Both | Light and dark: portal catalog |
| `console-gateways` | Console | `/gateways` | Light | Gateways |
| `console-gateway-overview` | Console | Contoso AI Gateway, **Overview** section | Dark | Gateway overview |
| `console-gateway-apis` | Console | Contoso AI Gateway, **APIs and endpoints** section | Light | APIs and endpoints |
| `console-models` | Console | `/models` | Dark | Models |
| `console-register-key-endpoint` | Console | `/models?register=1`, **Azure AI with an API key** tab with a pasted, masked key, filled in and never submitted | Light | Register with an API key |
| `console-key-endpoint` | Console | `/models?tab=endpoints`, Fabrikam partner Foundry selected, scrolled to its **Access** card with **Replace API key** | Dark | Endpoint reached with an API key |
| `console-pools` | Console | `/pools`, published Anthropic Claude pool and Claude evaluation draft, with separate Suggestions navigation | Light | Model pools |
| `console-pool-detail` | Console | Anthropic Claude detail, **Models and deployments** section | Dark | A model pool |
| `console-pool-access` | Console | Anthropic Claude detail, **Access and connection** section | Light | Who can call a pool |
| `console-pool-health` | Console | Anthropic Claude detail, **Health** section for the last 24 hours | Dark | Pool health |
| `console-pool-editor` | Console | `/pools?tab=suggestions`, **Create pool** on the Claude Haiku 4.5 suggestion, **3. Models** step, never saved | Light | Create a pool |
| `console-endpoint-pools` | Console | `/models?tab=endpoints`, Contoso AI Foundry selected, deployments and **Used by pools** | Dark | Pools on an endpoint |
| `console-mcps` | Console | `/mcps`, **Published MCP servers** and the application each calls models as | Light | MCP servers |
| `console-identity` | Console | `/identity?tab=agents` | Dark | Identity |
| `console-directory-picker` | Console | `/identity?tab=agents`, **Add agent** dialog with query `agent` | Light | Directory picker |
| `console-security-group-members` | Console | `/identity?tab=security-groups`, filtered to **AI Model Users** | Dark | Security group members |
| `console-entitlements` | Console | `/entitlements`, scrolled to **Grants** | Light | Entitlements |
| `console-access-requests` | Console | `/entitlements?tab=requests`, pending queue | Light | Access requests |
| `console-model-access-settings` | Console | `/entitlements?tab=settings`, GPT-4o selected | Dark | Model access settings |
| `console-overlapping-grants` | Console | `/entitlements?tab=effective`, **Overlapping grants** | Dark | Overlapping grants |
| `console-cost-centers` | Console | `/cost-centers` | Light | Cost centers |
| `console-cost-center-detail` | Console | Customer Insights detail, **Members** section | Dark | Cost center members |
| `console-mcp-publish` | Console | `/mcps`, **Review changes** for the published Docs MCP server, including the shared metadata API, operation, and operation policy | Light | MCP publish review |
| `console-unpublish-review` | Console | `/models`, **Unpublish** review for GPT-4o, never confirmed | Light | Unpublish review |
| `console-budgets` | Console | Customer Support detail, **Budget** section | Dark | Budgets |
| `console-analytics` | Console | `/analytics`, **Overview** section | Dark | Analytics |
| `console-analytics-cost` | Console | `/analytics?tab=cost` | Light | Cost |
| `console-pricing` | Console | `/pricing`, **Prices** section filtered to `2024-11-20` | Dark | Pricing |
| `console-analytics-consumers` | Console | `/analytics?tab=consumers` | Light | Consumers |
| `console-analytics-mcp-model-use` | Console | `/analytics?tab=consumers`, scrolled to **Model use through MCP servers** | Dark | Model use through MCP servers |
| `console-analytics-limits` | Console | `/analytics?tab=limits` | Dark | Grant limits |
| `console-gateway-telemetry` | Console | Contoso AI Gateway, **Overview** section, scrolled to **Telemetry** | Light | Gateway telemetry |
| `console-analytics-reliability` | Console | `/analytics?tab=reliability` | Dark | Reliability |
| `console-environments` | Console | `/settings` | Light | Environments |
| `console-environment-findings` | Console | `/settings?tab=findings` | Dark | Environment findings |
| `console-email-settings` | Console | `/settings?tab=email` | Light | Budget email |
| `portal-request-access` | Portal | `/catalog`, GPT-4o mini access options in an overlay drawer | Light | Request access |
| `portal-access` | Portal | `/access`, grouped model rows and an exact grant's **Connection details** open in a drawer | Light | My access |
| `portal-connection-cost-center` | Portal | `/access`, the GPT-4o grant under General with **Connection details** open, scrolled to **Cost center header** | Light | Choosing a cost center |
| `portal-pool-connection` | Portal | `/access`, Claude Sonnet 4.5 selected, **Connection details** drawer scrolled to **Authentication** and the shared-key note | Dark | Models that share a key |
| `portal-mcp-connection` | Portal | `/access`, with an enforced MCP grant's **Connection details** open | Dark | MCP connection |
| `portal-requests` | Portal | `/requests`, model-first cards with offering and version context | Dark | My requests |
| `portal-usage` | Portal | `/usage`, the default portal landing page | Light | Usage & cost |
| `portal-usage-resources` | Portal | `/usage?tab=resources`, **By resource** with exact offering context | Dark | Usage by resource |
| `portal-usage-breakdowns` | Portal | `/usage?tab=breakdowns`, **Cost centers** and pooled quotas | Light | Usage breakdowns |
| `portal-budget-banner` | Portal | `/access`, with the budget banner for Customer Insights | Light | Budget banner |
