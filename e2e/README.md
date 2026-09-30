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
the journeys the specs share in `src/journeys.ts`, and the runtime verifier runner in
`src/runtime.ts`. `tools/` holds the live driver, drive and login CLIs, `specs/` the ordered
Playwright journeys from `00-smoke` to `90-cleanup`, and `tests/` the harness unit tests.
