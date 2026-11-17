import type { GatewayPolicyCapabilities, GatewayPolicyName, PolicyCapability } from '../types'

function documentedPolicy(policy: GatewayPolicyName): PolicyCapability {
  return {
    policy,
    support: 'available',
    reason: 'documentedSupport',
    message: 'Documented for this recorded gateway and model schema.',
    nextAction: 'none',
    documentationUrl: `https://learn.microsoft.com/en-us/azure/api-management/${policy}-policy`,
  }
}

export const developerChatPolicyCapabilities: GatewayPolicyCapabilities = {
  gatewayId: 'gateway_1',
  skuName: 'Developer',
  tier: 'classic',
  apiShape: 'azureOpenAi',
  policies: {
    'rate-limit-by-key': documentedPolicy('rate-limit-by-key'),
    'quota-by-key': documentedPolicy('quota-by-key'),
    'llm-token-limit': documentedPolicy('llm-token-limit'),
    'llm-emit-token-metric': documentedPolicy('llm-emit-token-metric'),
  },
}
