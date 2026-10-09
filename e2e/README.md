# MOSAIC end-to-end harness

Live, human-in-the-loop Playwright tests for a deployed MOSAIC environment. The tests drive the
web console and the portal as real test accounts, from importing a model through to calling it.

```powershell
npm ci
npx playwright install chromium
Copy-Item targets.example.json targets.local.json   # then fill in your environment
npm run login -- admin                              # sign in once per persona
npm test
```

- [Runbook](../docs/e2e/runbook.md): setup, persona sign-in, the live driver, flags, the ordered
  suite and its cleanup, secret hygiene, and troubleshooting.
- [Roadmap](../docs/e2e/roadmap.md): the phases, the journey matrix, and the product gaps.

Layout: `src/` holds the shared library, with the console and portal page objects in `src/pages/`,
the journeys the specs share in `src/journeys.ts`, the runtime verifier runner in `src/runtime.ts`,
and the live driver's plans for the model and MCP verifiers in `src/verify.ts` and
`src/verify-mcp.ts`. `tools/` holds the live driver, drive and login CLIs, `specs/` the ordered
Playwright journeys from `00-smoke` to `90-cleanup`, and `tests/` the harness unit tests.
`mcp-servers/` holds the Phase 11 MCP test servers and the kit that deploys them, in Python and
apart from the harness: see [its README](mcp-servers/README.md).

`55-model-budgets` adds separately approved, isolated model cost-center and budget checks for
R10/R11 and the no-email UI/runtime legs of R12/R14. Generic write/inference flags **do not**
authorize them. R13 Government mail, R14 mailbox/dedupe, R10 Analytics/backend stripping and
R12 named-value write audits have explicit independent evidence boundaries; none is inferred
from a mock, source XML, cached sync status or ACS acceptance. See
[Model cost centers and budgets](../docs/e2e/runbook.md#model-cost-centers-and-budgets).

Offline validation uses no manifest credentials or persona profiles:

```powershell
npm run test:unit
npm run typecheck
npm run lint
npm run test:offline                     # temporary browser, all page traffic mocked
$env:MOSAIC_E2E_TARGETS = 'targets.example.json'
npm run model:plan                      # local parsing/readiness/hash only, no HTTP
```

These commands validate the harness, **not** live R10-R14. No live approval has been recorded;
all five live journeys remain **NOT RUN**.
