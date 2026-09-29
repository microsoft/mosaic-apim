import type { AiBackendKind, ManagementMode } from './types'

/** A count with its noun, so a single resource reads "1 gateway" rather than "1 gateways". */
export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** Names for a gateway's management mode, shared by the gateway header and its mode control. */
export const MANAGEMENT_MODE_LABELS: Record<ManagementMode, string> = {
  observe: 'Observe',
  manage: 'Manage',
}

/** Display names for model providers, shared by every surface that shows a detection result. */
export const AI_KIND_LABELS: Record<AiBackendKind, string> = {
  azureOpenAi: 'Azure OpenAI',
  azureAiFoundry: 'Azure AI Foundry',
  azureAiInference: 'Azure AI inference',
  openAi: 'OpenAI',
  anthropic: 'Anthropic',
  googleVertex: 'Google Vertex AI',
  awsBedrock: 'AWS Bedrock',
  otherLlm: 'Model endpoint',
  none: '',
}
