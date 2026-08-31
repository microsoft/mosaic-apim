import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  Select,
  Switch,
  Tab,
  TabList,
  Text,
  Textarea,
  Title2,
  Title3,
  Tooltip,
} from '@fluentui/react-components'
import { useMutation } from '@tanstack/react-query'
import { type FormEvent, type ReactNode, useMemo, useState } from 'react'
import { useMosaicApi } from '../api'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import type { TokenEnforcement } from '../types'
import styles from './PoliciesPage.module.css'

type PolicyStatus = 'Previewed' | 'Pending sync' | 'Attention'
type PreviewTab = 'xml' | 'metadata'
type QuotaPeriod = NonNullable<TokenEnforcement['tokenQuotaPeriod']>

interface SamplePolicyRecord {
  id: string
  name: string
  scope: string
  status: PolicyStatus
  revision: string
  environment: string
  owner: string
  lastPreviewedAt: string
  syncEnabled: boolean
  note: string
}

interface PolicyFormState {
  backendResource: string
  counterKeyExpression: string
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: '' | QuotaPeriod
  estimatePromptTokens: boolean
}

interface PolicyFormErrors {
  backendResource?: string
  counterKeyExpression?: string
  tokensPerMinute?: string
  tokenQuota?: string
  tokenQuotaPeriod?: string
}

interface ValidationResult {
  fieldErrors: PolicyFormErrors
  summary: string[]
  payload?: {
    enforcement: TokenEnforcement
    backendResource?: string
  }
}

const samplePolicies: SamplePolicyRecord[] = [
  {
    id: 'shared-chat-preview',
    name: 'Shared chat completions',
    scope: 'APIM product / shared-chat',
    status: 'Previewed',
    revision: 'rev-12',
    environment: 'MOSAIC / prod-westus3',
    owner: 'Platform security',
    lastPreviewedAt: '2026-08-13 09:48 ET',
    syncEnabled: true,
    note: 'Sample metadata only. This policy shape mirrors the current MOSAIC policy workspace.',
  },
  {
    id: 'embeddings-burst-cap',
    name: 'Embeddings burst cap',
    scope: 'APIM product / embeddings',
    status: 'Pending sync',
    revision: 'rev-7',
    environment: 'MOSAIC / prod-eastus2',
    owner: 'AI operations',
    lastPreviewedAt: '2026-08-12 17:20 ET',
    syncEnabled: false,
    note: 'Queued for future reconciliation. Save/apply is intentionally unavailable in this release.',
  },
  {
    id: 'finance-annual-quota',
    name: 'Finance annual quota',
    scope: 'APIM product / finops-assist',
    status: 'Attention',
    revision: 'rev-3',
    environment: 'MOSAIC / preprod-centralus',
    owner: 'FinOps governance',
    lastPreviewedAt: '2026-08-11 14:05 ET',
    syncEnabled: true,
    note: 'Sample status indicates human review is needed before a future APIM rollout path exists.',
  },
]

const quotaPeriods: QuotaPeriod[] = ['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly']

const initialForm: PolicyFormState = {
  backendResource: 'https://cognitiveservices.azure.com',
  counterKeyExpression: '@(context.Subscription.Id)',
  tokensPerMinute: '12000',
  tokenQuota: '500000',
  tokenQuotaPeriod: 'Monthly',
  estimatePromptTokens: true,
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The preview request failed.'
}

function parsePositiveInteger(value: string, fieldLabel: string): { value?: number; error?: string } {
  const trimmed = value.trim()
  if (!trimmed) {
    return {}
  }

  const parsed = Number(trimmed)
  if (!Number.isInteger(parsed) || parsed < 1) {
    return { error: `${fieldLabel} must be a whole number greater than 0.` }
  }

  return { value: parsed }
}

function validatePolicyForm(form: PolicyFormState): ValidationResult {
  const fieldErrors: PolicyFormErrors = {}
  const summary: string[] = []
  const backendResource = form.backendResource.trim()
  const counterKeyExpression = form.counterKeyExpression.trim()
  const tokensPerMinute = parsePositiveInteger(form.tokensPerMinute, 'Tokens per minute')
  const tokenQuota = parsePositiveInteger(form.tokenQuota, 'Token quota')

  if (!backendResource) {
    fieldErrors.backendResource = 'Backend resource is required.'
    summary.push(fieldErrors.backendResource)
  }

  if (!counterKeyExpression) {
    fieldErrors.counterKeyExpression = 'Counter key expression is required.'
    summary.push(fieldErrors.counterKeyExpression)
  }

  if (tokensPerMinute.error) {
    fieldErrors.tokensPerMinute = tokensPerMinute.error
    summary.push(tokensPerMinute.error)
  }

  if (tokenQuota.error) {
    fieldErrors.tokenQuota = tokenQuota.error
    summary.push(tokenQuota.error)
  }

  if (tokensPerMinute.value === undefined && tokenQuota.value === undefined) {
    const message = 'Configure at least one token rate or quota before generating a preview.'
    fieldErrors.tokensPerMinute = fieldErrors.tokensPerMinute ?? message
    fieldErrors.tokenQuota = fieldErrors.tokenQuota ?? message
    summary.push(message)
  }

  if (tokenQuota.value !== undefined && !form.tokenQuotaPeriod) {
    fieldErrors.tokenQuotaPeriod = 'Choose a quota period when token quota is set.'
    summary.push(fieldErrors.tokenQuotaPeriod)
  }

  if (tokenQuota.value === undefined && form.tokenQuotaPeriod) {
    fieldErrors.tokenQuota = 'Token quota is required when quota period is selected.'
    summary.push(fieldErrors.tokenQuota)
  }

  if (summary.length > 0) {
    return { fieldErrors, summary }
  }

  const tokenQuotaPeriod =
    tokenQuota.value === undefined ? undefined : (form.tokenQuotaPeriod as QuotaPeriod)

  return {
    fieldErrors,
    summary,
    payload: {
      backendResource,
      enforcement: {
        counterKeyExpression,
        tokensPerMinute: tokensPerMinute.value,
        tokenQuota: tokenQuota.value,
        tokenQuotaPeriod,
        estimatePromptTokens: form.estimatePromptTokens,
      },
    },
  }
}

function DisabledAction({
  children,
  hint,
}: {
  children: ReactNode
  hint: string
}) {
  return (
    <Tooltip content={hint} relationship="description">
      <span className={styles.disabledAction}>{children}</span>
    </Tooltip>
  )
}

export function PoliciesPage() {
  const api = useMosaicApi()
  const [selectedPolicyId, setSelectedPolicyId] = useState(samplePolicies[0]?.id ?? '')
  const [form, setForm] = useState<PolicyFormState>(initialForm)
  const [previewTab, setPreviewTab] = useState<PreviewTab>('xml')
  const [hasSubmitted, setHasSubmitted] = useState(false)
  const [generatedAt, setGeneratedAt] = useState('')
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')

  const previewMutation = useMutation({
    mutationFn: api.previewPolicy,
    onSuccess: () => {
      setGeneratedAt(new Date().toLocaleString())
      setCopyStatus('idle')
    },
  })

  const validation = useMemo(() => validatePolicyForm(form), [form])
  const selectedPolicy =
    samplePolicies.find((policy) => policy.id === selectedPolicyId) ?? samplePolicies[0]
  const preview = previewMutation.data
  const previewWarnings = preview?.warnings ?? []
  const validationMessages = hasSubmitted ? validation.summary : []
  const backendResourceDisplay = form.backendResource.trim() || '—'
  const counterKeyDisplay = form.counterKeyExpression.trim() || '—'

  async function copyPolicyXml() {
    if (!preview?.policyXml) {
      return
    }

    try {
      if (!navigator.clipboard) {
        throw new Error('Clipboard access is not available in this browser context.')
      }
      await navigator.clipboard.writeText(preview.policyXml)
      setCopyStatus('copied')
    } catch {
      setCopyStatus('failed')
    }
  }

  function updateForm<K extends keyof PolicyFormState>(key: K, value: PolicyFormState[K]) {
    setForm((current) => ({ ...current, [key]: value }))
    setCopyStatus('idle')
  }

  function submitPreview(event: FormEvent) {
    event.preventDefault()
    setHasSubmitted(true)
    if (!validation.payload) {
      return
    }
    previewMutation.mutate(validation.payload)
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Policies"
        description="Preview deterministic APIM token-enforcement XML while policy inventory, sync, save, apply, and rollback remain intentionally read-only."
        source="sample"
        actions={
          <div className={styles.headerActions}>
            <DisabledAction hint="Policy persistence is not available in this foundation release.">
              <Button disabled aria-describedby="policies-unavailable-actions">
                Save draft
              </Button>
            </DisabledAction>
            <DisabledAction hint="APIM reconciliation and rollout are not available yet.">
              <Button disabled aria-describedby="policies-unavailable-actions">
                Apply to APIM
              </Button>
            </DisabledAction>
            <DisabledAction hint="Rollback requires future revision history and apply support.">
              <Button disabled aria-describedby="policies-unavailable-actions">
                Roll back
              </Button>
            </DisabledAction>
          </div>
        }
      />

      <PreviewNotice kind="local">
        Sample policy inventory and metadata are shown below. Generate Preview calls the live,
        deterministic preview API; save, sync, apply, and rollback remain unavailable.
      </PreviewNotice>

      <Text
        id="policies-unavailable-actions"
        size={200}
        className={styles.unavailableActionsHint}
      >
        Save, sync, apply, and rollback are disabled until MOSAIC adds policy persistence and APIM
        reconciliation.
      </Text>

      <div className={styles.workspace}>
        <aside className={`panel ${styles.policyListPanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Policy list</Title3>
              <Text size={200}>Sample data</Text>
            </div>
            <Badge appearance="outline">MOSAIC sample</Badge>
          </div>
          <div className={styles.policyList}>
            {samplePolicies.map((policy) => (
              <button
                key={policy.id}
                type="button"
                className={
                  policy.id === selectedPolicy.id
                    ? `${styles.policyListItem} ${styles.policyListItemActive}`
                    : styles.policyListItem
                }
                aria-pressed={policy.id === selectedPolicy.id}
                onClick={() => setSelectedPolicyId(policy.id)}
              >
                <div className={styles.policyListRow}>
                  <strong>{policy.name}</strong>
                  <span
                    className={`${styles.statusPill} ${
                      policy.status === 'Previewed'
                        ? styles.statusPreviewed
                        : policy.status === 'Pending sync'
                          ? styles.statusPending
                          : styles.statusAttention
                    }`}
                  >
                    {policy.status}
                  </span>
                </div>
                <span>{policy.scope}</span>
                <small>{policy.revision}</small>
              </button>
            ))}
          </div>
        </aside>

        <div className={styles.editorColumn}>
          <Card className={styles.metadataCard}>
            <div className={styles.metadataHeader}>
              <div>
                <Title2 as="h2">{selectedPolicy.name}</Title2>
                <Text className={styles.sampleLabel}>Selected policy metadata · sample data</Text>
              </div>
              <Badge appearance="tint">Revision {selectedPolicy.revision}</Badge>
            </div>

            <div className={styles.metadataGrid}>
              <div>
                <span>Scope</span>
                <strong>{selectedPolicy.scope}</strong>
              </div>
              <div>
                <span>Environment</span>
                <strong>{selectedPolicy.environment}</strong>
              </div>
              <div>
                <span>Owner</span>
                <strong>{selectedPolicy.owner}</strong>
              </div>
              <div>
                <span>Last previewed</span>
                <strong>{selectedPolicy.lastPreviewedAt}</strong>
              </div>
            </div>

            <Text>{selectedPolicy.note}</Text>

            <div className={styles.syncRow}>
              <div>
                <Text className={styles.syncTitle}>Future APIM sync</Text>
                <Text size={200}>
                  Preview is live. Sync state and revision history remain sample-only until apply
                  workflows are implemented.
                </Text>
              </div>
              <DisabledAction hint="Sync will remain unavailable until reconciliation can safely write to APIM.">
                <Switch
                  checked={selectedPolicy.syncEnabled}
                  disabled
                  label="Sync enabled"
                  aria-describedby="policies-unavailable-actions"
                />
              </DisabledAction>
            </div>
          </Card>

          <div className={`panel ${styles.formPanel}`}>
            <div className="panel-header">
              <div>
                <Title3 as="h2">Enforcement form</Title3>
                <Text size={200}>Live preview request payload</Text>
              </div>
              <Badge appearance="outline">Deterministic</Badge>
            </div>
            <form className={styles.formGrid} onSubmit={submitPreview}>
              <Field label="Backend resource" required>
                <Input
                  value={form.backendResource}
                  aria-invalid={Boolean(hasSubmitted && validation.fieldErrors.backendResource)}
                  onChange={(_, data) => updateForm('backendResource', data.value)}
                />
                {hasSubmitted && validation.fieldErrors.backendResource && (
                  <Text className={styles.fieldError}>{validation.fieldErrors.backendResource}</Text>
                )}
              </Field>

              <Field label="Counter key expression" required>
                <Textarea
                  resize="vertical"
                  value={form.counterKeyExpression}
                  aria-invalid={Boolean(hasSubmitted && validation.fieldErrors.counterKeyExpression)}
                  onChange={(_, data) => updateForm('counterKeyExpression', data.value)}
                />
                {hasSubmitted && validation.fieldErrors.counterKeyExpression && (
                  <Text className={styles.fieldError}>
                    {validation.fieldErrors.counterKeyExpression}
                  </Text>
                )}
              </Field>

              <div className={styles.numericGrid}>
                <Field label="Tokens per minute">
                  <Input
                    type="number"
                    inputMode="numeric"
                    min={1}
                    value={form.tokensPerMinute}
                    aria-invalid={Boolean(hasSubmitted && validation.fieldErrors.tokensPerMinute)}
                    onChange={(_, data) => updateForm('tokensPerMinute', data.value)}
                  />
                  {hasSubmitted && validation.fieldErrors.tokensPerMinute && (
                    <Text className={styles.fieldError}>
                      {validation.fieldErrors.tokensPerMinute}
                    </Text>
                  )}
                </Field>

                <Field label="Token quota">
                  <Input
                    type="number"
                    inputMode="numeric"
                    min={1}
                    value={form.tokenQuota}
                    aria-invalid={Boolean(hasSubmitted && validation.fieldErrors.tokenQuota)}
                    onChange={(_, data) => updateForm('tokenQuota', data.value)}
                  />
                  {hasSubmitted && validation.fieldErrors.tokenQuota && (
                    <Text className={styles.fieldError}>{validation.fieldErrors.tokenQuota}</Text>
                  )}
                </Field>

                <Field label="Quota period">
                  <Select
                    value={form.tokenQuotaPeriod}
                    aria-invalid={Boolean(hasSubmitted && validation.fieldErrors.tokenQuotaPeriod)}
                    onChange={(event) =>
                      updateForm('tokenQuotaPeriod', event.target.value as PolicyFormState['tokenQuotaPeriod'])
                    }
                  >
                    <option value="">Select a period</option>
                    {quotaPeriods.map((period) => (
                      <option key={period} value={period}>
                        {period}
                      </option>
                    ))}
                  </Select>
                  {hasSubmitted && validation.fieldErrors.tokenQuotaPeriod && (
                    <Text className={styles.fieldError}>
                      {validation.fieldErrors.tokenQuotaPeriod}
                    </Text>
                  )}
                </Field>
              </div>

              <div className={styles.formFooter}>
                <Switch
                  checked={form.estimatePromptTokens}
                  label="Estimate prompt tokens"
                  onChange={(_, data) =>
                    updateForm('estimatePromptTokens', Boolean(data.checked))
                  }
                />
                <Button appearance="primary" type="submit" disabled={previewMutation.isPending}>
                  {previewMutation.isPending ? 'Generating preview…' : 'Generate Preview'}
                </Button>
              </div>
            </form>
          </div>

          <div className={`panel ${styles.previewPanel}`}>
            <div className="panel-header">
              <div>
                <Title3 as="h2">Preview output</Title3>
                <Text size={200}>Live XML and response metadata</Text>
              </div>
              <div className={styles.previewActions}>
                <Tooltip
                  content={
                    preview?.policyXml
                      ? 'Copy generated XML'
                      : 'Generate a live preview before copying XML.'
                  }
                  relationship="description"
                >
                  <span className={styles.disabledAction}>
                    <Button
                      appearance="subtle"
                      disabled={!preview?.policyXml}
                      onClick={() => void copyPolicyXml()}
                    >
                      Copy XML
                    </Button>
                  </span>
                </Tooltip>
                <span className={styles.copyStatus} role="status" aria-live="polite">
                  {copyStatus === 'copied'
                    ? 'Copied'
                    : copyStatus === 'failed'
                      ? 'Copy failed'
                      : preview?.policyXml
                        ? 'Ready to copy'
                        : 'No preview yet'}
                </span>
              </div>
            </div>

            <div className={styles.messageArea}>
              {validationMessages.length > 0 && (
                <MessageBar intent="error">
                  <MessageBarBody>
                    <ul className={styles.messageList}>
                      {validationMessages.map((message) => (
                        <li key={message}>{message}</li>
                      ))}
                    </ul>
                  </MessageBarBody>
                </MessageBar>
              )}
              {previewMutation.error && (
                <MessageBar intent="error">
                  <MessageBarBody>{getErrorMessage(previewMutation.error)}</MessageBarBody>
                </MessageBar>
              )}
              {previewWarnings.length > 0 && (
                <MessageBar intent="warning">
                  <MessageBarBody>
                    <ul className={styles.messageList}>
                      {previewWarnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  </MessageBarBody>
                </MessageBar>
              )}
              {!previewMutation.error && validationMessages.length === 0 && previewWarnings.length === 0 && (
                <div className={styles.emptyMessage}>
                  <Text size={200}>
                    API validation errors and warnings will appear here. No XML is fabricated when
                    preview generation fails.
                  </Text>
                </div>
              )}
            </div>

            <TabList
              selectedValue={previewTab}
              onTabSelect={(_, data) => setPreviewTab(String(data.value) as PreviewTab)}
            >
              <Tab value="xml">XML</Tab>
              <Tab value="metadata">Metadata</Tab>
            </TabList>

            {previewTab === 'xml' ? (
              preview?.policyXml ? (
                <pre className={styles.codeViewer}>
                  <code>{preview.policyXml}</code>
                </pre>
              ) : (
                <div className={styles.placeholder}>
                  <Text>Generate a live preview to inspect XML output.</Text>
                </div>
              )
            ) : (
              <div className={styles.previewMetadata}>
                <div>
                  <span>SHA-256 hash</span>
                  <code>{preview?.contentSha256 ?? '—'}</code>
                </div>
                <div>
                  <span>Backend resource</span>
                  <code>{backendResourceDisplay}</code>
                </div>
                <div>
                  <span>Generated at</span>
                  <strong>{generatedAt || '—'}</strong>
                </div>
                <div>
                  <span>Warnings</span>
                  <strong>{previewWarnings.length}</strong>
                </div>
                <div>
                  <span>Counter key</span>
                  <code>{counterKeyDisplay}</code>
                </div>
                <div>
                  <span>Prompt token estimation</span>
                  <strong>{form.estimatePromptTokens ? 'Enabled' : 'Disabled'}</strong>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
