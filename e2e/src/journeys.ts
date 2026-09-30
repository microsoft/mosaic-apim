import type { Locator } from '@playwright/test'
import {
  type SuiteDisposablePublication,
  type SuiteLimits,
  type SuiteModel,
  type SuiteTargets,
  type Targets,
  modelLabel,
  persona,
} from './config.ts'
import { type PersonaPool, expect, test } from './fixtures.ts'
import {
  type AccessMethods,
  type ApiEntitlement,
  type ApiGateway,
  type ApiModelApi,
  type ApiPlan,
  type ApiPublication,
  type ApiRun,
  MosaicApi,
} from './mosaic-api.ts'
import { expectOk, jsonOf } from './pages/common.ts'
import { EntitlementsPage } from './pages/console/entitlements.ts'
import { ModelsPage } from './pages/console/models.ts'
import type { PublishDialog } from './pages/console/publish-dialog.ts'
import {
  type ScopeContext,
  firstPublishProblems,
  foreignAccessChanges,
  foreignMentions,
  planScopeProblems,
  runFinished,
  runProblems,
  unpublishPlanProblems,
  unpublishProblems,
} from './plan-scope.ts'
import { done, pending, pollUntil } from './poll.ts'
import { redact } from './redact.ts'
import {
  type MethodNeeds,
  cleanupHint,
  disposableDisplayName,
  grantSettled,
  isDisposableName,
  isSuiteGrant,
  methodProblems,
  methodsLabel,
  missingSuite,
  personaPrincipal,
  suiteGateway,
  suiteNote,
  throwawayGrant,
} from './suite.ts'

/**
 * The journeys the ordered specs share: finding the suite's records, reviewing and applying a model's plan under
 * the live run's stop conditions, and publishing, resetting and removing the disposable publication. Every change
 * goes through the console, as a person's would; MOSAIC's API is only read, to check each step.
 */

/** Skips the running test. Unlike test.skip(condition, reason), TypeScript knows that nothing after it runs. */
export function skip(reason: string): never {
  test.skip(true, reason)
  throw new Error(reason)
}

/** Room for a few API Management applies in one test: each can take several minutes. */
export const applyTimeoutMs = 45 * 60_000
const runTimeoutMs = 20 * 60_000
const settleTimeoutMs = 5 * 60_000
const pollIntervalMs = 5_000

const message = (error: unknown) => (error instanceof Error ? error.message : String(error))

/** Notes something about the run in the report, redacted. */
export function note(type: string, description: string): void {
  test.info().annotations.push({ type, description: redact(description) })
}

export interface SuiteContext {
  personas: PersonaPool
  targets: Targets
  suite: SuiteTargets
  /** MOSAIC's API as the admin sees it. */
  api: MosaicApi
  gateway: ApiGateway
}

/** The manifest's suite section, its gateway, and MOSAIC as the admin sees it. Without a suite section the test is skipped. */
export async function suiteContext(personas: PersonaPool, targets: Targets): Promise<SuiteContext> {
  const suite = targets.suite ?? skip(missingSuite('gateway'))
  const api = await MosaicApi.as(personas, targets, targets.roles.admin)
  const gateway = await suiteGateway(api, suite)
  if (typeof gateway === 'string') throw new Error(gateway)
  return { personas, targets, suite, api, gateway }
}

export function requireDisposable(targets: Targets): SuiteDisposablePublication {
  return targets.suite?.disposable?.publication ?? skip(missingSuite('disposable.publication'))
}

/** Who holds a grant, as the verifier's token checks need them. */
export function holderOf(targets: Targets, personaKey: string): { personaKey: string; upn: string; objectId?: string } {
  const { upn, objectId } = persona(targets, personaKey)
  return { personaKey, upn, objectId }
}

/** The methods MOSAIC last applied for a grant, or else for its model. */
export function appliedMethods(grant: ApiEntitlement | undefined, publication: ApiPublication): AccessMethods | undefined {
  return grant?.runtime?.appliedMethods ?? publication.appliedAccess?.settings
}

export interface HeldGrant {
  label: string
  publication: ApiPublication
  modelApi: ApiModelApi
  grant: ApiEntitlement
}

/**
 * A grant the manifest says a persona already holds on a published model. Verify-only journeys read it and never
 * create it, so anything missing skips the journey with the reason.
 */
export async function heldGrant(ctx: SuiteContext, model: SuiteModel, personaKey: string): Promise<HeldGrant> {
  const label = modelLabel(model)
  const publication = await ctx.api.publicationOf(model, ctx.gateway.id)
  if (!publication || publication.status !== 'published') skip(`${label} isn't published on ${ctx.gateway.name}.`)
  const modelApi = (await ctx.api.modelApiOf(publication)) ?? skip(`${label} has no model API in MOSAIC.`)
  const principal =
    (await personaPrincipal(ctx.api, ctx.personas, ctx.targets, personaKey)) ?? skip(`MOSAIC has no principal for ${personaKey}.`)
  const [grant] = await ctx.api.grantsOf(principal.id, modelApi.id)
  if (!grant) skip(`${personaKey} holds no grant on ${label}, which the manifest expects. Grant it, review and apply it, then run this spec again.`)
  return { label, publication, modelApi, grant }
}

/** A workload's grant, which the manifest names by the workload's display name. */
export async function workloadGrant(ctx: SuiteContext, model: SuiteModel): Promise<HeldGrant> {
  const label = modelLabel(model)
  const publication = await ctx.api.publicationOf(model, ctx.gateway.id)
  if (!publication || publication.status !== 'published') skip(`${label} isn't published on ${ctx.gateway.name}.`)
  const modelApi = (await ctx.api.modelApiOf(publication)) ?? skip(`${label} has no model API in MOSAIC.`)
  const workload = (await ctx.api.workloadPrincipal()) ?? skip("MOSAIC has no principal with the manifest's workload display name.")
  const [grant] = await ctx.api.grantsOf(workload.id, modelApi.id)
  if (!grant) skip(`The workload holds no grant on ${label}, which suite.runtime.applicationGrant expects.`)
  return { label, publication, modelApi, grant }
}

// Plans and runs

export interface PlanScope extends ScopeContext {
  target: ApiPublication
  others: ApiPublication[]
  modelApi?: ApiModelApi
}

/** What a plan for this publication may touch, read fresh from MOSAIC. */
export async function scopeOf(ctx: SuiteContext, publicationId: string): Promise<PlanScope> {
  const all = await ctx.api.publications()
  const target = all.find((publication) => publication.id === publicationId) ?? (await ctx.api.publication(publicationId))
  const gateway = (await ctx.api.gateways()).find((candidate) => candidate.id === target.gatewayId)
  if (!gateway) throw new Error(`MOSAIC has no gateway for ${target.displayName}, so the suite can't tell which API Management service its plan may change.`)
  const modelApi = await ctx.api.modelApiOf(target)
  const grants = modelApi ? await ctx.api.entitlements({ resource: modelApi.id }) : []
  return {
    target,
    others: all.filter((publication) => publication.id !== target.id),
    gatewayResourceId: gateway.azureResourceId,
    targetGrantIds: new Set(
      grants.filter((grant) => grant.resource.kind === 'modelApi' && grant.resource.id === modelApi?.id).map((grant) => grant.id),
    ),
    modelApi,
  }
}

/** A dialog that shows a plan for review: the publish dialog, or the unpublish review. */
export interface PlanReview {
  readonly planSteps: Locator
  readonly planStepRows: Locator
  text(): Promise<string>
  close(): Promise<void>
}

/**
 * The live run's stop condition, before the plan runs: the plan, and everything the dialog shows, must be about
 * this model alone. Otherwise the dialog is closed without running the plan and the test fails, naming what was
 * out of scope. The button the plan would have run with is named in the failure: Apply, or Unpublish model.
 */
export async function expectPlanInScope(
  dialog: PlanReview,
  plan: ApiPlan,
  scope: PlanScope,
  extra: readonly string[],
  what: string,
  runsWith = 'Apply',
): Promise<void> {
  const problems = [...planScopeProblems(plan, scope), ...extra]
  if (plan.steps.length > 0) {
    await expect(dialog.planSteps).toBeVisible()
    const rows = await dialog.planStepRows.count()
    if (rows !== plan.steps.length) problems.push(`The review lists ${rows} step(s), and MOSAIC's plan has ${plan.steps.length}.`)
  }
  const named = foreignMentions(await dialog.text(), scope.target, scope.others)
  if (named.length > 0) problems.push(`The review names other models' resources: ${named.join(', ')}.`)
  for (const warning of plan.warnings) note('plan warning', warning)
  if (problems.length === 0) return
  await dialog.close().catch(() => undefined)
  throw new Error(redact(`Stopped before ${runsWith} on ${what}. ${problems.join(' ')}`))
}

/** Waits for a run MOSAIC started to stop, whatever its outcome. */
export function waitForRun(api: MosaicApi, publicationId: string, runId: string, what: string): Promise<ApiRun> {
  return pollUntil(
    `${what} to finish`,
    async () => {
      const run = await api.run(publicationId, runId)
      return runFinished(run.status) ? done(run) : pending(`the run is ${run.status}`)
    },
    { timeoutMs: runTimeoutMs, intervalMs: pollIntervalMs },
  )
}

export function expectRunSucceeded(run: ApiRun, what: string): void {
  const problems = runProblems(run)
  if (problems.length === 0) return
  const reported = run.errors.length > 0 ? ` MOSAIC reported: ${run.errors.join(' ')}` : ''
  throw new Error(redact(`${what} didn't apply cleanly. ${problems.join(' ')}${reported}`))
}

/** Waits for MOSAIC to report a governed model's access applied. A failed or unknown state fails at once. */
export function waitForAccessApplied(api: MosaicApi, publicationId: string, what: string): Promise<ApiPublication> {
  return pollUntil(
    `MOSAIC to report ${what} applied`,
    async () => {
      const publication = await api.publication(publicationId)
      if (publication.accessState === 'failed' || publication.accessState === 'unknown') {
        throw new Error(`MOSAIC reports ${what} as ${publication.accessState}. Recover it on the Entitlements page, then run 90-cleanup.`)
      }
      return publication.accessState === 'applied' ? done(publication) : pending(`its access is ${publication.accessState}`)
    },
    { timeoutMs: settleTimeoutMs, intervalMs: pollIntervalMs },
  )
}

/** Waits for a grant's applied state to match its intent: applied if enabled, revoked if disabled. */
export function waitForGrantSettled(api: MosaicApi, grantId: string, what: string): Promise<ApiEntitlement> {
  return pollUntil(
    `the grant in ${what} to reach its intent`,
    async () => {
      const grant = await api.entitlement(grantId)
      const status = grant.runtime?.status
      if (status === 'failed' || status === 'unknown') throw new Error(`MOSAIC reports the grant in ${what} as ${status}.`)
      return grantSettled(grant) ? done(grant) : pending(`${grant.enabled ? 'enabled' : 'disabled'} and ${status ?? 'never applied'}`)
    },
    { timeoutMs: settleTimeoutMs, intervalMs: pollIntervalMs },
  )
}

/**
 * Applies a reviewed plan and waits for its run to succeed. Returns false when MOSAIC refused a plan that went
 * stale after it was reviewed and the caller may review again; the dialog is closed either way.
 */
export async function applyReviewed(ctx: SuiteContext, dialog: PublishDialog, publicationId: string, what: string, mayRetry: boolean): Promise<boolean> {
  await expect(dialog.applyButton).toBeEnabled()
  const response = await dialog.apply()
  if (response.status() === 409 && mayRetry) {
    await expect(dialog.stalePlan).toBeVisible()
    await dialog.close()
    return false
  }
  const started = await jsonOf<ApiRun>(response, `Applying ${what}`)
  expectRunSucceeded(await waitForRun(ctx.api, publicationId, started.id, `The apply of ${what}`), what)
  await expect(dialog.appliedMessage).toBeVisible({ timeout: 60_000 })
  await dialog.close()
  return true
}

export interface ReviewOptions {
  /**
   * The suite's own grants on a model it shares with other people. A governed plan applies the whole model, so
   * the plan may change these grants and nothing else: anything else saved since the model's last apply stops
   * the journey before Apply.
   */
  ownGrants?: ReadonlySet<string>
  /** With ownGrants: the suite also owns the model's access methods. */
  methods?: boolean
  /** Grants to wait for until their applied state matches their intent. Defaults to ownGrants. */
  settle?: readonly string[]
}

/**
 * Reviews and applies a governed model's access on the Entitlements page, with "Review model changes" and "Apply
 * plan". It stops before Apply if the plan reaches beyond the model, and reviews once more if MOSAIC refuses a
 * plan that went stale. Then it waits for MOSAIC to report the model's access applied and each grant settled.
 */
export async function reviewAndApply(
  ctx: SuiteContext,
  entitlements: EntitlementsPage,
  publication: Pick<ApiPublication, 'id' | 'displayName'>,
  what: string,
  options: ReviewOptions = {},
): Promise<ApiPublication> {
  for (let attempt = 1; ; attempt += 1) {
    await entitlements.reload()
    await entitlements.selectPublication(publication.id, publication.displayName)
    await expect(entitlements.button('Review model changes')).toBeEnabled()
    const { dialog, plan: response } = await entitlements.reviewModelChanges()
    const plan = await jsonOf<ApiPlan>(response, `Planning ${what}`)
    const scope = await scopeOf(ctx, publication.id)
    const extra = options.ownGrants
      ? foreignAccessChanges(plan.accessSnapshot, scope.target.appliedAccess, options.ownGrants, { methods: options.methods })
      : []
    await expectPlanInScope(dialog, plan, scope, extra, what)
    if (plan.steps.length > 0 && plan.steps.every((step) => step.action === 'noChange')) {
      await expect(dialog.nothingToApply).toBeVisible()
      await dialog.close()
      break
    }
    if (await applyReviewed(ctx, dialog, publication.id, what, attempt === 1)) break
  }
  const applied = await waitForAccessApplied(ctx.api, publication.id, what)
  for (const grantId of options.settle ?? [...(options.ownGrants ?? [])]) await waitForGrantSettled(ctx.api, grantId, what)
  return applied
}

/** Sets the selected model's desired access methods and saves them, if they changed. Nothing is applied yet. */
export async function setMethods(entitlements: EntitlementsPage, methods: AccessMethods): Promise<void> {
  await entitlements.method('Dedicated subscription keys').setChecked(methods.keysEnabled)
  await entitlements.method('Microsoft Entra bearer tokens').setChecked(methods.entraEnabled)
  const save = entitlements.button('Save access settings')
  if (await save.isEnabled()) await expectOk(await entitlements.saveAccessSettings(), 'Saving the access settings')
  await expect(entitlements.savedMethods).toHaveText(`Saved desired methods: ${methodsLabel(methods)}`)
}

/** The grants table's badge for a grant matches what MOSAIC's API reports for it. */
export async function expectGrantBadge(ctx: SuiteContext, entitlements: EntitlementsPage, grantId: string): Promise<ApiEntitlement> {
  const grant = await ctx.api.entitlement(grantId)
  const status = grant.runtime?.status ?? skip('MOSAIC reports no runtime state for this grant, so the table shows none to compare.')
  await entitlements.reload()
  await expect(entitlements.grantState(grantId, status)).toBeVisible()
  return grant
}

// The disposable publication

const bothMethods: AccessMethods = { keysEnabled: true, entraEnabled: true }

function busy(publication: ApiPublication): boolean {
  return publication.status === 'applying' || publication.accessState === 'applying' || publication.accessState === 'unknown'
}

/** The model API MOSAIC makes when it publishes a model. Without one, nobody can be granted the model. */
async function madeModelApi(ctx: SuiteContext, publication: ApiPublication, label: string): Promise<ApiModelApi> {
  const modelApi = await ctx.api.modelApiOf(publication)
  if (!modelApi) throw new Error(`MOSAIC published ${label} but made no model API for it, so nobody can be granted it.`)
  return modelApi
}

/**
 * Publishes the disposable deployment through the publish dialog. The pre-state is checked first: the dialog
 * must offer the deployment, with no publication yet, on a gateway MOSAIC manages. The first plan must only create,
 * and only the disposable's own resources.
 */
export async function publishDisposable(ctx: SuiteContext, disposable: SuiteDisposablePublication): Promise<ApiPublication> {
  const label = modelLabel(disposable)
  const offered = await ctx.api.publishableModel(disposable, ctx.gateway.id)
  if (!offered) {
    throw new Error(
      `The publish dialog doesn't offer ${label} on ${ctx.gateway.name}. Sync ${disposable.endpoint}'s models on the Models page, or name another deployment in suite.disposable.publication.`,
    )
  }
  if (offered.publicationId) throw new Error(`MOSAIC already has a publication of ${label}. ${cleanupHint}`)
  if (!offered.publishable) throw new Error(redact(`MOSAIC won't publish ${label}: ${offered.unpublishableReason ?? 'it gave no reason'}`))
  if (offered.environmentVerdict?.level === 'blocked') throw new Error(`Environment rules block publishing ${label} to ${ctx.gateway.name}.`)
  if (ctx.gateway.managementMode !== 'manage' || !ctx.gateway.access.canWrite) {
    throw new Error(`${ctx.gateway.name} isn't in Manage mode with write access, so MOSAIC can't publish to it.`)
  }

  const models = await ModelsPage.open(ctx.personas, ctx.targets)
  const dialog = await models.openPublishDialog()
  await dialog.choose(ctx.gateway.id, disposable.deployment, offered.suggestedApiPath)
  const displayName = disposableDisplayName(disposable)
  await dialog.field('Display name').fill(displayName)
  const plan = await jsonOf<ApiPlan>(await dialog.reviewPlan(), `Planning the publish of ${label}`)
  const scope = await scopeOf(ctx, plan.publicationId)
  const extra = firstPublishProblems(plan)
  if (scope.target.displayName !== displayName) extra.push(`MOSAIC saved the publication as ${scope.target.displayName}, not ${displayName}.`)
  await expectPlanInScope(dialog, plan, scope, extra, `the publish of ${label}`)
  await applyReviewed(ctx, dialog, plan.publicationId, `the publish of ${label}`, false)

  const publication = await ctx.api.publication(plan.publicationId)
  expect(publication.status, `${label}'s status after its publish`).toBe('published')
  await models.reload()
  await expect(models.publishedRow(publication, ctx.gateway.name)).toContainText('Published')
  return publication
}

export interface DisposableState {
  publication: ApiPublication
  modelApi: ApiModelApi
}

/**
 * Brings the disposable publication to where the write journeys start: published by the suite, governed with both
 * access methods applied, and listed in the catalog. It publishes the model if it's absent and resets it if an
 * earlier run left it part done. It never touches a publication the suite didn't create.
 */
export async function ensureDisposable(ctx: SuiteContext, disposable: SuiteDisposablePublication): Promise<DisposableState> {
  const label = modelLabel(disposable)
  let publication = await ctx.api.publicationOf(disposable, ctx.gateway.id)
  if (publication && !isDisposableName(publication.displayName)) {
    throw new Error(
      `MOSAIC already publishes ${label} as ${publication.displayName}, which the suite didn't create. Name another deployment in suite.disposable.publication.`,
    )
  }
  if (publication && busy(publication)) throw new Error(`${label} is still applying, or its runtime state is unknown. ${cleanupHint}`)
  if (publication && publication.status !== 'published') {
    await cleanupDisposable(ctx, disposable)
    publication = undefined
  }
  publication ??= await publishDisposable(ctx, disposable)

  const desired = publication.governedAccess
  const applied = publication.appliedAccess?.settings
  const ready = desired?.keysEnabled && desired.entraEnabled && applied?.keysEnabled && applied.entraEnabled && publication.accessState === 'applied'
  if (!ready) {
    const entitlements = await EntitlementsPage.open(ctx.personas, ctx.targets)
    await entitlements.selectPublication(publication.id, publication.displayName)
    await setMethods(entitlements, bothMethods)
    publication = await reviewAndApply(ctx, entitlements, publication, `access to ${label}`)
  }

  const modelApi = await madeModelApi(ctx, publication, label)
  if (modelApi.visibility === 'catalog') return { publication, modelApi }
  const models = await ModelsPage.open(ctx.personas, ctx.targets)
  await expectOk(await models.setVisibility(modelApi.displayName, 'catalog'), `Listing ${label} in the catalog`)
  return { publication, modelApi: { ...modelApi, visibility: 'catalog' } }
}

/**
 * The disposable publication, published by the suite, with its access left as it is. It publishes the model if
 * it's absent, and resets it through ensureDisposable if an earlier run left it part done. A9 and A11 start
 * here, so that in a full run A11 opts in the publication A8 has just made, as a person would.
 */
export async function publishedDisposable(ctx: SuiteContext, disposable: SuiteDisposablePublication): Promise<DisposableState> {
  const label = modelLabel(disposable)
  const found = await ctx.api.publicationOf(disposable, ctx.gateway.id)
  if (found && (!isDisposableName(found.displayName) || found.status !== 'published' || busy(found))) return ensureDisposable(ctx, disposable)
  const publication = found ?? (await publishDisposable(ctx, disposable))
  return { publication, modelApi: await madeModelApi(ctx, publication, label) }
}

/** How many API Management resources every other publication has, to check that unpublishing left them alone. */
async function resourceCounts(api: MosaicApi, exceptId: string): Promise<Map<string, { name: string; count: number }>> {
  const all = await api.publications()
  return new Map(all.filter((item) => item.id !== exceptId).map((item) => [item.id, { name: item.displayName, count: item.resources.length }]))
}

async function lostResources(api: MosaicApi, before: ReadonlyMap<string, { name: string; count: number }>): Promise<string[]> {
  const after = new Map((await api.publications()).map((item) => [item.id, item.resources.length]))
  const lost: string[] = []
  for (const [id, { name, count }] of before) {
    const now = after.get(id)
    if (now !== undefined && now < count) lost.push(`${name} has ${count - now} fewer resource(s).`)
  }
  return lost
}

/**
 * Unpublishes through the unpublish review, under the live run's stop condition: before "Unpublish model" runs the
 * plan, the plan must delete everything the publication owns and nothing else, on its own gateway, and the review
 * must name no other model. If MOSAIC refuses the reviewed plan because the publication changed since, it reviews
 * once more. Returns the finished run, which must have deleted only what the publication owned.
 */
async function unpublishReviewed(ctx: SuiteContext, models: ModelsPage, publicationId: string, label: string): Promise<ApiRun> {
  const what = `the unpublish of ${label}`
  for (let attempt = 1; ; attempt += 1) {
    const publication = await ctx.api.publication(publicationId)
    if (busy(publication)) {
      throw new Error(`${label} is still applying, or its runtime state is unknown. Recover it on the Entitlements page, then run 90-cleanup again.`)
    }
    const owned = publication.resources.filter((resource) => resource.createdByMosaic)
    await models.reload()
    const { dialog, plan: response } = await models.openUnpublish(models.publishedRow(publication, ctx.gateway.name), publication.displayName)
    if (!response.ok()) await dialog.close().catch(() => undefined)
    const plan = await jsonOf<ApiPlan>(response, `Planning ${what}`)
    const scope = await scopeOf(ctx, publicationId)
    // The review says who loses access from the grants the gateway applied last, which can include one removed since.
    const applied = scope.target.appliedAccess?.grants.map((grant) => grant.entitlementId) ?? []
    const grants = new Set([...(scope.targetGrantIds ?? []), ...applied])
    await expect(dialog.review).toBeVisible()
    await expectPlanInScope(dialog, plan, { ...scope, targetGrantIds: grants }, unpublishPlanProblems(plan, owned), what, 'Unpublish model')
    await expect(dialog.confirmButton).toBeEnabled()
    const confirmed = await dialog.confirm()
    if (confirmed.status() === 409 && attempt === 1) {
      await expect(dialog.stalePlan).toBeVisible()
      await dialog.close()
      continue
    }
    const started = await jsonOf<ApiRun>(confirmed, `Unpublishing ${label}`)
    const run = await waitForRun(ctx.api, publicationId, started.id, `Unpublishing ${label}`)
    await expect(dialog.finished).toBeVisible({ timeout: 60_000 })
    await expect(dialog.runSteps).toBeVisible()
    await dialog.close()
    const problems = [...runProblems(run), ...unpublishProblems(run.steps, owned)]
    if (problems.length > 0) {
      throw new Error(redact(`Unpublishing ${label} didn't go cleanly. ${problems.join(' ')} ${run.errors.join(' ')}`.trim()))
    }
    return run
  }
}

/**
 * Removes the disposable publication and everything the suite made for it: open requests, API Management
 * resources, the publication record, grants, and the model API. It works from any state an earlier run could leave,
 * so 90-cleanup can recover the environment on its own, and does nothing if the disposable is already gone. It
 * returns what it did, for the report.
 */
export async function cleanupDisposable(ctx: SuiteContext, disposable: SuiteDisposablePublication): Promise<string[]> {
  const label = modelLabel(disposable)
  const displayName = disposableDisplayName(disposable)
  const did: string[] = []
  let publication = await ctx.api.publicationOf(disposable, ctx.gateway.id)
  if (publication && !isDisposableName(publication.displayName)) {
    throw new Error(`MOSAIC's publication of ${label} is named ${publication.displayName}, which the suite didn't create, so cleanup leaves it alone.`)
  }
  if (publication && busy(publication)) {
    throw new Error(`${label} is still applying, or its runtime state is unknown. Recover it on the Entitlements page, then run 90-cleanup again.`)
  }
  const modelApi =
    (publication ? await ctx.api.modelApiOf(publication) : undefined) ??
    (await ctx.api.modelApis()).find((item) => item.gatewayId === ctx.gateway.id && item.displayName === displayName)
  if (modelApi && !isDisposableName(modelApi.displayName)) {
    throw new Error(`The model API for ${label} is named ${modelApi.displayName}, which the suite didn't create, so cleanup leaves it alone.`)
  }

  if (modelApi) {
    const open = (await ctx.api.accessRequests('pending')).filter(
      (request) => request.resource.kind === 'modelApi' && request.resource.id === modelApi.id,
    )
    if (open.length > 0) {
      const entitlements = await EntitlementsPage.open(ctx.personas, ctx.targets)
      for (const request of open) {
        if (!request.justification) throw new Error(`An open request for ${label} has no justification, so the suite can't find its row to deny it.`)
        await expectOk(await entitlements.deny(entitlements.requestRow(request.justification)), `Denying an open request for ${label}`)
      }
      did.push(`Denied ${open.length} open access request(s) for ${label}.`)
    }
  }

  const models = await ModelsPage.open(ctx.personas, ctx.targets)
  const owned = publication?.resources.filter((resource) => resource.createdByMosaic) ?? []
  if (publication && owned.length > 0) {
    const id = publication.id
    const before = await resourceCounts(ctx.api, id)
    const run = await unpublishReviewed(ctx, models, id, label)
    const unpublished = await pollUntil(
      `${label} to own no API Management resources`,
      async () => {
        const current = await ctx.api.publication(id)
        const left = current.resources.filter((resource) => resource.createdByMosaic).length
        return current.status === 'draft' && left === 0 ? done(current) : pending(`${current.status}, owning ${left} resource(s)`)
      },
      { timeoutMs: settleTimeoutMs, intervalMs: pollIntervalMs },
    )
    const lost = await lostResources(ctx.api, before)
    if (lost.length > 0) throw new Error(`Unpublishing ${label} changed other publications. ${lost.join(' ')}`)
    await models.reload()
    await expect(models.publishedRow(unpublished, ctx.gateway.name), `${label}'s row after its unpublish`).toContainText('Unpublished')
    did.push(`Unpublished ${label}: ${run.steps.filter((step) => step.action === 'delete').length} resource(s) MOSAIC created were deleted.`)
    publication = unpublished
  }

  if (publication) {
    await models.reload()
    await expectOk(
      await models.removePublication(models.publishedRow(publication, ctx.gateway.name), publication.displayName),
      `Removing ${label}'s publication record`,
    )
    did.push(`Removed ${label}'s publication record.`)
  }

  if (modelApi) {
    const grants = (await ctx.api.entitlements({ resource: modelApi.id })).filter(
      (grant) => grant.resource.kind === 'modelApi' && grant.resource.id === modelApi.id,
    )
    if (grants.length > 0) {
      const entitlements = await EntitlementsPage.open(ctx.personas, ctx.targets)
      for (const grant of grants) await expectOk(await entitlements.remove(grant.id), `Removing a grant on ${label}`)
      did.push(`Removed ${grants.length} grant(s) on ${label}.`)
    }
    await models.reload()
    await expectOk(await models.removeModelApi(modelApi.displayName), `Removing ${label}'s model API`)
    did.push(`Removed ${label}'s model API.`)
  }

  const offered = await ctx.api.publishableModel(disposable, ctx.gateway.id)
  if (offered?.publicationId) throw new Error(`MOSAIC still has a publication of ${label} after cleanup.`)
  if ((await ctx.api.publicationOf(disposable, ctx.gateway.id)) !== undefined) throw new Error(`MOSAIC still lists ${label} after cleanup.`)
  return did
}

// Throwaway grants

export interface ThrowawaySpec {
  /** The journey the grant is for, which its note names. */
  journey: string
  model: SuiteModel
  persona: string
  limits: SuiteLimits
  /** The access methods the journey calls the model with. */
  needs: MethodNeeds
  /** The model is the suite's disposable publication: every grant on it is the suite's, and so are its methods. */
  owned?: boolean
}

export interface Throwaway {
  label: string
  publication: ApiPublication
  modelApi: ApiModelApi
  grant: ApiEntitlement
}

function reviewFor(spec: Pick<ThrowawaySpec, 'owned'>, grantId: string): ReviewOptions {
  return spec.owned ? { settle: [grantId] } : { ownGrants: new Set([grantId]) }
}

/** A grant at rest: disabled, and revoked in API Management by the last apply. */
function atRest(grant: Pick<ApiEntitlement, 'enabled' | 'runtime'>): boolean {
  return !grant.enabled && grant.runtime?.status === 'revoked'
}

/** The published model a throwaway grant goes on. Anything the journey can't use skips it with the reason. */
export async function throwawayModel(ctx: SuiteContext, spec: ThrowawaySpec): Promise<Omit<Throwaway, 'grant'>> {
  const label = modelLabel(spec.model)
  const publication = await ctx.api.publicationOf(spec.model, ctx.gateway.id)
  if (!publication || publication.status !== 'published') skip(`${label} isn't published on ${ctx.gateway.name}, so ${spec.journey} can't grant it.`)
  const modelApi = (await ctx.api.modelApiOf(publication)) ?? skip(`${label} has no model API, so ${spec.journey} can't grant it.`)
  const problems = methodProblems(publication, spec.needs)
  if (problems.length > 0) skip(`${spec.journey} calls ${label} in ways it doesn't allow now: ${problems.join(', ')}. The suite doesn't change a kept model's methods.`)
  return { label, publication, modelApi }
}

/**
 * Gives a persona the journey's throwaway grant and applies it. The suite reuses its own grant when it finds one,
 * re-enabling it if it was revoked, and adds one only if the persona has none. On a model the suite shares, the
 * plan may change nothing but that grant.
 */
export async function ensureThrowawayGrant(ctx: SuiteContext, spec: ThrowawaySpec): Promise<Throwaway> {
  const { label, publication, modelApi } = await throwawayModel(ctx, spec)
  const principal =
    (await personaPrincipal(ctx.api, ctx.personas, ctx.targets, spec.persona)) ??
    skip(`MOSAIC has no principal for ${spec.persona}. Add them on the Identity page, then run ${spec.journey} again.`)
  const decision = throwawayGrant(await ctx.api.grantsOf(principal.id, modelApi.id), spec.limits, spec.persona, { ownedModel: spec.owned })
  if (decision.action === 'skip') skip(decision.reason)

  const entitlements = await EntitlementsPage.open(ctx.personas, ctx.targets)
  let grantId: string
  if (decision.action === 'add') {
    await entitlements.selectPublication(publication.id, publication.displayName)
    const dialog = await entitlements.addDirectGrant()
    const added = await jsonOf<ApiEntitlement>(
      await dialog.grant(principal.id, spec.limits, suiteNote(spec.journey)),
      `Granting ${spec.persona} ${label}`,
    )
    grantId = added.id
  } else {
    grantId = decision.grant.id
    if (decision.action === 're-enable') await expectOk(await entitlements.reEnable(grantId), `Re-enabling ${spec.persona}'s grant on ${label}`)
  }
  if (!grantSettled(await ctx.api.entitlement(grantId))) {
    await reviewAndApply(ctx, entitlements, publication, `${spec.persona}'s grant on ${label}`, reviewFor(spec, grantId))
  }
  return { label, publication: await ctx.api.publication(publication.id), modelApi, grant: await ctx.api.entitlement(grantId) }
}

/** Puts a throwaway grant back to rest: disabled, and revoked in API Management. */
export async function restoreGrantResting(
  ctx: SuiteContext,
  publication: Pick<ApiPublication, 'id' | 'displayName'>,
  grantId: string,
  what: string,
  owned = false,
): Promise<void> {
  const grant = await ctx.api.entitlement(grantId)
  if (atRest(grant)) return
  const entitlements = await EntitlementsPage.open(ctx.personas, ctx.targets)
  if (grant.enabled) await expectOk(await entitlements.disable(grantId), `Revoking ${what}`)
  await reviewAndApply(ctx, entitlements, publication, `revoking ${what}`, reviewFor({ owned }, grantId))
}

/** Runs a proof with a throwaway grant applied, then puts the grant back to rest, even if the proof failed. */
export async function withThrowawayGrant(ctx: SuiteContext, spec: ThrowawaySpec, prove: (throwaway: Throwaway) => Promise<void>): Promise<void> {
  const throwaway = await ensureThrowawayGrant(ctx, spec)
  const what = `${spec.persona}'s grant on ${throwaway.label}`
  await restoringAfter(
    () => prove(throwaway),
    () => restoreGrantResting(ctx, throwaway.publication, throwaway.grant.id, what, spec.owned),
    `Revoking ${spec.persona}'s throwaway grant on ${throwaway.label}`,
  )
}

/**
 * Runs a journey, then restores what it changed, even if the journey failed. If both fail, the journey's error is
 * the one reported, and the restore's is noted with how to recover.
 */
export async function restoringAfter(journey: () => Promise<void>, restore: () => Promise<void>, restoring: string): Promise<void> {
  let failure: { error: unknown } | undefined
  try {
    await journey()
  } catch (error) {
    failure = { error }
  }
  try {
    await restore()
  } catch (error) {
    if (!failure) throw error
    note('cleanup', `${restoring} failed too: ${message(error)} ${cleanupHint}`)
  }
  if (failure) throw failure.error
}

/**
 * Puts every grant the suite added back to rest, for 90-cleanup. The note the suite writes finds them on any
 * model, including one the manifest no longer names after an edit. Grants the suite didn't add are left alone, and
 * so is a grant whose model isn't published, since it does nothing. Returns what it did.
 */
export async function restoreSuiteGrants(ctx: SuiteContext): Promise<string[]> {
  const awake = (await ctx.api.entitlements()).filter((grant) => isSuiteGrant(grant) && grant.resource.kind === 'modelApi' && !atRest(grant))
  if (awake.length === 0) return []
  const [modelApis, publications] = await Promise.all([ctx.api.modelApis(), ctx.api.publications()])
  const did: string[] = []
  for (const grant of awake) {
    const modelApi = modelApis.find((item) => item.id === grant.resource.id)
    const publication = publications.find(
      (item) => item.modelApiId === grant.resource.id || (modelApi?.publicationId !== undefined && item.id === modelApi.publicationId),
    )
    const name = publication?.displayName ?? modelApi?.displayName ?? 'a model API MOSAIC no longer has'
    if (publication?.status !== 'published') {
      did.push(`Left a grant the suite added on ${name} as it is: the model isn't published, so the grant does nothing.`)
      continue
    }
    const what = `a grant the suite added on ${name}`
    await restoreGrantResting(ctx, publication, grant.id, what, isDisposableName(publication.displayName))
    did.push(`Revoked ${what}.`)
  }
  return did
}
