import {
  Accordion,
  AccordionHeader,
  AccordionItem,
  AccordionPanel,
  Badge,
  Button,
  Card,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  Select,
  Text,
  Textarea,
  Title3,
} from '@fluentui/react-components'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { type FormEvent, useRef, useState } from 'react'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import { runtimeConfig } from '../runtime-config'
import styles from './SupportPage.module.css'

type RuntimeEndpoint = '/healthz' | '/readyz'
type TicketCategory = '' | 'incident' | 'question' | 'access' | 'feedback'
type TicketSeverity = '' | 'low' | 'medium' | 'high' | 'critical'

interface RuntimeCheckResult {
  endpoint: RuntimeEndpoint
  status: number
  responseText: string
  checkedAt: string
}

interface SupportTicketFormState {
  category: TicketCategory
  severity: TicketSeverity
  subject: string
  description: string
}

interface DraftReceipt {
  reference: string
  category: Exclude<TicketCategory, ''>
  severity: Exclude<TicketSeverity, ''>
  subject: string
}

type TicketErrors = Partial<Record<keyof SupportTicketFormState, string>>

const documentationLinks = [
  {
    title: 'Azure API Management fundamentals',
    description: 'Review gateway policies, products, subscriptions, and operational basics.',
    href: 'https://learn.microsoft.com/azure/api-management/api-management-key-concepts',
  },
  {
    title: 'Microsoft Entra ID overview',
    description: 'Understand tenants, applications, and identity governance concepts.',
    href: 'https://learn.microsoft.com/entra/fundamentals/whatis',
  },
  {
    title: 'Application Insights overview',
    description: 'Use telemetry, distributed tracing, and alerting to monitor MOSAIC.',
    href: 'https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview',
  },
]

const faqItems = [
  {
    key: 'preview',
    question: 'Does the support form send a ticket anywhere?',
    answer:
      'No. This page prepares a local draft reference only so administrators can review the issue details before using an external support workflow.',
  },
  {
    key: 'health',
    question: 'What do the system checks validate?',
    answer:
      'MOSAIC performs anonymous GET requests to the configured API base URL /healthz and /readyz endpoints and reports the live response state without fabricating fallbacks.',
  },
  {
    key: 'config',
    question: 'Why are only a few runtime values displayed?',
    answer:
      'Support surfaces only safe configuration hints such as auth mode, API base URL, and App Insights availability. Secrets, connection details, and token material remain hidden.',
  },
]

const defaultTicket: SupportTicketFormState = {
  category: '',
  severity: '',
  subject: '',
  description: '',
}

function apiBaseUrlWithSlash(): string {
  return runtimeConfig.apiBaseUrl.endsWith('/')
    ? runtimeConfig.apiBaseUrl
    : `${runtimeConfig.apiBaseUrl}/`
}

function endpointUrl(endpoint: RuntimeEndpoint): string {
  return new URL(endpoint.slice(1), apiBaseUrlWithSlash()).toString()
}

async function fetchRuntimeCheck(endpoint: RuntimeEndpoint): Promise<RuntimeCheckResult> {
  const response = await fetch(endpointUrl(endpoint), {
    method: 'GET',
    headers: { Accept: 'text/plain' },
  })
  const responseText = (await response.text()).trim()

  if (!response.ok) {
    throw new Error(
      `${endpoint} returned ${response.status}${responseText ? `: ${responseText}` : ''}`,
    )
  }

  return {
    endpoint,
    status: response.status,
    responseText: responseText || 'OK',
    checkedAt: new Date().toISOString(),
  }
}

function validateTicket(form: SupportTicketFormState): TicketErrors {
  const errors: TicketErrors = {}

  if (!form.category) {
    errors.category = 'Choose a support category.'
  }
  if (!form.severity) {
    errors.severity = 'Choose a severity.'
  }
  if (!form.subject.trim()) {
    errors.subject = 'Enter a subject.'
  }
  if (!form.description.trim()) {
    errors.description = 'Enter a description.'
  }

  return errors
}

function renderEndpointStatus(
  label: string,
  result: UseQueryResult<RuntimeCheckResult, Error>,
) {
  let badgeLabel = 'Loading'
  let badgeClassName = `${styles.statusBadge} ${styles.loading}`
  let detail = 'Checking live runtime status...'

  if (result.isError) {
    badgeLabel = 'Error'
    badgeClassName = `${styles.statusBadge} ${styles.error}`
    detail = result.error.message
  } else if (result.data) {
    badgeLabel = 'Healthy'
    badgeClassName = `${styles.statusBadge} ${styles.healthy}`
    detail = `${result.data.status} · ${result.data.responseText}`
  }

  return (
    <div className={styles.statusRow} key={label}>
      <div>
        <Text className={styles.emphasis}>{label}</Text>
        <Text className={styles.statusDetail}>{detail}</Text>
      </div>
      <Badge appearance="tint" className={badgeClassName}>
        {badgeLabel}
      </Badge>
    </div>
  )
}

export function SupportPage() {
  const health = useQuery({
    queryKey: ['support-runtime-check', runtimeConfig.apiBaseUrl, '/healthz'],
    queryFn: () => fetchRuntimeCheck('/healthz'),
    retry: false,
  })
  const readiness = useQuery({
    queryKey: ['support-runtime-check', runtimeConfig.apiBaseUrl, '/readyz'],
    queryFn: () => fetchRuntimeCheck('/readyz'),
    retry: false,
  })
  const [ticket, setTicket] = useState<SupportTicketFormState>(defaultTicket)
  const [errors, setErrors] = useState<TicketErrors>({})
  const [draftReceipt, setDraftReceipt] = useState<DraftReceipt | null>(null)
  const draftCounter = useRef(1)

  const statusState =
    health.isPending || readiness.isPending
      ? 'loading'
      : health.isError && readiness.isError
        ? 'error'
        : health.isError || readiness.isError
          ? 'degraded'
          : 'healthy'

  function updateTicketField<Field extends keyof SupportTicketFormState>(
    field: Field,
    value: SupportTicketFormState[Field],
  ) {
    setTicket((current) => ({ ...current, [field]: value }))
    setErrors((current) => ({ ...current, [field]: undefined }))
    setDraftReceipt(null)
  }

  function submitTicket(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const nextErrors = validateTicket(ticket)
    setErrors(nextErrors)

    if (Object.keys(nextErrors).length > 0) {
      setDraftReceipt(null)
      return
    }

    const reference = `LOCAL-SUPPORT-${String(draftCounter.current).padStart(4, '0')}`
    draftCounter.current += 1

    setDraftReceipt({
      reference,
      category: ticket.category as Exclude<TicketCategory, ''>,
      severity: ticket.severity as Exclude<TicketSeverity, ''>,
      subject: ticket.subject.trim(),
    })
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Support"
        description="Access documentation, inspect live MOSAIC runtime health, and prepare local support drafts without sending production data anywhere."
        source="live"
      />
      <PreviewNotice kind="local">
        Support ticket submission on this page is local-only. Diagnostics use live anonymous health
        probes against the configured API base URL.
      </PreviewNotice>

      <div className={styles.docsGrid}>
        {documentationLinks.map((link) => (
          <Card className={styles.card} key={link.href}>
            <Title3 as="h2">{link.title}</Title3>
            <Text className={styles.cardDescription}>{link.description}</Text>
            <a
              className={styles.docLink}
              href={link.href}
              rel="noreferrer"
              target="_blank"
            >
              Open Microsoft Learn
            </a>
          </Card>
        ))}
      </div>

      <div className={styles.mainGrid}>
        <Card className={styles.card}>
          <div className={styles.sectionHeader}>
            <div>
              <Title3 as="h2">System information and status</Title3>
              <Text className={styles.cardDescription}>
                Safe runtime metadata and live health endpoint results.
              </Text>
            </div>
          </div>

          {statusState === 'loading' && (
            <MessageBar>
              <MessageBarBody>Checking /healthz and /readyz at runtime.</MessageBarBody>
            </MessageBar>
          )}
          {statusState === 'healthy' && (
            <MessageBar intent="success">
              <MessageBarBody>Both runtime checks completed successfully.</MessageBarBody>
            </MessageBar>
          )}
          {statusState === 'degraded' && (
            <MessageBar intent="warning">
              <MessageBarBody>
                The API is reachable, but one of the runtime checks is degraded or unavailable.
              </MessageBarBody>
            </MessageBar>
          )}
          {statusState === 'error' && (
            <MessageBar intent="error">
              <MessageBarBody>
                MOSAIC could not confirm runtime health. Review the endpoint errors below.
              </MessageBarBody>
            </MessageBar>
          )}

          <div className={styles.infoGrid}>
            <div className={styles.infoTile}>
              <Text className={styles.infoLabel}>Authentication mode</Text>
              <Text className={styles.emphasis}>{runtimeConfig.authMode}</Text>
            </div>
            <div className={styles.infoTile}>
              <Text className={styles.infoLabel}>API base URL</Text>
              <Text className={`${styles.wrapValue} ${styles.emphasis}`}>
                {runtimeConfig.apiBaseUrl}
              </Text>
            </div>
            <div className={styles.infoTile}>
              <Text className={styles.infoLabel}>Application Insights</Text>
              <Text className={styles.emphasis}>
                {runtimeConfig.applicationInsightsConnectionString ? 'Configured' : 'Not configured'}
              </Text>
            </div>
          </div>

          <div className={styles.statusList}>
            {renderEndpointStatus('Liveness check (/healthz)', health)}
            {renderEndpointStatus('Readiness check (/readyz)', readiness)}
          </div>
        </Card>

        <Card className={styles.card}>
          <div className={styles.sectionHeader}>
            <div>
              <Title3 as="h2">Support ticket draft</Title3>
              <Text className={styles.cardDescription}>
                Validate the request details and keep the resulting draft in local component state.
              </Text>
            </div>
          </div>
          <form className={styles.ticketForm} onSubmit={submitTicket}>
            <Field
              label="Category"
              required
              validationMessage={errors.category}
              validationState={errors.category ? 'error' : 'none'}
            >
              <Select
                aria-label="Category"
                aria-invalid={Boolean(errors.category)}
                value={ticket.category}
                onChange={(event) =>
                  updateTicketField('category', event.target.value as TicketCategory)
                }
              >
                <option value="">Select a category</option>
                <option value="incident">Incident</option>
                <option value="question">Question</option>
                <option value="access">Access request</option>
                <option value="feedback">Feedback</option>
              </Select>
            </Field>

            <Field
              label="Severity"
              required
              validationMessage={errors.severity}
              validationState={errors.severity ? 'error' : 'none'}
            >
              <Select
                aria-label="Severity"
                aria-invalid={Boolean(errors.severity)}
                value={ticket.severity}
                onChange={(event) =>
                  updateTicketField('severity', event.target.value as TicketSeverity)
                }
              >
                <option value="">Select a severity</option>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="critical">Critical</option>
              </Select>
            </Field>

            <Field
              label="Subject"
              required
              validationMessage={errors.subject}
              validationState={errors.subject ? 'error' : 'none'}
            >
              <Input
                aria-label="Subject"
                aria-invalid={Boolean(errors.subject)}
                value={ticket.subject}
                onChange={(_, data) => updateTicketField('subject', data.value)}
              />
            </Field>

            <Field
              label="Description"
              required
              validationMessage={errors.description}
              validationState={errors.description ? 'error' : 'none'}
            >
              <Textarea
                aria-label="Description"
                aria-invalid={Boolean(errors.description)}
                rows={6}
                value={ticket.description}
                onChange={(_, data) => updateTicketField('description', data.value)}
              />
            </Field>

            <div className={styles.formActions}>
              <Button appearance="primary" type="submit">
                Create local draft
              </Button>
            </div>
          </form>

          {draftReceipt && (
            <MessageBar intent="success">
              <MessageBarBody>
                Draft {draftReceipt.reference} prepared locally for {draftReceipt.category} /
                {' '}{draftReceipt.severity}. Nothing was sent to MOSAIC, Microsoft Learn, or any
                external ticketing system.
              </MessageBarBody>
            </MessageBar>
          )}
          {draftReceipt && (
            <div className={styles.receipt}>
              <Text className={styles.infoLabel}>Local draft reference</Text>
              <Text className={styles.emphasis}>{draftReceipt.reference}</Text>
              <Text className={styles.cardDescription}>{draftReceipt.subject}</Text>
            </div>
          )}
        </Card>
      </div>

      <Card className={styles.card}>
        <div className={styles.sectionHeader}>
          <div>
            <Title3 as="h2">Frequently asked questions</Title3>
            <Text className={styles.cardDescription}>
              Common guidance for this support preview experience.
            </Text>
          </div>
        </div>
        <Accordion collapsible multiple>
          {faqItems.map((item) => (
            <AccordionItem key={item.key} value={item.key}>
              <AccordionHeader>{item.question}</AccordionHeader>
              <AccordionPanel>
                <Text>{item.answer}</Text>
              </AccordionPanel>
            </AccordionItem>
          ))}
        </Accordion>
      </Card>
    </section>
  )
}
