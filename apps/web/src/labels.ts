import type {
  AiBackendKind,
  EntitlementSubjectKind,
  GrantOverlapKind,
  ManagementMode,
  PrincipalKind,
} from './types'

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

export const PRINCIPAL_KIND_LABELS: Record<PrincipalKind, string> = {
  user: 'Person',
  agentIdentity: 'Agent',
  agentUser: 'Agent user',
  securityGroup: 'Security group',
  servicePrincipal: 'Application',
  managedIdentity: 'Managed identity',
}

export const ENTITLEMENT_SUBJECT_KIND_LABELS: Record<EntitlementSubjectKind, string> = {
  user: 'Person',
  application: 'Application',
  securityGroup: 'Security group',
  group: 'MOSAIC group',
}

export const GRANT_OVERLAP_KIND_LABELS: Record<GrantOverlapKind, string> = {
  groups: 'Security groups overlap',
  directAndGroup: 'Direct grant overrides group grant',
  multipleGroups: 'Multiple security groups apply',
}

/** An Identity page tab that lists principals. The dashboard counts principals by the same tabs. */
export type PrincipalTab = 'users' | 'agents' | 'workloads'

export function principalTabForKind(kind: PrincipalKind): PrincipalTab {
  if (kind === 'user') {
    return 'users'
  }
  if (kind === 'agentIdentity' || kind === 'agentUser') {
    return 'agents'
  }
  return 'workloads'
}

export function subjectKindForPrincipal(kind: PrincipalKind): Exclude<EntitlementSubjectKind, 'group'> {
  if (kind === 'user' || kind === 'agentUser') {
    return 'user'
  }
  if (kind === 'securityGroup') {
    return 'securityGroup'
  }
  return 'application'
}
