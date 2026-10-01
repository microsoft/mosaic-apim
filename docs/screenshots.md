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
  of it whose APIs still point at the production accounts, a partner gateway that answers `403`
  so it shows **Access needed**, Azure OpenAI and Foundry accounts, a Key Vault that MOSAIC keeps a
  partner's Foundry key in and the gateways can read it from, and MCP servers. MOSAIC reaches them
  through `httpx` mock transports, so nothing leaves the machine. A fake Log Analytics workspace
  answers MOSAIC's usage queries from the gateways' generated logs, a day at a time.
- `demo_api.py` runs the production FastAPI app with in-memory repositories and swaps its
  Azure-facing services for the fakes. It then seeds the estate through MOSAIC's own services, in
  the order an operator would: add a custom Partner environment, register, classify, and sync
  gateways, register a partner's Foundry resource by URL with a fictional API key, which MOSAIC
  stores in the Key Vault double, and declare its deployments, then import, publish, grant, and
  request, approve, and deny access. One MCP server keeps
  only a legacy label, so Settings has something to classify, and the development gateway's
  routes to production produce environment findings. Grants and decided requests are then dated
  back as far as 120 days. Last, it enables API diagnostics on the production gateway and
  generates a quarter of gateway traffic from the estate's people and workloads, including
  throttled and refused calls and calls to adopted APIs. MOSAIC's rollup job reads it all back,
  so the Dashboard, Analytics, and the portal's **Usage & cost** page show measured figures. They
  are priced from the price list MOSAIC ships, plus what the seed sets through the pricing
  service: the partner's declared GPT-4.1 mini deployment's type, and a negotiated GPT-4o rate
  from next month, which the **Pricing** page shows as scheduled. The Claude, Mistral Large,
  Cohere, and realtime deployments and the adopted APIs stay unpriced, so the **Unpriced
  deployments** tab has something to show. Every page therefore renders what the product would
  show for that estate. The demo runs no rollup loop afterwards, so every shot in a run shows the
  same figures. The traffic and timestamps follow the clock, though, so they shift a little from
  one run to the next. Requests from the portal's origin are answered as Megan Bowen with the
  `User` role, and all others as Adele Vance with `Admin` and `User`. Local authentication is
  refused outside local and test environments, so the demo can't be pointed at a deployed MOSAIC.
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
- `REDACTION_PATTERNS` in `capture.py`: GUIDs, email addresses, URLs and URIs, Azure resource IDs,
  Azure and Contoso hostnames, IPv4 addresses, MOSAIC record IDs and the APIM names derived from
  them, and the demo's placeholder keys.

Some things stay readable on purpose, because they explain the product without identifying
anything. These are the fictional people and workloads, the display names of gateways, models, and
MCP servers, API paths, deployment names, limits, and dates.

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
   - `height` is the viewport height in pixels. It defaults to 900.
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
| `portal-catalog-light`, `portal-catalog-dark` | Portal | `/catalog` | Both | Light and dark: portal catalog |
| `console-gateways` | Console | `/gateways` | Light | Gateways |
| `console-gateway-overview` | Console | Contoso AI Gateway, **Overview** tab | Dark | Gateway overview |
| `console-gateway-apis` | Console | Contoso AI Gateway, **APIs and endpoints** tab | Light | APIs and endpoints |
| `console-models` | Console | `/models` | Dark | Models |
| `console-register-key-endpoint` | Console | `/models?register=1`, **Azure AI with an API key** tab with a pasted, masked key, filled in and never submitted | Light | Register with an API key |
| `console-key-endpoint` | Console | `/models`, Fabrikam partner Foundry selected, scrolled to its **Access** card with **Replace API key** | Dark | Endpoint reached with an API key |
| `console-mcps` | Console | `/mcps`, showing published and registered MCP servers | Light | MCP servers |
| `console-identity` | Console | `/identity?tab=agents` | Dark | Identity |
| `console-directory-picker` | Console | `/identity?tab=agents`, **Add agent** dialog with query `agent` | Light | Directory picker |
| `console-security-group-members` | Console | `/identity?tab=workloads`, filtered to **AI Model Users** | Dark | Security group members |
| `console-entitlements` | Console | `/entitlements`, scrolled to **Grants** | Light | Entitlements |
| `console-overlapping-grants` | Console | `/entitlements`, scrolled to **Overlapping grants** | Dark | Overlapping grants |
| `console-mcp-publish` | Console | `/mcps`, **Plan and apply** review for the published Docs MCP server | Light | MCP publish review |
| `console-unpublish-review` | Console | `/models`, **Unpublish** review for GPT-4o, never confirmed | Light | Unpublish review |
| `console-analytics` | Console | `/analytics`, **Overview** tab | Dark | Analytics |
| `console-analytics-cost` | Console | `/analytics?tab=cost` | Light | Cost |
| `console-pricing` | Console | `/pricing`, **Prices** tab filtered to `2024-11-20` | Dark | Pricing |
| `console-analytics-consumers` | Console | `/analytics?tab=consumers` | Light | Consumers |
| `console-analytics-limits` | Console | `/analytics?tab=limits` | Dark | Grant limits |
| `console-gateway-telemetry` | Console | Contoso AI Gateway, **Overview** tab, scrolled to **Telemetry** | Light | Gateway telemetry |
| `console-analytics-reliability` | Console | `/analytics?tab=reliability` | Dark | Reliability |
| `console-environments` | Console | `/settings` | Light | Environments |
| `console-environment-findings` | Console | `/settings`, scrolled to **Findings** | Dark | Environment findings |
| `portal-access` | Portal | `/access`, with a grant's **Connection details** open | Light | My access |
| `portal-mcp-connection` | Portal | `/access`, with an enforced MCP grant's **Connection details** open | Dark | MCP connection |
| `portal-requests` | Portal | `/requests` | Dark | My requests |
| `portal-usage` | Portal | `/usage` | Light | Usage & cost |
| `portal-usage-resources` | Portal | `/usage`, scrolled to **By resource** | Dark | Usage by resource |
