import type { ApiShape, DeclaredDeploymentInput, ModelEndpoint, ModelProvider } from './types'

/** How each curated API shape is named where an administrator chooses one. */
export const API_SHAPE_LABELS: Record<ApiShape, string> = {
  azureOpenAi: 'Azure OpenAI API',
  foundryModels: 'Foundry Models API (chat completions)',
  anthropicMessages: 'Anthropic Messages API (Claude)',
}

/** The same names, short enough for a row of fields. */
export const API_SHAPE_SHORT_LABELS: Record<ApiShape, string> = {
  azureOpenAi: 'Azure OpenAI',
  foundryModels: 'Foundry Models',
  anthropicMessages: 'Anthropic Messages',
}

/**
 * Whether MOSAIC and its gateways reach this endpoint with an API key from Key Vault: an Azure AI
 * resource registered with a key, or AWS Bedrock.
 */
export function usesBackendKey(endpoint: ModelEndpoint): boolean {
  return endpoint.authMode === 'apiKey' && endpoint.provider !== 'openAiCompatible'
}

/** The resource kind an Azure AI host implies, as the API infers it, or null for any other host. */
export function keyedProvider(url: string): ModelProvider | null {
  let host: string
  try {
    host = new URL(url.trim()).hostname.toLowerCase()
  } catch {
    return null
  }
  if (host.endsWith('.openai.azure.com')) return 'azureOpenAi'
  if (host.endsWith('.services.ai.azure.com') || host.endsWith('.cognitiveservices.azure.com')) {
    return 'azureAiFoundry'
  }
  return null
}

/**
 * Whether a URL is on one of Amazon Bedrock's hosts, as the API decides it. A FIPS or control-plane
 * host counts, so the administrator is told which Bedrock host MOSAIC needs.
 */
export function isBedrockHost(url: string): boolean {
  let host: string
  try {
    host = new URL(url.trim()).hostname.toLowerCase().replace(/\.$/, '')
  } catch {
    return false
  }
  return (
    host.startsWith('bedrock') &&
    (host.endsWith('.amazonaws.com') ||
      host.endsWith('.amazonaws.com.cn') ||
      host.endsWith('.api.aws'))
  )
}

const BEDROCK_ANTHROPIC_MODEL_ID = /^(?:[a-z][a-z-]*\.)?anthropic\.(claude-[a-z0-9.:-]+)$/

/**
 * The model name Azure gives the Claude model a Bedrock model ID serves, such as
 * `claude-opus-4-5` for `us.anthropic.claude-opus-4-5-20251101-v1:0`, or '' for any other ID.
 * A Bedrock model pools beside Azure's only when its model name is the one Azure uses.
 */
export function bedrockModelName(modelId: string): string {
  const match = BEDROCK_ANTHROPIC_MODEL_ID.exec(modelId.trim().toLowerCase())
  if (!match) return ''
  return match[1].replace(/-v\d+(?::[a-z0-9]+)*$/, '').replace(/-\d{8}$/, '')
}

/**
 * Every Azure AI resource serves the Azure OpenAI API; only a Foundry resource serves the rest.
 * MOSAIC reaches AWS Bedrock only through its Anthropic Messages API.
 */
export function shapesFor(provider: ModelProvider | null): ApiShape[] {
  if (provider === 'awsBedrock') return ['anthropicMessages']
  return provider === 'azureOpenAi'
    ? ['azureOpenAi']
    : ['azureOpenAi', 'foundryModels', 'anthropicMessages']
}

/** The API a model most likely takes, until the administrator chooses one. */
export function suggestedShape(modelName: string, provider: ModelProvider | null): ApiShape {
  if (provider === 'azureOpenAi') return 'azureOpenAi'
  if (provider === 'awsBedrock') return 'anthropicMessages'
  return modelName.trim().toLowerCase().startsWith('claude') ? 'anthropicMessages' : 'foundryModels'
}

export interface DeclarationDraft {
  key: number
  deploymentName: string
  modelName: string
  apiShape: ApiShape
  shapeChosen: boolean
}

let draftKey = 0

export function blankDeclaration(provider: ModelProvider | null): DeclarationDraft {
  draftKey += 1
  return {
    key: draftKey,
    deploymentName: '',
    modelName: '',
    apiShape: suggestedShape('', provider),
    shapeChosen: false,
  }
}

/** The shape a draft publishes with: the one chosen, unless the resource can't serve it. */
export function effectiveShape(draft: DeclarationDraft, provider: ModelProvider | null): ApiShape {
  const allowed = shapesFor(provider)
  return allowed.includes(draft.apiShape) ? draft.apiShape : allowed[0]
}

/** The rows an administrator filled in, as the API takes them. Wholly empty rows are dropped. */
export function toDeclarations(
  drafts: DeclarationDraft[],
  provider: ModelProvider | null,
): DeclaredDeploymentInput[] {
  return drafts
    .filter((draft) => draft.deploymentName.trim() || draft.modelName.trim())
    .map((draft) => ({
      deploymentName: draft.deploymentName.trim(),
      modelName: draft.modelName.trim(),
      apiShape: effectiveShape(draft, provider),
    }))
}
