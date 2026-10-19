import type { Entitlement, McpPublication, ModelAccessSettings, ModelPool, Principal, PublishPlan, ReviewChange } from './types'
import { useEffect, useRef, useState } from 'react'

type Values = Record<string, string>
export interface EditContext {
  targetId: string
  fields: { key: string; before: string | null; after: string | null }[]
}

// Ephemeral workflow context only. Reloading falls back to the complete, unattributed pending diff.
const edits = new Map<string, { context: EditContext; savedAt: number }>()

export function rememberEdit(targetId: string, before: Values, after: Values) {
  const savedAt = Date.now()
  for (const [id, edit] of edits) {
    if (savedAt - edit.savedAt > 15 * 60_000) edits.delete(id)
  }
  edits.set(targetId, {
    savedAt,
    context: {
      targetId,
      fields: [...new Set([...Object.keys(before), ...Object.keys(after)])]
        .filter((key) => before[key] !== after[key])
        .map((key) => ({ key, before: before[key] ?? null, after: after[key] ?? null })),
    },
  })
}

export function takeEditContext(targetId: string): EditContext | undefined {
  const edit = edits.get(targetId)
  edits.delete(targetId)
  return edit && Date.now() - edit.savedAt <= 15 * 60_000 ? edit.context : undefined
}

export function creationContext(plan: PublishPlan): EditContext {
  return {
    targetId: plan.publicationId,
    fields: (plan.review?.target.fields ?? [])
      .filter((field) => !field.key.startsWith('grants/') && !field.key.startsWith('quotas/'))
      .map((field) => ({ key: field.key, before: null, after: field.value })),
  }
}

export function useEditContext(targetId?: string) {
  const [context, setContext] = useState<EditContext>()
  const consumed = useRef(false)
  useEffect(() => {
    if (!targetId || consumed.current) return
    consumed.current = true
    setContext(takeEditContext(targetId))
  }, [targetId])
  return [context, setContext] as const
}

function value(values: Values, key: string, input: string | number | boolean | null | undefined) {
  if (input != null) values[key] = typeof input === 'boolean' ? (input ? 'Enabled' : 'Disabled') : String(input)
}

export function grantReviewValues(grant: Entitlement): Values {
  const result: Values = {}
  const prefix = `grants/${grant.id}`
  value(result, `${prefix}/enabled`, grant.enabled)
  value(result, `${prefix}/limitsSource`, grant.enforcement ? 'Grant override' : 'Cost-center defaults')
  const tokens = grant.enforcement?.tokens
  const requests = grant.enforcement?.requests
  for (const key of ['tokensPerMinute', 'tokenQuota', 'tokenQuotaPeriod', 'estimatePromptTokens'] as const) {
    value(result, `${prefix}/${key}`, tokens?.[key])
  }
  for (const key of ['calls', 'renewalPeriodSeconds', 'callQuota', 'callQuotaPeriod'] as const) {
    value(result, `${prefix}/${key}`, requests?.[key])
  }
  return result
}

export function accessReviewValues(settings: ModelAccessSettings | null | undefined): Values {
  return settings ? {
    'authentication/keysEnabled': settings.keysEnabled ? 'Enabled' : 'Disabled',
    'authentication/entraEnabled': settings.entraEnabled ? 'Enabled' : 'Disabled',
  } : {}
}

export function mcpCallerReviewValues(publication: McpPublication, principals: Principal[]): Values {
  const principal = principals.find((item) => item.id === publication.modelCallerId)
  return principal ? {
    'mcp/modelCaller': principal.label || principal.objectId,
    'mcp/modelCallerObject': principal.objectId,
  } : {}
}

export function poolReviewValues(pool: ModelPool): Values {
  const result: Values = {
    'publication/displayName': pool.displayName,
    'publication/apiPath': pool.apiPath,
    'pool/poolType': pool.poolType,
    'pool/breakerPreset': pool.breakerPreset,
    'pool/maxRetries': String(pool.maxRetries),
  }
  for (const key of ['tokensPerMinute', 'tokenQuota', 'tokenQuotaPeriod'] as const) {
    value(result, `pool/safeguard/${key}`, pool.safeguard?.[key])
  }
  for (const model of pool.models) {
    const prefix = `models/${model.id}`
    value(result, `${prefix}/publicName`, model.publicName)
    value(result, `${prefix}/expectedVersion`, model.expectedVersion)
    value(result, `${prefix}/allowMixedVersions`, model.allowMixedVersions)
    model.members.forEach((member, index) => {
      const key = `${prefix}/members/${member.backendName}`
      value(result, `${key}/endpoint`, member.modelEndpointId)
      value(result, `${key}/deployment`, member.deploymentName)
      value(result, `${key}/weight`, member.weight)
      value(result, `${key}/drained`, member.drained)
      if (pool.poolType === 'linear') value(result, `${key}/order`, index + 1)
    })
  }
  return result
}

export function classifyChanges(changes: ReviewChange[], context?: EditContext) {
  const yours: ReviewChange[] = []
  const pending: ReviewChange[] = []
  const replaced = new Map<string, string>()
  for (const change of changes) {
    const edit = context?.fields.find((field) => field.key === change.key && field.after === change.after)
    if (edit) {
      yours.push(change)
      if (edit && change.previousKnown && edit.before !== change.before && edit.before !== null) {
        replaced.set(change.key, edit.before)
      }
    } else pending.push(change)
  }
  return { yours, pending, replaced }
}
