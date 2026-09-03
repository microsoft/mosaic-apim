import {
  Badge,
  Button,
  Card,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Select,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useMemo, useState } from 'react'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import styles from './AnalyticsPage.module.css'

type TimeRange = '24h' | '7d' | '30d'
type ModelName = 'gpt-4o' | 'gpt-4o-mini' | 'text-embedding-3-small' | 'mistral-large'
type ModelFilter = 'all' | ModelName
type ErrorSeverity = 'Critical' | 'Warning' | 'Info'
type IntegrationState = 'Healthy' | 'Delayed' | 'Planned'

interface TimelinePoint {
  label: string
  requestsByModel: Record<ModelName, number>
  tokensByModel: Record<ModelName, number>
}

interface GroupUsage {
  group: string
  owner: string
  tokensByModel: Record<ModelName, number>
  requestsByModel: Record<ModelName, number>
}

interface EndpointModelLatency {
  p50: number
  p95: number
  errorRate: number
}

interface EndpointLatency {
  endpoint: string
  operation: string
  byModel: Record<ModelName, EndpointModelLatency>
}

interface ErrorRecord {
  id: string
  timestamp: string
  severity: ErrorSeverity
  code: string
  message: string
  endpoint: string
  model: ModelName
  count: number
  impact: string
  correlationId: string
  remediation: string
}

interface IntegrationStatus {
  name: string
  status: IntegrationState
  detail: string
  freshness: string
}

interface RangeDataset {
  timeline: TimelinePoint[]
  estimatedCostUsdByModel: Record<ModelName, number>
  successRateByModel: Record<ModelName, number>
  groups: GroupUsage[]
  endpoints: EndpointLatency[]
  errors: ErrorRecord[]
  integrations: IntegrationStatus[]
}

interface FilteredGroupUsage {
  group: string
  owner: string
  tokens: number
  requests: number
}

interface FilteredEndpointLatency {
  endpoint: string
  operation: string
  p50: number
  p95: number
  errorRate: number
}

interface DerivedAnalytics {
  filteredTimeline: Array<{ label: string; requests: number; tokens: number }>
  filteredGroups: FilteredGroupUsage[]
  filteredEndpoints: FilteredEndpointLatency[]
  filteredErrors: ErrorRecord[]
  totalRequests: number
  totalTokens: number
  estimatedCost: number
  successRate: number
  p95Latency: number
  criticalErrors: number
}

const modelOptions: ModelFilter[] = [
  'all',
  'gpt-4o',
  'gpt-4o-mini',
  'text-embedding-3-small',
  'mistral-large',
]

const sampleDatasets: Record<TimeRange, RangeDataset> = {
  '24h': {
    timeline: [
      { label: '00:00', requestsByModel: { 'gpt-4o': 6800, 'gpt-4o-mini': 9200, 'text-embedding-3-small': 4600, 'mistral-large': 1300 }, tokensByModel: { 'gpt-4o': 4_800_000, 'gpt-4o-mini': 3_100_000, 'text-embedding-3-small': 1_400_000, 'mistral-large': 640_000 } },
      { label: '04:00', requestsByModel: { 'gpt-4o': 7600, 'gpt-4o-mini': 9800, 'text-embedding-3-small': 5200, 'mistral-large': 1500 }, tokensByModel: { 'gpt-4o': 5_300_000, 'gpt-4o-mini': 3_350_000, 'text-embedding-3-small': 1_520_000, 'mistral-large': 690_000 } },
      { label: '08:00', requestsByModel: { 'gpt-4o': 11_200, 'gpt-4o-mini': 15_600, 'text-embedding-3-small': 7600, 'mistral-large': 2100 }, tokensByModel: { 'gpt-4o': 7_900_000, 'gpt-4o-mini': 5_100_000, 'text-embedding-3-small': 2_120_000, 'mistral-large': 940_000 } },
      { label: '12:00', requestsByModel: { 'gpt-4o': 12_800, 'gpt-4o-mini': 17_100, 'text-embedding-3-small': 7900, 'mistral-large': 2600 }, tokensByModel: { 'gpt-4o': 8_700_000, 'gpt-4o-mini': 5_550_000, 'text-embedding-3-small': 2_260_000, 'mistral-large': 1_060_000 } },
      { label: '16:00', requestsByModel: { 'gpt-4o': 10_600, 'gpt-4o-mini': 14_900, 'text-embedding-3-small': 7100, 'mistral-large': 1900 }, tokensByModel: { 'gpt-4o': 7_300_000, 'gpt-4o-mini': 4_880_000, 'text-embedding-3-small': 2_060_000, 'mistral-large': 820_000 } },
      { label: '20:00', requestsByModel: { 'gpt-4o': 8400, 'gpt-4o-mini': 11_800, 'text-embedding-3-small': 5900, 'mistral-large': 1600 }, tokensByModel: { 'gpt-4o': 5_900_000, 'gpt-4o-mini': 3_920_000, 'text-embedding-3-small': 1_720_000, 'mistral-large': 700_000 } },
    ],
    estimatedCostUsdByModel: { 'gpt-4o': 4380, 'gpt-4o-mini': 1460, 'text-embedding-3-small': 540, 'mistral-large': 290 },
    successRateByModel: { 'gpt-4o': 99.42, 'gpt-4o-mini': 99.76, 'text-embedding-3-small': 99.91, 'mistral-large': 98.87 },
    groups: [
      { group: 'Customer Support', owner: 'Support engineering', tokensByModel: { 'gpt-4o': 14_800_000, 'gpt-4o-mini': 8_700_000, 'text-embedding-3-small': 3_500_000, 'mistral-large': 920_000 }, requestsByModel: { 'gpt-4o': 21_400, 'gpt-4o-mini': 26_000, 'text-embedding-3-small': 11_500, 'mistral-large': 1600 } },
      { group: 'Copilot Prototyping', owner: 'Developer platform', tokensByModel: { 'gpt-4o': 12_600_000, 'gpt-4o-mini': 9_500_000, 'text-embedding-3-small': 1_100_000, 'mistral-large': 1_820_000 }, requestsByModel: { 'gpt-4o': 18_900, 'gpt-4o-mini': 28_200, 'text-embedding-3-small': 3600, 'mistral-large': 2300 } },
      { group: 'Search Enrichment', owner: 'Knowledge systems', tokensByModel: { 'gpt-4o': 6_100_000, 'gpt-4o-mini': 5_900_000, 'text-embedding-3-small': 7_600_000, 'mistral-large': 0 }, requestsByModel: { 'gpt-4o': 8200, 'gpt-4o-mini': 12_400, 'text-embedding-3-small': 31_500, 'mistral-large': 0 } },
      { group: 'Finance Insights', owner: 'FinOps analytics', tokensByModel: { 'gpt-4o': 8_900_000, 'gpt-4o-mini': 4_700_000, 'text-embedding-3-small': 2_100_000, 'mistral-large': 1_280_000 }, requestsByModel: { 'gpt-4o': 11_500, 'gpt-4o-mini': 9200, 'text-embedding-3-small': 5100, 'mistral-large': 1500 } },
    ],
    endpoints: [
      { endpoint: '/chat/completions', operation: 'Interactive chat', byModel: { 'gpt-4o': { p50: 640, p95: 1180, errorRate: 0.42 }, 'gpt-4o-mini': { p50: 410, p95: 760, errorRate: 0.18 }, 'text-embedding-3-small': { p50: 160, p95: 280, errorRate: 0.05 }, 'mistral-large': { p50: 780, p95: 1480, errorRate: 0.93 } } },
      { endpoint: '/embeddings', operation: 'Search index updates', byModel: { 'gpt-4o': { p50: 220, p95: 410, errorRate: 0.08 }, 'gpt-4o-mini': { p50: 190, p95: 320, errorRate: 0.04 }, 'text-embedding-3-small': { p50: 110, p95: 190, errorRate: 0.02 }, 'mistral-large': { p50: 0, p95: 0, errorRate: 0 } } },
      { endpoint: '/responses', operation: 'Tool-enabled orchestration', byModel: { 'gpt-4o': { p50: 880, p95: 1710, errorRate: 0.61 }, 'gpt-4o-mini': { p50: 590, p95: 1120, errorRate: 0.27 }, 'text-embedding-3-small': { p50: 0, p95: 0, errorRate: 0 }, 'mistral-large': { p50: 1010, p95: 1860, errorRate: 1.04 } } },
    ],
    errors: [
      { id: 'err-241', timestamp: '2026-08-13 09:44 ET', severity: 'Critical', code: '429', message: 'Burst quota exceeded for Finance Insights shared key.', endpoint: '/chat/completions', model: 'gpt-4o', count: 19, impact: 'Finance Insights responses delayed for 4 minutes.', correlationId: '9ef9c946-daa7-4752-ae97-0e767e2f0c21', remediation: 'Review downstream budget window and move Finance Insights to a dedicated entitlement when apply workflows are available.' },
      { id: 'err-238', timestamp: '2026-08-13 08:17 ET', severity: 'Warning', code: '502', message: 'Gateway retry budget exhausted while calling tool-enabled orchestration.', endpoint: '/responses', model: 'mistral-large', count: 7, impact: 'Tool invocation degraded for developer prototypes.', correlationId: '1ed9f4f0-42d9-4fdd-b0d6-3d8d528d37a0', remediation: 'Validate backend retry policy and compare APIM timeout settings before enabling live rollout.' },
      { id: 'err-232', timestamp: '2026-08-13 07:06 ET', severity: 'Info', code: '401', message: 'Expired developer token rejected before backend dispatch.', endpoint: '/chat/completions', model: 'gpt-4o-mini', count: 11, impact: 'Expected auth hygiene event; no backend capacity impact.', correlationId: '2be71e9d-1888-4f48-b3c7-fb0a6bf40a18', remediation: 'Monitor for sustained growth before treating as a user-experience issue.' },
    ],
    integrations: [
      { name: 'Azure API Management', status: 'Healthy', detail: 'Gateway telemetry buffering within expected limits.', freshness: 'Updated 2 minutes ago' },
      { name: 'Azure Monitor queries', status: 'Planned', detail: 'This page uses local sample datasets; no Log Analytics query is running.', freshness: 'Not connected' },
      { name: 'Chargeback export', status: 'Delayed', detail: 'CSV export works locally, but finance system publication is not yet wired.', freshness: 'Pending rollout' },
    ],
  },
  '7d': {
    timeline: [
      { label: 'Mon', requestsByModel: { 'gpt-4o': 61_000, 'gpt-4o-mini': 78_000, 'text-embedding-3-small': 36_000, 'mistral-large': 10_500 }, tokensByModel: { 'gpt-4o': 43_000_000, 'gpt-4o-mini': 25_000_000, 'text-embedding-3-small': 10_800_000, 'mistral-large': 4_700_000 } },
      { label: 'Tue', requestsByModel: { 'gpt-4o': 58_400, 'gpt-4o-mini': 82_000, 'text-embedding-3-small': 38_000, 'mistral-large': 9800 }, tokensByModel: { 'gpt-4o': 40_600_000, 'gpt-4o-mini': 26_200_000, 'text-embedding-3-small': 11_000_000, 'mistral-large': 4_200_000 } },
      { label: 'Wed', requestsByModel: { 'gpt-4o': 64_200, 'gpt-4o-mini': 89_000, 'text-embedding-3-small': 41_300, 'mistral-large': 11_900 }, tokensByModel: { 'gpt-4o': 45_100_000, 'gpt-4o-mini': 28_900_000, 'text-embedding-3-small': 12_500_000, 'mistral-large': 5_080_000 } },
      { label: 'Thu', requestsByModel: { 'gpt-4o': 67_000, 'gpt-4o-mini': 92_400, 'text-embedding-3-small': 42_500, 'mistral-large': 12_800 }, tokensByModel: { 'gpt-4o': 47_200_000, 'gpt-4o-mini': 29_600_000, 'text-embedding-3-small': 13_100_000, 'mistral-large': 5_420_000 } },
      { label: 'Fri', requestsByModel: { 'gpt-4o': 70_500, 'gpt-4o-mini': 95_000, 'text-embedding-3-small': 44_100, 'mistral-large': 13_600 }, tokensByModel: { 'gpt-4o': 50_100_000, 'gpt-4o-mini': 30_100_000, 'text-embedding-3-small': 13_600_000, 'mistral-large': 5_930_000 } },
      { label: 'Sat', requestsByModel: { 'gpt-4o': 54_100, 'gpt-4o-mini': 74_200, 'text-embedding-3-small': 33_400, 'mistral-large': 8800 }, tokensByModel: { 'gpt-4o': 38_200_000, 'gpt-4o-mini': 23_400_000, 'text-embedding-3-small': 9_900_000, 'mistral-large': 3_900_000 } },
      { label: 'Sun', requestsByModel: { 'gpt-4o': 51_600, 'gpt-4o-mini': 71_400, 'text-embedding-3-small': 31_800, 'mistral-large': 8200 }, tokensByModel: { 'gpt-4o': 36_400_000, 'gpt-4o-mini': 22_800_000, 'text-embedding-3-small': 9_400_000, 'mistral-large': 3_600_000 } },
    ],
    estimatedCostUsdByModel: { 'gpt-4o': 28_240, 'gpt-4o-mini': 9360, 'text-embedding-3-small': 3120, 'mistral-large': 1760 },
    successRateByModel: { 'gpt-4o': 99.21, 'gpt-4o-mini': 99.68, 'text-embedding-3-small': 99.9, 'mistral-large': 98.55 },
    groups: [
      { group: 'Customer Support', owner: 'Support engineering', tokensByModel: { 'gpt-4o': 83_000_000, 'gpt-4o-mini': 51_000_000, 'text-embedding-3-small': 20_000_000, 'mistral-large': 5_400_000 }, requestsByModel: { 'gpt-4o': 120_000, 'gpt-4o-mini': 152_000, 'text-embedding-3-small': 68_000, 'mistral-large': 9100 } },
      { group: 'Copilot Prototyping', owner: 'Developer platform', tokensByModel: { 'gpt-4o': 71_000_000, 'gpt-4o-mini': 55_000_000, 'text-embedding-3-small': 7_400_000, 'mistral-large': 10_200_000 }, requestsByModel: { 'gpt-4o': 102_000, 'gpt-4o-mini': 164_000, 'text-embedding-3-small': 21_500, 'mistral-large': 13_900 } },
      { group: 'Search Enrichment', owner: 'Knowledge systems', tokensByModel: { 'gpt-4o': 36_000_000, 'gpt-4o-mini': 34_000_000, 'text-embedding-3-small': 58_000_000, 'mistral-large': 0 }, requestsByModel: { 'gpt-4o': 52_000, 'gpt-4o-mini': 69_000, 'text-embedding-3-small': 238_000, 'mistral-large': 0 } },
      { group: 'Finance Insights', owner: 'FinOps analytics', tokensByModel: { 'gpt-4o': 42_000_000, 'gpt-4o-mini': 23_000_000, 'text-embedding-3-small': 10_500_000, 'mistral-large': 7_200_000 }, requestsByModel: { 'gpt-4o': 55_000, 'gpt-4o-mini': 47_000, 'text-embedding-3-small': 24_000, 'mistral-large': 8100 } },
    ],
    endpoints: [
      { endpoint: '/chat/completions', operation: 'Interactive chat', byModel: { 'gpt-4o': { p50: 660, p95: 1260, errorRate: 0.51 }, 'gpt-4o-mini': { p50: 430, p95: 810, errorRate: 0.2 }, 'text-embedding-3-small': { p50: 170, p95: 290, errorRate: 0.06 }, 'mistral-large': { p50: 820, p95: 1540, errorRate: 1.02 } } },
      { endpoint: '/embeddings', operation: 'Search index updates', byModel: { 'gpt-4o': { p50: 230, p95: 420, errorRate: 0.09 }, 'gpt-4o-mini': { p50: 200, p95: 330, errorRate: 0.04 }, 'text-embedding-3-small': { p50: 120, p95: 200, errorRate: 0.02 }, 'mistral-large': { p50: 0, p95: 0, errorRate: 0 } } },
      { endpoint: '/responses', operation: 'Tool-enabled orchestration', byModel: { 'gpt-4o': { p50: 910, p95: 1790, errorRate: 0.74 }, 'gpt-4o-mini': { p50: 610, p95: 1160, errorRate: 0.32 }, 'text-embedding-3-small': { p50: 0, p95: 0, errorRate: 0 }, 'mistral-large': { p50: 1040, p95: 1910, errorRate: 1.21 } } },
    ],
    errors: [
      { id: 'err-713', timestamp: '2026-08-11 15:32 ET', severity: 'Critical', code: '429', message: 'Rate-limit window saturated for shared Finance Insights traffic.', endpoint: '/chat/completions', model: 'gpt-4o', count: 51, impact: 'Repeated throttling during finance close operations.', correlationId: 'd72ea6fe-abd2-46a1-a6b2-f08ca4f9231b', remediation: 'Isolate finance workloads behind a dedicated key or model deployment before enabling live sync.' },
      { id: 'err-706', timestamp: '2026-08-10 13:12 ET', severity: 'Warning', code: '504', message: 'Long-running tool response exceeded gateway timeout.', endpoint: '/responses', model: 'mistral-large', count: 18, impact: 'Prototype assistant answers retried by client workflows.', correlationId: 'a6f0fb75-75cf-4cf4-a4af-ac7ab8fd9dd9', remediation: 'Tune timeout envelope and reduce overly large tool payloads.' },
      { id: 'err-699', timestamp: '2026-08-09 10:21 ET', severity: 'Info', code: '401', message: 'Expired token rejected at gateway edge.', endpoint: '/chat/completions', model: 'gpt-4o-mini', count: 38, impact: 'Authentication hygiene event only.', correlationId: '1da13b4d-c0c7-4507-bba2-d5ecac9fbda4', remediation: 'Continue observing token refresh patterns.' },
    ],
    integrations: [
      { name: 'Azure API Management', status: 'Healthy', detail: 'Telemetry export remains within the expected ingestion window.', freshness: 'Updated 7 minutes ago' },
      { name: 'Azure Monitor queries', status: 'Planned', detail: 'Log Analytics integration has not been connected; all values are local sample data.', freshness: 'Not connected' },
      { name: 'Chargeback export', status: 'Delayed', detail: 'Finance workbook publication is queued behind analytics rollout.', freshness: 'Awaiting connector' },
    ],
  },
  '30d': {
    timeline: [
      { label: 'W1', requestsByModel: { 'gpt-4o': 236_000, 'gpt-4o-mini': 318_000, 'text-embedding-3-small': 151_000, 'mistral-large': 42_000 }, tokensByModel: { 'gpt-4o': 165_000_000, 'gpt-4o-mini': 100_000_000, 'text-embedding-3-small': 43_000_000, 'mistral-large': 18_000_000 } },
      { label: 'W2', requestsByModel: { 'gpt-4o': 248_000, 'gpt-4o-mini': 332_000, 'text-embedding-3-small': 159_000, 'mistral-large': 45_000 }, tokensByModel: { 'gpt-4o': 174_000_000, 'gpt-4o-mini': 105_000_000, 'text-embedding-3-small': 45_000_000, 'mistral-large': 19_400_000 } },
      { label: 'W3', requestsByModel: { 'gpt-4o': 260_000, 'gpt-4o-mini': 346_000, 'text-embedding-3-small': 166_000, 'mistral-large': 47_000 }, tokensByModel: { 'gpt-4o': 182_000_000, 'gpt-4o-mini': 110_000_000, 'text-embedding-3-small': 47_000_000, 'mistral-large': 20_600_000 } },
      { label: 'W4', requestsByModel: { 'gpt-4o': 272_000, 'gpt-4o-mini': 358_000, 'text-embedding-3-small': 171_000, 'mistral-large': 49_000 }, tokensByModel: { 'gpt-4o': 191_000_000, 'gpt-4o-mini': 114_000_000, 'text-embedding-3-small': 49_000_000, 'mistral-large': 21_300_000 } },
    ],
    estimatedCostUsdByModel: { 'gpt-4o': 116_000, 'gpt-4o-mini': 37_800, 'text-embedding-3-small': 12_600, 'mistral-large': 7300 },
    successRateByModel: { 'gpt-4o': 99.18, 'gpt-4o-mini': 99.64, 'text-embedding-3-small': 99.89, 'mistral-large': 98.44 },
    groups: [
      { group: 'Customer Support', owner: 'Support engineering', tokensByModel: { 'gpt-4o': 325_000_000, 'gpt-4o-mini': 202_000_000, 'text-embedding-3-small': 79_000_000, 'mistral-large': 21_000_000 }, requestsByModel: { 'gpt-4o': 468_000, 'gpt-4o-mini': 606_000, 'text-embedding-3-small': 270_000, 'mistral-large': 35_000 } },
      { group: 'Copilot Prototyping', owner: 'Developer platform', tokensByModel: { 'gpt-4o': 279_000_000, 'gpt-4o-mini': 221_000_000, 'text-embedding-3-small': 31_000_000, 'mistral-large': 42_000_000 }, requestsByModel: { 'gpt-4o': 401_000, 'gpt-4o-mini': 655_000, 'text-embedding-3-small': 92_000, 'mistral-large': 58_000 } },
      { group: 'Search Enrichment', owner: 'Knowledge systems', tokensByModel: { 'gpt-4o': 143_000_000, 'gpt-4o-mini': 137_000_000, 'text-embedding-3-small': 229_000_000, 'mistral-large': 0 }, requestsByModel: { 'gpt-4o': 206_000, 'gpt-4o-mini': 278_000, 'text-embedding-3-small': 940_000, 'mistral-large': 0 } },
      { group: 'Finance Insights', owner: 'FinOps analytics', tokensByModel: { 'gpt-4o': 169_000_000, 'gpt-4o-mini': 93_000_000, 'text-embedding-3-small': 44_000_000, 'mistral-large': 28_000_000 }, requestsByModel: { 'gpt-4o': 222_000, 'gpt-4o-mini': 191_000, 'text-embedding-3-small': 99_000, 'mistral-large': 31_000 } },
    ],
    endpoints: [
      { endpoint: '/chat/completions', operation: 'Interactive chat', byModel: { 'gpt-4o': { p50: 670, p95: 1280, errorRate: 0.56 }, 'gpt-4o-mini': { p50: 440, p95: 830, errorRate: 0.23 }, 'text-embedding-3-small': { p50: 180, p95: 300, errorRate: 0.06 }, 'mistral-large': { p50: 850, p95: 1590, errorRate: 1.08 } } },
      { endpoint: '/embeddings', operation: 'Search index updates', byModel: { 'gpt-4o': { p50: 240, p95: 430, errorRate: 0.1 }, 'gpt-4o-mini': { p50: 205, p95: 340, errorRate: 0.05 }, 'text-embedding-3-small': { p50: 125, p95: 205, errorRate: 0.02 }, 'mistral-large': { p50: 0, p95: 0, errorRate: 0 } } },
      { endpoint: '/responses', operation: 'Tool-enabled orchestration', byModel: { 'gpt-4o': { p50: 930, p95: 1820, errorRate: 0.79 }, 'gpt-4o-mini': { p50: 620, p95: 1180, errorRate: 0.34 }, 'text-embedding-3-small': { p50: 0, p95: 0, errorRate: 0 }, 'mistral-large': { p50: 1060, p95: 1960, errorRate: 1.26 } } },
    ],
    errors: [
      { id: 'err-3011', timestamp: '2026-08-03 12:05 ET', severity: 'Critical', code: '429', message: 'Sustained monthly burst quota overrun in finance tenant.', endpoint: '/chat/completions', model: 'gpt-4o', count: 118, impact: 'Finance assistant sessions were repeatedly throttled during close week.', correlationId: '86501c20-451b-4022-8b6f-d439436cde2e', remediation: 'Create separated quotas and revisit entitlement design before live rollout.' },
      { id: 'err-2998', timestamp: '2026-08-01 16:40 ET', severity: 'Warning', code: '504', message: 'Gateway timeout spike for tool-enabled developer assistants.', endpoint: '/responses', model: 'mistral-large', count: 43, impact: 'Prototype workflows retried and increased local latency.', correlationId: '5bf8c49f-c73f-4a64-a4a4-45117b865335', remediation: 'Review timeout budgets and upstream dependency health.' },
      { id: 'err-2975', timestamp: '2026-07-29 09:58 ET', severity: 'Info', code: '401', message: 'Expired developer bearer token rejected.', endpoint: '/chat/completions', model: 'gpt-4o-mini', count: 96, impact: 'Expected edge rejection pattern only.', correlationId: '8a3d8d88-c563-4866-80f7-f6152dffbf86', remediation: 'Continue monitoring token refresh behavior.' },
    ],
    integrations: [
      { name: 'Azure API Management', status: 'Healthy', detail: 'Gateway ingestion stayed within monthly alerting thresholds.', freshness: 'Updated 12 minutes ago' },
      { name: 'Azure Monitor queries', status: 'Planned', detail: 'This dashboard is still sample-only; no Azure Monitor data source is queried.', freshness: 'Not connected' },
      { name: 'Chargeback export', status: 'Delayed', detail: 'Chargeback workbook publishing remains in rollout planning.', freshness: 'Pending approval' },
    ],
  },
}

function sumByModel<T extends Record<ModelName, number>>(values: T, selectedModel: ModelFilter): number {
  if (selectedModel === 'all') {
    return Object.values(values).reduce((total, current) => total + current, 0)
  }
  return values[selectedModel]
}

function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: value >= 1_000_000 ? 1 : 0,
  }).format(value)
}

function formatTokenCount(value: number): string {
  if (value >= 1_000_000_000) {
    return `${(value / 1_000_000_000).toFixed(1)}B`
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}K`
  }
  return value.toString()
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(value)
}

function formatPercent(value: number): string {
  return `${value.toFixed(2)}%`
}

function formatLatency(value: number): string {
  return `${Math.round(value)} ms`
}

function escapeCsvValue(value: string | number): string {
  const text = String(value)
  if (/[",\n]/.test(text)) {
    return `"${text.replaceAll('"', '""')}"`
  }
  return text
}

function buildPolylinePoints(values: number[]): string {
  const maximum = Math.max(...values, 1)
  return values
    .map((value, index) => {
      const x = values.length === 1 ? 50 : (index / (values.length - 1)) * 100
      const y = 92 - (value / maximum) * 76
      return `${x},${y}`
    })
    .join(' ')
}

function KpiCard({
  label,
  value,
  detail,
  accent,
}: {
  label: string
  value: string
  detail: string
  accent?: 'warning'
}) {
  return (
    <Card className={`${styles.kpiCard} ${accent === 'warning' ? styles.kpiWarning : ''}`}>
      <Text className={styles.kpiLabel}>{label}</Text>
      <div className={styles.kpiValue}>{value}</div>
      <Text size={200} className={accent === 'warning' ? styles.warningDetail : styles.kpiDetail}>
        {detail}
      </Text>
    </Card>
  )
}

export function AnalyticsPage() {
  const [timeRange, setTimeRange] = useState<TimeRange>('24h')
  const [modelFilter, setModelFilter] = useState<ModelFilter>('all')
  const [selectedError, setSelectedError] = useState<ErrorRecord | null>(null)
  const [exportStatus, setExportStatus] = useState('Ready to export current sample view.')

  const dataset = sampleDatasets[timeRange]

  const derived = useMemo<DerivedAnalytics>(() => {
    const filteredTimeline = dataset.timeline.map((point) => ({
      label: point.label,
      requests: sumByModel(point.requestsByModel, modelFilter),
      tokens: sumByModel(point.tokensByModel, modelFilter),
    }))

    const filteredGroups = dataset.groups
      .map<FilteredGroupUsage>((group) => ({
        group: group.group,
        owner: group.owner,
        tokens: sumByModel(group.tokensByModel, modelFilter),
        requests: sumByModel(group.requestsByModel, modelFilter),
      }))
      .filter((group) => group.tokens > 0 || group.requests > 0)
      .sort((left, right) => right.tokens - left.tokens)

    const filteredEndpoints = dataset.endpoints
      .map<FilteredEndpointLatency>((endpoint) => {
        if (modelFilter === 'all') {
          const metrics = Object.values(endpoint.byModel).filter((item) => item.p95 > 0)
          const count = Math.max(metrics.length, 1)
          return {
            endpoint: endpoint.endpoint,
            operation: endpoint.operation,
            p50: metrics.reduce((total, item) => total + item.p50, 0) / count,
            p95: metrics.reduce((total, item) => total + item.p95, 0) / count,
            errorRate: metrics.reduce((total, item) => total + item.errorRate, 0) / count,
          }
        }

        const selected = endpoint.byModel[modelFilter]
        return {
          endpoint: endpoint.endpoint,
          operation: endpoint.operation,
          p50: selected.p50,
          p95: selected.p95,
          errorRate: selected.errorRate,
        }
      })
      .filter((endpoint) => endpoint.p95 > 0)
      .sort((left, right) => right.p95 - left.p95)

    const filteredErrors =
      modelFilter === 'all'
        ? dataset.errors
        : dataset.errors.filter((error) => error.model === modelFilter)

    const totalRequests = filteredTimeline.reduce((total, point) => total + point.requests, 0)
    const totalTokens = filteredTimeline.reduce((total, point) => total + point.tokens, 0)
    const estimatedCost =
      modelFilter === 'all'
        ? Object.values(dataset.estimatedCostUsdByModel).reduce((total, value) => total + value, 0)
        : dataset.estimatedCostUsdByModel[modelFilter]
    const successRate =
      modelFilter === 'all'
        ? Object.values(dataset.successRateByModel).reduce((total, value) => total + value, 0) /
          Object.values(dataset.successRateByModel).length
        : dataset.successRateByModel[modelFilter]
    const p95Latency =
      filteredEndpoints.reduce((total, item) => total + item.p95, 0) /
      Math.max(filteredEndpoints.length, 1)
    const criticalErrors = filteredErrors
      .filter((error) => error.severity === 'Critical')
      .reduce((total, error) => total + error.count, 0)

    return {
      filteredTimeline,
      filteredGroups,
      filteredEndpoints,
      filteredErrors,
      totalRequests,
      totalTokens,
      estimatedCost,
      successRate,
      p95Latency,
      criticalErrors,
    }
  }, [dataset, modelFilter])

  const requestPoints = useMemo(
    () => buildPolylinePoints(derived.filteredTimeline.map((point) => point.requests)),
    [derived.filteredTimeline],
  )

  const requestMax = Math.max(
    ...derived.filteredTimeline.map((point) => point.requests),
    1,
  )
  const largestGroupToken = Math.max(...derived.filteredGroups.map((group) => group.tokens), 1)
  const highestEndpointLatency = Math.max(
    ...derived.filteredEndpoints.map((endpoint) => endpoint.p95),
    1,
  )

  function exportCsv() {
    const rows: string[] = []
    rows.push('Section,Name,Value,Detail')
    rows.push(
      [
        'Summary',
        'Time range',
        timeRange,
        modelFilter === 'all' ? 'All models' : modelFilter,
      ]
        .map(escapeCsvValue)
        .join(','),
    )
    rows.push(
      ['Summary', 'Total requests', derived.totalRequests, formatCompactNumber(derived.totalRequests)]
        .map(escapeCsvValue)
        .join(','),
    )
    rows.push(
      ['Summary', 'Total tokens', derived.totalTokens, formatTokenCount(derived.totalTokens)]
        .map(escapeCsvValue)
        .join(','),
    )
    rows.push(
      ['Summary', 'Estimated cost', derived.estimatedCost, formatCurrency(derived.estimatedCost)]
        .map(escapeCsvValue)
        .join(','),
    )
    rows.push(
      ['Summary', 'Success rate', derived.successRate, formatPercent(derived.successRate)]
        .map(escapeCsvValue)
        .join(','),
    )
    rows.push('')
    rows.push('Timeline label,Requests,Tokens')
    derived.filteredTimeline.forEach((point) => {
      rows.push([point.label, point.requests, point.tokens].map(escapeCsvValue).join(','))
    })
    rows.push('')
    rows.push('Group,Owner,Requests,Tokens')
    derived.filteredGroups.forEach((group) => {
      rows.push([group.group, group.owner, group.requests, group.tokens].map(escapeCsvValue).join(','))
    })
    rows.push('')
    rows.push('Endpoint,Operation,P50 ms,P95 ms,Error rate %')
    derived.filteredEndpoints.forEach((endpoint) => {
      rows.push(
        [endpoint.endpoint, endpoint.operation, endpoint.p50, endpoint.p95, endpoint.errorRate]
          .map(escapeCsvValue)
          .join(','),
      )
    })
    rows.push('')
    rows.push('Error ID,Timestamp,Severity,Code,Endpoint,Model,Count,Message')
    derived.filteredErrors.forEach((error) => {
      rows.push(
        [
          error.id,
          error.timestamp,
          error.severity,
          error.code,
          error.endpoint,
          error.model,
          error.count,
          error.message,
        ]
          .map(escapeCsvValue)
          .join(','),
      )
    })
    rows.push('')
    rows.push('Integration,Status,Freshness,Detail')
    dataset.integrations.forEach((integration) => {
      rows.push(
        [integration.name, integration.status, integration.freshness, integration.detail]
          .map(escapeCsvValue)
          .join(','),
      )
    })

    const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' })
    const objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    const fileModel = modelFilter === 'all' ? 'all-models' : modelFilter
    link.href = objectUrl
    link.download = `mosaic-analytics-${timeRange}-${fileModel}.csv`
    document.body.append(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
    setExportStatus(`Exported ${link.download}`)
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Analytics"
        description="Inspect sample observability, usage, and chargeback views for MOSAIC while Azure Monitor integration is still staged."
        source="sample"
        actions={
          <div className={styles.headerControls}>
            <label className={styles.filterControl}>
              <span>Time range</span>
              <Select
                value={timeRange}
                onChange={(event) => setTimeRange(event.target.value as TimeRange)}
              >
                <option value="24h">Last 24 hours</option>
                <option value="7d">Last 7 days</option>
                <option value="30d">Last 30 days</option>
              </Select>
            </label>
            <label className={styles.filterControl}>
              <span>Model</span>
              <Select
                value={modelFilter}
                onChange={(event) => setModelFilter(event.target.value as ModelFilter)}
              >
                {modelOptions.map((option) => (
                  <option key={option} value={option}>
                    {option === 'all' ? 'All models' : option}
                  </option>
                ))}
              </Select>
            </label>
            <Button
              appearance="primary"
              onClick={exportCsv}
            >
              Export CSV
            </Button>
          </div>
        }
      />

      <PreviewNotice>
        Azure Monitor, Log Analytics, and chargeback systems are not queried here yet. Filters and
        exports operate only on the local sample dataset rendered in this page.
      </PreviewNotice>

      <div className={styles.exportStatus} role="status" aria-live="polite">
        {exportStatus}
      </div>

      <div className={styles.kpiGrid}>
        <KpiCard
          label="Requests"
          value={formatCompactNumber(derived.totalRequests)}
          detail={`${timeRange} · ${modelFilter === 'all' ? 'all models' : modelFilter}`}
        />
        <KpiCard
          label="Token usage"
          value={formatTokenCount(derived.totalTokens)}
          detail="Illustrative APIM-routed token volume"
        />
        <KpiCard
          label="Success rate"
          value={formatPercent(derived.successRate)}
          detail={`${derived.filteredErrors.length} recent error patterns`}
        />
        <KpiCard
          label="Estimated cost"
          value={formatCurrency(derived.estimatedCost)}
          detail={`${derived.criticalErrors} critical-error events in view`}
          accent="warning"
        />
      </div>

      <div className={styles.analyticsGrid}>
        <div className={`panel ${styles.volumePanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Request volume</Title3>
              <Text size={200}>Sample demand curve for the selected time range</Text>
            </div>
            <Badge appearance="outline">{modelFilter === 'all' ? 'All models' : modelFilter}</Badge>
          </div>
          <div className={styles.chartArea}>
            <div className={styles.chartGrid} aria-hidden="true">
              <span />
              <span />
              <span />
              <span />
            </div>
            <svg
              className={styles.lineChart}
              viewBox="0 0 100 100"
              preserveAspectRatio="none"
              role="img"
              aria-label={`Sample request volume for ${timeRange}`}
            >
              <polyline className={styles.areaLine} points={`0,100 ${requestPoints} 100,100`} />
              <polyline className={styles.dataLine} points={requestPoints} />
            </svg>
          </div>
          <div className={styles.axisLabels}>
            {derived.filteredTimeline.map((point) => (
              <span key={point.label}>{point.label}</span>
            ))}
          </div>
          <div className={styles.chartSummary}>
            <div>
              <span>Peak requests</span>
              <strong>{formatCompactNumber(requestMax)}</strong>
            </div>
            <div>
              <span>Average p95 latency</span>
              <strong>{formatLatency(derived.p95Latency)}</strong>
            </div>
          </div>
        </div>

        <div className={`panel ${styles.groupsPanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Token usage by group</Title3>
              <Text size={200}>Sample chargeback distribution</Text>
            </div>
          </div>
          <div className={styles.tableWrap}>
            <table aria-label="Token usage by group">
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Owner</th>
                  <th>Requests</th>
                  <th>Tokens</th>
                </tr>
              </thead>
              <tbody>
                {derived.filteredGroups.map((group) => (
                  <tr key={group.group}>
                    <td>{group.group}</td>
                    <td>{group.owner}</td>
                    <td>{formatCompactNumber(group.requests)}</td>
                    <td>
                      <div className={styles.metricBarCell}>
                        <span>{formatTokenCount(group.tokens)}</span>
                        <div className={styles.progressTrack} aria-hidden="true">
                          <span style={{ width: `${(group.tokens / largestGroupToken) * 100}%` }} />
                        </div>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className={`panel ${styles.latencyPanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Endpoint latency</Title3>
              <Text size={200}>Filtered APIM endpoint performance sample</Text>
            </div>
          </div>
          <div className={styles.endpointList} role="list" aria-label="Endpoint latency list">
            {derived.filteredEndpoints.map((endpoint) => (
              <div key={endpoint.endpoint} className={styles.endpointRow} role="listitem">
                <div className={styles.endpointHeader}>
                  <div>
                    <code>{endpoint.endpoint}</code>
                    <Text size={200}>{endpoint.operation}</Text>
                  </div>
                  <Badge appearance="outline">{formatPercent(endpoint.errorRate)}</Badge>
                </div>
                <div className={styles.endpointMetrics}>
                  <span>P50 {formatLatency(endpoint.p50)}</span>
                  <span>P95 {formatLatency(endpoint.p95)}</span>
                </div>
                <div className={styles.progressTrack} aria-hidden="true">
                  <span style={{ width: `${(endpoint.p95 / highestEndpointLatency) * 100}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className={`panel ${styles.errorsPanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Recent errors</Title3>
              <Text size={200}>Select a row for drill-in details</Text>
            </div>
          </div>
          <div className={styles.tableWrap}>
            <table aria-label="Recent analytics errors">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Severity</th>
                  <th>Issue</th>
                  <th>Count</th>
                  <th>
                    <span className="sr-only">Details</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {derived.filteredErrors.length > 0 ? (
                  derived.filteredErrors.map((error) => (
                    <tr key={error.id}>
                      <td>{error.timestamp}</td>
                      <td>
                        <span
                          className={`${styles.severityPill} ${
                            error.severity === 'Critical'
                              ? styles.severityCritical
                              : error.severity === 'Warning'
                                ? styles.severityWarning
                                : styles.severityInfo
                          }`}
                        >
                          {error.severity}
                        </span>
                      </td>
                      <td>
                        <div className={styles.errorMessage}>
                          <strong>{error.code}</strong>
                          <span>{error.message}</span>
                        </div>
                      </td>
                      <td>{error.count}</td>
                      <td>
                        <Button appearance="subtle" onClick={() => setSelectedError(error)}>
                          View
                        </Button>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5}>
                      <div className={styles.emptyState}>
                        <Text>No sample errors match the selected model filter.</Text>
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className={`panel ${styles.integrationPanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Integration status</Title3>
              <Text size={200}>Current sample integration posture</Text>
            </div>
          </div>
          <div className={styles.integrationList}>
            {dataset.integrations.map((integration) => (
              <div key={integration.name} className={styles.integrationCard}>
                <div className={styles.integrationHeader}>
                  <strong>{integration.name}</strong>
                  <span
                    className={`${styles.integrationBadge} ${
                      integration.status === 'Healthy'
                        ? styles.integrationHealthy
                        : integration.status === 'Delayed'
                          ? styles.integrationDelayed
                          : styles.integrationPlanned
                    }`}
                  >
                    {integration.status}
                  </span>
                </div>
                <Text>{integration.detail}</Text>
                <Text size={200} className={styles.mutedText}>
                  {integration.freshness}
                </Text>
              </div>
            ))}
          </div>
        </div>
      </div>

      <Dialog
        open={selectedError !== null}
        onOpenChange={(_, data) => {
          if (!data.open) {
            setSelectedError(null)
          }
        }}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>{selectedError?.code ?? 'Error details'}</DialogTitle>
            <DialogContent>
              {selectedError && (
                <div className={styles.dialogContent}>
                  <div>
                    <span>Timestamp</span>
                    <strong>{selectedError.timestamp}</strong>
                  </div>
                  <div>
                    <span>Endpoint</span>
                    <code>{selectedError.endpoint}</code>
                  </div>
                  <div>
                    <span>Model</span>
                    <strong>{selectedError.model}</strong>
                  </div>
                  <div>
                    <span>Impact</span>
                    <Text>{selectedError.impact}</Text>
                  </div>
                  <div>
                    <span>Correlation ID</span>
                    <code>{selectedError.correlationId}</code>
                  </div>
                  <div>
                    <span>Suggested follow-up</span>
                    <Text>{selectedError.remediation}</Text>
                  </div>
                </div>
              )}
            </DialogContent>
            <DialogActions>
              <Button appearance="primary" onClick={() => setSelectedError(null)}>
                Close
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </section>
  )
}
