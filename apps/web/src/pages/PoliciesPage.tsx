import {
  Badge,
  Button,
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
  Title3,
  Tooltip,
} from '@fluentui/react-components'
import { ShieldTaskRegular } from '@fluentui/react-icons'
import { useMutation } from '@tanstack/react-query'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { type FormEvent, type ReactNode, useMemo, useState } from 'react'
import { useMosaicApi } from '../api'
import { PageHeader } from '../components/PageHeader'
import type { TokenEnforcement } from '../types'
import { PolicyFacetItem } from '../components/PolicyFacets'
import styles from './PoliciesPage.module.css'

type PreviewTab = 'rules' | 'metadata'
type QuotaPeriod = NonNullable<TokenEnforcement['tokenQuotaPeriod']>

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
  const sections = [
    { value: 'preview', label: 'Enforcement preview', icon: <ShieldTaskRegular />, description: 'Generate rules and metadata from the form below using the preview API. This does not save intent or change APIM.' },
  ]
  const [section, selectSection] = usePageSection(sections)
  const api = useMosaicApi()
  const [form, setForm] = useState<PolicyFormState>(initialForm)
  const [submittedForm, setSubmittedForm] = useState<PolicyFormState | null>(null)
  const [previewTab, setPreviewTab] = useState<PreviewTab>('rules')
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
  const preview = previewMutation.data
  const previewWarnings = preview?.warnings ?? []
  const validationMessages = hasSubmitted ? validation.summary : []
  const backendResourceDisplay = submittedForm?.backendResource.trim() || '—'
  const counterKeyDisplay = submittedForm?.counterKeyExpression.trim() || '—'
  const previewIsStale = preview && submittedForm && JSON.stringify(form) !== JSON.stringify(submittedForm)

  async function copyPolicyDigest() {
    if (!preview?.contentSha256) {
      return
    }

    try {
      if (!navigator.clipboard) {
        throw new Error('Clipboard access is not available in this browser context.')
      }
      await navigator.clipboard.writeText(preview.contentSha256)
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
    setSubmittedForm({ ...form })
    previewMutation.mutate(validation.payload)
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Policies"
        description="Inspect enforcement rules before making changes. This workspace previews only; publish and review model changes in Models to apply a supported one-shot plan."
        actions={
          <div className={styles.headerActions}>
            <DisabledAction hint="This workspace cannot apply policy to APIM. Publishing a model from the Models workspace is the supported way to write policy today.">
              <Button disabled aria-describedby="policies-unavailable-actions">
                Apply to APIM
              </Button>
            </DisabledAction>
          </div>
        }
      />

      <Text
        id="policies-unavailable-actions"
        size={200}
        className={styles.unavailableActionsHint}
      >
        Preview does not persist policy or apply it. Model publication applies a reviewed plan
        once, on request; this is not continuous reconciliation or policy revision history.
      </Text>

      <SectionLayout label="Policies" sections={sections} active={section} onSelect={selectSection}>
        <div className={styles.editorColumn}>
          <div className={`panel ${styles.formPanel}`}>
            <div className="panel-header">
              <div>
                <Title3 as="h2">Enforcement form</Title3>
                <Text size={200}>Preview request payload</Text>
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
                <Text size={200}>Plain-language rules and response metadata</Text>
              </div>
              <div className={styles.previewActions}>
                <Tooltip
                  content={
                    preview?.contentSha256
                      ? 'Copy the revision digest'
                      : 'Generate a live preview before copying the digest.'
                  }
                  relationship="description"
                >
                  <span className={styles.disabledAction}>
                    <Button
                      appearance="subtle"
                      disabled={!preview?.contentSha256}
                      onClick={() => void copyPolicyDigest()}
                    >
                      Copy digest
                    </Button>
                  </span>
                </Tooltip>
                <span className={styles.copyStatus} role="status" aria-live="polite">
                  {copyStatus === 'copied'
                    ? 'Copied'
                    : copyStatus === 'failed'
                      ? 'Copy failed'
                      : preview?.contentSha256
                        ? 'Ready to copy'
                        : 'No preview yet'}
                </span>
              </div>
            </div>

            <div className={styles.messageArea}>
              {previewIsStale && <MessageBar intent="warning"><MessageBarBody>The form has changed. Generate Preview again to inspect these values; the output below is the previous request.</MessageBarBody></MessageBar>}
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
                    API validation errors and warnings will appear here. Nothing is fabricated
                    when preview generation fails.
                  </Text>
                </div>
              )}
            </div>

            <TabList
              selectedValue={previewTab}
              onTabSelect={(_, data) => setPreviewTab(String(data.value) as PreviewTab)}
            >
              <Tab value="rules">Rules</Tab>
              <Tab value="metadata">Metadata</Tab>
            </TabList>

            {previewTab === 'rules' ? (
              preview?.facets.length ? (
                <ul className={styles.ruleList}>
                  {preview.facets.map((facet, index) => (
                    <PolicyFacetItem key={`${facet.element}-${index}`} facet={facet} />
                  ))}
                </ul>
              ) : (
                <div className={styles.placeholder}>
                  <Text>Generate a live preview to see the rules it would apply.</Text>
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
                  <strong>{submittedForm ? submittedForm.estimatePromptTokens ? 'Enabled' : 'Disabled' : '—'}</strong>
                </div>
              </div>
            )}
          </div>
        </div>
      </SectionLayout>
    </section>
  )
}
