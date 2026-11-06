import {
  Badge,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Link,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Radio,
  RadioGroup,
  Select,
  Spinner,
  Text,
  Textarea,
} from '@fluentui/react-components'
import {
  AddRegular,
  ArrowUploadRegular,
  CloudRegular,
  DocumentRegular,
  MoneyRegular,
  PlugConnectedRegular,
  WarningRegular,
} from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ChangeEvent, type FormEvent, Fragment, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Link as RouterLink } from 'react-router-dom'
import { SectionLayout } from '../components/SectionLayout'
import { ApiError, useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { RowActions } from '../components/RowActions'
import { WorkspaceEditorFrame } from '../components/WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from '../components/WorkspaceLeaveGuard'
import { formatRate } from '../cost-format'
import { plural } from '../labels'
import type {
  AzureBillingRetrievalMode,
  BillingCandidateMapping,
  BillingCandidateRate,
  BillingConnectionCheckResult,
  BillingConnectionCreate,
  BillingConnectionUpdate,
  BillingConnectionView,
  BillingMappingScope,
  BillingProvider,
  BillingPublicationView,
  BillingRunDetailView,
  BillingRunStatus,
  ConnectedBillingProvider,
  EndpointPricingUpdate,
  EndpointPricingView,
  PriceCreate,
  PriceLineView,
  PriceSource,
  PricingCloud,
  PriceView,
  UnpricedDeploymentRow,
  UnpricedReason,
} from '../types'
import styles from './PricingPage.module.css'

type PricingTab = 'prices' | 'billing' | 'unpriced' | 'endpoints' | 'sources'
type PriceKind = 'tokens' | 'ptu' | 'monthly'

const tabs: Array<{ key: PricingTab; label: string }> = [
  { key: 'prices', label: 'Prices' },
  { key: 'billing', label: 'Billing data' },
  { key: 'unpriced', label: 'Unpriced deployments' },
  { key: 'endpoints', label: 'Endpoint facts' },
  { key: 'sources', label: 'Sources' },
]

const DEPLOYMENT_TYPES = [
  'GlobalStandard',
  'DataZoneStandard',
  'Standard',
  'GlobalBatch',
  'DataZoneBatch',
  'GlobalProvisionedManaged',
  'DataZoneProvisionedManaged',
  'ProvisionedManaged',
  'DeveloperTier',
]

const DEPLOYMENT_TYPE_LABELS: Record<string, string> = {
  GlobalStandard: 'Global Standard',
  DataZoneStandard: 'Data Zone Standard',
  Standard: 'Standard (regional)',
  GlobalBatch: 'Global Batch',
  DataZoneBatch: 'Data Zone Batch',
  GlobalProvisionedManaged: 'Global Provisioned',
  DataZoneProvisionedManaged: 'Data Zone Provisioned',
  ProvisionedManaged: 'Regional Provisioned',
  DeveloperTier: 'Developer',
}

const STATUS_LABELS: Record<PriceView['status'], string> = {
  current: 'In effect',
  upcoming: 'Scheduled',
  past: 'Superseded',
  corrected: 'Corrected',
}

const REASON_LABELS: Record<UnpricedReason, string> = {
  unknownDeployment: 'Deployment unknown',
  noCloud: 'Cloud unknown',
  noModel: 'Model unknown',
  noDeploymentType: 'Deployment type unknown',
  noCapacity: 'PTUs unknown',
  noPrice: 'No price listed',
  notYetEffective: 'Price not yet in effect',
  noOutputPrice: 'No output price',
  beforeDeployment: 'Not yet deployed',
}

// Reasons an endpoint's facts fix, rather than a price.
const ENDPOINT_FIXES = new Set<UnpricedReason>(['noCloud', 'noDeploymentType', 'noCapacity'])
const PRICE_FIXES = new Set<UnpricedReason>(['noPrice', 'noOutputPrice', 'notYetEffective'])
const REASON_HELP: Record<UnpricedReason, string> = {
  unknownDeployment: 'Review and sync the endpoint inventory in Models before pricing its deployment.',
  noCloud: 'Set the endpoint cloud or provider in Endpoint facts.',
  noModel: 'Review the endpoint inventory in Models; a model identity is needed before a price can match.',
  noDeploymentType: 'Set the deployment type in Endpoint facts.',
  noCapacity: 'Set the provisioned capacity (PTUs) in Endpoint facts.',
  noPrice: 'Add a matching model price with its source and effective date.',
  notYetEffective: 'Review the price effective date. Add a price only if a sourced rate covers the usage date.',
  noOutputPrice: 'Add a sourced rate that includes output tokens.',
  beforeDeployment: 'Usage predates the deployment. Adding a price will not fix this date mismatch.',
}

function typeLabel(value: string | null | undefined, empty = 'Any type') {
  if (!value) return empty
  return DEPLOYMENT_TYPE_LABELS[value] ?? value
}

function isProvisioned(value: string | null | undefined) {
  return Boolean(value && value.toLowerCase().includes('provisioned'))
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' }).format(
    new Date(value.length === 10 ? `${value}T00:00:00Z` : value),
  )
}

function formatCount(value: number) {
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function regionsLabel(regions: string[] | null) {
  if (!regions || regions.length === 0) return 'Every region'
  if (regions.length <= 3) return regions.join(', ')
  return `${regions.slice(0, 2).join(', ')} and ${regions.length - 2} more`
}

function today() {
  return new Date().toISOString().slice(0, 10)
}

function previousCompleteMonth() {
  const now = new Date()
  const year = now.getUTCFullYear()
  const month = now.getUTCMonth()
  if (month === 0) return `${year - 1}-12`
  return `${year}-${String(month).padStart(2, '0')}`
}

function formatUsd(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

const PROVIDER_LABELS: Record<string, string> = {
  azure: 'Azure Cost Management',
  aws: 'AWS CUR 2.0 (S3)',
  gcp: 'GCP BigQuery Billing Export',
  mosaicTemplate: 'MOSAIC Billing Template',
}

function providerLabel(provider: string | null | undefined) {
  if (!provider) return 'Unknown source'
  return PROVIDER_LABELS[provider] ?? provider
}

function extractBillingRunId(source: PriceSource, fallbackRunId?: string | null): string | null {
  if (source.runId) return source.runId
  const match = source.reference?.match(/^mosaic:\/\/pricing\/billing-runs\/([^#?]+)/)
  if (match?.[1]) return match[1]
  if (source.kind === 'billingRun' && fallbackRunId) return fallbackRunId
  return null
}

function sourceName(source: { title: string; url: string }) {
  if (source.url.startsWith('https://prices.azure.com/')) return 'Azure Retail Prices API'
  if (source.url.startsWith('https://learn.microsoft.com/')) return 'Microsoft Learn'
  return source.title === 'Entered by an administrator' ? 'Administrator source' : source.title
}

function Sources({ price, onOpenBillingRun }: { price: PriceView; onOpenBillingRun?: (runId: string) => void }) {
  return (
    <span className={styles.sources}>
      {price.sources.map((source, index) => {
        const runId = extractBillingRunId(source, price.billingRunId)
        if (source.kind === 'billingRun' || !source.url) {
          const key = source.reference ?? source.runId ?? `${source.title}-${index}`
          if (runId && onOpenBillingRun) {
            return (
              <Link
                key={key}
                as="button"
                type="button"
                onClick={() => onOpenBillingRun(runId)}
                title={source.reference ?? source.title}
                inline
              >
                {source.title}
              </Link>
            )
          }
          return (
            <Text key={key} size={200} title={source.reference ?? undefined}>
              {source.title}
            </Text>
          )
        }
        return (
          <Link key={source.url} href={source.url} target="_blank" rel="noreferrer" title={source.title} inline>
            {sourceName({ title: source.title, url: source.url })}
          </Link>
        )
      })}
    </span>
  )
}

function PriceCells({ price }: { price: PriceView }) {
  if (price.monthlyAmount != null) {
    return <td colSpan={3}>{formatRate(price.monthlyAmount)} a month for this deployment</td>
  }
  if (price.ptuHourly != null) {
    return <td colSpan={3}>{formatRate(price.ptuHourly)} per PTU an hour</td>
  }
  return (
    <>
      <td className={styles.number}>{formatRate(price.inputPerMillion)}</td>
      <td className={styles.number}>{formatRate(price.cachedInputPerMillion)}</td>
      <td className={styles.number}>{formatRate(price.outputPerMillion)}</td>
    </>
  )
}

function modelDetail(price: PriceView) {
  return [
    price.publisher ?? 'Any publisher',
    price.version ? `version ${price.version}` : 'any version',
    price.aliases.length ? `also ${price.aliases.join(', ')}` : null,
    price.deployment ? `deployment ${price.deployment.split('/').pop()}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
}

function HistoryRows({
  lineId,
  columns,
  onOpenBillingRun,
}: {
  lineId: string
  columns: number
  onOpenBillingRun?: (runId: string) => void
}) {
  const api = useMosaicApi()
  const history = useQuery({ queryKey: ['pricing', 'history', lineId], queryFn: () => api.getPriceHistory(lineId) })
  return (
    <tr className={styles.historyRow}>
      <td colSpan={columns}>
        {history.isPending && <Spinner size="tiny" label="Loading price history" />}
        {history.isError && <ErrorState error={history.error} title="Unable to load this price's history" />}
        {history.data && (
          <table aria-label="Price history" className={styles.historyTable}>
            <thead>
              <tr>
                <th>Status</th>
                <th>Effective</th>
                <th>Price</th>
                <th>Recorded</th>
                <th>Source and note</th>
              </tr>
            </thead>
            <tbody>
              {history.data.versions.map((version) => (
                <tr key={version.id}>
                  <td>
                    <Badge appearance="tint" color={version.status === 'current' ? 'success' : version.status === 'upcoming' ? 'brand' : 'subtle'}>
                      {STATUS_LABELS[version.status]}
                    </Badge>
                  </td>
                  <td>
                    {formatDate(version.effectiveFrom)}
                    {version.effectiveUntil && <Text block size={200}>Until {formatDate(version.effectiveUntil)}</Text>}
                  </td>
                  <td>
                    {version.ptuHourly != null
                      ? `${formatRate(version.ptuHourly)} per PTU an hour`
                      : version.monthlyAmount != null
                        ? `${formatRate(version.monthlyAmount)} a month`
                        : `${formatRate(version.inputPerMillion)} in · ${formatRate(version.outputPerMillion)} out`}
                  </td>
                  <td>
                    {version.origin === 'seed' ? 'Shipped with MOSAIC' : formatDate(version.recordedAt)}
                    {version.origin === 'billing' && (
                      <Text block size={200} className={styles.muted}>
                        {version.sourceMonth ? `From ${version.sourceMonth} billing` : 'Billing-derived'}
                      </Text>
                    )}
                    {version.recordedBy && <Text block size={200}>{version.recordedBy}</Text>}
                  </td>
                  <td>
                    <Sources price={version} onOpenBillingRun={onOpenBillingRun} />
                    {version.note && <Text block size={200}>{version.note}</Text>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </td>
    </tr>
  )
}

function PriceTable({
  lines,
  onOverride,
  onOpenBillingRun,
}: {
  lines: PriceLineView[]
  onOverride: (price: PriceView) => void
  onOpenBillingRun?: (runId: string) => void
}) {
  const [open, setOpen] = useState<string | null>(null)
  const columns = 9
  return (
    <div className={`table-scroll ${styles.priceTableScroll}`}>
      <table aria-label="Prices" className={styles.priceTable}>
        <thead>
          <tr>
            <th>Model</th>
            <th>Deployment type</th>
            <th>Regions</th>
            <th className={styles.number}>Input / 1M</th>
            <th className={styles.number}>Cached input / 1M</th>
            <th className={styles.number}>Output / 1M</th>
            <th>In effect from</th>
            <th>Source</th>
            <th><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => {
            const price = line.current ?? line.upcoming
            if (!price) return null
            const expanded = open === line.lineId
            return (
              <Fragment key={line.lineId}>
                <tr>
                  <td>
                    <strong>{price.model === '*' ? 'Every model' : price.model}</strong>
                    <Text block size={200} className={styles.muted}>{modelDetail(price)}</Text>
                  </td>
                  <td>{typeLabel(price.deploymentType)}</td>
                  <td title={price.regions?.join(', ')}>{regionsLabel(price.regions)}</td>
                  <PriceCells price={price} />
                  <td>
                    {line.current ? formatDate(line.current.effectiveFrom) : 'Not yet'}
                    {line.upcoming && (
                      <Text block size={200} className={styles.muted}>
                        Changes {formatDate(line.upcoming.effectiveFrom)}
                      </Text>
                    )}
                  </td>
                  <td>
                    <Sources price={price} onOpenBillingRun={onOpenBillingRun} />
                    <Badge
                      appearance="outline"
                      color={price.origin === 'seed' ? 'informative' : price.origin === 'billing' ? 'success' : 'brand'}
                      className={styles.originBadge}
                    >
                      {price.origin === 'seed'
                        ? 'List price'
                        : price.origin === 'billing'
                          ? price.sourceMonth
                            ? `From ${price.sourceMonth} billing`
                            : 'Billing-derived'
                          : 'Administrator'}
                    </Badge>
                  </td>
                  <td className={styles.actions}>
                    <RowActions
                      label={`More actions for ${price.model === '*' ? 'Every model' : price.model}`}
                      primary={<Button size="small" onClick={() => onOverride(price)}>Override</Button>}
                      actions={[{
                        label: expanded ? 'Hide history' : `History (${line.versions})`,
                        'aria-expanded': expanded,
                        onClick: () => setOpen(expanded ? null : line.lineId),
                      }]}
                    />
                  </td>
                </tr>
                {expanded && <HistoryRows lineId={line.lineId} columns={columns} onOpenBillingRun={onOpenBillingRun} />}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

interface PriceDraft {
  title: string
  cloud: string
  publisher: string
  model: string
  version: string
  deploymentType: string
  regions: string
  deployment: string | null
  kind: PriceKind
  input: string
  cached: string
  output: string
  ptuHourly: string
  monthly: string
  effectiveFrom: string
  sourceUrl: string
  note: string
  overrides: string | null
  // An override keeps what makes the price the same price, and changes only the amounts and date.
  locked: boolean
}

function emptyDraft(cloud: string): PriceDraft {
  return {
    title: 'Add a price',
    cloud,
    publisher: '',
    model: '',
    version: '',
    deploymentType: '',
    regions: '',
    deployment: null,
    kind: 'tokens',
    input: '',
    cached: '',
    output: '',
    ptuHourly: '',
    monthly: '',
    effectiveFrom: today(),
    sourceUrl: '',
    note: '',
    overrides: null,
    locked: false,
  }
}

function overrideDraft(price: PriceView): PriceDraft {
  const amount = (value: number | null) => (value == null ? '' : String(value))
  return {
    title: `Override ${price.model === '*' ? 'every model' : price.model}`,
    cloud: price.cloud,
    publisher: price.publisher ?? '',
    model: price.model,
    version: price.version ?? '',
    deploymentType: price.deploymentType ?? '',
    regions: (price.regions ?? []).join(', '),
    deployment: price.deployment,
    kind: price.monthlyAmount != null ? 'monthly' : price.ptuHourly != null ? 'ptu' : 'tokens',
    input: amount(price.inputPerMillion),
    cached: amount(price.cachedInputPerMillion),
    output: amount(price.outputPerMillion),
    ptuHourly: amount(price.ptuHourly),
    monthly: amount(price.monthlyAmount),
    effectiveFrom: today(),
    sourceUrl: '',
    note: '',
    overrides: price.id,
    locked: true,
  }
}

function unpricedDraft(row: UnpricedDeploymentRow, cloud: string): PriceDraft {
  return {
    ...emptyDraft(row.cloud ?? cloud),
    title: `Price ${row.label}`,
    model: row.model ?? '',
    version: row.version ?? '',
    deploymentType: row.deploymentType ?? '',
    regions: row.region ?? '',
    kind: isProvisioned(row.deploymentType) ? 'ptu' : 'tokens',
  }
}

function numberOrNull(value: string) {
  const trimmed = value.trim()
  return trimmed === '' ? null : Number(trimmed)
}

function payloadFrom(draft: PriceDraft): PriceCreate {
  const regions = draft.regions.split(',').map((region) => region.trim()).filter(Boolean)
  return {
    cloud: draft.cloud.trim().toLowerCase(),
    publisher: draft.publisher.trim() || null,
    model: draft.model.trim(),
    version: draft.version.trim() || null,
    deploymentType: draft.deploymentType || null,
    regions: regions.length ? regions : null,
    deployment: draft.deployment,
    inputPerMillion: draft.kind === 'tokens' ? numberOrNull(draft.input) : null,
    cachedInputPerMillion: draft.kind === 'tokens' ? numberOrNull(draft.cached) : null,
    outputPerMillion: draft.kind === 'tokens' ? numberOrNull(draft.output) : null,
    ptuHourly: draft.kind === 'ptu' ? numberOrNull(draft.ptuHourly) : null,
    monthlyAmount: draft.kind === 'monthly' ? numberOrNull(draft.monthly) : null,
    effectiveFrom: draft.effectiveFrom,
    sourceUrl: draft.sourceUrl.trim(),
    note: draft.note.trim(),
    overrides: draft.overrides,
  }
}

function PriceDialog({
  draft: initial,
  clouds,
  onClose,
}: {
  draft: PriceDraft
  clouds: PricingCloud[]
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(initial)
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    return () => {
      for (const child of Array.from(document.body.children)) {
        if (child instanceof HTMLElement && child.getAttribute('aria-hidden') === 'true') {
          child.removeAttribute('aria-hidden')
        }
      }
      if (opener?.isConnected) opener.focus({ preventScroll: true })
    }
  }, [])
  const save = useMutation({
    mutationFn: () => api.addPrice(payloadFrom(draft)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['pricing'] })
      await queryClient.invalidateQueries({ queryKey: ['analytics'] })
      onClose()
    },
  })
  const set = (key: keyof PriceDraft) => (_: unknown, data: { value: string }) =>
    setDraft((current) => ({ ...current, [key]: data.value }))
  const knownCloud = clouds.some((cloud) => cloud.key === draft.cloud)

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }

  return (
    <Dialog open onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface className={styles.dialog}>
        <form onSubmit={submit}>
          <DialogBody>
            <DialogTitle>{draft.title}</DialogTitle>
            <DialogContent className={styles.form}>
              <Text className={styles.muted}>
                {draft.locked
                  ? 'The new version takes effect on its date. Days before it keep the price they had. To correct a price, use the date it took effect.'
                  : 'Prices are in US dollars. The most specific price wins: one for a region beats one for every region, and one for a version beats one for every version.'}
              </Text>
              <div className={styles.formGrid}>
                <Field label="Cloud" required hint={knownCloud ? undefined : 'A new cloud or provider, such as openai'}>
                  <Input value={draft.cloud} onChange={set('cloud')} disabled={draft.locked} list="pricing-clouds" />
                </Field>
                <datalist id="pricing-clouds">
                  {clouds.map((cloud) => <option key={cloud.key} value={cloud.key}>{cloud.label}</option>)}
                </datalist>
                <Field label="Publisher" hint="Leave empty for any publisher">
                  <Input value={draft.publisher} onChange={set('publisher')} disabled={draft.locked} placeholder="OpenAI" />
                </Field>
                <Field label="Model" required hint="* prices every model of the publisher">
                  <Input value={draft.model} onChange={set('model')} disabled={draft.locked} placeholder="gpt-4o" />
                </Field>
                <Field label="Version" hint="Leave empty for every version">
                  <Input value={draft.version} onChange={set('version')} disabled={draft.locked} placeholder="2024-11-20" />
                </Field>
                <Field label="Deployment type">
                  <Select value={draft.deploymentType} onChange={set('deploymentType')} disabled={draft.locked}>
                    <option value="">Any type</option>
                    {DEPLOYMENT_TYPES.map((type) => <option key={type} value={type}>{typeLabel(type)}</option>)}
                  </Select>
                </Field>
                <Field label="Regions" hint="Comma-separated, such as eastus2. Leave empty for every region">
                  <Input value={draft.regions} onChange={set('regions')} disabled={draft.locked} />
                </Field>
              </div>
              {draft.deployment && (
                <MessageBar intent="info">
                  <MessageBarBody>This price is for deployment {draft.deployment.split('/').pop()} only.</MessageBarBody>
                </MessageBar>
              )}
              <Field label="Charged by">
                <RadioGroup layout="horizontal" value={draft.kind} onChange={(_, data) => setDraft((current) => ({ ...current, kind: data.value as PriceKind }))}>
                  <Radio value="tokens" label="Tokens" disabled={draft.locked} />
                  <Radio value="ptu" label="PTU hours" disabled={draft.locked} />
                  <Radio value="monthly" label="Monthly amount" disabled={draft.locked || !draft.deployment} />
                </RadioGroup>
              </Field>
              {draft.kind === 'tokens' && (
                <div className={styles.formGrid}>
                  <Field label="Input per 1M tokens" required>
                    <Input type="number" min={0} step="any" value={draft.input} onChange={set('input')} contentBefore="$" />
                  </Field>
                  <Field label="Cached input per 1M tokens">
                    <Input type="number" min={0} step="any" value={draft.cached} onChange={set('cached')} contentBefore="$" />
                  </Field>
                  <Field label="Output per 1M tokens">
                    <Input type="number" min={0} step="any" value={draft.output} onChange={set('output')} contentBefore="$" />
                  </Field>
                </div>
              )}
              {draft.kind === 'ptu' && (
                <Field label="Per PTU an hour" required hint="The deployment's PTUs times this, for every hour of the month">
                  <Input type="number" min={0} step="any" value={draft.ptuHourly} onChange={set('ptuHourly')} contentBefore="$" />
                </Field>
              )}
              {draft.kind === 'monthly' && (
                <Field label="Each month" required hint="Such as a reservation's monthly cost">
                  <Input type="number" min={0} step="any" value={draft.monthly} onChange={set('monthly')} contentBefore="$" />
                </Field>
              )}
              <div className={styles.formGrid}>
                <Field label="In effect from" required>
                  <Input type="date" value={draft.effectiveFrom} onChange={set('effectiveFrom')} />
                </Field>
                <Field label="Source URL" required hint="Where this price comes from, such as a contract or a price page">
                  <Input type="url" value={draft.sourceUrl} onChange={set('sourceUrl')} placeholder="https://" />
                </Field>
              </div>
              <Field label="Note" required hint="Why this price, for whoever reads the history">
                <Textarea value={draft.note} onChange={set('note')} resize="vertical" />
              </Field>
              {save.isError && (
                <MessageBar intent="error">
                  <MessageBarBody>
                    <MessageBarTitle>MOSAIC didn't save this price</MessageBarTitle>
                    {save.error instanceof Error ? save.error.message : 'Try again.'}
                  </MessageBarBody>
                </MessageBar>
              )}
            </DialogContent>
            <DialogActions>
              <Button onClick={onClose} disabled={save.isPending}>Cancel</Button>
              <Button appearance="primary" type="submit" disabled={save.isPending}>
                {save.isPending ? 'Saving…' : 'Save price'}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  )
}

function PricesTab({
  cloud,
  clouds,
  onCloud,
  onOverride,
  onOpenBillingRun,
}: {
  cloud: string
  clouds: PricingCloud[]
  onCloud: (cloud: string) => void
  onOverride: (price: PriceView) => void
  onOpenBillingRun: (runId: string) => void
}) {
  const api = useMosaicApi()
  const [filter, setFilter] = useState('')
  const prices = useQuery({ queryKey: ['pricing', 'prices', cloud], queryFn: () => api.listPrices(cloud) })
  const lines = useMemo(() => {
    const query = filter.trim().toLowerCase()
    return (prices.data?.lines ?? []).filter((line) => {
      const price = line.current ?? line.upcoming
      if (!price || !query) return Boolean(price)
      return [price.model, price.publisher, price.version, price.deploymentType, ...price.aliases]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query))
    })
  }, [prices.data, filter])
  return (
    <div className={styles.stack}>
      <div className={styles.controls}>
        <label className={styles.control}>
          <span>Cloud</span>
          <Select value={cloud} onChange={(event) => onCloud(event.target.value)}>
            {clouds.map((item) => (
              <option key={item.key} value={item.key}>{`${item.label} (${plural(item.prices, 'price')})`}</option>
            ))}
          </Select>
        </label>
        <label className={styles.control}>
          <span>Find a model</span>
          <Input value={filter} onChange={(_, data) => setFilter(data.value)} placeholder="gpt-4o, Phi-4, Standard…" />
        </label>
      </div>
      {prices.isPending && <Loading label="Loading prices" />}
      {prices.isError && <ErrorState error={prices.error} />}
      {prices.data && lines.length === 0 && (
        <EmptyState title={filter ? 'No price matches' : `No prices for ${prices.data.cloudLabel} yet`}>
          {filter
            ? 'Try another model name, or clear the search.'
            : 'Add a price for each model this cloud serves. Until then its deployments have no cost.'}
        </EmptyState>
      )}
      {lines.length > 0 && <PriceTable lines={lines} onOverride={onOverride} onOpenBillingRun={onOpenBillingRun} />}
    </div>
  )
}

function UnpricedTab({ onPrice, onEndpoint }: { onPrice: (row: UnpricedDeploymentRow) => void; onEndpoint: (row: UnpricedDeploymentRow) => void }) {
  const api = useMosaicApi()
  const unpriced = useQuery({ queryKey: ['pricing', 'unpriced'], queryFn: api.getUnpricedDeployments })
  if (unpriced.isPending) return <Loading label="Loading unpriced deployments" />
  if (unpriced.isError) return <ErrorState error={unpriced.error} />
  const report = unpriced.data
  return (
    <div className={styles.stack}>
      <Text className={styles.muted}>
        {`${report.pricedDeployments} of ${plural(report.deployments, 'deployment')} MOSAIC knows ${report.pricedDeployments === 1 ? 'has' : 'have'} a price today. Usage on the ones below has no cost until they get one, and is never counted as $0. Calls and tokens cover the last ${report.days} days.`}
      </Text>
      {report.rows.length === 0 ? (
        <EmptyState title="Every deployment has a price">MOSAIC can put a cost on all the usage it measures.</EmptyState>
      ) : (
        <div className="table-scroll">
          <table aria-label="Unpriced deployments">
            <thead>
              <tr>
                <th>Deployment</th>
                <th>Model</th>
                <th>Deployment type</th>
                <th>Cloud and region</th>
                <th>Why it has no price</th>
                <th className={styles.number}>Tokens</th>
                <th><span className="sr-only">Fix</span></th>
              </tr>
            </thead>
            <tbody>
              {report.rows.map((row) => (
                <tr key={row.key}>
                  <td>
                    <strong>{row.label}</strong>
                    <Text block size={200} className={styles.muted}>
                      {row.kind === 'api' ? `Adopted API on ${row.gatewayName ?? 'a gateway'}` : row.endpointName}
                      {row.declared && ' · declared'}
                    </Text>
                  </td>
                  <td>{row.model ? `${row.model}${row.version ? ` ${row.version}` : ''}` : '—'}</td>
                  <td>{typeLabel(row.deploymentType, row.kind === 'api' ? '—' : 'Unknown')}</td>
                  <td>{row.kind === 'api' ? '—' : `${row.cloud ?? 'Unknown cloud'}${row.region ? ` · ${row.region}` : ''}`}</td>
                  <td>
                    <Badge appearance="tint" color="warning">{REASON_LABELS[row.reason]}</Badge>
                    <Text block size={200} className={styles.reason}>{row.message}</Text>
                    <Text block size={200} className={styles.muted}>
                      {row.kind === 'api' ? 'Imported API usage has no matched deployment. Review its inventory and model linkage; adding an unrelated price does not price these calls.' : REASON_HELP[row.reason]}
                    </Text>
                  </td>
                  <td className={styles.number}>
                    {formatCount(row.totalTokens)}
                    <Text block size={200} className={styles.muted}>{plural(row.requests, 'call')}</Text>
                  </td>
                  <td className={styles.actions}>
                    {row.kind === 'deployment' && ENDPOINT_FIXES.has(row.reason) && (
                      <Button size="small" onClick={() => onEndpoint(row)}>Set facts</Button>
                    )}
                    {row.kind === 'deployment' && PRICE_FIXES.has(row.reason) && (
                      <Button size="small" onClick={() => onPrice(row)}>Add price</Button>
                    )}
                    {row.kind === 'deployment' && (row.reason === 'unknownDeployment' || row.reason === 'noModel') && (
                      <RouterLink to="/models?tab=endpoints">Review endpoint</RouterLink>
                    )}
                    {row.kind === 'api' && <RouterLink to="/models?tab=imported">Review imported API</RouterLink>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

interface EndpointDraft {
  cloud: string
  customCloud: string
  region: string
  deployments: Record<string, { deploymentType: string; capacity: string }>
}

function endpointDraft(endpoint: EndpointPricingView): EndpointDraft {
  const deployments: EndpointDraft['deployments'] = {}
  for (const deployment of endpoint.deployments) {
    if (deployment.deploymentTypeSource !== 'observed') {
      deployments[deployment.deploymentName] = {
        deploymentType: deployment.deploymentType ?? '',
        capacity: deployment.capacity == null ? '' : String(deployment.capacity),
      }
    }
  }
  return {
    cloud: endpoint.cloudSource === 'override' ? endpoint.cloud ?? '' : '',
    customCloud: '',
    region: endpoint.regionSource === 'override' ? endpoint.region ?? '' : '',
    deployments,
  }
}

function EndpointEditor({ endpoint, clouds, onDone }: { endpoint: EndpointPricingView; clouds: PricingCloud[]; onDone: () => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(() => endpointDraft(endpoint))
  // The version the form's values came from. A refetch can bring a newer one while the form still
  // holds what it opened with, so a save sends this one and fails if anyone saved since.
  const [openedVersion, setOpenedVersion] = useState(() => endpoint.version ?? null)
  const save = useMutation({
    mutationFn: () => {
      const cloud = draft.cloud === '__custom' ? draft.customCloud.trim().toLowerCase() : draft.cloud
      const payload: EndpointPricingUpdate = {
        cloud: cloud || null,
        region: draft.region.trim() || null,
        deployments: Object.entries(draft.deployments).map(([deploymentName, item]) => ({
          deploymentName,
          deploymentType: item.deploymentType || null,
          capacity: item.capacity.trim() ? Number(item.capacity) : null,
        })),
        // The form holds every fact as it was when opened, so a save after someone else's must fail.
        version: openedVersion,
      }
      return api.updateEndpointPricing(endpoint.endpointId, payload)
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['pricing'] })
      await queryClient.invalidateQueries({ queryKey: ['analytics'] })
      onDone()
    },
    // Someone else's save leaves this form stale, so fetch what they saved.
    onError: () => queryClient.invalidateQueries({ queryKey: ['pricing', 'endpoints'] }),
  })
  const editable = Object.keys(draft.deployments)
  // Once their change has arrived, the form can start again from it.
  const reloadable = save.error instanceof ApiError && save.error.status === 409 && (endpoint.version ?? null) !== openedVersion
  const reload = () => {
    setDraft(endpointDraft(endpoint))
    setOpenedVersion(endpoint.version ?? null)
    save.reset()
  }
  return (
    <form
      className={styles.endpointEditor}
      onSubmit={(event) => {
        event.preventDefault()
        save.mutate()
      }}
    >
      <div className={styles.formGrid}>
        <Field label="Cloud" hint={endpoint.detectedCloud ? `The host says ${endpoint.detectedCloud}` : "The host doesn't say"}>
          <Select value={draft.cloud} onChange={(_, data) => setDraft((current) => ({ ...current, cloud: data.value }))}>
            <option value="">{endpoint.detectedCloud ? `As detected (${endpoint.detectedCloud})` : 'Not set'}</option>
            {clouds.map((cloud) => <option key={cloud.key} value={cloud.key}>{cloud.label}</option>)}
            <option value="__custom">Another cloud or provider…</option>
          </Select>
        </Field>
        {draft.cloud === '__custom' && (
          <Field label="Cloud name" hint="Lowercase letters, digits and hyphens, such as openai">
            <Input value={draft.customCloud} onChange={(_, data) => setDraft((current) => ({ ...current, customCloud: data.value }))} />
          </Field>
        )}
        <Field label="Region" hint="Leave empty to use the endpoint's own">
          <Input value={draft.region} onChange={(_, data) => setDraft((current) => ({ ...current, region: data.value }))} placeholder="eastus2" />
        </Field>
      </div>
      {editable.length > 0 && (
        <div className={styles.deploymentFacts}>
          <Text weight="semibold">Deployments Azure doesn&apos;t describe to MOSAIC</Text>
          {editable.map((name) => {
            const item = draft.deployments[name]
            return (
              <div key={name} className={styles.formGrid}>
                <Field label={`${name} deployment type`}>
                  <Select
                    value={item.deploymentType}
                    onChange={(_, data) => setDraft((current) => ({
                      ...current,
                      deployments: { ...current.deployments, [name]: { ...item, deploymentType: data.value } },
                    }))}
                  >
                    <option value="">Unknown</option>
                    {DEPLOYMENT_TYPES.map((type) => <option key={type} value={type}>{typeLabel(type)}</option>)}
                  </Select>
                </Field>
                {isProvisioned(item.deploymentType) && (
                  <Field label={`${name} PTUs`}>
                    <Input
                      type="number"
                      min={1}
                      value={item.capacity}
                      onChange={(_, data) => setDraft((current) => ({
                        ...current,
                        deployments: { ...current.deployments, [name]: { ...item, capacity: data.value } },
                      }))}
                    />
                  </Field>
                )}
              </div>
            )
          })}
        </div>
      )}
      {save.isError && (
        <MessageBar intent="error">
          <MessageBarBody>{save.error instanceof Error ? save.error.message : 'Saving failed.'}</MessageBarBody>
        </MessageBar>
      )}
      <div className={styles.editorActions}>
        {reloadable && <Button onClick={reload}>Load the latest</Button>}
        <Button onClick={onDone} disabled={save.isPending}>Cancel</Button>
        <Button appearance="primary" type="submit" disabled={save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</Button>
      </div>
    </form>
  )
}

function EndpointsTab({ clouds, focus }: { clouds: PricingCloud[]; focus: string | null }) {
  const api = useMosaicApi()
  const endpoints = useQuery({ queryKey: ['pricing', 'endpoints'], queryFn: api.listEndpointPricing })
  const [editing, setEditing] = useState<string | null>(focus)
  if (endpoints.isPending) return <Loading label="Loading endpoints" />
  if (endpoints.isError) return <ErrorState error={endpoints.error} />
  if (endpoints.data.length === 0) {
    return <EmptyState title="No model endpoints yet">Register a model endpoint, and MOSAIC prices its deployments by the cloud its host is in.</EmptyState>
  }
  return (
    <div className={styles.stack}>
      <Text className={styles.muted}>
        MOSAIC reads an endpoint&apos;s cloud from its host: .azure.com is Azure Commercial and .azure.us is Azure Government. Choose another cloud for a sovereign cloud or another provider, and price it on the Prices tab.
      </Text>
      <div className="table-scroll">
        <table aria-label="Endpoint pricing">
          <thead>
            <tr>
              <th>Endpoint</th>
              <th>Cloud</th>
              <th>Region</th>
              <th>Deployments priced</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {endpoints.data.map((endpoint) => {
              const priced = endpoint.deployments.filter((deployment) => deployment.priced).length
              const open = editing === endpoint.endpointId
              return (
                <Fragment key={endpoint.endpointId}>
                  <tr>
                    <td>
                      <strong>{endpoint.name}</strong>
                      <Text block size={200} className={styles.muted}>{endpoint.host ?? endpoint.provider}</Text>
                    </td>
                    <td>
                      {endpoint.cloud ? endpoint.cloudLabel : 'Unknown'}
                      <Text block size={200} className={styles.muted}>
                        {endpoint.cloudSource === 'override' ? 'Set by an administrator' : endpoint.cloudSource === 'detected' ? 'From the host' : 'Set one to price it'}
                      </Text>
                    </td>
                    <td>
                      {endpoint.region ?? 'Unknown'}
                      {endpoint.regionSource === 'override' && <Text block size={200} className={styles.muted}>Set by an administrator</Text>}
                    </td>
                    <td>
                      {`${priced} of ${endpoint.deployments.length}`}
                      {endpoint.deployments.some((deployment) => !deployment.priced) && (
                        <Text block size={200} className={styles.muted}>
                          {endpoint.deployments.filter((deployment) => !deployment.priced).map((deployment) => deployment.deploymentName).join(', ')} unpriced
                        </Text>
                      )}
                    </td>
                    <td className={styles.actions}>
                      <Button size="small" aria-expanded={open} onClick={() => setEditing(open ? null : endpoint.endpointId)}>
                        {open ? 'Close' : 'Edit'}
                      </Button>
                    </td>
                  </tr>
                  {open && (
                    <tr className={styles.historyRow}>
                      <td colSpan={5}>
                        <EndpointEditor endpoint={endpoint} clouds={clouds} onDone={() => setEditing(null)} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const RUN_STATUS_LABELS: Record<BillingRunStatus, string> = {
  queued: 'Running',
  running: 'Running',
  readyForReview: 'Ready for review',
  published: 'Published',
  failed: 'Failed',
  superseded: 'Superseded',
}

function runStatusColor(status: BillingRunStatus): 'success' | 'warning' | 'danger' | 'informative' | 'subtle' {
  switch (status) {
    case 'published':
      return 'success'
    case 'readyForReview':
      return 'warning'
    case 'failed':
      return 'danger'
    case 'running':
    case 'queued':
      return 'informative'
    default:
      return 'subtle'
  }
}

function connectionScopeSummary(conn: BillingConnectionView): string {
  if (conn.provider === 'azure') {
    return conn.azureScope ?? 'Azure scope not set'
  }
  if (conn.provider === 'aws') {
    const prefix = conn.awsCurPrefix ? `/${conn.awsCurPrefix.replace(/^\/+/, '')}` : ''
    return conn.awsCurBucket ? `s3://${conn.awsCurBucket}${prefix}` : (conn.awsAccountId ?? 'AWS CUR')
  }
  if (conn.provider === 'gcp') {
    return [conn.gcpProjectId, conn.gcpDatasetId, conn.gcpTableId].filter(Boolean).join('.') || 'GCP BigQuery'
  }
  return '—'
}

function formatCandidateExistingRate(cand: BillingCandidateRate): string {
  if (!cand.existingPriceId && cand.existingOrigin == null) {
    return 'No price'
  }
  if (cand.existingMonthlyAmount != null) {
    return `${formatRate(cand.existingMonthlyAmount)} / month`
  }
  if (cand.existingPtuHourly != null) {
    return `${formatRate(cand.existingPtuHourly)} / PTU hr`
  }
  const parts: string[] = []
  if (cand.existingInputPerMillion != null) parts.push(`${formatRate(cand.existingInputPerMillion)} in`)
  if (cand.existingCachedInputPerMillion != null) parts.push(`${formatRate(cand.existingCachedInputPerMillion)} cached`)
  if (cand.existingOutputPerMillion != null) parts.push(`${formatRate(cand.existingOutputPerMillion)} out`)
  return parts.length ? parts.join(' · ') : 'No price'
}

function formatCandidateProposedRate(cand: BillingCandidateRate): string {
  if (cand.monthlyAmount != null) {
    return `${formatRate(cand.monthlyAmount)} / month`
  }
  if (cand.ptuHourly != null) {
    return `${formatRate(cand.ptuHourly)} / PTU hr`
  }
  const parts: string[] = []
  if (cand.inputPerMillion != null) parts.push(`${formatRate(cand.inputPerMillion)} in`)
  if (cand.cachedInputPerMillion != null) parts.push(`${formatRate(cand.cachedInputPerMillion)} cached`)
  if (cand.outputPerMillion != null) parts.push(`${formatRate(cand.outputPerMillion)} out`)
  return parts.length ? parts.join(' · ') : '—'
}

function formatCandidateQuantities(cand: BillingCandidateRate): string {
  const parts: string[] = []
  if (cand.inputTokens != null && cand.inputTokens > 0) parts.push(`${formatCount(cand.inputTokens)} in`)
  if (cand.cachedInputTokens != null && cand.cachedInputTokens > 0) parts.push(`${formatCount(cand.cachedInputTokens)} cached`)
  if (cand.outputTokens != null && cand.outputTokens > 0) parts.push(`${formatCount(cand.outputTokens)} out`)
  if (cand.ptuHours != null && cand.ptuHours > 0) parts.push(`${formatCount(cand.ptuHours)} PTU hrs`)
  if (cand.commitmentMonths != null && cand.commitmentMonths > 0) parts.push(`${cand.commitmentMonths} commitment mo`)
  return parts.length ? parts.join(' · ') : 'No billable quantity'
}

function defaultMappingsForRun(run: BillingRunDetailView): Record<string, BillingCandidateMapping> {
  const existingById = new Map(run.mappings.map((item) => [item.candidateId, item]))
  const record: Record<string, BillingCandidateMapping> = {}
  for (const cand of run.candidates) {
    const saved = existingById.get(cand.candidateId)
    if (saved) {
      record[cand.candidateId] = {
        candidateId: cand.candidateId,
        scope: saved.scope,
        deploymentKey: saved.deploymentKey ?? cand.suggestedDeploymentKey ?? null,
        cloud: saved.cloud ?? cand.pricingCloud,
        confirmed: Boolean(saved.confirmed),
        replaceManualOverride: Boolean(saved.replaceManualOverride),
      }
    } else {
      record[cand.candidateId] = {
        candidateId: cand.candidateId,
        scope: cand.publishable ? cand.suggestedScope : 'excluded',
        deploymentKey: cand.suggestedDeploymentKey ?? null,
        cloud: cand.pricingCloud,
        confirmed: cand.publishable && cand.mappingConfidence === 'exact' && !cand.conflictsWithManualOverride,
        replaceManualOverride: false,
      }
    }
  }
  return record
}

function BillingImportWorkspace({
  defaultCloud,
  clouds,
  onClose,
  onImported,
}: {
  defaultCloud: string
  clouds: PricingCloud[]
  onClose: () => void
  onImported: (runId: string) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [provider, setProvider] = useState<BillingProvider>('azure')
  const [pricingCloud, setPricingCloud] = useState(defaultCloud || 'commercial')
  const [sourceMonth, setSourceMonth] = useState(previousCompleteMonth)
  const [sourceScope, setSourceScope] = useState('')
  const [fileName, setFileName] = useState('upload.csv')
  const [contentText, setContentText] = useState('')
  const [contentBase64, setContentBase64] = useState<string | null>(null)
  const [force, setForce] = useState(false)
  const [dirty, setDirty] = useState(false)

  const templates = useQuery({
    queryKey: ['pricing', 'billing', 'templates'],
    queryFn: () => api.listBillingTemplates(),
  })

  const activeTemplate = useMemo(
    () => templates.data?.find((item) => item.provider === provider) ?? null,
    [templates.data, provider],
  )

  const importMutation = useMutation({
    mutationFn: () =>
      api.importBillingData({
        provider,
        pricingCloud: pricingCloud.trim().toLowerCase(),
        sourceMonth: sourceMonth.trim() || null,
        fileName: fileName.trim() || 'upload.csv',
        sourceScope: sourceScope.trim() || null,
        contentText: contentBase64 ? null : contentText,
        contentBase64,
        force,
      }),
    onSuccess: async (run) => {
      queryClient.setQueryData(['pricing', 'billing', 'run', run.id], run)
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'runs'] })
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'overview'] })
      completeWorkspaceNavigation(() => onImported(run.id))
    },
  })

  async function handleFileSelect(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    setDirty(true)
    setFileName(file.name)
    const lower = file.name.toLowerCase()
    if (lower.endsWith('.parquet') || lower.endsWith('.gz')) {
      const buffer = await file.arrayBuffer()
      const bytes = new Uint8Array(buffer)
      let binary = ''
      for (const byte of bytes) {
        binary += String.fromCharCode(byte)
      }
      setContentBase64(btoa(binary))
      setContentText('')
    } else {
      const text = await file.text()
      setContentText(text)
      setContentBase64(null)
    }
  }

  const canSubmit = Boolean(
    pricingCloud.trim() &&
      sourceMonth.trim() &&
      fileName.trim() &&
      (contentText.trim() || contentBase64),
  )

  return (
    <WorkspaceEditorFrame
      title="Import billing file"
      description="Normalize a completed calendar month’s USD cloud billing export into reviewable model rate candidates. Importing creates a draft for review and never changes active prices until you publish."
      backLabel="Back to Billing data"
      onBack={onClose}
      onCancel={onClose}
      dirty={dirty}
      pending={importMutation.isPending}
      pendingLabel="Normalizing billing data…"
      error={
        importMutation.isError
          ? importMutation.error instanceof Error
            ? importMutation.error.message
            : 'Unable to import billing file.'
          : undefined
      }
      primaryAction={{
        label: 'Import & review',
        disabled: !canSubmit,
        onClick: () => importMutation.mutate(),
      }}
    >
      <form
        className={styles.workspaceForm}
        onChangeCapture={() => setDirty(true)}
        onSubmit={(event) => {
          event.preventDefault()
          if (canSubmit) importMutation.mutate()
        }}
      >
        <MessageBar intent="info" layout="multiline">
          <MessageBarBody>
            MOSAIC accepts USD-denominated billing exports for a completed calendar month and derives quantity-weighted effective rates from contract-discounted and amortized usage (excluding taxes and one-off credits). Raw upload streams are discarded after normalization.
          </MessageBarBody>
        </MessageBar>

        <div className={styles.workspaceSection}>
          <h3>Source & period</h3>
          <div className={styles.formGrid}>
            <Field label="Billing source" required>
              <Select
                value={provider}
                onChange={(_, data) => setProvider(data.value as BillingProvider)}
              >
                <option value="azure">Azure Cost Management CSV</option>
                <option value="aws">AWS CUR / CUR 2.0 (.csv, .csv.gz, .parquet)</option>
                <option value="gcp">GCP BigQuery Billing Export (.csv, .json, .ndjson)</option>
                <option value="mosaicTemplate">MOSAIC Billing Template (mosaic_billing_v1)</option>
              </Select>
            </Field>
            <Field label="Target pricing cloud" required hint="Such as commercial, government, aws, or gcp">
              <Input
                value={pricingCloud}
                onChange={(_, data) => setPricingCloud(data.value)}
                list="billing-import-clouds"
              />
            </Field>
            <datalist id="billing-import-clouds">
              {clouds.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </datalist>
            <Field label="Source month" required hint="Completed UTC calendar month (YYYY-MM)">
              <Input
                value={sourceMonth}
                onChange={(_, data) => setSourceMonth(data.value)}
                placeholder="2026-09"
              />
            </Field>
            <Field label="Source scope" hint="Optional subscription, AWS account, or GCP billing account">
              <Input
                value={sourceScope}
                onChange={(_, data) => setSourceScope(data.value)}
                placeholder="/subscriptions/..."
              />
            </Field>
            <Field label="File name" required>
              <Input
                value={fileName}
                onChange={(_, data) => setFileName(data.value)}
                placeholder="upload.csv"
              />
            </Field>
          </div>
        </div>

        <div className={styles.workspaceSection}>
          <h3>Billing dataset</h3>
          <Field label="Upload billing file" hint="Select a .csv, .csv.gz, .parquet, .json, or .ndjson export file">
            <input
              type="file"
              aria-label="Upload billing file"
              accept=".csv,.gz,.parquet,.json,.ndjson,text/csv,application/json"
              onChange={(event) => void handleFileSelect(event)}
            />
          </Field>
          <Field
            label="Billing data content"
            hint="Paste CSV or JSON billing rows directly, or inspect a text file before importing"
          >
            <Textarea
              value={contentText}
              onChange={(_, data) => {
                setContentText(data.value)
                setContentBase64(null)
              }}
              rows={8}
              resize="vertical"
              placeholder="Paste CSV or JSON billing rows…"
            />
          </Field>
          <Checkbox
            label="Create a new draft even if an identical file was already imported"
            checked={force}
            onChange={(_, data) => setForce(Boolean(data.checked))}
          />
        </div>

        {activeTemplate && (
          <div className={styles.workspaceSection}>
            <h3>{activeTemplate.title} schema & guidance</h3>
            <Text size={200} className={styles.muted}>
              Supported formats: {activeTemplate.supportedFormats.join(', ')} · Required columns:{' '}
              {activeTemplate.requiredColumns.join(', ')}
            </Text>
            {activeTemplate.notes.map((note) => (
              <Text key={note} size={200} className={styles.muted}>
                {note}
              </Text>
            ))}
            {activeTemplate.extractionQuery && (
              <div>
                <Text weight="semibold" size={200}>
                  BigQuery parameterized extraction query
                </Text>
                <pre className={styles.codeBlock}>{activeTemplate.extractionQuery}</pre>
              </div>
            )}
            {activeTemplate.sampleCsv && (
              <div>
                <div className={styles.billingToolbar}>
                  <Text weight="semibold" size={200}>
                    Sample CSV
                  </Text>
                  <Button
                    size="small"
                    onClick={() => {
                      setDirty(true)
                      setContentText(activeTemplate.sampleCsv ?? '')
                      setContentBase64(null)
                    }}
                  >
                    Use sample CSV
                  </Button>
                </div>
                <pre className={styles.codeBlock}>{activeTemplate.sampleCsv}</pre>
              </div>
            )}
          </div>
        )}
      </form>
    </WorkspaceEditorFrame>
  )
}

function BillingConnectionWorkspace({
  connection,
  defaultCloud,
  clouds,
  onClose,
}: {
  connection: BillingConnectionView | null
  defaultCloud: string
  clouds: PricingCloud[]
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [openedVersion, setOpenedVersion] = useState<string | null>(connection?.version ?? null)
  const [name, setName] = useState(connection?.name ?? '')
  const [provider, setProvider] = useState<ConnectedBillingProvider>(connection?.provider ?? 'azure')
  const [pricingCloud, setPricingCloud] = useState(connection?.pricingCloud ?? (defaultCloud || 'commercial'))

  const [azureCloud, setAzureCloud] = useState<'commercial' | 'government'>(connection?.azureCloud ?? 'commercial')
  const [azureConnectionId, setAzureConnectionId] = useState(connection?.azureConnectionId ?? '')
  const [azureScope, setAzureScope] = useState(connection?.azureScope ?? '')
  const [azureRetrievalMode, setAzureRetrievalMode] = useState<AzureBillingRetrievalMode>(
    connection?.azureRetrievalMode ?? 'costDetailsApi',
  )
  const [azureExportManifestUrl, setAzureExportManifestUrl] = useState(connection?.azureExportManifestUrl ?? '')

  const [awsRoleArn, setAwsRoleArn] = useState(connection?.awsRoleArn ?? '')
  const [awsAccountId, setAwsAccountId] = useState(connection?.awsAccountId ?? '')
  const [awsCurBucket, setAwsCurBucket] = useState(connection?.awsCurBucket ?? '')
  const [awsCurPrefix, setAwsCurPrefix] = useState(connection?.awsCurPrefix ?? '')
  const [awsExportName, setAwsExportName] = useState(connection?.awsExportName ?? '')
  const [awsRegion, setAwsRegion] = useState(connection?.awsRegion ?? 'us-east-1')
  const [awsExternalId, setAwsExternalId] = useState(connection?.awsExternalId ?? '')
  const [awsAudience, setAwsAudience] = useState(connection?.awsAudience ?? '')

  const [gcpProjectId, setGcpProjectId] = useState(connection?.gcpProjectId ?? '')
  const [gcpDatasetId, setGcpDatasetId] = useState(connection?.gcpDatasetId ?? '')
  const [gcpTableId, setGcpTableId] = useState(connection?.gcpTableId ?? '')
  const [gcpBillingAccountId, setGcpBillingAccountId] = useState(connection?.gcpBillingAccountId ?? '')
  const [gcpLocation, setGcpLocation] = useState(connection?.gcpLocation ?? 'US')
  const [gcpWorkloadIdentityAudience, setGcpWorkloadIdentityAudience] = useState(
    connection?.gcpWorkloadIdentityAudience ?? '',
  )
  const [gcpServiceAccountEmail, setGcpServiceAccountEmail] = useState(connection?.gcpServiceAccountEmail ?? '')
  const [gcpMaxBytesBilled, setGcpMaxBytesBilled] = useState(
    connection?.gcpMaxBytesBilled != null ? String(connection.gcpMaxBytesBilled) : '1000000000',
  )
  const [credentialSecret, setCredentialSecret] = useState('')
  const [clearStoredSecret, setClearStoredSecret] = useState(false)

  const [scheduleEnabled, setScheduleEnabled] = useState(Boolean(connection?.schedule?.enabled))
  const [dayOfMonth, setDayOfMonth] = useState(String(connection?.schedule?.dayOfMonth ?? 3))
  const [timeUtc, setTimeUtc] = useState(connection?.schedule?.timeUtc ?? '04:00')
  const [timeZone, setTimeZone] = useState(connection?.schedule?.timeZone ?? 'UTC')
  const [costNoticeAcknowledged, setCostNoticeAcknowledged] = useState(
    connection ? connection.costNoticeAcknowledged : false,
  )
  const [dirty, setDirty] = useState(false)

  function applyConnectionState(latest: BillingConnectionView) {
    setOpenedVersion(latest.version ?? null)
    setName(latest.name)
    setProvider(latest.provider)
    setPricingCloud(latest.pricingCloud)
    setAzureCloud(latest.azureCloud ?? 'commercial')
    setAzureConnectionId(latest.azureConnectionId ?? '')
    setAzureScope(latest.azureScope ?? '')
    setAzureRetrievalMode(latest.azureRetrievalMode ?? 'costDetailsApi')
    setAzureExportManifestUrl(latest.azureExportManifestUrl ?? '')
    setAwsRoleArn(latest.awsRoleArn ?? '')
    setAwsAccountId(latest.awsAccountId ?? '')
    setAwsCurBucket(latest.awsCurBucket ?? '')
    setAwsCurPrefix(latest.awsCurPrefix ?? '')
    setAwsExportName(latest.awsExportName ?? '')
    setAwsRegion(latest.awsRegion ?? 'us-east-1')
    setAwsExternalId(latest.awsExternalId ?? '')
    setAwsAudience(latest.awsAudience ?? '')
    setGcpProjectId(latest.gcpProjectId ?? '')
    setGcpDatasetId(latest.gcpDatasetId ?? '')
    setGcpTableId(latest.gcpTableId ?? '')
    setGcpBillingAccountId(latest.gcpBillingAccountId ?? '')
    setGcpLocation(latest.gcpLocation ?? 'US')
    setGcpWorkloadIdentityAudience(latest.gcpWorkloadIdentityAudience ?? '')
    setGcpServiceAccountEmail(latest.gcpServiceAccountEmail ?? '')
    setGcpMaxBytesBilled(latest.gcpMaxBytesBilled != null ? String(latest.gcpMaxBytesBilled) : '1000000000')
    setCredentialSecret('')
    setClearStoredSecret(false)
    setScheduleEnabled(Boolean(latest.schedule?.enabled))
    setDayOfMonth(String(latest.schedule?.dayOfMonth ?? 3))
    setTimeUtc(latest.schedule?.timeUtc ?? '04:00')
    setTimeZone(latest.schedule?.timeZone ?? 'UTC')
    setCostNoticeAcknowledged(latest.costNoticeAcknowledged)
    setDirty(false)
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      const schedule = {
        enabled: scheduleEnabled,
        dayOfMonth: Number(dayOfMonth) || 3,
        timeUtc: timeUtc.trim() || '04:00',
        timeZone: timeZone.trim() || 'UTC',
      }
      const common = {
        name: name.trim(),
        pricingCloud: pricingCloud.trim().toLowerCase(),
        azureCloud: provider === 'azure' ? azureCloud : null,
        azureConnectionId: provider === 'azure' ? azureConnectionId.trim() || null : null,
        azureScope: provider === 'azure' ? azureScope.trim() || null : null,
        azureRetrievalMode: provider === 'azure' ? azureRetrievalMode : null,
        azureExportManifestUrl:
          provider === 'azure' && azureExportManifestUrl.trim() ? azureExportManifestUrl.trim() : null,
        awsRoleArn: provider === 'aws' ? awsRoleArn.trim() || null : null,
        awsAccountId: provider === 'aws' ? awsAccountId.trim() || null : null,
        awsCurBucket: provider === 'aws' ? awsCurBucket.trim() || null : null,
        awsCurPrefix: provider === 'aws' ? awsCurPrefix.trim() || null : null,
        awsExportName: provider === 'aws' ? awsExportName.trim() || null : null,
        awsRegion: provider === 'aws' ? awsRegion.trim() || null : null,
        awsExternalId: provider === 'aws' ? awsExternalId.trim() || null : null,
        awsAudience: provider === 'aws' ? awsAudience.trim() || null : null,
        gcpProjectId: provider === 'gcp' ? gcpProjectId.trim() || null : null,
        gcpDatasetId: provider === 'gcp' ? gcpDatasetId.trim() || null : null,
        gcpTableId: provider === 'gcp' ? gcpTableId.trim() || null : null,
        gcpBillingAccountId: provider === 'gcp' ? gcpBillingAccountId.trim() || null : null,
        gcpLocation: provider === 'gcp' ? gcpLocation.trim() || null : null,
        gcpWorkloadIdentityAudience:
          provider === 'gcp' ? gcpWorkloadIdentityAudience.trim() || null : null,
        gcpServiceAccountEmail: provider === 'gcp' ? gcpServiceAccountEmail.trim() || null : null,
        gcpMaxBytesBilled:
          provider === 'gcp' && gcpMaxBytesBilled.trim() ? Number(gcpMaxBytesBilled) : null,
        credentialSecret: credentialSecret || null,
        schedule,
        costNoticeAcknowledged,
      }
      if (connection) {
        const updatePayload: BillingConnectionUpdate = {
          ...common,
          clearStoredSecret,
          version: openedVersion,
        }
        return api.updateBillingConnection(connection.id, updatePayload)
      }
      const createPayload: BillingConnectionCreate = {
        ...common,
        provider,
      }
      return api.createBillingConnection(createPayload)
    },
    onSuccess: async () => {
      setCredentialSecret('')
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'connections'] })
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'overview'] })
      completeWorkspaceNavigation(onClose)
    },
  })

  const isConflict = saveMutation.error instanceof ApiError && saveMutation.error.status === 409

  async function reloadConnection() {
    if (!connection) return
    const latest = await api.getBillingConnection(connection.id)
    applyConnectionState(latest)
    saveMutation.reset()
  }

  const providerFieldsValid =
    provider === 'azure'
      ? Boolean(azureScope.trim() && (azureRetrievalMode !== 'exportManifest' || azureExportManifestUrl.trim()))
      : provider === 'aws'
        ? Boolean(awsRoleArn.trim() && awsCurBucket.trim())
        : Boolean(gcpProjectId.trim() && gcpDatasetId.trim() && gcpTableId.trim())

  const canSave = Boolean(name.trim() && pricingCloud.trim() && costNoticeAcknowledged && providerFieldsValid)

  return (
    <WorkspaceEditorFrame
      title={connection ? `Edit ${connection.name}` : 'New billing connection'}
      description="Configure read-only access to your cloud provider’s billing dataset. Credentials are stored only in the MOSAIC management vault and never exposed to gateways or browser storage."
      backLabel="Back to Billing data"
      onBack={onClose}
      onCancel={onClose}
      dirty={dirty}
      pending={saveMutation.isPending}
      error={
        saveMutation.isError
          ? saveMutation.error instanceof Error
            ? saveMutation.error.message
            : 'Unable to save billing connection.'
          : undefined
      }
      actions={
        isConflict && connection ? (
          <Button onClick={() => void reloadConnection()}>Reload latest connection</Button>
        ) : undefined
      }
      primaryAction={{
        label: connection ? 'Save connection' : 'Create connection',
        disabled: !canSave,
        onClick: () => saveMutation.mutate(),
      }}
    >
      <form
        className={styles.workspaceForm}
        onChangeCapture={() => setDirty(true)}
        onSubmit={(event) => {
          event.preventDefault()
          if (canSave) saveMutation.mutate()
        }}
      >
        <div className={styles.workspaceSection}>
          <h3>Connection identity</h3>
          <div className={styles.formGrid}>
            <Field label="Connection name" required>
              <Input value={name} onChange={(_, data) => setName(data.value)} maxLength={120} />
            </Field>
            <Field label="Billing provider" required>
              <Select
                value={provider}
                disabled={Boolean(connection)}
                onChange={(_, data) => {
                  const next = data.value as ConnectedBillingProvider
                  setProvider(next)
                  if (next === 'aws' && pricingCloud === 'commercial') setPricingCloud('aws')
                  if (next === 'gcp' && (pricingCloud === 'commercial' || pricingCloud === 'aws')) {
                    setPricingCloud('gcp')
                  }
                }}
              >
                <option value="azure">Azure Cost Management</option>
                <option value="aws">AWS CUR 2.0 (S3)</option>
                <option value="gcp">GCP BigQuery Billing Export</option>
              </Select>
            </Field>
            <Field label="Target pricing cloud" required hint="Cloud key these rates price, such as commercial, aws, or gcp">
              <Input
                value={pricingCloud}
                onChange={(_, data) => setPricingCloud(data.value)}
                list="billing-connection-clouds"
              />
            </Field>
            <datalist id="billing-connection-clouds">
              {clouds.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </datalist>
          </div>
        </div>

        {provider === 'azure' && (
          <div className={styles.workspaceSection}>
            <h3>Azure Cost Management settings</h3>
            <div className={styles.formGrid}>
              <Field label="Azure cloud" required>
                <Select
                  value={azureCloud}
                  onChange={(_, data) =>
                    setAzureCloud(data.value === 'government' ? 'government' : 'commercial')
                  }
                >
                  <option value="commercial">Azure Commercial</option>
                  <option value="government">Azure Government</option>
                </Select>
              </Field>
              <Field label="Azure management connection ID" hint="Leave empty to use the default Azure identity">
                <Input
                  value={azureConnectionId}
                  onChange={(_, data) => setAzureConnectionId(data.value)}
                />
              </Field>
              <Field
                label="Billing or subscription scope"
                required
                hint="Subscription (/subscriptions/...) or billing account/profile scope. Management Groups are unsupported for amortized Cost Details."
              >
                <Input
                  value={azureScope}
                  onChange={(_, data) => setAzureScope(data.value)}
                  placeholder="/subscriptions/00000000-0000-0000-0000-000000000000"
                />
              </Field>
              <Field label="Retrieval mode">
                <Select
                  value={azureRetrievalMode}
                  onChange={(_, data) => setAzureRetrievalMode(data.value as AzureBillingRetrievalMode)}
                >
                  <option value="costDetailsApi">Cost Details API (AmortizedCost)</option>
                  <option value="exportManifest">Export manifest</option>
                </Select>
              </Field>
              {azureRetrievalMode === 'exportManifest' && (
                <Field label="Export manifest URL" required hint="HTTPS URL of the Azure Cost Management export manifest">
                  <Input
                    value={azureExportManifestUrl}
                    onChange={(_, data) => setAzureExportManifestUrl(data.value)}
                    placeholder="https://"
                  />
                </Field>
              )}
            </div>
          </div>
        )}

        {provider === 'aws' && (
          <div className={styles.workspaceSection}>
            <h3>AWS CUR 2.0 (S3) settings</h3>
            <div className={styles.formGrid}>
              <Field label="IAM Role ARN" required hint="arn:aws:iam::<accountId>:role/<roleName>">
                <Input
                  value={awsRoleArn}
                  onChange={(_, data) => setAwsRoleArn(data.value)}
                  placeholder="arn:aws:iam::123456789012:role/MosaicBillingReadRole"
                />
              </Field>
              <Field label="AWS Account ID" hint="12-digit account number (inferred from role ARN if omitted)">
                <Input
                  value={awsAccountId}
                  onChange={(_, data) => setAwsAccountId(data.value)}
                  placeholder="123456789012"
                />
              </Field>
              <Field label="S3 CUR Bucket" required>
                <Input
                  value={awsCurBucket}
                  onChange={(_, data) => setAwsCurBucket(data.value)}
                  placeholder="contoso-cur-exports"
                />
              </Field>
              <Field label="S3 Prefix">
                <Input
                  value={awsCurPrefix}
                  onChange={(_, data) => setAwsCurPrefix(data.value)}
                  placeholder="cur2/mosaic"
                />
              </Field>
              <Field label="Export name">
                <Input
                  value={awsExportName}
                  onChange={(_, data) => setAwsExportName(data.value)}
                  placeholder="mosaic-cur2"
                />
              </Field>
              <Field label="AWS Region">
                <Input
                  value={awsRegion}
                  onChange={(_, data) => setAwsRegion(data.value)}
                  placeholder="us-east-1"
                />
              </Field>
              <Field
                label="External ID"
                hint="Leave blank for built-in OIDC authentication. External IDs require a custom signed AssumeRole session provider."
              >
                <Input value={awsExternalId} onChange={(_, data) => setAwsExternalId(data.value)} />
              </Field>
              <Field label="OIDC Audience">
                <Input value={awsAudience} onChange={(_, data) => setAwsAudience(data.value)} />
              </Field>
            </div>
          </div>
        )}

        {provider === 'gcp' && (
          <div className={styles.workspaceSection}>
            <h3>GCP BigQuery Billing Export settings</h3>
            <div className={styles.formGrid}>
              <Field label="BigQuery Project ID" required>
                <Input
                  value={gcpProjectId}
                  onChange={(_, data) => setGcpProjectId(data.value)}
                  placeholder="contoso-billing-prj"
                />
              </Field>
              <Field label="BigQuery Dataset ID" required>
                <Input
                  value={gcpDatasetId}
                  onChange={(_, data) => setGcpDatasetId(data.value)}
                  placeholder="billing_export"
                />
              </Field>
              <Field label="BigQuery Table ID" required>
                <Input
                  value={gcpTableId}
                  onChange={(_, data) => setGcpTableId(data.value)}
                  placeholder="gcp_billing_export_resource_v1"
                />
              </Field>
              <Field label="Billing Account ID">
                <Input
                  value={gcpBillingAccountId}
                  onChange={(_, data) => setGcpBillingAccountId(data.value)}
                  placeholder="012345-6789AB-CDEF01"
                />
              </Field>
              <Field label="BigQuery Location">
                <Input
                  value={gcpLocation}
                  onChange={(_, data) => setGcpLocation(data.value)}
                  placeholder="US"
                />
              </Field>
              <Field label="Workload Identity Audience">
                <Input
                  value={gcpWorkloadIdentityAudience}
                  onChange={(_, data) => setGcpWorkloadIdentityAudience(data.value)}
                />
              </Field>
              <Field label="Service Account Email">
                <Input
                  value={gcpServiceAccountEmail}
                  onChange={(_, data) => setGcpServiceAccountEmail(data.value)}
                />
              </Field>
              <Field label="Max bytes billed cap" hint="Bounds BigQuery query scan cost per run">
                <Input
                  type="number"
                  min={1000000}
                  value={gcpMaxBytesBilled}
                  onChange={(_, data) => setGcpMaxBytesBilled(data.value)}
                />
              </Field>
              <Field
                label="Credential secret (optional)"
                hint="Write-only JSON credential or token material stored in the management vault; never shown again."
              >
                <Input
                  type="password"
                  autoComplete="new-password"
                  value={credentialSecret}
                  onChange={(_, data) => setCredentialSecret(data.value)}
                />
              </Field>
            </div>
            {connection?.hasStoredSecret && (
              <Checkbox
                label="Clear stored credential secret"
                checked={clearStoredSecret}
                onChange={(_, data) => setClearStoredSecret(Boolean(data.checked))}
              />
            )}
          </div>
        )}

        <div className={styles.workspaceSection}>
          <h3>Monthly draft refresh schedule</h3>
          <Text size={200} className={styles.muted}>
            Scheduled refreshes only prepare a draft run for administrator review and never approve or activate rates automatically.
          </Text>
          <Checkbox
            label="Enable monthly scheduled draft refresh"
            checked={scheduleEnabled}
            onChange={(_, data) => setScheduleEnabled(Boolean(data.checked))}
          />
          {scheduleEnabled && (
            <div className={styles.formGrid}>
              <Field label="Day of month (1–28)">
                <Input
                  type="number"
                  min={1}
                  max={28}
                  value={dayOfMonth}
                  onChange={(_, data) => setDayOfMonth(data.value)}
                />
              </Field>
              <Field label="Time (UTC)">
                <Input
                  value={timeUtc}
                  onChange={(_, data) => setTimeUtc(data.value)}
                  placeholder="04:00"
                />
              </Field>
              <Field label="Time zone">
                <Input
                  value={timeZone}
                  onChange={(_, data) => setTimeZone(data.value)}
                  placeholder="UTC"
                />
              </Field>
            </div>
          )}
        </div>

        <div className={styles.workspaceSection}>
          <h3>Provider query cost acknowledgment</h3>
          <Checkbox
            label="I acknowledge that connected billing queries and export object reads can incur cloud provider charges."
            checked={costNoticeAcknowledged}
            onChange={(_, data) => setCostNoticeAcknowledged(Boolean(data.checked))}
          />
        </div>
      </form>
    </WorkspaceEditorFrame>
  )
}

function BillingRunReviewWorkspace({
  runId,
  onClose,
}: {
  runId: string
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()

  const runQuery = useQuery({
    queryKey: ['pricing', 'billing', 'run', runId],
    queryFn: () => api.getBillingRun(runId),
  })
  const endpointsQuery = useQuery({
    queryKey: ['pricing', 'endpoints'],
    queryFn: api.listEndpointPricing,
  })

  const [initializedForRun, setInitializedForRun] = useState<string | null>(null)
  const [openedVersion, setOpenedVersion] = useState<string | null>(null)
  const [openedPreviewRevision, setOpenedPreviewRevision] = useState<number | null>(null)
  const [mappings, setMappings] = useState<Record<string, BillingCandidateMapping>>({})
  const [mappingsDirty, setMappingsDirty] = useState(false)
  const [effectiveFrom, setEffectiveFrom] = useState<string>(today)
  const [acknowledgeBackdateImpact, setAcknowledgeBackdateImpact] = useState(false)
  const [acknowledgeManualOverrideReplacement, setAcknowledgeManualOverrideReplacement] = useState(false)
  const [note, setNote] = useState('')
  const [publication, setPublication] = useState<BillingPublicationView | null>(null)

  useEffect(() => {
    if (runQuery.data && initializedForRun !== runQuery.data.id) {
      setInitializedForRun(runQuery.data.id)
      setOpenedVersion(runQuery.data.version ?? null)
      setOpenedPreviewRevision(runQuery.data.previewRevision)
      setMappings(defaultMappingsForRun(runQuery.data))
      setMappingsDirty(false)
      setEffectiveFrom(runQuery.data.effectiveFromDefault ?? today())
      setAcknowledgeBackdateImpact(false)
      setAcknowledgeManualOverrideReplacement(false)
    }
  }, [runQuery.data, initializedForRun])

  const knownDeploymentKeys = useMemo(() => {
    const keys = new Set<string>()
    for (const ep of endpointsQuery.data ?? []) {
      for (const dep of ep.deployments) {
        keys.add(`${ep.endpointId}/${dep.deploymentName}`)
      }
    }
    for (const cand of runQuery.data?.candidates ?? []) {
      if (cand.suggestedDeploymentKey) keys.add(cand.suggestedDeploymentKey)
      for (const amb of cand.ambiguousDeploymentKeys) {
        keys.add(amb)
      }
    }
    return Array.from(keys)
  }, [endpointsQuery.data, runQuery.data?.candidates])

  const saveMappingsMutation = useMutation({
    mutationFn: () =>
      api.updateBillingMappings(runId, {
        mappings: Object.values(mappings),
        previewRevision: openedPreviewRevision,
        version: openedVersion,
      }),
    onSuccess: async (updatedRun) => {
      queryClient.setQueryData(['pricing', 'billing', 'run', runId], updatedRun)
      setOpenedVersion(updatedRun.version ?? null)
      setOpenedPreviewRevision(updatedRun.previewRevision)
      setMappings(defaultMappingsForRun(updatedRun))
      setMappingsDirty(false)
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'runs'] })
    },
  })

  const publishMutation = useMutation({
    mutationFn: () =>
      api.publishBillingRun(runId, {
        effectiveFrom,
        acknowledgeBackdateImpact,
        acknowledgeManualOverrideReplacement,
        note: note.trim() || null,
        previewRevision: openedPreviewRevision,
        version: openedVersion,
      }),
    onSuccess: async (pub) => {
      setPublication(pub)
      setMappingsDirty(false)
      await queryClient.invalidateQueries({ queryKey: ['pricing'] })
      await queryClient.invalidateQueries({ queryKey: ['analytics'] })
      await queryClient.invalidateQueries({ queryKey: ['budgets'] })
    },
  })

  const pollMutation = useMutation({
    mutationFn: () => api.pollBillingRun(runId),
    onSuccess: async (updatedRun) => {
      queryClient.setQueryData(['pricing', 'billing', 'run', runId], updatedRun)
      setOpenedVersion(updatedRun.version ?? null)
      setOpenedPreviewRevision(updatedRun.previewRevision)
      setMappings(defaultMappingsForRun(updatedRun))
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'runs'] })
    },
  })

  const isStaleConflict =
    (saveMappingsMutation.error instanceof ApiError && saveMappingsMutation.error.status === 409) ||
    (publishMutation.error instanceof ApiError && publishMutation.error.status === 409)

  async function reloadLatestPreview() {
    const result = await runQuery.refetch()
    const latest = result.data ?? (await api.getBillingRun(runId))
    queryClient.setQueryData(['pricing', 'billing', 'run', runId], latest)
    setOpenedVersion(latest.version ?? null)
    setOpenedPreviewRevision(latest.previewRevision)
    setMappings(defaultMappingsForRun(latest))
    setMappingsDirty(false)
    saveMappingsMutation.reset()
    publishMutation.reset()
  }

  if (runQuery.isPending) {
    return <Loading label="Loading billing run review" />
  }
  if (runQuery.isError) {
    return <ErrorState error={runQuery.error} title="Unable to load billing run" />
  }

  const run = runQuery.data
  const defaultEffectiveDate = run.effectiveFromDefault ?? today()
  const isBackdated = Boolean(effectiveFrom && effectiveFrom < defaultEffectiveDate)

  const includedCandidates = run.candidates.filter((cand) => {
    const m = mappings[cand.candidateId]
    return (m?.scope ?? (cand.publishable ? cand.suggestedScope : 'excluded')) !== 'excluded'
  })

  const hasManualOverrideConflict = includedCandidates.some(
    (cand) => cand.conflictsWithManualOverride || cand.existingOrigin === 'admin',
  )

  const allIncludedValid = includedCandidates.every((cand) => {
    if (!cand.publishable) return false
    const m = mappings[cand.candidateId]
    if (!m) return false
    if (m.scope === 'deployment' && !m.deploymentKey?.trim()) return false
    if ((m.scope === 'cloudModel' || cand.mappingConfidence !== 'exact' || cand.ambiguousDeploymentKeys.length > 0) && !m.confirmed) {
      return false
    }
    if ((cand.conflictsWithManualOverride || cand.existingOrigin === 'admin') && !m.replaceManualOverride) {
      return false
    }
    return true
  })

  const canPublish =
    run.status === 'readyForReview' &&
    !publication &&
    !mappingsDirty &&
    includedCandidates.length > 0 &&
    allIncludedValid &&
    (!isBackdated || acknowledgeBackdateImpact) &&
    (!hasManualOverrideConflict || acknowledgeManualOverrideReplacement)

  const activeError = publishMutation.error ?? saveMappingsMutation.error ?? pollMutation.error

  return (
    <WorkspaceEditorFrame
      title={`Review billing run (${run.sourceMonth})`}
      description="Verify derived effective rates, deployment bindings, exclusions, and effective date before publishing into the shared price book."
      backLabel="Back to Billing data"
      onBack={onClose}
      onCancel={onClose}
      dirty={mappingsDirty}
      pending={saveMappingsMutation.isPending || publishMutation.isPending || pollMutation.isPending}
      pendingLabel={
        publishMutation.isPending
          ? 'Publishing rates…'
          : saveMappingsMutation.isPending
            ? 'Saving mappings…'
            : 'Checking status…'
      }
      error={
        activeError
          ? activeError instanceof Error
            ? activeError.message
            : 'Operation failed.'
          : undefined
      }
      actions={
        <>
          {isStaleConflict && (
            <Button onClick={() => void reloadLatestPreview()}>Reload latest preview</Button>
          )}
          {(run.status === 'running' || run.status === 'queued') && (
            <Button onClick={() => pollMutation.mutate()} disabled={pollMutation.isPending}>
              Check status
            </Button>
          )}
          {run.status === 'readyForReview' && !publication && (
            <Button
              onClick={() => saveMappingsMutation.mutate()}
              disabled={saveMappingsMutation.isPending || publishMutation.isPending}
            >
              Save mappings
            </Button>
          )}
        </>
      }
      primaryAction={
        run.status === 'readyForReview' && !publication
          ? {
              label: 'Publish rates',
              disabled: !canPublish,
              onClick: () => publishMutation.mutate(),
            }
          : undefined
      }
    >
      <div className={styles.workspaceForm}>
        {publication && (
          <MessageBar intent="success" layout="multiline">
            <MessageBarBody>
              <MessageBarTitle>Billing-derived rates published</MessageBarTitle>
              Published {plural(publication.priceVersionIds.length, 'price version')} effective{' '}
              {formatDate(publication.effectiveFrom)} affecting{' '}
              {plural(publication.affectedDeploymentKeys.length, 'deployment')}.
            </MessageBarBody>
          </MessageBar>
        )}

        <MessageBar intent="info" layout="multiline">
          <MessageBarBody>
            A completed calendar month does not guarantee all late provider billing adjustments or credit true-ups have arrived. Re-fetching a changed export creates a new draft revision and never rewrites published rates.
          </MessageBarBody>
        </MessageBar>

        <div className={styles.workspaceSection}>
          <h3>Acquisition summary</h3>
          <dl className={styles.factList}>
            <div className={styles.factItem}>
              <dt>Status</dt>
              <dd>
                <Badge appearance="tint" color={runStatusColor(publication ? 'published' : run.status)}>
                  {RUN_STATUS_LABELS[publication ? 'published' : run.status]}
                </Badge>
              </dd>
            </div>
            <div className={styles.factItem}>
              <dt>Billing source</dt>
              <dd>{providerLabel(run.provider)}</dd>
            </div>
            <div className={styles.factItem}>
              <dt>Target pricing cloud</dt>
              <dd>{run.pricingCloudLabel}</dd>
            </div>
            <div className={styles.factItem}>
              <dt>Source month</dt>
              <dd>
                {run.sourceMonth}
                {run.invoiceMonths.length > 0 && ` (invoice: ${run.invoiceMonths.join(', ')})`}
              </dd>
            </div>
            <div className={styles.factItem}>
              <dt>Eligible USD spend</dt>
              <dd>{formatUsd(run.totalEligibleCostUsd)}</dd>
            </div>
            <div className={styles.factItem}>
              <dt>Excluded USD spend</dt>
              <dd>{formatUsd(run.totalExcludedCostUsd)}</dd>
            </div>
            <div className={styles.factItem}>
              <dt>Rows processed</dt>
              <dd>
                {run.eligibleRowCount} eligible · {run.excludedRowCount} excluded ({run.rawRowCount} raw)
              </dd>
            </div>
            {run.contentFingerprint && (
              <div className={styles.factItem}>
                <dt>Content fingerprint</dt>
                <dd className="mono">{run.contentFingerprint.slice(0, 16)}</dd>
              </div>
            )}
          </dl>
          {run.statusMessage && (
            <Text size={200} className={styles.muted}>
              {run.statusMessage}
            </Text>
          )}
        </div>

        {run.issues.length > 0 && (
          <div className={styles.workspaceSection}>
            <h3>Issues & accuracy warnings</h3>
            <ul className={styles.issueList} aria-label="Billing review issues">
              {run.issues.map((issue, idx) => (
                <li key={`${issue.code}-${idx}`} className={styles.issueItem}>
                  <Badge
                    appearance="tint"
                    color={
                      issue.severity === 'blocker'
                        ? 'danger'
                        : issue.severity === 'warning'
                          ? 'warning'
                          : 'informative'
                    }
                  >
                    {issue.severity === 'blocker'
                      ? 'Blocker'
                      : issue.severity === 'warning'
                        ? 'Warning'
                        : 'Info'}
                  </Badge>
                  <strong>{issue.code}</strong>
                  <Text>{issue.message}</Text>
                  {(issue.model || issue.region) && (
                    <Text size={200} className={styles.muted}>
                      {[issue.model, issue.region].filter(Boolean).join(' · ')}
                    </Text>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className={styles.workspaceSection}>
          <h3>Candidate rates & deployment mappings</h3>
          {run.candidates.length === 0 ? (
            <EmptyState title="No candidate rates derived">
              No eligible model usage rows produced candidate rates in this billing run.
            </EmptyState>
          ) : (
            <div className="table-scroll">
              <table aria-label="Candidate rates">
                <thead>
                  <tr>
                    <th>Model & source scope</th>
                    <th>Derived quantities</th>
                    <th className={styles.number}>Eligible USD</th>
                    <th>Existing rate</th>
                    <th>Proposed billing rate</th>
                    <th>Mapping & confirmation</th>
                  </tr>
                </thead>
                <tbody>
                  {run.candidates.map((cand) => {
                    const mapping = mappings[cand.candidateId] ?? {
                      candidateId: cand.candidateId,
                      scope: cand.publishable ? cand.suggestedScope : 'excluded',
                      deploymentKey: cand.suggestedDeploymentKey ?? null,
                      cloud: cand.pricingCloud,
                      confirmed: false,
                      replaceManualOverride: false,
                    }
                    const isAmbiguous =
                      cand.mappingConfidence === 'ambiguous' || cand.ambiguousDeploymentKeys.length > 0
                    const hasManualConflict =
                      cand.conflictsWithManualOverride || cand.existingOrigin === 'admin'
                    const readOnly = run.status !== 'readyForReview' || Boolean(publication)

                    return (
                      <tr key={cand.candidateId}>
                        <td>
                          <strong>{cand.model}</strong>
                          <Text block size={200} className={styles.muted}>
                            {[
                              cand.publisher,
                              cand.version ? `v${cand.version}` : null,
                              cand.deploymentType,
                              cand.region,
                              cand.serviceTier,
                              cand.routingMode,
                            ]
                              .filter(Boolean)
                              .join(' · ')}
                          </Text>
                          {(cand.sourceResourceName || cand.sourceResourceId) && (
                            <Text block size={200} className={styles.muted}>
                              Source: {cand.sourceResourceName ?? cand.sourceResourceId}
                            </Text>
                          )}
                        </td>
                        <td>{formatCandidateQuantities(cand)}</td>
                        <td className={styles.number}>{formatUsd(cand.totalEligibleCostUsd)}</td>
                        <td>
                          <div>{formatCandidateExistingRate(cand)}</div>
                          {cand.existingOrigin && (
                            <Badge
                              appearance="outline"
                              color={
                                cand.existingOrigin === 'seed'
                                  ? 'informative'
                                  : cand.existingOrigin === 'billing'
                                    ? 'success'
                                    : 'brand'
                              }
                              className={styles.originBadge}
                            >
                              {cand.existingOrigin === 'seed'
                                ? 'List price'
                                : cand.existingOrigin === 'billing'
                                  ? 'Billing-derived'
                                  : 'Manual override'}
                            </Badge>
                          )}
                        </td>
                        <td>
                          {cand.publishable ? (
                            <strong>{formatCandidateProposedRate(cand)}</strong>
                          ) : (
                            <div>
                              <Badge appearance="tint" color="warning">
                                Partial / unpublishable
                              </Badge>
                              <Text block size={200} className={styles.reason}>
                                {cand.unpublishableReason ??
                                  'Incomplete billing dimensions; existing rate stays in effect.'}
                              </Text>
                            </div>
                          )}
                          {cand.warnings.map((warn) => (
                            <Text key={warn} block size={200} className={styles.muted}>
                              {warn}
                            </Text>
                          ))}
                        </td>
                        <td>
                          <div className={styles.candidateMappingCell}>
                            <Field label={`Mapping scope for ${cand.model}`}>
                              <Select
                                aria-label={`Mapping scope for ${cand.model}`}
                                value={mapping.scope}
                                disabled={readOnly}
                                onChange={(_, data) => {
                                  setMappingsDirty(true)
                                  setMappings((current) => ({
                                    ...current,
                                    [cand.candidateId]: {
                                      ...mapping,
                                      scope: data.value as BillingMappingScope,
                                    },
                                  }))
                                }}
                              >
                                <option value="deployment" disabled={!cand.publishable}>
                                  Specific deployment
                                </option>
                                <option value="cloudModel" disabled={!cand.publishable}>
                                  Cloud & model
                                </option>
                                <option value="excluded">Exclude</option>
                              </Select>
                            </Field>

                            {mapping.scope === 'deployment' && (
                              <Field label={`Target deployment for ${cand.model}`}>
                                <Input
                                  aria-label={`Target deployment for ${cand.model}`}
                                  value={mapping.deploymentKey ?? ''}
                                  disabled={readOnly}
                                  list={`deployments-${cand.candidateId}`}
                                  placeholder="endpoint-id/deployment-name"
                                  onChange={(_, data) => {
                                    setMappingsDirty(true)
                                    setMappings((current) => ({
                                      ...current,
                                      [cand.candidateId]: {
                                        ...mapping,
                                        deploymentKey: data.value,
                                      },
                                    }))
                                  }}
                                />
                                <datalist id={`deployments-${cand.candidateId}`}>
                                  {knownDeploymentKeys.map((key) => (
                                    <option key={key} value={key}>
                                      {key}
                                    </option>
                                  ))}
                                </datalist>
                              </Field>
                            )}

                            {mapping.scope !== 'excluded' && (isAmbiguous || mapping.scope === 'cloudModel' || cand.mappingConfidence !== 'exact') && (
                              <div>
                                {isAmbiguous && (
                                  <Badge appearance="tint" color="warning">
                                    Ambiguous deployment mapping
                                  </Badge>
                                )}
                                {cand.ambiguousDeploymentKeys.length > 0 && (
                                  <Text block size={200} className={styles.muted}>
                                    Candidates: {cand.ambiguousDeploymentKeys.join(', ')}
                                  </Text>
                                )}
                                <Checkbox
                                  aria-label={`Confirm mapping for ${cand.model}`}
                                  label={mapping.scope === 'cloudModel'
                                    ? 'Confirm applying to all matching deployments in this cloud'
                                    : 'Confirm deployment mapping'}
                                  disabled={readOnly}
                                  checked={Boolean(mapping.confirmed)}
                                  onChange={(_, data) => {
                                    setMappingsDirty(true)
                                    setMappings((current) => ({
                                      ...current,
                                      [cand.candidateId]: {
                                        ...mapping,
                                        confirmed: Boolean(data.checked),
                                      },
                                    }))
                                  }}
                                />
                              </div>
                            )}

                            {mapping.scope !== 'excluded' && hasManualConflict && (
                              <div>
                                <Badge appearance="tint" color="warning">
                                  Manual override conflict
                                </Badge>
                                <Checkbox
                                  aria-label={`Replace manual override for ${cand.model}`}
                                  label="Replace manual override"
                                  disabled={readOnly}
                                  checked={Boolean(mapping.replaceManualOverride)}
                                  onChange={(_, data) => {
                                    setMappingsDirty(true)
                                    setMappings((current) => ({
                                      ...current,
                                      [cand.candidateId]: {
                                        ...mapping,
                                        replaceManualOverride: Boolean(data.checked),
                                      },
                                    }))
                                  }}
                                />
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {run.status === 'readyForReview' && !publication && (
          <div className={styles.workspaceSection}>
            <h3>Publication controls</h3>
            <Text size={200} className={styles.muted}>
              Publishing writes immutable dated price versions and activates them atomically. Budget spend re-evaluates on the next budget check and may block or unblock cost centers.
            </Text>
            <div className={styles.formGrid}>
              <Field label="Effective from" required>
                <Input
                  type="date"
                  aria-label="Effective from"
                  value={effectiveFrom}
                  onChange={(_, data) => setEffectiveFrom(data.value)}
                />
              </Field>
              <Field label="Publication note" hint="Optional context recorded in price history">
                <Input
                  aria-label="Publication note"
                  value={note}
                  onChange={(_, data) => setNote(data.value)}
                  placeholder={`Derived from ${run.sourceMonth} ${run.provider} billing data`}
                />
              </Field>
            </div>

            {isBackdated && (
              <>
                <MessageBar intent="warning" layout="multiline">
                  <MessageBarBody>
                    <MessageBarTitle>Backdating changes historical reports and budget status</MessageBarTitle>
                    Setting an effective date earlier than {formatDate(defaultEffectiveDate)} recalculates historical cost reports and current-month budget spend, which may block or unblock cost centers on the next budget check.
                  </MessageBarBody>
                </MessageBar>
                <Checkbox
                  label="I understand backdating changes historical cost reports and budget calculations"
                  checked={acknowledgeBackdateImpact}
                  onChange={(_, data) => setAcknowledgeBackdateImpact(Boolean(data.checked))}
                />
              </>
            )}

            {hasManualOverrideConflict && (
              <Checkbox
                label="I confirm replacing overlapping administrator manual price overrides"
                checked={acknowledgeManualOverrideReplacement}
                onChange={(_, data) => setAcknowledgeManualOverrideReplacement(Boolean(data.checked))}
              />
            )}
          </div>
        )}

        <div className={styles.workspaceSection}>
          <h3>Normalized billing evidence ({run.evidenceItems.length})</h3>
          {run.evidenceItems.length === 0 ? (
            <Text size={200} className={styles.muted}>
              No retained line-item evidence for this run.
            </Text>
          ) : (
            <div className="table-scroll">
              <table aria-label="Normalized billing evidence">
                <thead>
                  <tr>
                    <th>Line key</th>
                    <th>Resource & SKU</th>
                    <th>Model & region</th>
                    <th>Category</th>
                    <th className={styles.number}>Normalized quantity</th>
                    <th className={styles.number}>Eligible USD</th>
                    <th className={styles.number}>Amortized benefit USD</th>
                    <th>Excluded USD & reason</th>
                  </tr>
                </thead>
                <tbody>
                  {run.evidenceItems.map((item) => (
                    <tr key={item.lineKey}>
                      <td className="mono">{item.lineKey}</td>
                      <td>
                        <strong>{item.sourceResourceName ?? item.sourceResourceId ?? '—'}</strong>
                        <Text block size={200} className={styles.muted}>
                          {[item.skuDescription, item.skuId, item.meterId].filter(Boolean).join(' · ')}
                        </Text>
                      </td>
                      <td>
                        {[item.model, item.modelVersion, item.region, item.deploymentType]
                          .filter(Boolean)
                          .join(' · ') || '—'}
                      </td>
                      <td>{item.usageCategory}</td>
                      <td className={styles.number}>
                        {formatCount(item.normalizedQuantity)} {item.normalizedUnit ?? ''}
                      </td>
                      <td className={styles.number}>{formatUsd(item.eligibleCostUsd)}</td>
                      <td className={styles.number}>{formatUsd(item.amortizedBenefitCostUsd)}</td>
                      <td>
                        {item.excludedCostUsd > 0
                          ? `${formatUsd(item.excludedCostUsd)}${item.excludedReason ? ` (${item.excludedReason})` : ''}`
                          : item.excludedReason ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </WorkspaceEditorFrame>
  )
}

type BillingEditorState =
  | { kind: 'import' }
  | { kind: 'createConnection' }
  | { kind: 'editConnection'; connection: BillingConnectionView }
  | null

function BillingTab({
  cloud,
  clouds,
  activeRunId,
  schedulerAlwaysOn,
  onSelectRun,
}: {
  cloud: string
  clouds: PricingCloud[]
  activeRunId: string | null
  schedulerAlwaysOn?: boolean
  onSelectRun: (runId: string | null) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [editor, setEditor] = useState<BillingEditorState>(null)
  const [fetchMonth, setFetchMonth] = useState(previousCompleteMonth)
  const [checkResult, setCheckResult] = useState<BillingConnectionCheckResult | null>(null)
  const [removingConnection, setRemovingConnection] = useState<BillingConnectionView | null>(null)

  const connections = useQuery({
    queryKey: ['pricing', 'billing', 'connections'],
    queryFn: () => api.listBillingConnections(),
  })
  const runs = useQuery({
    queryKey: ['pricing', 'billing', 'runs'],
    queryFn: () => api.listBillingRuns(),
  })

  const checkMutation = useMutation({
    mutationFn: (connectionId: string) => api.checkBillingConnection(connectionId),
    onSuccess: async (result) => {
      setCheckResult(result)
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'connections'] })
    },
  })

  const fetchRunMutation = useMutation({
    mutationFn: (connectionId: string) =>
      api.startBillingConnectionRun(connectionId, { sourceMonth: fetchMonth.trim() || null }),
    onSuccess: async (run) => {
      queryClient.setQueryData(['pricing', 'billing', 'run', run.id], run)
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'runs'] })
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'overview'] })
      if (run.status === 'readyForReview') {
        onSelectRun(run.id)
      }
    },
  })

  const pollRunMutation = useMutation({
    mutationFn: (runId: string) => api.pollBillingRun(runId),
    onSuccess: async (run) => {
      queryClient.setQueryData(['pricing', 'billing', 'run', run.id], run)
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'runs'] })
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'overview'] })
    },
  })

  const deleteConnectionMutation = useMutation({
    mutationFn: (conn: BillingConnectionView) => api.deleteBillingConnection(conn.id, conn.version),
    onSuccess: async () => {
      setRemovingConnection(null)
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'billing', 'connections'] })
      await queryClient.invalidateQueries({ queryKey: ['pricing', 'overview'] })
    },
  })

  if (activeRunId) {
    return <BillingRunReviewWorkspace key={activeRunId} runId={activeRunId} onClose={() => onSelectRun(null)} />
  }

  if (editor?.kind === 'import') {
    return (
      <BillingImportWorkspace
        defaultCloud={cloud}
        clouds={clouds}
        onClose={() => setEditor(null)}
        onImported={(runId) => {
          setEditor(null)
          onSelectRun(runId)
        }}
      />
    )
  }

  if (editor?.kind === 'createConnection' || editor?.kind === 'editConnection') {
    return (
      <BillingConnectionWorkspace
        key={editor.kind === 'editConnection' ? editor.connection.id : 'create'}
        connection={editor.kind === 'editConnection' ? editor.connection : null}
        defaultCloud={cloud}
        clouds={clouds}
        onClose={() => setEditor(null)}
      />
    )
  }

  const anyScheduled = (connections.data ?? []).some((conn) => conn.schedule?.enabled)

  return (
    <div className={styles.stack}>
      <div className={styles.billingToolbar}>
        <Text className={styles.muted}>
          Derive customer-specific USD model rates from a completed calendar month’s amortized cloud billing data, review deployment bindings, and publish approved rates into the price book.
        </Text>
        <div className={styles.billingToolbarActions}>
          <Button
            appearance="primary"
            icon={<ArrowUploadRegular />}
            onClick={() => setEditor({ kind: 'import' })}
          >
            Import billing file
          </Button>
          <Button icon={<AddRegular />} onClick={() => setEditor({ kind: 'createConnection' })}>
            New billing connection
          </Button>
        </div>
      </div>

      <MessageBar intent="info" layout="multiline">
        <MessageBarBody>
          MOSAIC derives USD-only, quantity-weighted effective rates from a completed month’s contract-discounted and amortized usage (excluding taxes and one-off credits). Connected cloud billing queries and export object reads can themselves incur provider charges.
        </MessageBarBody>
      </MessageBar>

      {schedulerAlwaysOn === false && anyScheduled && (
        <MessageBar intent="warning" layout="multiline">
          <MessageBarBody>
            <MessageBarTitle>Scheduled monthly draft refresh requires an always-on API host</MessageBarTitle>
            One or more billing connections have monthly draft schedules enabled, but this API host does not have always-on background scheduling enabled.
          </MessageBarBody>
        </MessageBar>
      )}

      {checkResult && (
        <MessageBar
          intent={checkResult.status === 'ready' ? 'success' : checkResult.status === 'failed' ? 'error' : 'info'}
          layout="multiline"
        >
          <MessageBarBody>
            <MessageBarTitle>
              Connection check: {checkResult.status === 'ready' ? 'Dataset access verified' : 'Access issue'}
            </MessageBarTitle>
            {checkResult.message}
            {checkResult.scopeSummary && ` (${checkResult.scopeSummary})`}
            {checkResult.warnings.length > 0 && ` · Warnings: ${checkResult.warnings.join('; ')}`}
          </MessageBarBody>
        </MessageBar>
      )}

      {(checkMutation.isError || fetchRunMutation.isError || pollRunMutation.isError) && (
        <ErrorState error={checkMutation.error ?? fetchRunMutation.error ?? pollRunMutation.error} />
      )}

      <div className={styles.billingSection}>
        <div className={styles.billingSectionHeader}>
          <h3>Connected billing sources</h3>
          <label className={styles.control}>
            <span>Source month to fetch (YYYY-MM)</span>
            <Input
              aria-label="Source month to fetch"
              value={fetchMonth}
              onChange={(_, data) => setFetchMonth(data.value)}
              placeholder="2026-09"
            />
          </label>
        </div>

        {connections.isPending && <Loading label="Loading billing connections" />}
        {connections.isError && <ErrorState error={connections.error} />}
        {connections.data && connections.data.length === 0 && (
          <EmptyState title="No connected billing sources">
            Connect Azure Cost Management, AWS CUR 2.0 in S3, or GCP BigQuery Billing Export for read-only monthly retrieval, or import a billing file directly.
          </EmptyState>
        )}
        {connections.data && connections.data.length > 0 && (
          <div className="table-scroll">
            <table aria-label="Connected billing sources">
              <thead>
                <tr>
                  <th>Connection</th>
                  <th>Provider & cloud</th>
                  <th>Scope / dataset</th>
                  <th>Status</th>
                  <th>Monthly draft schedule</th>
                  <th><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {connections.data.map((conn) => (
                  <tr key={conn.id}>
                    <td>
                      <strong>{conn.name}</strong>
                    </td>
                    <td>
                      {providerLabel(conn.provider)}
                      <Text block size={200} className={styles.muted}>
                        {conn.pricingCloudLabel}
                      </Text>
                    </td>
                    <td>{connectionScopeSummary(conn)}</td>
                    <td>
                      <Badge
                        appearance="tint"
                        color={
                          conn.status === 'ready'
                            ? 'success'
                            : conn.status === 'failed'
                              ? 'danger'
                              : 'informative'
                        }
                      >
                        {conn.status === 'ready'
                          ? 'Ready'
                          : conn.status === 'failed'
                            ? 'Failed'
                            : 'Untested'}
                      </Badge>
                      {conn.statusMessage && (
                        <Text block size={200} className={styles.muted}>
                          {conn.statusMessage}
                        </Text>
                      )}
                    </td>
                    <td>
                      {conn.schedule?.enabled
                        ? `Day ${conn.schedule.dayOfMonth ?? 3} at ${conn.schedule.timeUtc ?? '04:00'} ${conn.schedule.timeZone ?? 'UTC'} (draft only)`
                        : 'Manual only'}
                      {conn.schedule?.lastError && (
                        <Text block size={200} className={styles.reason}>
                          {conn.schedule.lastError}
                        </Text>
                      )}
                    </td>
                    <td className={styles.actions}>
                      <Button
                        size="small"
                        appearance="primary"
                        disabled={fetchRunMutation.isPending}
                        onClick={() => fetchRunMutation.mutate(conn.id)}
                      >
                        Fetch month
                      </Button>
                      <Button
                        size="small"
                        disabled={checkMutation.isPending}
                        onClick={() => checkMutation.mutate(conn.id)}
                      >
                        Check access
                      </Button>
                      <Button
                        size="small"
                        onClick={() => setEditor({ kind: 'editConnection', connection: conn })}
                      >
                        Edit
                      </Button>
                      <Button
                        size="small"
                        onClick={() => {
                          deleteConnectionMutation.reset()
                          setRemovingConnection(conn)
                        }}
                      >
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className={styles.billingSection}>
        <div className={styles.billingSectionHeader}>
          <h3>Billing runs & draft reviews</h3>
        </div>
        {runs.isPending && <Loading label="Loading billing runs" />}
        {runs.isError && <ErrorState error={runs.error} />}
        {runs.data && runs.data.length === 0 && (
          <EmptyState title="No billing runs yet">
            Import a completed month’s billing extract or fetch a month from a connected billing source to review candidate rates.
          </EmptyState>
        )}
        {runs.data && runs.data.length > 0 && (
          <div className="table-scroll">
            <table aria-label="Billing runs">
              <thead>
                <tr>
                  <th>Source month</th>
                  <th>Source</th>
                  <th>Acquisition</th>
                  <th>Status</th>
                  <th>Candidates & issues</th>
                  <th className={styles.number}>Eligible USD spend</th>
                  <th><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <strong>{run.sourceMonth}</strong>
                      {run.invoiceMonths.length > 0 && (
                        <Text block size={200} className={styles.muted}>
                          Invoice: {run.invoiceMonths.join(', ')}
                        </Text>
                      )}
                    </td>
                    <td>
                      {providerLabel(run.provider)}
                      <Text block size={200} className={styles.muted}>
                        {run.connectionName ?? run.sourceFileName ?? run.sourceScope ?? run.pricingCloudLabel}
                      </Text>
                    </td>
                    <td>
                      {run.acquisitionKind === 'upload'
                        ? 'Upload'
                        : run.acquisitionKind === 'scheduled'
                          ? 'Scheduled'
                          : 'Connected'}
                    </td>
                    <td>
                      <Badge appearance="tint" color={runStatusColor(run.status)}>
                        {RUN_STATUS_LABELS[run.status]}
                      </Badge>
                      {run.statusMessage && (
                        <Text block size={200} className={styles.muted}>
                          {run.statusMessage}
                        </Text>
                      )}
                    </td>
                    <td>
                      {`${run.publishableCandidateCount} of ${plural(run.candidateCount, 'candidate')} publishable`}
                      {(run.blockerCount > 0 || run.warningCount > 0) && (
                        <Text block size={200} className={styles.muted}>
                          {[
                            run.blockerCount > 0 ? plural(run.blockerCount, 'blocker') : null,
                            run.warningCount > 0 ? plural(run.warningCount, 'warning') : null,
                          ]
                            .filter(Boolean)
                            .join(' · ')}
                        </Text>
                      )}
                    </td>
                    <td className={styles.number}>
                      {formatUsd(run.totalEligibleCostUsd)}
                      {run.totalExcludedCostUsd > 0 && (
                        <Text block size={200} className={styles.muted}>
                          {formatUsd(run.totalExcludedCostUsd)} excluded
                        </Text>
                      )}
                    </td>
                    <td className={styles.actions}>
                      {run.status === 'readyForReview' && (
                        <Button size="small" appearance="primary" onClick={() => onSelectRun(run.id)}>
                          Review & publish
                        </Button>
                      )}
                      {run.status === 'published' && (
                        <Button size="small" onClick={() => onSelectRun(run.id)}>
                          View evidence
                        </Button>
                      )}
                      {(run.status === 'running' || run.status === 'queued') && (
                        <>
                          <Button
                            size="small"
                            disabled={pollRunMutation.isPending}
                            onClick={() => pollRunMutation.mutate(run.id)}
                          >
                            Check status
                          </Button>
                          <Button size="small" onClick={() => onSelectRun(run.id)}>
                            View run
                          </Button>
                        </>
                      )}
                      {(run.status === 'failed' || run.status === 'superseded') && (
                        <Button size="small" onClick={() => onSelectRun(run.id)}>
                          View details
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Dialog
        open={removingConnection !== null}
        onOpenChange={(_, data) => {
          if (!data.open && !deleteConnectionMutation.isPending) setRemovingConnection(null)
        }}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Delete billing connection?</DialogTitle>
            <DialogContent>
              Remove {removingConnection?.name} from MOSAIC and delete any stored credential secret. Existing billing runs and published prices are retained.
              {deleteConnectionMutation.isError && <ErrorState error={deleteConnectionMutation.error} />}
            </DialogContent>
            <DialogActions>
              <Button
                disabled={deleteConnectionMutation.isPending}
                onClick={() => setRemovingConnection(null)}
              >
                Cancel
              </Button>
              <Button
                appearance="primary"
                disabled={deleteConnectionMutation.isPending}
                onClick={() => {
                  if (removingConnection) deleteConnectionMutation.mutate(removingConnection)
                }}
              >
                Delete connection
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </div>
  )
}

export function PricingPage() {
  const api = useMosaicApi()
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') as PricingTab | null
  const tab: PricingTab = requested && tabs.some((item) => item.key === requested) ? requested : 'prices'
  const cloud = params.get('cloud') ?? 'commercial'
  const activeRunId = params.get('run')
  const overview = useQuery({ queryKey: ['pricing', 'overview'], queryFn: api.getPricingOverview })
  const unpriced = useQuery({ queryKey: ['pricing', 'unpriced'], queryFn: api.getUnpricedDeployments })
  const [draft, setDraft] = useState<PriceDraft | null>(null)
  const clouds = overview.data?.clouds ?? []

  function update(next: Record<string, string | null>) {
    const search = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value) search.set(key, value)
      else search.delete(key)
    }
    setParams(search)
  }

  const unpricedCount = unpriced.data?.rows.length
  const pendingBillingReviews = overview.data?.pendingBillingReviews

  return (
    <section className={styles.page}>
      <PageHeader
        title="Pricing"
        description="The list prices MOSAIC turns measured usage into cost with, each with its source. Add prices for other clouds and providers, or override one from a date."
        actions={
          <Button appearance="primary" icon={<AddRegular />} onClick={() => setDraft(emptyDraft(cloud))}>
            Add price
          </Button>
        }
      />
      {overview.isError && <ErrorState error={overview.error} />}
      {overview.data && (
        <div className={styles.summary}>
          <Text>
            {`${overview.data.pricedDeployments} of ${plural(overview.data.deployments, 'deployment')} priced today.`}
          </Text>
          <Text className={styles.muted}>
            {overview.data.pricingBasis === 'billingDerived'
              ? `US dollars from published billing-derived rates${overview.data.lastBillingSourceMonth ? ` (${overview.data.lastBillingSourceMonth})` : ''}. Seeded list prices from the Azure Retail Prices API on ${formatDate(overview.data.seedLastUpdated)} remain as fallback.`
              : overview.data.pricingBasis === 'customManual'
                ? `US dollars using administrator overrides and seeded list prices from the Azure Retail Prices API (${formatDate(overview.data.seedLastUpdated)}).`
                : overview.data.pricingBasis === 'mixed'
                  ? `US dollars combining published billing-derived rates${overview.data.lastBillingSourceMonth ? ` (${overview.data.lastBillingSourceMonth})` : ''}, administrator overrides, and Azure Retail Prices API list prices (${formatDate(overview.data.seedLastUpdated)}).`
                  : `US dollars at list price, before any discount. Seeded from the Azure Retail Prices API on ${formatDate(overview.data.seedLastUpdated)}.`}
          </Text>
        </div>
      )}
      <SectionLayout
        label="Pricing"
        sections={tabs.map((item) => ({
          value: item.key,
          icon:
            item.key === 'prices' ? (
              <MoneyRegular />
            ) : item.key === 'billing' ? (
              <CloudRegular />
            ) : item.key === 'unpriced' ? (
              <WarningRegular />
            ) : item.key === 'endpoints' ? (
              <PlugConnectedRegular />
            ) : (
              <DocumentRegular />
            ),
          label: item.key === 'unpriced' && unpricedCount ? `${item.label} (${unpricedCount})` : item.label,
          description:
            item.key === 'unpriced' && unpricedCount !== undefined
              ? `${unpricedCount} unpriced entries. Fix missing endpoint facts or add the missing price using each row's action.`
              : item.key === 'billing' && pendingBillingReviews
                ? `${plural(pendingBillingReviews, 'billing run')} ready for review.`
                : undefined,
        }))}
        active={tab}
        onSelect={(value) => update({ tab: value, endpoint: null, run: null })}
      >
        {tab === 'prices' && (
          <PricesTab
            cloud={cloud}
            clouds={clouds}
            onCloud={(value) => update({ cloud: value })}
            onOverride={(price) => setDraft(overrideDraft(price))}
            onOpenBillingRun={(runId) => update({ tab: 'billing', run: runId, endpoint: null })}
          />
        )}
        {tab === 'billing' && (
          <BillingTab
            cloud={cloud}
            clouds={clouds}
            activeRunId={activeRunId}
            schedulerAlwaysOn={overview.data?.billingSchedulerAlwaysOn}
            onSelectRun={(runId) => update({ tab: 'billing', run: runId })}
          />
        )}
        {tab === 'unpriced' && (
          <UnpricedTab
            onPrice={(row) => setDraft(unpricedDraft(row, cloud))}
            onEndpoint={(row) => update({ tab: 'endpoints', endpoint: row.endpointId ?? null })}
          />
        )}
        {tab === 'endpoints' && <EndpointsTab key={params.get('endpoint') ?? 'all'} clouds={clouds} focus={params.get('endpoint')} />}
        {tab === 'sources' && overview.data && (
          <div className={styles.stack}>
            <Text className={styles.muted}>
              Every price MOSAIC ships names where it came from. An administrator&apos;s price names the source they gave.
            </Text>
            <ul className={styles.sourceList} aria-label="Price sources">
              {overview.data.sources.map((source, idx) => (
                <li key={source.url ?? source.reference ?? `${source.title}-${idx}`}>
                  {source.url ? (
                    <Link href={source.url} target="_blank" rel="noreferrer">{source.title}</Link>
                  ) : (
                    <Text>{source.title}</Text>
                  )}
                  {source.retrievedOn && <Text size={200} className={styles.muted}>Retrieved {formatDate(source.retrievedOn)}</Text>}
                </li>
              ))}
            </ul>
          </div>
        )}
        {tab === 'sources' && overview.isPending && <Loading label="Loading sources" />}
      </SectionLayout>
      {draft && <PriceDialog draft={draft} clouds={clouds} onClose={() => setDraft(null)} />}
    </section>
  )
}
