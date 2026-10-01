import {
  Badge,
  Button,
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
  Tab,
  TabList,
  Text,
  Textarea,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, Fragment, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { formatRate } from '../cost-format'
import { plural } from '../labels'
import type {
  EndpointPricingUpdate,
  EndpointPricingView,
  PriceCreate,
  PriceLineView,
  PricingCloud,
  PriceView,
  UnpricedDeploymentRow,
  UnpricedReason,
} from '../types'
import styles from './PricingPage.module.css'

type PricingTab = 'prices' | 'unpriced' | 'endpoints' | 'sources'
type PriceKind = 'tokens' | 'ptu' | 'monthly'

const tabs: Array<{ key: PricingTab; label: string }> = [
  { key: 'prices', label: 'Prices' },
  { key: 'unpriced', label: 'Unpriced deployments' },
  { key: 'endpoints', label: 'Clouds and endpoints' },
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

function sourceName(source: { title: string; url: string }) {
  if (source.url.startsWith('https://prices.azure.com/')) return 'Azure Retail Prices API'
  if (source.url.startsWith('https://learn.microsoft.com/')) return 'Microsoft Learn'
  return source.title === 'Entered by an administrator' ? 'Administrator source' : source.title
}

function Sources({ price }: { price: PriceView }) {
  return (
    <span className={styles.sources}>
      {price.sources.map((source) => (
        <Link key={source.url} href={source.url} target="_blank" rel="noreferrer" title={source.title} inline>
          {sourceName(source)}
        </Link>
      ))}
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

function HistoryRows({ lineId, columns }: { lineId: string; columns: number }) {
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
                    {version.recordedBy && <Text block size={200}>{version.recordedBy}</Text>}
                  </td>
                  <td>
                    <Sources price={version} />
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

function PriceTable({ lines, onOverride }: { lines: PriceLineView[]; onOverride: (price: PriceView) => void }) {
  const [open, setOpen] = useState<string | null>(null)
  const columns = 9
  return (
    <div className="table-scroll">
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
                    <Sources price={price} />
                    <Badge appearance="outline" color={price.origin === 'seed' ? 'informative' : 'brand'} className={styles.originBadge}>
                      {price.origin === 'seed' ? 'List price' : 'Administrator'}
                    </Badge>
                  </td>
                  <td className={styles.actions}>
                    <Button size="small" onClick={() => onOverride(price)}>Override</Button>
                    <Button
                      size="small"
                      appearance="subtle"
                      aria-expanded={expanded}
                      onClick={() => setOpen(expanded ? null : line.lineId)}
                    >
                      {expanded ? 'Hide history' : `History (${line.versions})`}
                    </Button>
                  </td>
                </tr>
                {expanded && <HistoryRows lineId={line.lineId} columns={columns} />}
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
}: {
  cloud: string
  clouds: PricingCloud[]
  onCloud: (cloud: string) => void
  onOverride: (price: PriceView) => void
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
      {lines.length > 0 && <PriceTable lines={lines} onOverride={onOverride} />}
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
                  </td>
                  <td className={styles.number}>
                    {formatCount(row.totalTokens)}
                    <Text block size={200} className={styles.muted}>{plural(row.requests, 'call')}</Text>
                  </td>
                  <td className={styles.actions}>
                    {row.kind === 'deployment' && ENDPOINT_FIXES.has(row.reason) && (
                      <Button size="small" onClick={() => onEndpoint(row)}>Set facts</Button>
                    )}
                    {row.kind === 'deployment' && !ENDPOINT_FIXES.has(row.reason) && (
                      <Button size="small" onClick={() => onPrice(row)}>Add price</Button>
                    )}
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

export function PricingPage() {
  const api = useMosaicApi()
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') as PricingTab | null
  const tab: PricingTab = requested && tabs.some((item) => item.key === requested) ? requested : 'prices'
  const cloud = params.get('cloud') ?? 'commercial'
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

  return (
    <section className={styles.page}>
      <PageHeader
        title="Pricing"
        description="The list prices MOSAIC turns measured usage into cost with, each with its source. Add prices for other clouds and providers, or override one from a date."
        source={overview.data ? 'live' : undefined}
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
            {`US dollars at list price, before any discount. Seeded from the Azure Retail Prices API on ${formatDate(overview.data.seedLastUpdated)}.`}
          </Text>
        </div>
      )}
      <TabList className={styles.tabs} selectedValue={tab} onTabSelect={(_, data) => update({ tab: data.value as string, endpoint: null })}>
        {tabs.map((item) => (
          <Tab key={item.key} value={item.key}>
            {item.label}
            {item.key === 'unpriced' && unpricedCount ? <Badge appearance="filled" color="warning" size="small" className={styles.tabBadge}>{unpricedCount}</Badge> : null}
          </Tab>
        ))}
      </TabList>
      {tab === 'prices' && (
        <PricesTab
          cloud={cloud}
          clouds={clouds}
          onCloud={(value) => update({ cloud: value })}
          onOverride={(price) => setDraft(overrideDraft(price))}
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
            {overview.data.sources.map((source) => (
              <li key={source.url}>
                <Link href={source.url} target="_blank" rel="noreferrer">{source.title}</Link>
                {source.retrievedOn && <Text size={200} className={styles.muted}>Retrieved {formatDate(source.retrievedOn)}</Text>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {tab === 'sources' && overview.isPending && <Loading label="Loading sources" />}
      {draft && <PriceDialog draft={draft} clouds={clouds} onClose={() => setDraft(null)} />}
    </section>
  )
}
