import { test, expect } from '@playwright/test'
import { CostCenterBudgetPage } from '../src/pages/console/cost-centers.ts'
import { assertModelReadiness, modelScopeHash } from '../src/model-config.ts'
import { modelTargets } from '../tests/model-fixtures.ts'

test('budget UI sends only scoped form saves, clears recipients and never enables owner email', async ({ page }) => {
  const targets = modelTargets()
  const id = targets.modelJourneys!.budget!.grant.costCenterId
  targets.origins.web = 'https://mosaic.invalid'
  targets.origins.api = 'https://mosaic.invalid'
  const writes: object[] = []
  const unexpected: string[] = []
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== targets.origins.api) { unexpected.push(url.origin); await route.abort(); return }
    if (url.pathname === `/api/v1/cost-centers/${id}/budget` && route.request().method() === 'PUT') {
      writes.push(route.request().postDataJSON())
      await route.fulfill({ json: { amount: writes.length === 1 ? 0.01 : 0.1 } })
    } else if (url.pathname === `/cost-centers/${id}`) {
      // The mock mirrors BudgetForm's accessible controls and actual request shape, not its implementation.
      await route.fulfill({ contentType: 'text/html', body: `
        <h1>Fictional budget</h1><form aria-label="Cost center budget">
        <input aria-label="Monthly amount (USD)" id="amount" type="number" step="any">
        <input aria-label="Warn at (%)" id="thresholds">
        <textarea aria-label="Also email" id="recipients">wrong@example.invalid</textarea>
        <input role="switch" aria-label="Email the owners" type="checkbox" id="owners" checked>
        <input type="radio" name="action" id="block" aria-label="Block calls at the gateway until the budget is raised or the month ends">
        <button type="submit">Set budget</button></form>
        <script>document.querySelector('form').onsubmit = async (e) => {
          e.preventDefault();
          await fetch('/api/v1/cost-centers/${id}/budget', { method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount: Number(document.querySelector('#amount').value),
              thresholds: document.querySelector('#thresholds').value.split(',').map(Number),
              recipients: document.querySelector('#recipients').value.split(',').filter(Boolean),
              notifyOwners: document.querySelector('#owners').checked,
              action: document.querySelector('#block').checked ? 'block' : 'continue' }) });
          document.querySelector('button').textContent = 'Save budget';
        }</script>` })
    } else { unexpected.push(url.pathname); await route.abort() }
  })
  await page.goto(`${targets.origins.web}/cost-centers/${id}`)
  const budget = new CostCenterBudgetPage(page, targets, id)
  await budget.loaded('Fictional budget')
  let authorizationChecks = 0
  const authorize = () => { authorizationChecks++ }
  const block = await budget.save(0.01, authorize)
  await budget.save(0.1, authorize)
  expect(authorizationChecks).toBe(4)
  expect(Date.parse(block.savedAt)).toBeGreaterThanOrEqual(Date.parse(block.startedAt))
  expect(writes).toEqual([
    { amount: 0.01, thresholds: [80, 100], recipients: [], notifyOwners: false, action: 'block' },
    { amount: 0.1, thresholds: [80, 100], recipients: [], notifyOwners: false, action: 'block' },
  ])
  expect(unexpected).toEqual([])
})

test('a refused UI budget save fails instead of returning a success-shaped timing', async ({ page }) => {
  const targets = modelTargets()
  targets.origins.api = 'https://mosaic.invalid'
  await page.route('**/*', async (route) => {
    if (route.request().method() === 'PUT') await route.fulfill({ status: 403, json: { detail: 'Not approved' } })
    else await route.fulfill({ contentType: 'text/html', body: `<form aria-label="Cost center budget">
      <input aria-label="Monthly amount (USD)"><input aria-label="Warn at (%)"><textarea aria-label="Also email"></textarea>
      <input role="switch" aria-label="Email the owners" type="checkbox"><input type="radio" aria-label="Block calls at the gateway until the budget is raised or the month ends">
      <button>Set budget</button></form><script>document.querySelector('form').onsubmit = e => { e.preventDefault(); fetch('/api/v1/cost-centers/cc_fictional_budget/budget', { method: 'PUT' }) }</script>` })
  })
  await page.goto('https://mosaic.invalid/')
  const budget = new CostCenterBudgetPage(page, targets, 'cc_fictional_budget')
  await expect(budget.save(0.01, () => {})).rejects.toThrow(/answered 403/)
})

test('approval lost after preflight prevents the UI submit, including after form preparation', async ({ page }) => {
  const targets = modelTargets()
  targets.origins.api = 'https://mosaic.invalid'
  const scope = targets.modelJourneys!
  const preflightAt = Date.now()
  scope.price.date = new Date(preflightAt).toISOString().slice(0, 10)
  scope.approval = { reference: 'offline-only', expiresAt: new Date(preflightAt + 30_000).toISOString(),
    scopeSha256: modelScopeHash(targets), journeys: ['R12'], revealExistingKey: false, budgetWritesAcrossAllManagedGateways: true }
  const env = { MOSAIC_E2E_MODEL_SCOPE: scope.ownerTag, MOSAIC_E2E_MODEL_JOURNEY: 'R12' }
  assertModelReadiness(targets, 'R12', env, preflightAt)
  let writes = 0
  await page.route('**/*', async (route) => {
    if (route.request().method() === 'PUT') {
      writes++
      await route.fulfill({ json: {} })
    } else await route.fulfill({ contentType: 'text/html', body: `<form aria-label="Cost center budget">
      <input aria-label="Monthly amount (USD)"><input aria-label="Warn at (%)"><textarea aria-label="Also email"></textarea>
      <input role="switch" aria-label="Email the owners" type="checkbox"><input type="radio" aria-label="Block calls at the gateway until the budget is raised or the month ends">
      <button>Set budget</button></form><script>document.querySelector('form').onsubmit = e => { e.preventDefault(); fetch('/api/v1/cost-centers/${scope.budget!.grant.costCenterId}/budget', { method: 'PUT' }) }</script>` })
  })
  await page.goto('https://mosaic.invalid/')
  const budget = new CostCenterBudgetPage(page, targets, scope.budget!.grant.costCenterId)
  await expect(budget.save(0.01, () => assertModelReadiness(targets, 'R12', env, preflightAt + 30_001)))
    .rejects.toThrow(/expired/)
  await expect(page.getByLabel('Monthly amount (USD)')).toHaveValue('0.01')
  expect(writes).toBe(0)
})

test('approval expiring between the pre-click check and outgoing save blocks the gateway write', async ({ page }) => {
  const targets = modelTargets()
  targets.origins.api = 'https://mosaic.invalid'
  const scope = targets.modelJourneys!
  const preflightAt = Date.now()
  scope.price.date = new Date(preflightAt).toISOString().slice(0, 10)
  scope.approval = { reference: 'offline-only', expiresAt: new Date(preflightAt + 30_000).toISOString(),
    scopeSha256: modelScopeHash(targets), journeys: ['R14'], revealExistingKey: false, budgetWritesAcrossAllManagedGateways: true }
  const env = { MOSAIC_E2E_MODEL_SCOPE: scope.ownerTag, MOSAIC_E2E_MODEL_JOURNEY: 'R14' }
  let now = preflightAt
  let writes = 0
  await page.route('**/*', async (route) => {
    if (route.request().method() === 'PUT') {
      writes++
      await route.fulfill({ json: {} })
    } else await route.fulfill({ contentType: 'text/html', body: `<form aria-label="Cost center budget">
      <input aria-label="Monthly amount (USD)"><input aria-label="Warn at (%)"><textarea aria-label="Also email"></textarea>
      <input role="switch" aria-label="Email the owners" type="checkbox"><input type="radio" aria-label="Block calls at the gateway until the budget is raised or the month ends">
      <button>Set budget</button></form><script>document.querySelector('form').onsubmit = e => { e.preventDefault(); fetch('/api/v1/cost-centers/${scope.budget!.grant.costCenterId}/budget', { method: 'PUT' }) }</script>` })
  })
  await page.goto('https://mosaic.invalid/')
  const budget = new CostCenterBudgetPage(page, targets, scope.budget!.grant.costCenterId)
  const authorize = () => {
    assertModelReadiness(targets, 'R14', env, now)
    now = preflightAt + 30_001
  }
  await expect(budget.save(0.01, authorize)).rejects.toThrow(/expired/)
  expect(writes).toBe(0)
})
