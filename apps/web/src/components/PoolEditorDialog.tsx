import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Radio,
  RadioGroup,
  Select,
  Switch,
  Tab,
  TabList,
  Text,
  Textarea,
  mergeClasses,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { QUOTA_PERIODS } from '../entitlement-limits'
import { useEnvironmentCatalog } from '../environments'
import { plural } from '../labels'
import {
  BREAKER_PRESETS,
  BREAKER_PRESET_DESCRIPTIONS,
  BREAKER_PRESET_LABELS,
  DEFAULT_POOL_RETRIES,
  LINEAR_PRESET_DESCRIPTIONS,
  MAX_POOL_RETRIES,
  POOL_TYPES,
  POOL_TYPE_DESCRIPTIONS,
  POOL_TYPE_LABELS,
  POOL_VISIBILITY_LABELS,
  apiNameProblem,
  apiPathProblem,
  checkDrafts,
  defaultPoolApiName,
  defaultPoolApiPath,
  deploymentLookup,
  draftsFromPool,
  poolFamilyLabel,
  poolPublishBlocker,
  safeguardFields,
  safeguardFromFields,
  safeguardTierNote,
  specsFromDrafts,
} from '../pools'
import type { DraftPoolModel, PoolFamily, PoolPrefill, SafeguardFields } from '../pools'
import { holdsApi } from '../publication-state'
import { runtimeConfig } from '../runtime-config'
import type {
  BreakerPreset,
  Gateway,
  ModelPool,
  ModelPoolType,
  ModelPoolUpdate,
  ModelPoolVisibility,
  QuotaPeriod,
} from '../types'
import { ErrorState, Loading } from './AsyncState'
import { EnvironmentBadge } from './EnvironmentBadge'
import { PoolModelsEditor } from './PoolModelsEditor'
import { PoolTypeDiagram } from './PoolTypeDiagram'
import styles from './PoolDialogs.module.css'

type EditorStep = 'basics' | 'routing' | 'models' | 'review'

const STEPS: EditorStep[] = ['basics', 'routing', 'models', 'review']

const STEP_LABELS: Record<EditorStep, string> = {
  basics: 'Basics',
  routing: 'Routing',
  models: 'Models',
  review: 'Limits and review',
}

interface FormState {
  gatewayId: string
  displayName: string
  description: string
  /** Null until edited, so the name and path follow the pool's name. */
  apiName: string | null
  apiPath: string | null
  visibility: ModelPoolVisibility
  showCapacity: boolean
  poolType: ModelPoolType
  breakerPreset: BreakerPreset
  maxRetries: string
  safeguard: SafeguardFields
}

function initialForm(pool: ModelPool | null | undefined, prefill?: Pick<PoolPrefill, 'displayName'> | null): FormState {
  return {
    gatewayId: pool?.gatewayId ?? '',
    displayName: pool?.displayName ?? prefill?.displayName ?? '',
    description: pool?.description ?? '',
    apiName: null,
    apiPath: null,
    visibility: pool?.visibility ?? 'listed',
    showCapacity: pool?.showCapacity ?? true,
    poolType: pool?.poolType ?? 'breaker',
    breakerPreset: pool?.breakerPreset ?? 'throttling',
    maxRetries: String(pool?.maxRetries ?? DEFAULT_POOL_RETRIES),
    safeguard: safeguardFields(pool?.safeguard),
  }
}

/** The gateway a new pool starts on: the one asked for, else the first MOSAIC can publish to. */
function startingGateway(gateways: Gateway[], preferred?: string | null): Gateway | undefined {
  return (
    gateways.find((gateway) => gateway.id === preferred) ??
    gateways.find((gateway) => gateway.managementMode === 'manage' && gateway.access.canWrite) ??
    gateways[0]
  )
}

function stripSlashes(value: string): string {
  return value.trim().replace(/^\/+|\/+$/g, '')
}

export interface PoolEditorDialogProps {
  /** The pool to edit. Omit it to create one. */
  pool?: ModelPool | null
  /** The gateway a new pool starts on, such as the one the list is filtered to. */
  initialGatewayId?: string | null
  /** The name and models a new pool starts with, such as a suggested pool's. */
  prefill?: Pick<PoolPrefill, 'displayName' | 'models'> | null
  onClose: () => void
  /** Called once the pool is saved. `review` asks for its plan next. */
  onSaved: (pool: ModelPool, review: boolean) => void
}

/** Create a model pool, or change one. Saving records intent only; publishing is a reviewed plan. */
export function PoolEditorDialog({ pool, initialGatewayId, prefill, onClose, onSaved }: PoolEditorDialogProps) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [step, setStep] = useState<EditorStep>('basics')
  const [form, setForm] = useState<FormState>(() => initialForm(pool, prefill))
  const [drafts, setDrafts] = useState<DraftPoolModel[]>(() =>
    pool ? draftsFromPool(pool) : (prefill?.models ?? []),
  )
  const [attempted, setAttempted] = useState(false)
  const [stepMoves, setStepMoves] = useState(0)
  const [blockedSaves, setBlockedSaves] = useState(0)
  const tabRefs = useRef<Partial<Record<EditorStep, HTMLButtonElement | null>>>({})
  const problemsRef = useRef<HTMLDivElement>(null)
  const errorRef = useRef<HTMLDivElement>(null)

  const update = (patch: Partial<FormState>) => setForm((current) => ({ ...current, ...patch }))
  const updateSafeguard = (patch: Partial<SafeguardFields>) =>
    setForm((current) => ({ ...current, safeguard: { ...current.safeguard, ...patch } }))

  const gateways = useQuery({ queryKey: ['gateways'], queryFn: () => api.listGateways() })
  const gatewayList = gateways.data ?? []
  const gateway = pool
    ? gatewayList.find((candidate) => candidate.id === pool.gatewayId)
    : (gatewayList.find((candidate) => candidate.id === form.gatewayId) ??
      startingGateway(gatewayList, initialGatewayId))
  const gatewayId = pool?.gatewayId ?? gateway?.id ?? ''

  const candidates = useQuery({
    queryKey: ['model-pools', 'candidates', gatewayId],
    queryFn: () => api.getPoolCandidates(gatewayId),
    enabled: Boolean(gatewayId),
  })
  const typeReason = (type: ModelPoolType): string | null => candidates.data?.poolTypes[type] ?? null
  // A new pool moves off a type its gateway can't run. An existing one keeps it, and says why it can't.
  const poolType =
    !pool && typeReason(form.poolType)
      ? (POOL_TYPES.find((type) => !typeReason(type)) ?? form.poolType)
      : form.poolType

  const apiName = pool?.apiName ?? form.apiName ?? defaultPoolApiName(form.displayName)
  const apiPath = pool?.apiPath ?? form.apiPath ?? defaultPoolApiPath(form.displayName)
  const gatewayUrl = gateway?.capabilities.gatewayUrl?.replace(/\/+$/, '')
  const baseUrl = gatewayUrl ? `${gatewayUrl}/${stripSlashes(apiPath)}` : null
  const subscriptionName = pool?.subscriptionName ?? apiName.trim()
  const locked: PoolFamily | null =
    pool && holdsApi(pool) && pool.apiShape ? { vendor: pool.vendor ?? null, apiShape: pool.apiShape } : null
  const vendor = drafts[0]?.modelFormat ?? pool?.vendor ?? null
  const apiShape = drafts[0]?.apiShape ?? pool?.apiShape ?? null
  const memberCount = drafts.reduce((total, draft) => total + draft.members.length, 0)

  const retries = Number(form.maxRetries)
  const retriesValid =
    form.maxRetries.trim() !== '' && Number.isInteger(retries) && retries >= 0 && retries <= MAX_POOL_RETRIES
  const retriesProblem =
    poolType !== 'linear' && !retriesValid ? `Enter a whole number of retries from 0 to ${MAX_POOL_RETRIES}.` : null
  const displayName = form.displayName.trim()
  const nameProblem = !displayName
    ? 'Enter a name for the pool.'
    : displayName.length > 200
      ? 'Shorten the name to 200 characters or fewer.'
      : null
  const descriptionProblem =
    form.description.trim().length > 2000 ? 'Shorten the description to 2,000 characters or fewer.' : null
  const apiNameIssue = pool ? null : apiNameProblem(apiName)
  const apiPathIssue = pool ? null : apiPathProblem(apiPath)
  const safeguard = safeguardFromFields(form.safeguard)
  const unavailableType = typeReason(poolType)
  const draftCheck = checkDrafts(
    drafts,
    poolType,
    candidates.data ? deploymentLookup(candidates.data.models) : undefined,
    retriesValid ? retries : (pool?.maxRetries ?? DEFAULT_POOL_RETRIES),
  )
  const problems = [
    ...new Set([
      ...(gatewayId ? [] : ['Choose a gateway.']),
      ...(nameProblem ? [nameProblem] : []),
      ...(descriptionProblem ? [descriptionProblem] : []),
      ...(apiNameIssue ? [`API name: ${apiNameIssue}`] : []),
      ...(apiPathIssue ? [`API path: ${apiPathIssue}`] : []),
      ...(unavailableType ? [unavailableType] : []),
      ...(retriesProblem ? [retriesProblem] : []),
      ...(safeguard.problem ? [`Safeguard: ${safeguard.problem}`] : []),
      ...draftCheck.problems,
    ]),
  ]
  const tierNote = safeguard.safeguard ? safeguardTierNote(apiShape, gateway?.capabilities.skuName) : null
  const cautions = [...draftCheck.cautions, ...(tierNote ? [tierNote] : [])]
  const blocker =
    poolPublishBlocker(gateway) ?? (drafts.length === 0 ? 'Add at least one model before you publish the pool.' : null)

  const save = useMutation({
    mutationFn: async (review: boolean) => {
      const intent: ModelPoolUpdate = {
        displayName,
        description: form.description.trim() || null,
        visibility: form.visibility,
        showCapacity: form.showCapacity,
        poolType,
        breakerPreset: form.breakerPreset,
        maxRetries: retriesValid ? retries : (pool?.maxRetries ?? DEFAULT_POOL_RETRIES),
        safeguard: safeguard.safeguard,
        models: specsFromDrafts(drafts),
      }
      const saved = pool
        ? await api.updateModelPool(pool.id, intent)
        : await api.createModelPool({
            ...intent,
            displayName,
            gatewayId,
            // Left out, the API derives both from the name exactly as the defaults shown here.
            ...(form.apiName !== null ? { apiName: apiName.trim() } : {}),
            ...(form.apiPath !== null ? { apiPath: stripSlashes(apiPath) } : {}),
          })
      return { saved, review }
    },
    onSuccess: ({ saved, review }) => {
      void queryClient.invalidateQueries({ queryKey: ['model-pools'] })
      onSaved(saved, review)
    },
  })

  useEffect(() => {
    if (stepMoves) tabRefs.current[step]?.focus()
  }, [stepMoves, step])
  useEffect(() => {
    if (blockedSaves) problemsRef.current?.focus()
  }, [blockedSaves])
  useEffect(() => {
    if (save.error) errorRef.current?.focus()
  }, [save.error])

  const index = STEPS.indexOf(step)
  const move = (next: EditorStep) => {
    setStep(next)
    setStepMoves((count) => count + 1)
  }
  const submit = (review: boolean) => {
    setAttempted(true)
    if (problems.length > 0) {
      setStep('review')
      setBlockedSaves((count) => count + 1)
      return
    }
    save.mutate(review)
  }
  const shown = (problem: string | null, value: string) => (problem && (attempted || value.trim()) ? problem : undefined)

  return (
    <Dialog open onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface className={styles.surface}>
        <DialogBody>
          <DialogTitle>{pool ? `Edit ${pool.displayName}` : 'Create a model pool'}</DialogTitle>
          <DialogContent className={styles.content}>
            <div className={styles.intro}>
              <Text>
                A pool serves one vendor’s models through one API. Callers name a model, and the gateway
                chooses which deployment serves each request. They never see the endpoints behind it.
              </Text>
              {runtimeConfig.authMode === 'local' && (
                <Text size={200} className={styles.muted}>
                  Local development mode: responses may be simulated and do not prove a live APIM change.
                </Text>
              )}
            </div>
            <TabList
              className={styles.steps}
              selectedValue={step}
              onTabSelect={(_, data) => setStep(data.value as EditorStep)}
            >
              {STEPS.map((value, position) => (
                <Tab
                  key={value}
                  value={value}
                  ref={(element) => {
                    tabRefs.current[value] = element
                  }}
                >
                  {position + 1}. {STEP_LABELS[value]}
                </Tab>
              ))}
            </TabList>
            {gateways.isPending && <Loading label="Loading gateways" />}
            {gateways.isError && <ErrorState error={gateways.error} title="MOSAIC couldn’t load the gateways" />}

            {step === 'basics' && (
              <div className={styles.form}>
                {pool ? (
                  <dl className={styles.summaryList}>
                    <dt>Gateway</dt>
                    <dd>
                      {gateway?.name ?? pool.gatewayId}{' '}
                      {gateway && (
                        <EnvironmentBadge environment={gateway.environment} catalog={catalog.data} size="small" />
                      )}
                    </dd>
                    <dt>API name</dt>
                    <dd className={styles.code}>{pool.apiName}</dd>
                    <dt>Base URL</dt>
                    <dd className={styles.code}>{baseUrl ?? `/${stripSlashes(pool.apiPath)}`}</dd>
                  </dl>
                ) : (
                  <Field
                    label="Gateway"
                    required
                    hint={
                      gateway
                        ? 'The pool takes the gateway’s environment, and can use only deployments the gateway may front.'
                        : 'Register a gateway first.'
                    }
                    validationState={poolPublishBlocker(gateway) ? 'warning' : undefined}
                    validationMessage={poolPublishBlocker(gateway) ?? undefined}
                  >
                    <Select
                      value={gateway?.id ?? ''}
                      disabled={gatewayList.length === 0}
                      onChange={(event) => update({ gatewayId: event.target.value })}
                    >
                      {gatewayList.length === 0 && <option value="">No gateways registered</option>}
                      {gatewayList.map((option) => (
                        <option key={option.id} value={option.id}>
                          {option.name}
                          {option.environment ? ` (${option.environment})` : ''}
                          {poolPublishBlocker(option) ? ', drafts only' : ''}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
                <Field
                  label="Name"
                  required
                  validationMessage={shown(nameProblem, form.displayName)}
                  hint="For administrators, for example Anthropic. The portal lists the pool’s models by their own names and never names the pool."
                >
                  <Input value={form.displayName} onChange={(_, data) => update({ displayName: data.value })} />
                </Field>
                <Field
                  label="Description"
                  validationMessage={descriptionProblem ?? undefined}
                  hint="Optional. What the pool is for, and who should use it."
                >
                  <Textarea
                    resize="vertical"
                    value={form.description}
                    onChange={(_, data) => update({ description: data.value })}
                  />
                </Field>
                {!pool && (
                  <div className={styles.fieldRow}>
                    <Field
                      label="API name"
                      validationMessage={shown(apiNameIssue, apiName)}
                      hint="The pool’s API in API Management. Its product and subscription share the name."
                    >
                      <Input value={apiName} onChange={(_, data) => update({ apiName: data.value })} />
                    </Field>
                    <Field
                      label="API path"
                      validationMessage={shown(apiPathIssue, apiPath)}
                      hint={baseUrl ? `Callers send requests to ${baseUrl}.` : 'Callers add it to the gateway’s URL.'}
                    >
                      <Input value={apiPath} onChange={(_, data) => update({ apiPath: data.value })} />
                    </Field>
                  </div>
                )}
                <Field
                  label="Portal catalog"
                  hint="Listed models appear in the portal catalog once callers need a grant and the gateway serves them. Hidden ones can’t be found or requested, but anyone already granted one still connects."
                >
                  <RadioGroup
                    layout="horizontal"
                    value={form.visibility}
                    onChange={(_, data) => update({ visibility: data.value as ModelPoolVisibility })}
                  >
                    <Radio value="listed" label="Listed" />
                    <Radio value="hidden" label="Hidden" />
                  </RadioGroup>
                </Field>
                <Switch
                  checked={form.showCapacity}
                  onChange={(_, data) => update({ showCapacity: data.checked })}
                  label="Tell users whether each model runs on provisioned or pay-as-you-go capacity"
                />
              </div>
            )}

            {step === 'routing' && (
              <div className={styles.form}>
                <fieldset className={styles.fieldset}>
                  <legend className={styles.legend}>How each model chooses a deployment</legend>
                  <div className={styles.typeOptions}>
                    {POOL_TYPES.map((type) => {
                      const reason = typeReason(type)
                      const selected = poolType === type
                      return (
                        <label
                          key={type}
                          className={mergeClasses(
                            styles.typeOption,
                            selected && styles.typeOptionSelected,
                            reason ? styles.typeOptionDisabled : undefined,
                          )}
                        >
                          <input
                            type="radio"
                            name="pool-type"
                            value={type}
                            checked={selected}
                            disabled={reason != null}
                            onChange={() => update({ poolType: type })}
                          />
                          <span className={styles.typeOptionBody}>
                            <Text weight="semibold">{POOL_TYPE_LABELS[type]}</Text>
                            <PoolTypeDiagram type={type} className={styles.diagram} />
                            <Text size={200}>{POOL_TYPE_DESCRIPTIONS[type]}</Text>
                            {reason && (
                              <Text size={200} className={styles.unavailable}>
                                {reason}
                              </Text>
                            )}
                          </span>
                        </label>
                      )
                    })}
                  </div>
                </fieldset>
                <Field label="When a deployment fails">
                  <RadioGroup
                    value={form.breakerPreset}
                    onChange={(_, data) => update({ breakerPreset: data.value as BreakerPreset })}
                  >
                    {BREAKER_PRESETS.map((preset) => (
                      <Radio
                        key={preset}
                        value={preset}
                        label={
                          <span className={styles.cellStack}>
                            <span>{BREAKER_PRESET_LABELS[preset]}</span>
                            <Text size={200} className={styles.muted}>
                              {(poolType === 'linear' ? LINEAR_PRESET_DESCRIPTIONS : BREAKER_PRESET_DESCRIPTIONS)[preset]}
                            </Text>
                          </span>
                        }
                      />
                    ))}
                  </RadioGroup>
                </Field>
                {poolType === 'linear' ? (
                  <Text size={200} className={styles.muted}>
                    A linear pool tries each active deployment once, in order, so it has no retry count.
                  </Text>
                ) : (
                  <Field
                    className={styles.narrowField}
                    label="Retries"
                    validationMessage={retriesProblem ?? undefined}
                    hint={`How many times the gateway tries again after a failed attempt, from 0 to ${MAX_POOL_RETRIES}. Each attempt adds to the caller’s wait.`}
                  >
                    <Input
                      type="number"
                      min={0}
                      max={MAX_POOL_RETRIES}
                      value={form.maxRetries}
                      onChange={(_, data) => update({ maxRetries: data.value })}
                    />
                  </Field>
                )}
              </div>
            )}

            {step === 'models' && (
              <>
                {gatewayId && candidates.isPending && <Loading label="Loading model deployments" />}
                {candidates.isError && (
                  <ErrorState error={candidates.error} title="MOSAIC couldn’t load the model deployments" />
                )}
                {candidates.data && (
                  <PoolModelsEditor
                    candidates={candidates.data.models}
                    poolType={poolType}
                    drafts={drafts}
                    onChange={setDrafts}
                    poolId={pool?.id}
                    locked={locked}
                    catalog={catalog.data}
                    disabled={save.isPending}
                  />
                )}
              </>
            )}

            {step === 'review' && (
              <div className={styles.form}>
                <fieldset className={styles.fieldset}>
                  <legend className={styles.legend}>Safeguard</legend>
                  <Text size={200} className={styles.muted}>
                    A token limit every caller of a model shares, so the pool can’t send more than you plan
                    for. Each model counts separately. Leave both limits empty for none.
                  </Text>
                  <div className={styles.fieldRow}>
                    <Field label="Tokens per minute">
                      <Input
                        type="number"
                        min={1}
                        value={form.safeguard.tokensPerMinute}
                        onChange={(_, data) => updateSafeguard({ tokensPerMinute: data.value })}
                      />
                    </Field>
                    <Field label="Token quota">
                      <Input
                        type="number"
                        min={1}
                        value={form.safeguard.tokenQuota}
                        onChange={(_, data) => updateSafeguard({ tokenQuota: data.value })}
                      />
                    </Field>
                    <Field label="Quota period">
                      <Select
                        value={form.safeguard.tokenQuotaPeriod}
                        onChange={(event) =>
                          updateSafeguard({ tokenQuotaPeriod: event.target.value as QuotaPeriod | '' })
                        }
                      >
                        <option value="">None</option>
                        {QUOTA_PERIODS.map((period) => (
                          <option key={period} value={period}>
                            {period}
                          </option>
                        ))}
                      </Select>
                    </Field>
                  </div>
                  {safeguard.problem && <Text className={styles.caution}>{safeguard.problem}</Text>}
                </fieldset>

                <dl className={styles.summaryList} aria-label="Pool summary">
                  <dt>Gateway</dt>
                  <dd>
                    {gateway?.name ?? 'None chosen'}{' '}
                    {gateway && (
                      <EnvironmentBadge environment={gateway.environment} catalog={catalog.data} size="small" />
                    )}
                  </dd>
                  <dt>Serves</dt>
                  <dd>{vendor || apiShape ? poolFamilyLabel(vendor, apiShape) : 'Set by the first model you add'}</dd>
                  <dt>Models</dt>
                  <dd>
                    {plural(drafts.length, 'model')}, served by {plural(memberCount, 'deployment')}
                  </dd>
                  <dt>Routing</dt>
                  <dd>
                    {POOL_TYPE_LABELS[poolType]}, {BREAKER_PRESET_LABELS[form.breakerPreset].toLowerCase()}
                    {poolType !== 'linear' && retriesValid
                      ? `, up to ${retries} ${retries === 1 ? 'retry' : 'retries'}`
                      : ''}
                  </dd>
                  <dt>Base URL</dt>
                  <dd className={styles.code}>{baseUrl ?? `/${stripSlashes(apiPath)}`}</dd>
                  <dt>Subscription</dt>
                  <dd className={styles.code}>{subscriptionName}</dd>
                  <dt>Portal</dt>
                  <dd>
                    {POOL_VISIBILITY_LABELS[form.visibility]}.{' '}
                    {form.showCapacity ? 'Users see each model’s capacity.' : 'Users don’t see capacity.'}
                  </dd>
                </dl>

                {problems.length > 0 && (
                  <div ref={problemsRef} tabIndex={-1}>
                    <MessageBar intent="error">
                      <MessageBarBody>
                        <MessageBarTitle>Fix these before saving</MessageBarTitle>
                        <ul className={styles.findings}>
                          {problems.map((problem) => (
                            <li key={problem}>{problem}</li>
                          ))}
                        </ul>
                      </MessageBarBody>
                    </MessageBar>
                  </div>
                )}
                {cautions.length > 0 && (
                  <MessageBar intent="warning">
                    <MessageBarBody>
                      <MessageBarTitle>Before you publish</MessageBarTitle>
                      <ul className={styles.findings}>
                        {cautions.map((caution) => (
                          <li key={caution}>{caution}</li>
                        ))}
                      </ul>
                    </MessageBarBody>
                  </MessageBar>
                )}
                <MessageBar intent="info">
                  <MessageBarBody>
                    <MessageBarTitle>How callers connect</MessageBarTitle>
                    Callers use the pool’s subscription, {subscriptionName}. Once the pool is published, copy its
                    key from the gateway’s Subscriptions page in the Azure portal. Access requests, per-person
                    limits, and cost-center budgets don’t cover pools yet.
                  </MessageBarBody>
                </MessageBar>
                {pool && holdsApi(pool) && (
                  <Text size={200}>
                    Saving changes only MOSAIC’s record. The gateway keeps serving the published pool until you
                    apply a new plan.
                  </Text>
                )}
                {blocker && <Text className={styles.caution}>{blocker}</Text>}
              </div>
            )}

            {save.error && (
              <div ref={errorRef} tabIndex={-1}>
                <ErrorState error={save.error} title="MOSAIC couldn’t save the pool" />
              </div>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={onClose}>
              Cancel
            </Button>
            {index > 0 && (
              <Button appearance="secondary" onClick={() => move(STEPS[index - 1])}>
                Back
              </Button>
            )}
            {/* A browser takes focus off a button that becomes disabled, so a busy button stays focusable. */}
            {(pool || step === 'review') && (
              <Button appearance="secondary" disabledFocusable={save.isPending} onClick={() => submit(false)}>
                {save.isPending && save.variables === false ? 'Saving…' : pool ? 'Save changes' : 'Save as draft'}
              </Button>
            )}
            {step === 'review' ? (
              <Button
                appearance="primary"
                disabled={blocker != null}
                disabledFocusable={save.isPending}
                onClick={() => submit(true)}
              >
                {save.isPending && save.variables === true ? 'Saving…' : 'Save and review plan'}
              </Button>
            ) : (
              <Button appearance="primary" onClick={() => move(STEPS[index + 1])}>
                Next
              </Button>
            )}
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
