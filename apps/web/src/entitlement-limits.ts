import type { Entitlement, ModelAccessSettings, QuotaPeriod, TokenEnforcement } from './types'

export const DEFAULT_COUNTER_KEY = '@(context.Subscription?.Key)'
export const GOVERNED_COUNTER_KEY = '@(context.Subscription.Id)'

export const QUOTA_PERIODS: QuotaPeriod[] = [
  'Hourly',
  'Daily',
  'Weekly',
  'Monthly',
  'Yearly',
]

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

export function describeLimits(
  entitlement: Pick<Entitlement, 'enforcement'>,
  publicationLimits?: TokenEnforcement | null,
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
  if (publicationLimits) {
    sentences.push(...describePublicationLimits(publicationLimits).map((limit) => `Publication: ${limit}`))
  }
  return sentences
}

export function describePublicationLimits(enforcement: TokenEnforcement): string[] {
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
