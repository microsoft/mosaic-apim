import {
  Accordion,
  AccordionHeader,
  AccordionItem,
  AccordionPanel,
  Badge,
  Card,
  MessageBar,
  MessageBarBody,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { PageHeader } from '../components/PageHeader'
import { runtimeConfig } from '../runtime-config'
import styles from './SupportPage.module.css'

type RuntimeEndpoint = '/healthz' | '/readyz'

interface RuntimeCheckResult {
  endpoint: RuntimeEndpoint
  status: number
  responseText: string
  checkedAt: string
}

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

  const statusState =
    health.isPending || readiness.isPending
      ? 'loading'
      : health.isError && readiness.isError
        ? 'error'
        : health.isError || readiness.isError
          ? 'degraded'
          : 'healthy'

  return (
    <section className={styles.page}>
      <PageHeader
        title="Support"
        description="Access documentation and inspect MOSAIC runtime health through anonymous diagnostics."
      />

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
            <Title3 as="h2">Frequently asked questions</Title3>
            <Text className={styles.cardDescription}>
              Guidance for runtime checks and safe configuration details.
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
