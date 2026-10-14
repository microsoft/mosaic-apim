import { expect, test } from '@playwright/test'
import type { ApiCatalogEntry } from '../src/mosaic-api.ts'
import { CatalogPage } from '../src/pages/portal/catalog.ts'
import { MyAccessPage } from '../src/pages/portal/my-access.ts'
import { portalCatalogResourceKey } from '../src/suite.ts'
import { modelTargets } from '../tests/model-fixtures.ts'

test('grouped catalog journeys require and select the exact offering without losing the row', async ({ page }) => {
  const targets = modelTargets()
  const entry: ApiCatalogEntry = {
    kind: 'poolModel', id: 'model_fictional', scopeId: 'pool_second', displayName: 'Second offering',
    gatewayName: null, entitled: false, requestState: null, model: { name: 'Fictional model', format: 'OpenAI' },
  }
  const first = { ...entry, scopeId: 'pool_first', displayName: 'First offering' }
  await page.setContent(`
    <h1>Catalog</h1><ul class="catalog-list"><li class="resource-row"><h2>Fictional model</h2>
    <button id="open">View access options for Fictional model</button></li></ul>
    <section role="dialog" aria-label="Access to Fictional model" hidden>
      <button id="close">Close details</button><label>Access option<select id="options"></select></label>
      <label id="center-label">Cost center<select><option>Fictional research</option></select></label>
    </section>
    <script>
      const dialog = document.querySelector('[role="dialog"]');
      const options = document.querySelector('#options');
      options.add(new Option('Choose an offering', ''));
      for (const entry of ${JSON.stringify([first, entry])}) {
        options.add(new Option(entry.displayName, JSON.stringify([entry.kind, entry.id, entry.scopeId])));
      }
      document.querySelector('#open').onclick = () => { options.value = ''; dialog.hidden = false; };
      document.querySelector('#close').onclick = () => { dialog.hidden = true; };
      options.onchange = () => { document.querySelector('#center-label').firstChild.textContent =
        'Cost center for ' + options.selectedOptions[0].textContent; };
    </script>`)
  const catalog = new CatalogPage(page, targets)
  await catalog.loaded()
  const row = catalog.card('Fictional model')
  await expect(catalog.openOptions(row)).rejects.toThrow(/exact resource is required/)
  await catalog.openOptions(row, entry)
  await expect(page.getByRole('combobox', { name: 'Access option' })).toHaveValue(portalCatalogResourceKey(entry))
  await expect(catalog.costCenter(row, entry.displayName)).toBeVisible()
  await catalog.openOptions(row, first)
  await expect(page.getByRole('combobox', { name: 'Access option' })).toHaveValue(portalCatalogResourceKey(first))
  await expect(catalog.costCenter(row, first.displayName)).toBeVisible()
  await expect(catalog.cards).toHaveCount(1)
})

test('grouped access journeys select exact grants, reload drawers and preserve deep-link context', async ({ page }) => {
  const targets = modelTargets()
  const origin = 'https://mosaic.invalid'
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== origin || url.pathname !== '/access') {
      await route.abort()
      throw new Error('Unexpected network request in the offline portal journey.')
    }
    await route.fulfill({ contentType: 'text/html', body: `
      <main><h1>My access</h1><ul class="access-list"><li class="resource-row"><h2>Fictional model</h2>
      <button id="open">View access for Fictional model</button></li></ul></main>
      <section role="dialog" aria-label="Access to Fictional model" hidden>
        <button id="close">Close details</button><label>Grant<select id="grants">
          <option value="">Choose a grant</option><option value="grant_first">Research</option>
          <option value="grant_second">Operations</option></select></label>
        <div id="details" hidden><h3>Configured grant limits</h3><ul><li id="limit"></li></ul>
          <button id="connect" aria-expanded="false">Connection details</button>
          <div class="connection-panel" hidden><h3>Endpoint</h3><h3>Authentication</h3></div>
        </div>
      </section>
      <script>
        const main = document.querySelector('main'), dialog = document.querySelector('[role="dialog"]');
        const grants = document.querySelector('#grants'), details = document.querySelector('#details');
        function show(id) {
          dialog.hidden = false; main.setAttribute('aria-hidden', 'true');
          grants.value = id; details.hidden = !id;
          document.querySelector('#limit').textContent = id === 'grant_second' ? '2 calls per minute' : '1 call per minute';
          document.querySelector('.connection-panel').hidden = true;
          document.querySelector('#connect').setAttribute('aria-expanded', 'false');
        }
        document.querySelector('#open').onclick = () => show('');
        document.querySelector('#close').onclick = () => {
          dialog.hidden = true; main.removeAttribute('aria-hidden');
          const url = new URL(location.href); url.searchParams.delete('grant'); url.searchParams.delete('resource');
          history.replaceState(null, '', url);
        };
        grants.onchange = () => {
          const url = new URL(location.href); url.searchParams.set('grant', grants.value);
          url.searchParams.delete('resource'); history.replaceState(null, '', url); show(grants.value);
        };
        document.querySelector('#connect').onclick = () => {
          document.querySelector('.connection-panel').hidden = false;
          document.querySelector('#connect').setAttribute('aria-expanded', 'true');
        };
        const grant = new URL(location.href).searchParams.get('grant');
        if (['grant_first', 'grant_second'].includes(grant)) show(grant);
      </script>` })
  })
  await page.goto(`${origin}/access?from=requests`)
  const access = new MyAccessPage(page, targets)
  await access.loaded()
  const row = access.card('Fictional model')
  await expect(access.grantDetails(row)).rejects.toThrow(/exact grant ID is required/)
  await access.grantDetails(row, 'grant_second')
  await expect(row).toHaveCount(1)
  await expect(access.limits(row)).toHaveText(['2 calls per minute'])
  await access.reload()
  await expect(access.limits(row)).toHaveText(['2 calls per minute'])
  await access.grantDetails(row, 'grant_first')
  await expect(access.limits(row)).toHaveText(['1 call per minute'])
  await page.goto(`${origin}/access?from=requests&resource=fictional_group`)
  const panel = await access.connectionForGrant('grant_second')
  const url = new URL(page.url())
  expect(url.searchParams.get('grant')).toBe('grant_second')
  expect(url.searchParams.get('from')).toBe('requests')
  expect(url.searchParams.has('resource')).toBe(false)
  await expect(panel.section('Authentication')).toBeVisible()
  await expect(panel.keys.secret).toHaveCount(0)
})
