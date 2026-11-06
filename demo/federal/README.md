# Federal public-snapshot runtime

One reusable, pinned Python 3.13 image provides eight independent MCP services and five manual
Production workload profiles. This is an **unofficial demonstration**, not an agency service,
current operational reference, compliance claim, forecast, or eligibility decision.

The transport is MCP **1.30.0** Streamable HTTP at `/mcp`, stateless and JSON-response enabled.
There is no persistent session store, background inference, schedule, new UI, or Azure provisioning
in this package. The unchanged M-agent `ModelSettings`, error types, and privacy-preserving logging
helpers are reused from `e2e\mcp-servers\m-agent\m_agent`; the E2E server itself is not copied.

## Environment contract

| Variable | Contract |
| --- | --- |
| `FEDERAL_MCP_PROFILE` | One logical service key from the table below. Required for the server. |
| `FEDERAL_ENVIRONMENT` | `Development` (server default), `Staging`, or `Production`. Jobs require explicit `Production`. |
| `PORT` | Server port, default `8000`. |
| `AZURE_CLIENT_ID` | Required for inference and backend verification: the **deployment's own precreated user-assigned managed identity** client ID. No system-assigned, CLI/developer or anonymous credential fallback. |
| `FEDERAL_BACKEND_AUTH_VERIFIED` | Literal `true` enables server inference after the operator has verified backend protection. Default `false`: read tools and tool sync work, summary tools return an explicit error. This flag is a readiness gate, **not an authentication mechanism**. |
| `MOSAIC_MODEL_ENDPOINT` | HTTPS **published APIM model base URL** from MOSAIC connection details, before `/openai/deployments/...`. Never an AI account URL or a query-bearing URL. Required when inference is enabled. |
| `MOSAIC_MODEL_DEPLOYMENT` | Exact published deployment. Workloads enforce the approved model in their configuration table. |
| `MOSAIC_MODEL_API_VERSION` | Published supported chat-completions API version, e.g. `2024-10-21`. |
| `MOSAIC_RUNTIME_SCOPE` | `api://<runtime-client-id>/.default`, used for the application's own Entra token. |
| `MOSAIC_COST_CENTER` | Required application's explicitly granted center code, not a human caller's center. |
| `MOSAIC_MODEL_TOKEN_PARAMETER` | `max_completion_tokens` (default) or `max_tokens`, according to the actual published model contract. |
| `MOSAIC_MODEL_MAX_TOKENS` | Integer `1..256`, default `256`. The model must actually support this output envelope; an empty/length-exhausted answer is an error, never a placeholder. |
| `FEDERAL_WORKLOAD_PROFILE` | One of the five manual job keys below. |
| `MOSAIC_MCP_GATEWAY_URL` | Exact published agency Streamable HTTP `/mcp` URL on the **same Production APIM host** as the model endpoint. Required for jobs. |

No secrets, API keys, passwords, bearer-token environment variables, arbitrary source URLs, or
live-API credentials are needed. Different identities are a deployment requirement: never share
one identity across the eight service deployments or five jobs.

### Required hosted protection

This process intentionally delegates ingress token validation to Container Apps **EasyAuth**.
Never deploy it as a directly reachable, unauthenticated inference service.

1. Configure HTTPS, tenant issuer, environment backend audience, and allowed applications limited
   to the matching environment's gateway MI plus the MOSAIC API MI (tool registration/sync).
2. Verify an anonymous direct call is 401 and an authenticated wrong caller is rejected. Verify
   the intended gateway and MOSAIC API can initialize/list tools. Another environment must fail.
3. Only then enable `FEDERAL_BACKEND_AUTH_VERIFIED=true` and separately assign the service MI
   `Models.Invoke.Application` plus its exact MOSAIC same-environment model grant. An MCP grant
   alone never authorizes inference. Bind the publication's **Calls models as** to that MI.

The runtime always obtains the model bearer token using `ManagedIdentityCredential`. It copies
`x-mosaic-on-behalf-of` opaquely from the **current tool request only** to that request's model
call. It never parses, persists, logs, invents, or reuses the value. Model spend remains charged
to the application's grant/center. Non-Production FAA service model callers cannot use FAA-OPS.
Keep APIM request/response body diagnostics at **zero bytes** for both MCP and model publications.

### Wrong-client backend verification (no inference)

`python -m federal_runtime.verify_backend --targets-json "<JSON array>"` is a read-only command
override for an **existing manual job**, using that job's own `AZURE_CLIENT_ID`. It never calls a
model, initializes MCP, invokes a tool, changes Azure/Entra roles, or changes caller bindings.
It ignores all model/workload configuration.

The operator supplies exactly **24 unique owned backend URLs**, with **three distinct backend
audiences and eight URLs per audience**, as an array of objects containing only `url` and
`audience`. URLs must be HTTPS public `*.azurecontainerapps.io` DNS names with exactly `/mcp`:
no credentials, explicit ports, query strings, fragments, redirects or alternate paths.
Each audience must be `api://<UUID>` without `/.default`; the verifier adds that scope suffix
when acquiring the token. Input is capped at 16 KiB. Inventory ownership and correct
environment-to-audience binding must be verified by the operator before running this command.

Before running it, the operator temporarily grants the verifier job MI **Backend.Invoke** on
the three backend audiences, while deliberately leaving that MI **outside** EasyAuth's
`allowedApplications`. This tests a **valid-token, right-audience, wrong-client** request, not
an expired/invalid token or a wrong audience. The verifier obtains one current MI token per
audience and performs one `GET` per URL with `follow_redirects=False`. It uses
`ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)` with retries disabled.

Only **24 HTTP 403 responses**, with no token/network/response/deadline/cleanup errors, produce
exit code zero and `outcome=ok`. **401 is a failure**, not evidence of valid-client denial;
200, 406, redirects and every other status also fail. Token acquisition failure never triggers
an anonymous request or reuse of another audience's token. There are no HTTP retries or
success-shaped fallbacks. Each response is capped at 64 KiB decoded bytes; token acquisition
is capped at 20 seconds, HTTP requests at 10 seconds (5-second connect), and verification at
180 seconds total, plus at most five seconds each to close the HTTP client and credential.
Tokens exist only in memory and neither tokens nor upstream bodies are persisted.

Stdout is one bounded JSON object containing only outcome, target/attempt counts, HTTP
status/error counts and `targets_sha256`—never URLs, audiences, client IDs, tokens, session IDs
or upstream text. The digest is SHA-256 of UTF-8 compact JSON (`sort_keys=True`,
`separators=(",", ":")`) containing the validated `{url,audience}` objects sorted by URL then
audience; host and UUID hex are normalized to lowercase. Thus array/field ordering does not
change the digest. Invalid configuration exits nonzero; its digest is null when targets could
not be validated. SDK/HTTP warning messages are withheld and sent only to stderr.

The operator must revoke the temporary Backend.Invoke assignments and restore the job command
afterward **on success or failure**. Anonymous, wrong-audience, correct-gateway/API positive
checks, binding ownership, and permission restoration are separate operator responsibilities.
This negative check alone does not authorize model roles or establish full backend readiness.

## Eight service profiles

| `FEDERAL_MCP_PROFILE` | Service and purpose | Tools |
| --- | --- | --- |
| `doe-energy-data` | **DOE Energy Data Desk**: compare EIA state generation/mix. | `state_energy_profile`, `compare_generation_mix`, `source_details` |
| `doe-lab-research` | **DOE Lab Research Library**: discover OSTI metadata and draft cited metadata-only briefs. | `search_research`, `get_record`, `summarize_research` |
| `faa-airport-reference` | **FAA Airport Reference**: historical NASR airport metadata. | `find_airport`, `airport_profile`, `source_cycle` |
| `faa-regulatory-reference` | **FAA Regulatory Reference**: dated Title 14 sections and curated FAA guidance. | `lookup_citation`, `find_guidance`, `summarize_reference` |
| `usda-crop-insights` | **USDA Crop Insights**: published national crop yields/rounded acreage. | `crop_summary`, `compare_yields`, `source_release` |
| `usda-conservation-guide` | **USDA Conservation Guide**: national NRCS practice-page discovery/explanations. | `lookup_practice`, `find_practices`, `summarize_practices` |
| `nasa-mission-archive` | **NASA Mission Archive**: historical NASA media metadata and cited briefing. | `search_missions`, `mission_sources`, `build_mission_brief` |
| `nasa-earthdata-explorer` | **NASA Earthdata Explorer**: public CMR collection coverage discovery/comparison. | `search_collections`, `describe_collection`, `compare_coverage` |

Every successful tool result includes source URLs, release/version, capture date, snapshot mode,
and scope/disclaimer. Summaries additionally include deterministic citations to selected records.
Every search is limited to five results and an 80-character nonblank query. Record IDs are bounded,
summary selection is one to three records and focus is at most 160 characters. A summary makes
exactly **one** model request, with no inference retries; prompt limit is 12,000 characters, HTTP
response limit 64 KB, answer limit 4,000 characters and configured output cap at most 256 tokens.
The three curated GPT deployments receive `reasoning_effort=none`: these short reference briefs
must return answer text within that small output allowance instead of exhausting it on hidden
reasoning. An HTTP 200 without answer text still fails; there is no automatic paid retry.
HTTP redirects are refused. Tool results are limited to 48 KB.

### Public snapshot inventory and provenance

The committed `federal_runtime\snapshots\*.json` files are **real selected public records**, not
synthetic fixtures and not live data. No keys or paid data subscriptions are prerequisites.

| Profile | Included source/release and limitations |
| --- | --- |
| DOE energy | EIA **State Electricity Profiles 2024**, Table 5, XLSX files for AL, CA and TX; 2023 and 2024 total-electric-industry generation. MWh and derived percent shares retain nulls; nested fuel subcategories are excluded to avoid double counting. Rounding can affect totals. |
| DOE research | OSTI API v1, five solar-search metadata records. Titles, dates, DOI, organizations and subjects only. Abstract/full text is **not** rehosted or claimed to have been reviewed. |
| FAA airports | FAA NASR **2026-10-01** `APT_BASE.csv`: IAD, DEN, SFO, DFW and JFK. Historical cycle expires 2026-10-29. No runway/NOTAM/navigation/operational data or decisions. |
| FAA regulation | eCFR Title 14 sections **107.19, 107.31, 107.39 as of 2026-10-27**, plus FAA's Part 107 waivers reference page. Educational discovery, not legal or operational advice. |
| USDA crops | NASS **Crop Production 2025 Summary**, approved **2026-01-12**: national corn yields in 2024/2025, corn 2025 rounded harvested acreage, and soybeans 2025 yield/acreage. No state/county/farm statistics. Only corn has both years for `compare_yields`; unavailable soybeans 2024 is an error, not invented data. |
| USDA conservation | NRCS national reference-page metadata for **340 Cover Crop, 590 Nutrient Management, 330 Contour Farming**. Capture-date version; no unverified publication date asserted. State standards/benefits/eligibility are not evaluated. |
| NASA missions | NASA Images API v1.1: three Artemis I/Orion media-metadata records dated 2021/2022. Links only, no image/media rehosting. Not mission telemetry or current status. |
| NASA Earthdata | NASA CMR: three MODIS collection metadata records, with source collection versions, bounding boxes and dates. A missing end date is **not** evidence of current availability. No authenticated granule downloads. |

Full-source captures record a SHA-256 digest of retrieved bytes. Two capture limitations are
explicit rather than hidden:

* Direct eCFR API requests returned HTTP 406 in this environment, while the web-fetch tool
  successfully retrieved the dated official responses. `source_excerpts\ecfr-*.xml` preserves
  the selected XML elements; its digest is explicitly `excerpt_sha256`, not a whole-source hash.
* NASS's official text report was successfully retrieved by web-fetch, but later direct refresh
  attempts failed (403/DNS). `source_excerpts\nass-cropan26.txt` preserves the selected retrieved
  public report text, with `excerpt_sha256` and a capture note. Figures were not guessed.

Excerpt provenance keeps the original **2026-10-29** retrieval date even when explicitly
reprocessed later; `snapshot_generated_at` is separate. A date-only capture means the precise
retrieval time was not preserved, not that it was retrieved at midnight.

The runtime is snapshot-only: there is **no live adapter or silent network fallback**.
Refreshing is a separate, explicit, read-only operator action:

```powershell
# From repository root. Current allowlisted sources are fixed in the script.
python demo\federal\refresh_snapshots.py
# Explicitly reprocess previously retrieved excerpts; no claim of a fresh live fetch:
python demo\federal\refresh_snapshots.py --profile faa-regulatory-reference --ecfr-excerpts demo\federal\source_excerpts
python demo\federal\refresh_snapshots.py --profile usda-crop-insights --nass-excerpt demo\federal\source_excerpts\nass-cropan26.txt
```

Any source/schema/network error exits nonzero and writes **none of the profiles selected in that
invocation**. Existing snapshot files are left untouched, not relabeled as refreshed. Review
versioned source changes and the generated data diff before publishing an image. The allowlisted
capture downloads are capped at 12 MB each; runtime tools never make public-source HTTP calls.

## Five manual Production jobs

| `FEDERAL_WORKLOAD_PROFILE` | Job/application | Center | Model | Published MCP read |
| --- | --- | --- | --- | --- |
| `nasa-brief` | NASA Mission Briefing | NASA-MISSION | `gpt-5.6-terra` | NASA archive `search_missions` |
| `faa-airspace` | FAA Airspace Briefing | FAA-OPS | `gpt-5.6-luna` | Airport `airport_profile` |
| `faa-standards` | FAA Flight Standards Review | FAA-RND | `gpt-5.6-luna` | Regulatory `lookup_citation` |
| `doe-research` | DOE Lab Research | DOE-LABS | `gpt-5.1` | Research `search_research` |
| `usda-field` | USDA Field Insights | USDA-DATA | `gpt-5.6-luna` | Crop `crop_summary` |

Each own MI requires its exact Production MCP/model grants and runtime application roles, including
`Models.Invoke.Application`. The runner initializes through the published agency MCP, lists tools,
performs **one read**, validates matching snapshot provenance, then performs **one actual model
call** through APIM. It does not import the local snapshot as a substitute for MCP retrieval.
It never manufactures human attribution. An operator starting a job does not become its caller.

There are no automatic retries (including paid inference), no schedules, a 90-second total run
deadline, 30-second MCP HTTP timeout, 45-second model HTTP timeout, and bounded response streams.
Set hosting parallelism/completion count to one and platform retry limit to zero. A failed
MCP call/provenance check makes **zero** model calls. Any error exits nonzero and prints an
`outcome=error` JSON record, never a fake summary or success-shaped usage record.

Stdout contains only profile/status, counts and answer length—not source payloads, prompts,
answers, token values, identities, reference headers, session IDs, URLs or upstream error bodies.
The model answer is intentionally **not persisted or printed** by a job. Governed telemetry
demonstrates actual invocation; stdout is not a content store or a fabricated usage feed.

## Run and validate

From the repository root:

```powershell
python -m uv venv --python 3.13 demo\federal\.venv
python -m pip --python demo\federal\.venv install --require-hashes -r demo\federal\requirements-dev.txt
demo\federal\.venv\Scripts\python.exe -m pytest -c demo\federal\pytest.ini demo\federal\tests
demo\federal\.venv\Scripts\python.exe -m ruff check demo\federal
demo\federal\.venv\Scripts\python.exe -m ruff format --check demo\federal
```

Dependency installation uses committed hashes, TLS validation and the repository-compatible MCP
1.x baseline. To deliberately regenerate locks, run `python demo\federal\compile_requirements.py`
with uv installed. If uv's TLS transport cannot connect in the operator's environment, use the
Python 3.13 environment's installed `pip-tools` compiler without disabling TLS:

```powershell
demo\federal\.venv\Scripts\python.exe demo\federal\compile_requirements.py --pip-tools
```

The fallback is an explicit lock-compilation transport choice, not a data/inference fallback.
Always review lock changes. Tests make no Azure writes or paid model calls. They cover all tools,
schemas, dated provenance, invalid input, every profile's SDK handshake over actual local HTTP,
model denials/malformed/oversized responses, missing MI tokens, opaque per-request attribution,
safe logging and all five job flows across real local MCP and model HTTP servers.

For a local **read-only** server, set `FEDERAL_MCP_PROFILE`, keep the protection flag false,
change directory to `demo\federal`, and run `python -m federal_runtime.server` with that environment.
Local requests are not an ingress-protection verification.

Build the image from the repository root:

```powershell
docker build -f demo\federal\Dockerfile -t mosaic-federal-runtime:local .
```

The Dockerfile-specific ignore allowlists only the runtime/lock and three unchanged M-agent
helpers; tenant/E2E execution artifacts are excluded. The non-root image's default command is
`python -m federal_runtime.server`. Each manual job overrides it with
`python -m federal_runtime.workload`, using its own environment/identity.

**Cloud readiness remains separate:** these offline/local-network tests cannot prove deployed
EasyAuth issuer/audience/caller restrictions, app-role assignments, real published model API
compatibility, APIM attribution, limits/budget enforcement, or paid inference success. Verify those
explicitly after protected provisioning and reviewed grants. No image build, Azure deployment or
paid inference is implied merely by passing the local tests.
