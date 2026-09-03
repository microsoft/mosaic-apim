import type { AiBackendKind } from './types'

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
