# Screenshot gallery

[Documentation](README.md) | [MOSAIC](../README.md)

The [README](../README.md#screenshots) keeps a short selection; this is the complete gallery.

- [Light and dark](#light-and-dark)
- [Administrator console](#administrator-console)
- [End-user portal](#end-user-portal)


These show the administrator console (`apps/web`) and the end-user portal (`apps/portal`) running
against a fictional Contoso estate that [`scripts/screenshots`](../scripts/screenshots) seeds
locally, not a real tenant; that is why the console reports **Local mode**. Directory data,
Graph permission claims, and verification results are simulated to illustrate Entra-backed
workflows; they are not live connectivity evidence. Normal local-authentication deployments
cannot perform Graph lookups. Endpoint URLs, host
and resource names, tenant and object IDs, MOSAIC record IDs, and keys are blurred. When the UI
changes, regenerate them and update their descriptions as described in
[screenshot maintenance](screenshots.md).

## Light and dark

The blue mosaic-tile mark anchors collapsible primary navigation in both apps. Section menus
separate administrative workflows; the portal opens on Usage & cost, with model-first lists
and accessible detail drawers that leave the page layout in place.

Both apps follow the operating system's light or dark setting, and the console can pin either
one using the sidebar's **Light / Dark / System** controls or **Settings > Appearance**.

<table>
  <tr>
    <th width="50%">Light</th>
    <th width="50%">Dark</th>
  </tr>
  <tr>
    <td><img src="images/screenshots/console-dashboard-light.png" alt="The console overview in the light theme"></td>
    <td><img src="images/screenshots/console-dashboard-dark.png" alt="The console overview in the dark theme"></td>
  </tr>
  <tr>
    <td colspan="2"><b>Console overview.</b> Registered identity counts open their corresponding
    Identity sections. Gateway usage, cost and forecast, telemetry freshness, rankings, budgets,
    pools needing attention, and environment inventory summarize the fictional estate.</td>
  </tr>
  <tr>
    <td><img src="images/screenshots/portal-catalog-light.png" alt="The portal catalog in the light theme"></td>
    <td><img src="images/screenshots/portal-catalog-dark.png" alt="The portal catalog in the dark theme"></td>
  </tr>
  <tr>
    <td colspan="2"><b>Portal catalog.</b> Model-first rows group known offerings and show their
    clouds, environments, and existing access. Search and cloud/environment/type filters narrow the list;
    access options open in an overlay drawer without rearranging the catalog.</td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-government-connection.png" alt="A Government model grant with its AzGov cloud, Production environment, shared cost center, and subscription-key connection details">
      <p><b>Government key access.</b> The exact Government offering shows its cloud, environment,
      cost center, and applied key-only access. Connection details keep key reveal explicit and
      show how to call that offering without commercial Entra authentication.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-azure-connections.png" alt="Settings Azure connections with separate Commercial host identity and Government service-principal connection, authentication status and management actions">
      <p><b>Azure connections.</b> Manage the separate identities used for Commercial and
      Government resources, test authentication, and replace a write-only secret. Cloud is
      independent of environment classification.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-government-gateway.png" alt="A Government gateway overview with a separate AzGov cloud badge, Production environment, management connection, and inventory">
      <p><b>Government gateway.</b> Inspect a Government APIM service through its selected
      management connection while keeping its cloud and environment distinct. The same reviewed
      management controls govern its model and tool publications.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-government-mcp-connection.png" alt="A Government MCP grant's key-only connection details and custom-header client configuration">
      <p><b>Government MCP connection.</b> An applied Government tool grant uses its own
      subscription key rather than commercial OAuth discovery. Connection details show
      compatible custom-header client configuration without revealing the key automatically.</p>
    </td>
  </tr>
</table>

## Administrator console

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-gateways.png" alt="A compact registered-gateway list beside the selected gateway's overview and inventory counts">
      <p><b>Gateways.</b> A compact list selects an existing API Management service and opens its
      overview in the same workspace. The selected gateway shows its cloud, environment, read/manage mode,
      inventory counts, and service details.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-gateway-overview.png" alt="A gateway overview with inventory counts and service details">
      <p><b>Gateway overview.</b> One gateway at a glance: its AI APIs, operations, MCP servers,
      access paths, and policy rules, with its environment, mode, and service details. The section
      menu opens its read-only APIM inventory, separately from MOSAIC-managed actions.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-gateway-apis.png" alt="A gateway's APIs described in plain language with their operations">
      <p><b>APIs and endpoints.</b> Each API on a gateway described in plain language: the
      backend it points at, what it exposes, the policies that govern it, and its operations.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-models.png" alt="A compact published-model list beside selected publication details, gateway state, and actions">
      <p><b>Models.</b> A compact publication list opens the selected model's gateway state,
      API path, access settings, and actions in a detail workspace. Publishing changes APIM only when a reviewed plan is
      applied, and only on a gateway in manage mode.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-register-key-endpoint.png" alt="The full-workspace Register model endpoint editor on its API key tab, with a masked key and two declared deployments">
      <p><b>Register with an API key.</b> When MOSAIC can't reach a resource with its managed
      identity, such as a Foundry project in another tenant, an administrator gives its URL and its
      API key, which MOSAIC keeps in its own Key Vault, and declares the deployments to publish with
      the API each one takes.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-key-endpoint.png" alt="A key-authenticated endpoint's access card, with Replace API key, and its declared deployments">
      <p><b>Endpoint reached with an API key.</b> MOSAIC shows that the endpoint accepts the key it
      keeps, whether each gateway can read the key itself, and the deployments declared for
      publishing. An administrator can replace the key without ever seeing it again.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-deploy-model.png" alt="The Azure model deployment workspace with explicit management opt-in, a live model version, deployment type, native capacity, quota, and content-filter selection">
      <p><b>Deploy an Azure model.</b> Query an existing account's live model catalogue and configure
      a new deployment, with explicit management opt-in, native Azure capacity, and verified quota.
      Registration and deployment are connected without automatically publishing a model.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-deploy-model-review.png" alt="Azure model deployment review showing the target account, exact model version, configuration, quota, billing warnings, and explicit Deploy to Azure action">
      <p><b>Review before Azure creation.</b> Inspect the exact account, model version, deployment
      type, capacity, and billing implications before deploying. Successful provisioning refreshes
      inventory; uncertain operations and failed refreshes have separate recovery actions.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-pools.png" alt="A compact model-pool list beside the selected published pool's overview, readiness, and routing settings">
      <p><b>Model pools.</b> A compact list opens a pool's overview, showing its gateway, routing,
      publication state, and readiness. Suggestions are a separate destination for models deployed
      on multiple reachable endpoints without a pool.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-pool-detail.png" alt="A breaker pool serving Claude Opus 4.5 and Claude Sonnet 4.5 from five deployments in three regions, with weights, readiness, and drain switches">
      <p><b>A model pool.</b> Compact navigation rails give the Models and deployments section room
      to show each model's deployments, regions, capacity, readiness, weights, and drain controls. Overview, access, health, and
      policy/run history are separate destinations.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-pool-access.png" alt="A governed pool's Access and connection section, with key and Entra sign-in switches, saved and applied authentication, grants and pooled quotas, and a redacted connection example">
      <p><b>Who can call a pool.</b> A governed pool admits only callers granted one of its models,
      each charging a cost center and signing in with their own key or a Microsoft Entra token. The
      card lists the grants in force, their limits, and each cost center's pooled quota.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-pool-health.png" alt="A breaker pool's Health card for the last 24 hours, with each Claude model's calls, successes, unavailable calls, and retries, and how each regional deployment answered, including throttled attempts and breaker trips">
      <p><b>Pool health.</b> How the gateway's calls to each model ended, and how each deployment
      answered them, read from the traces the pool's policy writes to the gateway's logs. An
      administrator sees which region throttles or trips its breaker, and how often the pool retried
      elsewhere.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-pool-editor.png" alt="The full-workspace Create a model pool editor on its Models step, with declared models and deployment controls">
      <p><b>Create a pool.</b> A suggestion opens the pool editor with the model and every
      deployment the gateway can use already chosen. An administrator names the model for callers
      and for the portal, weights or drains each deployment, and reviews the pool's routing and
      limits before saving it.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-endpoint-pools.png" alt="A selected endpoint's Used by pools card listing two Claude pools, their gateways, deployment dependencies, and publication state">
      <p><b>Pools on an endpoint.</b> The selected endpoint lists every pool that uses it and the
      deployments each pool depends on. Before removing an endpoint or a
      deployment, an administrator can see which pools would lose it.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-mcps.png" alt="A published MCP server selected beside a compact list, with gateway state, authentication, and its model-calling identity">
      <p><b>MCP servers.</b> Selected server details show gateway state, tools, and the
      model-calling identity, with separate sections for imported and registered
      servers. MOSAIC reads what a server offers and never calls a tool.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-identity.png" alt="An Agents workbench with a compact selectable identity list and selected identity details">
      <p><b>Identity.</b> The people, agents, applications, and security groups MOSAIC references
      by Entra object ID, each in its own section, separately from MOSAIC-local groups. A compact
      list opens the selected identity's defaults, verification state, and actions.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-directory-picker.png" alt="The full-workspace Add agent editor with directory results, a selected identity, and a separate Add action">
      <p><b>Directory picker.</b> <b>Add agent</b> in the Agents section searches Microsoft Entra for
      agents; the same picker finds people, security groups, applications, and managed identities.
      Select a result and review its default cost center before explicitly adding a MOSAIC reference.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-security-group-members.png" alt="A selected security group's detail workspace with members loaded from Microsoft Graph">
      <p><b>Security group members.</b> A recorded Entra security group shows its read-only
      Microsoft Graph member list. MOSAIC can show which members are also recorded locally for
      direct grants.</p>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <img src="images/screenshots/console-directory-verification.png" alt="An explicit complete-group read with group-scoped status, timestamps, and diagnostic freshness, separate from the member preview">
      <p><b>Complete group-read verification.</b> An explicit check shows a selected Entra group's
      complete-read result, scope, and freshness, illustrated here with a simulated Graph response. This process-local
      diagnostic does not grant access or replace owner eligibility.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-people-search.png" alt="The full-workspace Add person editor with a selected Entra search result and explicit Add action">
      <p><b>Find a person.</b> Search Microsoft Entra instead of copying object IDs into a form.
      Select an identity, review the local default, and add it without creating or changing an Entra account.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-identity-detail.png" alt="A selected agent's overview and local defaults beside the compact identity list">
      <p><b>Identity details.</b> A generous detail workspace shows the selected identity's overview
      and local defaults. Editing takes over the workspace, and leaving protects unsaved drafts.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-entitlements.png" alt="A compact grant list beside selected grant limits, desired and applied state, and binding details">
      <p><b>Entitlements.</b> A compact grant list opens the selected caller's resource, cost center,
      limits, desired and applied state, and APIM binding. Filters narrow the list; gateway
      enforcement changes only through a reviewed apply.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-overlapping-grants.png" alt="Overlapping grants showing which grant applies, each grant's limits, and links to the grants">
      <p><b>Overlapping grants.</b> MOSAIC explains when multiple grants under one cost center
      can reach the same caller on the same resource, with each grant's limits and a link to it.
      Direct grants win over group grants, and the most generous security-group grant wins among
      groups.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-cost-centers.png" alt="A selectable cost-center list and selected center detail, with a separate New cost center action">
      <p><b>Cost centers.</b> A compact list selects a charge scope and shows its setup state,
      dedicated Entra group, responsible contacts, key setting, and budget. General is the explicit
      exception to custom centers' group requirement.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-cost-center-detail.png" alt="A cost center's real Entra direct members, directory capabilities, and operation history">
      <p><b>Cost center members.</b> Add or remove real direct Entra relationships rather than
      editing a MOSAIC-local roster. Operation outcomes distinguish directory updates from token
      propagation and pending individual-key revoke/apply work.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-cost-center-access.png" alt="A cost center's exact-resource access definitions with Automatic embeddings access, allowance, and saved caller settings">
      <p><b>Cost-center resource access.</b> Inspect Automatic, Request-only, or Not available
      access for each exact model, pool model, or MCP offering. Automatic uses one stable group
      grant per offering; saving configuration still requires a reviewed gateway apply.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-cost-center-setup.png" alt="An approved Knowledge Engineering cost center awaiting dedicated Entra security-group setup">
      <p><b>Finish group setup.</b> An approved center stays unusable until an eligible,
      exclusive Entra group is verified. Attach an existing group, create one with optional
      consent, or retain the explicit Setup required state.</p>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <img src="images/screenshots/console-cost-center-request-review.png" alt="Cost-center request review with the submitted proposal, optional group suggestion, explicit group-setup choices, and deferred setup">
      <p><b>Review a cost-center request.</b> Review the submitted purpose and optional group
      suggestion, then choose the center's setup and whether to add its requester. Approval,
      group readiness, real membership, and gateway apply remain separate outcomes.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-mcp-publish.png" alt="An MCP review separating intended configuration changes from operational impact and collapsed technical execution">
      <p><b>MCP publish review.</b> See pending changes and the batch's operational impact without
      reading unchanged grants. Expand Technical execution for the full target and ordered gateway writes.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics.png" alt="Analytics with request, token, cost, caller, error, and latency figures, a daily trend, and the top models, callers, and cost centers">
      <p><b>Analytics.</b> Real gateway usage rolled up from Log Analytics, filtered by time range,
      cloud, gateway, environment, resource, cost center, and kind of subject. The overview compares the
      headline figures, cost among them, with the previous period and ranks the top models,
      callers, cost centers, and APIs, and each report has a CSV export.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics-cost.png" alt="The Cost section with this month's spend, the month-end forecast, the range's cost, a cost and token trend, and cost by model, consumer, and cost center">
      <p><b>Cost.</b> What measured usage cost at list price: this month's spend and its month-end
      forecast, the range's cost and trend, and cost by model, consumer, cost center, API, and
      deployment, with a chargeback export that names each row's cost center. Usage MOSAIC can't
      price shows <b>No price</b>, never $0.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-pricing.png" alt="The Pricing page listing one GPT-4o version's input, cached-input, and output prices by deployment type and region">
      <p><b>Pricing.</b> The list prices MOSAIC turns usage into cost with, for each cloud, each
      linked to its source. Administrators add prices for other clouds and providers, or override
      one from a date without changing the days before it, and see which deployments have no price
      and why.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics-consumers.png" alt="The Consumers section listing people, agents, applications, and security groups with their requests, tokens, grants, resources, last call, and cost">
      <p><b>Consumers.</b> Each person, agent, application, and security group that called a
      governed API, with requests, tokens, grants, resources, when they were last seen, and what
      their calls cost. Below it, model use through MCP servers by person, and the same usage by
      grant, by cost center, and by client application.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics-limits.png" alt="The Limits section showing each grant's cost center and its quota and rate-limit use with reached, near-limit, and OK badges">
      <p><b>Grant limits.</b> How close each grant is to its quotas and rate limits, busiest first,
      with its cost center and the calls the gateway throttled or refused for quota. A grant is
      near its limit at 80% and has reached it once the gateway refuses its calls.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-gateway-telemetry.png" alt="A gateway's Telemetry section with readiness checks and the diagnostic state of each governed API">
      <p><b>Gateway telemetry.</b> Whether MOSAIC can measure a gateway's usage: its Azure Monitor
      logger, logs sent to Log Analytics, MOSAIC's read access, API diagnostics, and rollups, with
      the command that fixes a failing check. Administrators can enable API diagnostics on what
      MOSAIC published, refresh now, or backfill from the workspace's retention.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics-reliability.png" alt="The Reliability section with successful, throttled, backend 429, and denied counts, a latency histogram, and denials by reason">
      <p><b>Reliability.</b> Successful calls, calls the gateway throttled, 429s from the model
      deployments, and denied calls, with an estimated latency histogram. Denials are broken down by
      reason, such as a caller who isn't signed in or a key the gateway doesn't know.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-environments.png" alt="The environment catalog in Settings with usage counts and one unclassified resource">
      <p><b>Environments.</b> The catalog administrators define in Settings: the built-in
      environments plus custom ones such as Partner, each with a color and a production-class
      flag. Gateways, model endpoints, and MCP servers are each classified into one, and any not
      yet classified wait below with suggestions to review.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-environment-findings.png" alt="Findings where a development gateway routes to production endpoints">
      <p><b>Environment findings.</b> Pairings already in API Management that break the
      environment rules, such as a development gateway routing to production endpoints, with the
      evidence and how confident the match is. Findings are advisory and never block anything.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-unpublish-review.png" alt="An unpublish review explaining gateway removal and operational impact, with a table of grants that lose access">
      <p><b>Unpublish review.</b> Before MOSAIC removes a model from API Management, the
      administrator sees which grants lose access, the cost center each charges, what stops working
      for each, with resource deletions available in Technical execution. Only confirming this review unpublishes it.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-budgets.png" alt="A cost center's budget past its limit and blocked, with the gateway refusing its calls and the emails sent at 80%, at 100%, and when it was blocked, beside the budget's amount, thresholds, recipients, and action">
      <p><b>Budgets.</b> Each cost center can have a monthly budget in dollars across every
      gateway, which emails anyone at 80% and 100% and either lets calls continue or blocks them at
      the gateway. A blocked budget shows each gateway refusing its calls and every email sent this
      month.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-email-settings.png" alt="Notifications Email setup with a Graph sending mailbox, read-only access checks, and the last explicit test result">
      <p><b>Email setup.</b> Configure one Microsoft Graph sending mailbox, check the API's managed
      identity, and send an explicit test. Separate checks explain missing access and how to fix it;
      provider acceptance is not a delivery receipt.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics-mcp-model-use.png" alt="The Consumers section's Model use through MCP servers table, listing each person, the MCP server, and the application that called models for them, with requests, tokens, share, and cost, below a count of the references MOSAIC couldn't attribute, by reason">
      <p><b>Model use through MCP servers.</b> The model calls an MCP server's application made for
      each person who called the server, with the application, requests, tokens, and cost, and the
      references MOSAIC couldn't use, by reason. The calls stay the application's own in every
      other figure, so the table adds to no total.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-teams-shared.png" alt="Shared Teams setup with a selected Workflow, a failed secret-access check, corrective instructions, and saved test evidence">
      <p><b>Shared Teams access.</b> Check a Workflow's secret, approved endpoint and notification
      routing without sending a message. Failed checks identify what to correct and who can fix it;
      Workflow execution and channel receipt remain separate checks.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-teams-private.png" alt="Private Teams setup with the saved bot registration, runtime OAuth check, personal installation and opt-in checks, and last synthetic test">
      <p><b>Private Teams access.</b> Check the bot identity, your authenticated installation and
      your channel preferences, then explicitly test your own private chat. Saved results belong
      to your signed-in persona and never redirect private notifications to a shared channel.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-access-requests.png" alt="A compact access-request list beside the selected request's resource, cost center, justification, and approval or denial actions">
      <p><b>Access requests.</b> The focused list-and-detail approval queue is reachable from the header's
      pending count or the Entitlements section menu. Approval saves grant intent; administrators
      still review and apply the gateway changes separately.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-model-access-settings.png" alt="A selected published model's authentication settings, desired and applied access state, and model-wide review controls">
      <p><b>Model access settings.</b> A dedicated section configures authentication for a
      selected published model, distinct from filtering individual grants. Review includes all
      saved grants and settings for that model and does not apply them automatically.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-analytics-filters.png" alt="A grouped Analytics Filters popover with draft controls, Apply filters, Reset, and Close filters">
      <p><b>Analytics filters.</b> Change the entire report scope in one grouped filter panel and
      apply it once. Dismissing the draft leaves the report and CSV export scope unchanged.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-new-cost-center.png" alt="A full-workspace New cost center editor with Back, creation fields, and Create cost center">
      <p><b>New cost center.</b> Use the full workspace to attach an existing Entra group, create
      one with separately consented permissions, or save an unusable setup-required center for
      later. Back protects unsaved changes.</p>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <img src="images/screenshots/console-request-notifications.png" alt="The pending-approvals popover with requester names, requested resources, and exact review links, separate from the unread notification inbox">
      <p><b>Pending approvals.</b> Each pending request separates the requester, requested
      resource, and time, and opens that exact request for review. This queue is separate from
      the persistent notification inbox and its unread count.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-runtime-configuration.png" alt="Runtime configuration with read-only deployment details, Entra identity and app-registration references, Graph consent with role IDs, and separate complete-group-read diagnostic status">
      <p><b>Runtime configuration.</b> Read-only deployment details show the API managed identity,
      Entra app registrations, and Graph permission role IDs while keeping consent separate from
      complete-group-read verification; the demo's Graph states are simulated. Refresh status, open
      Portal and CLI consent remediation, or choose an Entra group in Identity for an explicit
      check.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-notification-readiness.png" alt="Notification readiness checks with evidence and remediation for storage, always-on hosting, email, public message links, and disabled workers">
      <p><b>Notification readiness.</b> Inspect prerequisite evidence and specific recovery
      guidance for persistence, always-on hosting, email, public message links, and workers.
      Refreshing checks sends no message, and saved configuration is not proof of delivery.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-approval-policy.png" alt="A disabled development approval policy editor with environment scope, office-hour windows, minimum request age, bounded limits, and dry-run controls">
      <p><b>Scheduled approval policy.</b> Define environment and resource scope, a minimum
      waiting period, local-time approval windows, and bounded grant limits.
      Preview pending requests without approving them, and enable automation explicitly.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-bulk-approval.png" alt="Full-workspace bulk approval review with two explicit requests, current environment facts, inherited limits, per-request controls, and advisory AI actions">
      <p><b>Bulk approval review.</b> Review an explicit selection against current environment,
      cost-center, and limit facts, with independent inclusion and advisory AI controls.
      Approvals save grant intent; gateway plans still require separate review and apply.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-notification-preferences.png" alt="Personal administrator notification preferences with per-category in-app, email and private Teams cadence controls, digest schedules, and usage thresholds">
      <p><b>Administrator preferences.</b> Choose channels and cadence per event category,
      digest schedules, quiet hours, and usage-alert thresholds. These choices belong only to
      the administrator persona and do not enable an unavailable integration.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/console-notification-inbox.png" alt="The administrator notification inbox with category and unread filters, selected notification details, read controls, and an exact related-record link">
      <p><b>Administrator inbox.</b> Filter persistent updates, inspect an exact notification,
      and open the related record. Marking an update read does not approve a request or resolve
      an operational issue.</p>
    </td>
  </tr>
</table>

<p>
  <img src="images/screenshots/console-model-change-review.png" width="720" alt="A model review separating the current authentication edit from a previously saved grant, with before and after values, impact, and collapsed execution">
</p>
<p><b>Model change review.</b> See the current edit separately from an earlier saved grant, with
before-and-after values and the impact of applying the whole batch. Unchanged settings stay out
of the main review; Technical execution retains the complete target and resource writes.</p>

## End-user portal

The portal shares the console's cool-neutral palette, blue-tinted glass navigation, rounded
controls, and full-width workspace. Choose Light, Dark, or System beside the account menu;
resource lists, forms, and connection drawers remain solid for readability.

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-access.png" alt="A model grant with its cost center, environment, limits, and expanded connection details">
      <p><b>My access.</b> Models and servers appear once in a searchable list, with clouds, environments,
      cost centers, and gateway state. Choose an exact grant in the overlay drawer to see its
      limits, attribution, and connection instructions.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-mcp-connection.png" alt="Automatic cost-center MCP access with Entra sign-in and VS Code mcp.json connection details">
      <p><b>MCP connection.</b> Automatic cost-center access shows the server URL, Entra sign-in
      guidance, VS Code <code>mcp.json</code> snippet, and call limits. It offers no inherited
      subscription key.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-connection-cost-center.png" alt="A grant's cost-center header, a curl sample that sends it with an Entra token, and response headers that report remaining limits">
      <p><b>Choosing a cost center.</b> Each grant names the cost center it charges and the
      <code>x-mosaic-cost-center</code> header that picks it on a call, with a copy-ready curl for a
      Microsoft Entra token and the response headers that say what's left of each limit. The
      header lets the caller charge that grant instead of their default cost center.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-requests.png" alt="Resource-access and cost-center creation requests with type and status filters">
      <p><b>My requests.</b> Resource-access and cost-center creation requests share a typed,
      filterable history showing each request's context and status. Withdrawing a pending
      request requires confirmation.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-usage.png" alt="A budget banner, then measured requests, tokens, errors and throttling, estimated cost, and busiest resource, with a daily trend and the last 24 hours">
      <p><b>Usage &amp; cost.</b> The portal's landing page shows a person's requests, tokens,
      errors, throttling, and estimated cost, measured from gateway logs. Daily trends and the
      last 24 hours show when they used their resources.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-usage-resources.png" alt="Usage by granted resource with requests, tokens, estimated cost, quotas, rate limits, and usage tracking">
      <p><b>Usage by resource.</b> A scrollable table retains each offering and shows its grant's
      requests, tokens, estimated cost or <b>No price</b>, quota use, busiest minute, and attribution.
      Overview and breakdowns share the same period and cloud/resource/environment filters.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-budget-banner.png" alt="My access with a banner saying a cost center the person charges is near its monthly budget, and that its calls will be refused at 100%">
      <p><b>Budget banner.</b> When a cost center the person charges nears, passes, or is blocked
      at its monthly budget, My access and Usage &amp; cost say so, and what happens at 100%. It
      shows the cost center's total against its budget, never anyone's own share.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-pool-connection.png" alt="Claude Sonnet authentication, key controls, the other model covered by its key, and call samples">
      <p><b>Models that share a key.</b> A model's connection drawer names the other models its key
      covers under the same cost center. Authentication instructions and call samples show the
      public model endpoint, never the deployments behind it.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-request-access.png" alt="The catalog with a contextual access request panel for a selected model">
      <p><b>Request access.</b> Open a model's options without shifting the catalog, then choose
      the exact offering and cost center for a request. The drawer shows existing access or
      pending requests and lets the person add a justification.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-usage-breakdowns.png" alt="Usage breakdowns with aggregate cost-center totals and pooled quotas">
      <p><b>Usage breakdowns.</b> Each cost center shows aggregate monthly totals and pooled
      quotas for resources the person holds, never another person's activity. The same report
      separates model calls an MCP server made on their behalf from their own usage totals.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-notifications.png" alt="A personal notification inbox with request and usage updates, category and unread filters, exact request links, and separate read state">
      <p><b>Personal inbox.</b> Follow request status and attributed usage updates, filter
      unread messages, and open the exact related request or access record. Inbox read state is
      separate from approval and gateway readiness.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-notification-preferences.png" alt="End-user preferences with independent in-app, email, and private Teams choices for each event category">
      <p><b>Personal notification preferences.</b> Choose in-app, email, or private Teams
      cadence separately for each category. External channels and recurring summaries require
      explicit opt-in and ready integrations.</p>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <img src="images/screenshots/portal-notification-digests.png" alt="Daily, weekly, and monthly digest schedules with time-zone, delivery-time, quiet-hour, threshold, and repeat-cooldown controls">
      <p><b>Usage summaries and alert timing.</b> Schedule daily, weekly, and monthly summaries
      in your time zone, with quiet hours and configurable threshold reminders. These choices
      change notification timing, not grant quotas or budget enforcement.</p>
    </td>
  </tr>
</table>

### Cost-center requests and Automatic access

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-cost-center-request.png" alt="A cost-center proposal form with purpose, optional code, and an optional existing Entra group object ID">
      <p><b>Request a cost center.</b> Explain the proposed team's purpose and optionally suggest
      an existing Entra group. Submission does not create a group, add members, or grant resource
      access.</p>
    </td>
    <td width="50%" valign="top">
      <img src="images/screenshots/portal-cost-center-request-status.png" alt="An approved cost-center request that still needs group setup and administrator attention">
      <p><b>Track incomplete setup.</b> An approved proposal can still be waiting for its Entra
      group and membership. The result explicitly separates approval from usable resource access
      and gateway apply.</p>
    </td>
  </tr>
  <tr>
    <td colspan="2" valign="top">
      <img src="images/screenshots/portal-automatic-access.png" alt="Automatic cost-center access identified separately from an approved individual grant">
      <p><b>Automatic access.</b> Members see access inherited through an applied grant for their
      cost center's exact offering, rather than a fabricated personal entitlement. Its token-only
      connection offers no inherited key.</p>
    </td>
  </tr>
</table>
