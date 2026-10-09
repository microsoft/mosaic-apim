import { test, expect } from '@playwright/test'
import { CostCenterBudgetPage } from '../src/pages/console/cost-centers.ts'
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
  const block = await budget.save(0.01)
  await budget.save(0.1)
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
  await expect(budget.save(0.01)).rejects.toThrow(/answered 403/)
})
