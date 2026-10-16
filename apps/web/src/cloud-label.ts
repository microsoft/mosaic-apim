import type { AzureCloud } from './types'

export function cloudLabel(cloud?: AzureCloud | null) {
  return cloud === 'government' ? 'Azure Government' : 'Azure Commercial'
}
