import type {
  CostCenter,
  Entitlement,
  EntitlementEnforcement,
  EntitlementResource,
  ModelAccessSettings,
  PersonLimits,
  PoolSafeguard,
  QuotaPeriod,
  TokenEnforcement,
} from './types'

export const DEFAULT_COUNTER_KEY = '@(context.Subscription?.Key)'
export const GOVERNED_COUNTER_KEY = '@(context.Subscription.Id)'

export const QUOTA_PERIODS: QuotaPeriod[] = [
  'Hourly',
  'Daily',
  'Weekly',
  'Monthly',
  'Yearly',
]

export type LimitNumberField = 'tokensPerMinute' | 'tokenQuota' | 'calls' | 'renewalPeriodSeconds' | 'callQuota'
const NUMBER_FIELDS: LimitNumberField[] = ['tokensPerMinute', 'tokenQuota', 'calls', 'renewalPeriodSeconds', 'callQuota']

/** Empty amounts mean no limit; native badInput must not masquerade as an intentional clear. */
export interface LimitForm {
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: QuotaPeriod
  calls: string
  renewalPeriodSeconds: string
  callQuota: string
  callQuotaPeriod: QuotaPeriod
  invalidNumbers?: Partial<Record<LimitNumberField, boolean>>
}

export const emptyLimitForm: LimitForm = {
  tokensPerMinute: '',
  tokenQuota: '',
  tokenQuotaPeriod: 'Monthly',
  calls: '',
  renewalPeriodSeconds: '60',
  callQuota: '',
  callQuotaPeriod: 'Monthly',
}

export function copyLimitForm(form: LimitForm): LimitForm {
  return {
    tokensPerMinute: form.tokensPerMinute,
    tokenQuota: form.tokenQuota,
    tokenQuotaPeriod: form.tokenQuotaPeriod,
    calls: form.calls,
    renewalPeriodSeconds: form.renewalPeriodSeconds,
    callQuota: form.callQuota,
    callQuotaPeriod: form.callQuotaPeriod,
    ...(form.invalidNumbers ? { invalidNumbers: { ...form.invalidNumbers } } : {}),
  }
}

export function callOnlyLimitForm(form: LimitForm): LimitForm {
  return {
    ...copyLimitForm(form), tokensPerMinute: '', tokenQuota: '',
    invalidNumbers: { ...form.invalidNumbers, tokensPerMinute: false, tokenQuota: false },
  }
}

export function limitFormsEqual(left: LimitForm, right: LimitForm): boolean {
  const fields: (keyof LimitForm)[] = [
    'tokensPerMinute', 'tokenQuota', 'tokenQuotaPeriod', 'calls',
    'renewalPeriodSeconds', 'callQuota', 'callQuotaPeriod',
  ]
  return fields.every(key => left[key] === right[key])
    && NUMBER_FIELDS.every(key => Boolean(left.invalidNumbers?.[key]) === Boolean(right.invalidNumbers?.[key]))
}

export function hasLimitValues(form: LimitForm): boolean {
  return [form.tokensPerMinute, form.tokenQuota, form.calls, form.callQuota]
    .some(value => value.trim() !== '')
    || NUMBER_FIELDS.some(key => Boolean(form.invalidNumbers?.[key]))
}

export function personLimitsFor(
  costCenter: Pick<CostCenter, 'limits'> | undefined,
  resource: Pick<EntitlementResource, 'kind' | 'id' | 'scopeId'>,
): PersonLimits | null | undefined {
  return costCenter ? costCenter.limits.find(
    limit => limit.resource.kind === resource.kind && limit.resource.id === resource.id
      && (limit.resource.scopeId ?? '') === (resource.scopeId ?? ''),
  )?.person ?? null : undefined
}

export function limitFormFromPerson(person: PersonLimits): LimitForm {
  return {
    ...emptyLimitForm,
    tokensPerMinute: person.tokensPerMinute == null ? '' : String(person.tokensPerMinute),
    tokenQuota: person.tokenQuota == null ? '' : String(person.tokenQuota),
    tokenQuotaPeriod: person.tokenQuotaPeriod ?? emptyLimitForm.tokenQuotaPeriod,
    calls: person.callsPerMinute == null ? '' : String(person.callsPerMinute),
    callQuota: person.callQuota == null ? '' : String(person.callQuota),
    callQuotaPeriod: person.callQuotaPeriod ?? emptyLimitForm.callQuotaPeriod,
  }
}

/**
 * Start a grant's limits from its publication's per-grant token limit, the only default a
 * governed resource records. Nothing else prefills, so an administrator never approves a limit
 * the resource does not already enforce.
 */
export function limitFormFrom(tokens?: TokenEnforcement | null): LimitForm {
  return {
    ...emptyLimitForm,
    tokensPerMinute: tokens?.tokensPerMinute ? String(tokens.tokensPerMinute) : '',
    tokenQuota: tokens?.tokenQuota ? String(tokens.tokenQuota) : '',
    tokenQuotaPeriod: tokens?.tokenQuotaPeriod ?? emptyLimitForm.tokenQuotaPeriod,
  }
}

export function limitFormFromEnforcement(enforcement?: EntitlementEnforcement | null): LimitForm {
  return {
    ...limitFormFrom(enforcement?.tokens),
    calls: enforcement?.requests?.calls == null ? '' : String(enforcement.requests.calls),
    renewalPeriodSeconds: String(enforcement?.requests?.renewalPeriodSeconds ?? 60),
    callQuota: enforcement?.requests?.callQuota == null ? '' : String(enforcement.requests.callQuota),
    callQuotaPeriod: enforcement?.requests?.callQuotaPeriod ?? emptyLimitForm.callQuotaPeriod,
  }
}

/** Governed model grants must count on the subscription ID the managed policy keys on. */
export function buildEnforcement(
  form: LimitForm,
  governed: boolean,
  existing?: EntitlementEnforcement | null,
): EntitlementEnforcement | null {
  const original = existing ? limitFormFromEnforcement(existing) : undefined
  if (original && limitFormsEqual(form, original)) return existing ?? null
  const error = limitInputError(form, original)
  if (error) throw new Error(error)
  const tokensPerMinute = Number(form.tokensPerMinute) || undefined
  const tokenQuota = Number(form.tokenQuota) || undefined
  const calls = Number(form.calls) || undefined
  const renewalPeriodSeconds = Number(form.renewalPeriodSeconds) || undefined
  const callQuota = Number(form.callQuota) || undefined
  const counterKeyExpression = governed ? GOVERNED_COUNTER_KEY : DEFAULT_COUNTER_KEY

  const enforcement: EntitlementEnforcement = {}
  if (tokensPerMinute || tokenQuota) {
    enforcement.tokens = {
      counterKeyExpression: existing?.tokens?.counterKeyExpression ?? counterKeyExpression,
      estimatePromptTokens: existing?.tokens?.estimatePromptTokens ?? true,
      ...(tokensPerMinute ? { tokensPerMinute } : {}),
      ...(tokenQuota ? { tokenQuota, tokenQuotaPeriod: form.tokenQuotaPeriod } : {}),
    }
  }
  if (calls || callQuota) {
    enforcement.requests = {
      counterKeyExpression: existing?.requests?.counterKeyExpression ?? counterKeyExpression,
      ...(calls ? { calls, renewalPeriodSeconds } : {}),
      ...(callQuota ? { callQuota, callQuotaPeriod: form.callQuotaPeriod } : {}),
    }
  }
  return enforcement.tokens || enforcement.requests ? enforcement : null
}

function periodPhrase(period: QuotaPeriod): string {
  switch (period) {
    case 'Hourly':
      return 'hour'
    case 'Daily':
      return 'day'
    case 'Weekly':
      return 'week'
    case 'Monthly':
      return 'month'
    default:
      return 'year'
  }
}

/** A cost center's per-person defaults as a phrase, such as "20,000 tokens per minute". */
export function describePersonLimits(person: PersonLimits): string {
  const n = (value: number) => value.toLocaleString()
  const parts: string[] = []
  if (person.tokensPerMinute) parts.push(`${n(person.tokensPerMinute)} tokens per minute`)
  if (person.tokenQuota) {
    parts.push(`${n(person.tokenQuota)} tokens per ${periodPhrase(person.tokenQuotaPeriod ?? 'Monthly')}`)
  }
  if (person.callsPerMinute) parts.push(`${n(person.callsPerMinute)} calls per minute`)
  if (person.callQuota) {
    parts.push(`${n(person.callQuota)} calls per ${periodPhrase(person.callQuotaPeriod ?? 'Monthly')}`)
  }
  return parts.join(', ')
}

/**
 * A call rate needs both halves. Half of one would otherwise be dropped when the payload is
 * built, and the grant would be created unrestricted while the UI reported success.
 */
export function callRateError(form: {
  calls: string
  renewalPeriodSeconds: string
}): string | null {
  const calls = Number(form.calls) || 0
  const renewal = Number(form.renewalPeriodSeconds) || 0
  if (calls > 0 && renewal <= 0) {
    return 'Enter how many seconds the call limit is measured over, or clear the call count.'
  }
  if (renewal > 0 && calls <= 0 && form.calls.trim() !== '') {
    return 'Enter a call count above zero, or clear it.'
  }
  return null
}

export function limitFieldErrors(
  form: LimitForm,
  original?: LimitForm,
): Partial<Record<keyof LimitForm, string>> {
  const errors: Partial<Record<keyof LimitForm, string>> = {}
  const changed = (key: keyof LimitForm) => !original || form[key] !== original[key]
  for (const key of NUMBER_FIELDS) {
    if (form.invalidNumbers?.[key]) errors[key] = 'Enter a positive whole number, or clear this field.'
  }
  for (const key of ['tokensPerMinute', 'tokenQuota', 'calls', 'callQuota'] as const) {
    if (changed(key) && form[key].trim() !== ''
      && (!Number.isSafeInteger(Number(form[key])) || Number(form[key]) <= 0)) {
      errors[key] = 'Limits must be positive whole numbers, or left empty.'
    }
  }
  if (changed('calls') || changed('renewalPeriodSeconds')) {
    const pair = callRateError(form)
    if (pair) errors.renewalPeriodSeconds = pair
    else if (form.calls.trim() !== '') {
      if (!Number.isSafeInteger(Number(form.renewalPeriodSeconds)) || Number(form.renewalPeriodSeconds) <= 0) {
        errors.renewalPeriodSeconds = 'Call renewal period must be a positive whole number.'
      } else if (Number(form.renewalPeriodSeconds) > 300) {
        errors.renewalPeriodSeconds = 'A new or changed call rate must use 1-300 seconds. Use a call quota for longer periods.'
      }
    }
  }
  for (const [quota, period] of [
    ['tokenQuota', 'tokenQuotaPeriod'], ['callQuota', 'callQuotaPeriod'],
  ] as const) {
    if ((changed(quota) || changed(period)) && form[quota].trim() !== ''
      && !QUOTA_PERIODS.some(value => value === form[period])) {
      errors[period] = `Choose a ${quota === 'callQuota' ? 'call' : 'token'} quota period.`
    }
  }
  return errors
}

export function limitInputError(form: LimitForm, original?: LimitForm): string | null {
  return Object.values(limitFieldErrors(form, original))[0] ?? null
}

export function describeLimits(
  entitlement: Pick<Entitlement, 'enforcement'>,
  publicationLimits?: TokenEnforcement | null,
  inheritedLabel = 'Publication',
): string[] {
  const enforcement = entitlement.enforcement
  const sentences: string[] = []
  const tokens = enforcement?.tokens
  if (tokens?.tokensPerMinute) {
    sentences.push(`Limits usage to ${tokens.tokensPerMinute.toLocaleString()} tokens per minute.`)
  }
  if (tokens?.tokenQuota && tokens.tokenQuotaPeriod) {
    sentences.push(
      `Allows ${tokens.tokenQuota.toLocaleString()} tokens per ` +
        `${periodPhrase(tokens.tokenQuotaPeriod)}.`,
    )
  }
  const requests = enforcement?.requests
  if (requests?.calls && requests.renewalPeriodSeconds) {
    const per =
      requests.renewalPeriodSeconds === 60
        ? 'minute'
        : requests.renewalPeriodSeconds === 3600
          ? 'hour'
          : `${requests.renewalPeriodSeconds} seconds`
    sentences.push(`Limits traffic to ${requests.calls.toLocaleString()} calls per ${per}.`)
  }
  if (requests?.callQuota && requests.callQuotaPeriod) {
    sentences.push(
      `Allows ${requests.callQuota.toLocaleString()} calls per ` +
        `${periodPhrase(requests.callQuotaPeriod)}.`,
    )
  }
  if (sentences.length === 0) {
    sentences.push('No grant-specific limit is configured.')
  }
  if (publicationLimits !== undefined) {
    sentences.push(
      ...describePublicationLimits(publicationLimits).map((limit) => `${inheritedLabel}: ${limit}`),
    )
  }
  return sentences
}

/**
 * A pool model's shared token limit, which every caller of the model shares. A gateway tier that
 * can't count the model's tokens enforces none, whatever the pool saved.
 */
export function describePoolSafeguard(
  safeguard: PoolSafeguard | TokenEnforcement | null | undefined,
  tokenMetering = true,
): string[] {
  if (!tokenMetering) return ["This gateway's tier can't count this model's tokens."]
  if (!safeguard?.tokensPerMinute && !safeguard?.tokenQuota) {
    return ['No shared token limit is set for this model.']
  }
  const sentences: string[] = []
  if (safeguard.tokensPerMinute) {
    sentences.push(
      `Every caller shares ${safeguard.tokensPerMinute.toLocaleString()} tokens per minute.`,
    )
  }
  if (safeguard.tokenQuota) {
    sentences.push(
      `Every caller shares ${safeguard.tokenQuota.toLocaleString()} tokens per ` +
        `${periodPhrase(safeguard.tokenQuotaPeriod ?? 'Monthly')}.`,
    )
  }
  return sentences
}

export function describePublicationLimits(enforcement: TokenEnforcement | null | undefined): string[] {
  if (!enforcement) {
    return ["Token limits are unavailable for this model on this gateway's tier."]
  }
  if (!enforcement.tokensPerMinute && !enforcement.tokenQuota) {
    return ['No publication token limit is configured.']
  }
  return describeLimits({ enforcement: { tokens: enforcement } })
}

export function describeAccessMethods(settings?: ModelAccessSettings | null): string {
  if (!settings) return 'Not applied'
  if (settings.keysEnabled && settings.entraEnabled) return 'Subscription key OR Entra token'
  if (settings.keysEnabled) return 'Subscription key only'
  if (settings.entraEnabled) return 'Entra token only'
  return 'Deny all — both methods disabled'
}
