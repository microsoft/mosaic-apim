import {
  Badge, Button, Checkbox, Field, Input, Link, MessageBar, MessageBarBody,
  MessageBarTitle, Radio, RadioGroup, Textarea,
} from '@fluentui/react-components'
import { ArrowDownloadRegular, ArrowUploadRegular, CloudArrowDownRegular, HistoryRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import { ErrorState, Loading } from '../components/AsyncState'
import { WorkspaceEditorFrame } from '../components/WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from '../components/WorkspaceLeaveGuard'
import { formatRate } from '../cost-format'
import type {
  PublicCatalogApproval, PublicCatalogDraft, PublicCatalogPreview, PublicCatalogRate,
  PublicCatalogSource,
} from '../types'
import styles from './PricingCatalogWorkspace.module.css'

type Stage = 'validate' | 'review'
interface Props {
  draftId: string | null
  stage: Stage
  onSelectDraft: (id: string | null, stage?: Stage) => void
  onWorkspaceChange: (editing: boolean) => void
}

const today = () => new Date().toISOString().slice(0, 10)
const cloudName = (cloud: string | null) => cloud === 'commercial' ? 'Azure Commercial' : cloud === 'government' ? 'Azure Government' : cloud ?? 'Unknown cloud'
function dateLabel(value: string) {
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' })
    .format(new Date(value.length === 10 ? `${value}T00:00:00Z` : value))
}
function rateLabel(rate: PublicCatalogRate | null, missing = 'No public rate') {
  if (!rate) return missing
  if (rate.ptuHourly != null) return `${formatRate(rate.ptuHourly)} per PTU-hour`
  return `${formatRate(rate.inputPerMillion)} input${rate.outputPerMillion != null ? ` / ${formatRate(rate.outputPerMillion)} output` : ' / output not listed'} per 1M tokens`
}
function sourceMethod(source: PublicCatalogSource) {
  return source.method === 'supplied' ? 'Administrator supplied' : source.method === 'retail' ? 'Fetched from Retail' : 'Packaged source'
}

function Steps({ step }: { step: 1 | 2 | 3 }) {
  return <ol className={styles.steps} aria-label="Public catalog workflow">
    {['Acquire', 'Validate', 'Review & activate'].map((label, index) => (
      <li key={label} aria-current={index + 1 === step ? 'step' : undefined}>
        <span className={styles.stepNumber}>{index + 1 < step ? '✓' : index + 1}</span>
        <span>{label}{index + 1 < step ? <span className="sr-only"> complete</span> : null}</span>
      </li>
    ))}
  </ol>
}

function Sources({ sources }: { sources: PublicCatalogSource[] }) {
  return <ul className={styles.sources} aria-label="Public catalog sources">
    {sources.map((source) => <li key={source.id}>
      <div className={styles.sourceHeading}>
        <Link href={source.url} target="_blank" rel="noreferrer">{source.title}</Link>
        <Badge appearance="tint" color={source.method === 'supplied' ? 'warning' : 'informative'}>{sourceMethod(source)}</Badge>
      </div>
      <span className={styles.muted}>{source.method === 'supplied' ? 'Claimed source-read date' : 'Source-read date'}: {dateLabel(source.retrievedOn)} ({source.ageDays} days ago).
        {source.acquiredAt ? ` Acquired ${dateLabel(source.acquiredAt)}.` : ''}</span>
      {source.method === 'supplied' && <span className={styles.muted}>Not independently fetched or verified by MOSAIC. Inspect the supplied evidence before activation.</span>}
    </li>)}
  </ul>
}

function Evidence({ draft }: { draft: PublicCatalogDraft }) {
  const [visibleMappings, setVisibleMappings] = useState(100)
  const [visibleEvidence, setVisibleEvidence] = useState(100)
  return <section className={styles.panel} aria-labelledby="catalog-source-heading">
    <h3 id="catalog-source-heading">Source evidence & freshness</h3>
    <Sources sources={draft.sources} />
    {draft.evidence.length > 0 && <details className={styles.disclosure}>
      <summary>Supplied numeric evidence ({draft.evidence.length})</summary>
      <p className={styles.muted}>These are administrator-supplied source assertions, not an independent verification of a provider price.</p>
      <div className={styles.evidence}>
        {draft.evidence.slice(0, visibleEvidence).map((item, index) => <article key={`${item.sourceId}-${index}`}>
          <strong>{cloudName(item.cloud)}</strong>
          <p>{item.excerpt}</p>
          <span className={styles.muted}>Source: {draft.sources.find((source) => source.id === item.sourceId)?.title ?? item.sourceId}</span>
        </article>)}
      </div>
      {visibleEvidence < draft.evidence.length && <Button onClick={() => setVisibleEvidence((value) => value + 100)}>Show 100 more evidence excerpts</Button>}
    </details>}
    <details className={styles.disclosure}>
      <summary>Model and meter mappings ({draft.mappings.length})</summary>
      <div className={styles.scroll} tabIndex={0} role="region" aria-label="Model and meter mappings">
        <table className={styles.table}><thead><tr><th>Cloud / model</th><th>Type / units</th><th>Exact product / meters</th></tr></thead><tbody>
          {draft.mappings.slice(0, visibleMappings).map((mapping, index) => <tr key={index}>
            <td><strong>{mapping.model}</strong><small>{cloudName(mapping.cloud)} · {mapping.publisher} · {mapping.version ?? 'Any version'}</small></td>
            <td>{mapping.deploymentType ?? 'Any supported type'}<small>{mapping.kind === 'ptu' ? 'PTU-hours' : 'Tokens'}</small></td>
            <td>{mapping.meters ? <>{mapping.meters.product}<small>{[mapping.meters.input, mapping.meters.output, mapping.meters.cachedInput, mapping.meters.provisioned].filter(Boolean).join(' / ')}</small></> : 'No public meter mapping'}{mapping.availabilitySource && <small><Link href={mapping.availabilitySource.url} target="_blank" rel="noreferrer">Availability evidence only</Link> · {dateLabel(mapping.availabilitySource.retrievedOn)}</small>}</td>
          </tr>)}
        </tbody></table>
      </div>
      {visibleMappings < draft.mappings.length && <Button onClick={() => setVisibleMappings((value) => value + 100)}>Show 100 more mappings</Button>}
    </details>
  </section>
}

function Changes({ preview }: { preview: PublicCatalogPreview }) {
  return <section aria-labelledby="catalog-changes-heading">
    <div className={styles.sectionHeading}><h3 id="catalog-changes-heading">Review public-price changes</h3>
      <span className={styles.muted}>{preview.added} added · {preview.changed} changed · {preview.unchanged} unchanged</span>
    </div>
    <div className={styles.scroll} tabIndex={0} role="region" aria-label="Proposed public-price changes">
      <table className={styles.table} aria-label="Public-price changes"><thead><tr>
        <th>Model / exact price scope</th><th>Cloud / region</th><th>Current → proposed</th><th>Change / effective date</th>
      </tr></thead><tbody>{preview.changes.map((item) => <tr key={item.price.id}>
        <td><strong>{item.price.model}</strong><small>{item.price.publisher ?? 'Publisher not stated'} · {item.price.version ?? 'Any version'}</small><small>{item.price.deploymentType ?? 'Any supported type'}</small></td>
        <td>{cloudName(item.price.cloud)}<small>{item.price.regions?.join(', ') ?? 'Every matching region'}</small></td>
        <td className={styles.rates}><span className={styles.muted}>{rateLabel(item.previous, item.previousLabel)} →</span><strong>{rateLabel(item.price)}</strong>
          {item.price.cachedInputPerMillion != null && <small>Cached input: {formatRate(item.price.cachedInputPerMillion)} / 1M; measured prompts still use ordinary input pricing.</small>}
        </td>
        <td><Badge appearance="tint" color={item.change === 'added' ? 'success' : item.change === 'changed' ? 'informative' : 'subtle'}>{item.change === 'unchanged' ? 'Unchanged rate' : item.change === 'added' ? 'Added' : 'Changed'}</Badge>
          <small>{dateLabel(item.effectiveFrom)}</small><small>Source effective {dateLabel(item.price.effectiveFrom)}</small></td>
      </tr>)}</tbody></table>
    </div>
    {preview.totalChanges === 0 && <p>No numeric rates are proposed. Validated model/meter definitions may still be activated; missing rates remain unpriced.</p>}
    <p className={styles.muted}>{preview.changes.length} of {preview.totalChanges} proposed public-price rows shown. Manual and approved billing rates are not replaced.</p>
  </section>
}

function Effects({ preview }: { preview: PublicCatalogPreview }) {
  const before = preview.coverage.reduce((sum, item) => sum + item.pricedBefore, 0)
  const after = preview.coverage.reduce((sum, item) => sum + item.pricedAfter, 0)
  const total = preview.coverage.reduce((sum, item) => sum + item.deployments, 0)
  return <aside className={styles.effects} aria-label="Catalog activation effects">
    <section className={styles.panel}>
      <h3>What activation changes</h3>
      <p><strong>{preview.added + preview.changed}</strong> public-price updates</p>
      <h4>Deployed coverage</h4>
      <dl className={styles.coverage}>{preview.coverage.map((row) => <div key={row.cloud}><dt>{cloudName(row.cloud)}</dt><dd>{row.pricedAfter} / {row.deployments}</dd></div>)}</dl>
      <p className={styles.muted}>{after} of {total} deployments priced after activation; {before} before. Catalog entries do not create end-user offerings.</p>
    </section>
    <section className={styles.panel}>
      <h3>What stays unchanged</h3>
      <p><strong>{preview.protectedManual} manual & {preview.protectedBilling} billing rates</strong> retained under the existing matching rules.</p>
      <p className={styles.muted}>{preview.retained} existing price versions retained.</p>
      <p className={styles.muted}>No model deployments, endpoint bindings, publications, grants, or billing connections change.</p>
    </section>
  </aside>
}

function GuidedReview({ draftId, stage, onSelectDraft }: Omit<Props, 'onWorkspaceChange'> & { draftId: string }) {
  const api = useMosaicApi()
  const client = useQueryClient()
  const initialDate = useRef(today())
  const [effectiveFrom, setEffectiveFrom] = useState(initialDate.current)
  const [note, setNote] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const [backdateAcknowledged, setBackdateAcknowledged] = useState(false)
  const [offset, setOffset] = useState(0)
  const [uncertain, setUncertain] = useState(false)
  const [confirmedId, setConfirmedId] = useState<string | null>(null)
  const [statusMessage, setStatusMessage] = useState('')
  const submitted = useRef<PublicCatalogApproval | null>(null)
  const result = useRef<HTMLDivElement>(null)
  const draft = useQuery({ queryKey: ['pricing', 'catalog-draft', draftId], queryFn: () => api.getPublicCatalogDraft(draftId) })
  const validDate = /^\d{4}-\d{2}-\d{2}$/.test(effectiveFrom) && !Number.isNaN(Date.parse(`${effectiveFrom}T00:00:00Z`))
  const preview = useQuery({
    queryKey: ['pricing', 'catalog-preview', draftId, effectiveFrom, offset],
    queryFn: () => api.previewPublicCatalog(draftId, effectiveFrom, offset),
    enabled: !confirmedId && stage === 'review' && validDate && draft.data?.status === 'readyForReview',
    retry: false,
  })
  async function invalidatePricing() {
    await Promise.all(['pricing', 'analytics', 'dashboard', 'budgets'].map((key) => client.invalidateQueries({
      queryKey: [key], predicate: (query) => query.queryKey[1] !== 'catalog-preview',
    })))
  }
  const activate = useMutation({
    mutationFn: (approval: PublicCatalogApproval) => api.activatePublicCatalog(draftId, approval),
    retry: false,
    onSuccess: async (activation) => {
      client.setQueryData<PublicCatalogDraft>(['pricing', 'catalog-draft', draftId],
        (saved) => saved ? { ...saved, status: 'activated', activationId: activation.id } : saved)
      setConfirmedId(activation.id)
      setUncertain(false); setStatusMessage('Catalog activation recorded.')
      await invalidatePricing()
    },
    onError: (error) => {
      if (!(error instanceof ApiError) || error.status >= 500) setUncertain(true)
    },
  })
  const fetchMappings = useMutation({
    mutationFn: () => api.refreshPublicCatalog(draft.data?.baseRevision ?? null, draftId),
    retry: false,
    onSuccess: async (created) => {
      await client.invalidateQueries({ queryKey: ['pricing', 'catalog'] })
      completeWorkspaceNavigation(() => onSelectDraft(created.id, 'validate'))
    },
  })
  const checking = useMutation({
    mutationFn: () => api.getPublicCatalogDraft(draftId),
    onSuccess: async (latest) => {
      client.setQueryData(['pricing', 'catalog-draft', draftId], latest)
      setStatusMessage(latest.status === 'activated'
        ? 'The activation is recorded. No retry is needed.'
        : 'No activation is recorded yet. Check again, or explicitly retry the identical reviewed activation.')
      if (latest.status === 'activated') { setConfirmedId(latest.activationId); activate.reset(); setUncertain(false); await invalidatePricing() }
    },
  })
  const recorded = Boolean(confirmedId) || draft.data?.status === 'activated' || activate.isSuccess
  const pending = activate.isPending || fetchMappings.isPending || checking.isPending
  const backdated = preview.data?.backdated ?? effectiveFrom < today()
  const stale = activate.error instanceof ApiError && activate.error.status === 409
  const dirty = !recorded && (Boolean(note || acknowledged || backdateAcknowledged) || effectiveFrom !== initialDate.current || uncertain)
  const activationBlocked = !preview.data || !preview.isSuccess || preview.isFetching || !validDate ||
    !note.trim() || !acknowledged || (backdated && !backdateAcknowledged) || stale
  useEffect(() => { if (recorded) result.current?.focus() }, [recorded])

  function submit() {
    if (!preview.data || !draft.data || uncertain) return
    const approval: PublicCatalogApproval = {
      draftVersion: preview.data.draft.version, baseRevision: preview.data.baseRevision,
      previewDigest: preview.data.previewDigest, effectiveFrom, note: note.trim(),
      acknowledgeSourceAndGaps: acknowledged, acknowledgeBackdateImpact: backdateAcknowledged,
    }
    submitted.current = approval
    activate.mutate(approval)
  }
  async function reload() {
    activate.reset()
    setAcknowledged(false)
    setBackdateAcknowledged(false)
    await Promise.all([draft.refetch(), preview.refetch(), client.invalidateQueries({ queryKey: ['pricing', 'catalog'] })])
  }
  if (draft.isPending) return <Loading label="Loading catalog draft" />
  if (draft.isError) return <><ErrorState error={draft.error} /><Button onClick={() => onSelectDraft(null)}>Back to catalog</Button></>
  const current = draft.data
  return <WorkspaceEditorFrame
    className={styles.workspace}
    title={recorded ? 'Catalog activation recorded' : stage === 'validate' ? 'Validate public catalog' : 'Review public catalog'}
    description="Source evidence, coverage and price effects stay separate from the explicit activation decision."
    backLabel="Back to catalog" onBack={() => onSelectDraft(null)} onCancel={recorded || stage === 'review' ? undefined : () => onSelectDraft(null)}
    dirty={dirty} pending={pending} pendingLabel={activate.isPending ? 'Recording catalog activation…' : 'Checking public catalog…'}
    error={(activate.error ?? fetchMappings.error ?? checking.error)?.message}
    errorKey={activate.submittedAt + fetchMappings.submittedAt + checking.submittedAt}
    primaryAction={!recorded && !uncertain && current.status !== 'invalid' && stage === 'validate'
      ? { label: 'Review changes', onClick: () => completeWorkspaceNavigation(() => onSelectDraft(draftId, 'review')) }
      : undefined}
  >
    {(requestLeave) => <>
    <Steps step={stage === 'validate' ? 2 : 3} />
    {statusMessage && <MessageBar intent="info"><MessageBarBody>{statusMessage}</MessageBarBody></MessageBar>}
    {recorded && <div ref={result} tabIndex={-1} role="status" aria-label="Catalog activation result"><MessageBar intent="success"><MessageBarBody><MessageBarTitle>Activation is recorded</MessageBarTitle>
      Source dates and the reviewed effective date remain distinct. Earlier days change only for an acknowledged correction; inspect the dated history in Public catalog.
    </MessageBarBody></MessageBar></div>}
    {current.error && <MessageBar intent="error"><MessageBarBody>{current.error} The last usable catalog is unchanged. Return to Public catalog to retry or import corrected data.</MessageBarBody></MessageBar>}
    {stage === 'validate' && !recorded && <section className={styles.panel}>
      <div className={styles.sectionHeading}><h3>Validation result</h3><Badge appearance="tint" color={current.status === 'invalid' ? 'danger' : 'success'}>{current.status === 'invalid' ? 'Invalid acquisition' : 'Ready for review'}</Badge></div>
      <p>{current.definitions} model/meter definitions · {current.prices} public-price rows · {current.sources.length} sources.</p>
      <p className={styles.muted}>Schema, required units, source references and declared cloud scopes were checked. Integrity validation is not independent proof of a supplied source or complete provider coverage.</p>
      <p className={styles.muted}>Existing Government records and provenance are retained unless a supported, explicitly reviewed replacement is activated. Missing rates are not zero and never fall back to another cloud.</p>
      {current.method === 'supplied' && <Button icon={<CloudArrowDownRegular />} disabled={pending}
        onClick={() => { if (!pending) fetchMappings.mutate() }}>Fetch Commercial prices using these mappings</Button>}
    </section>}
    {(stage === 'validate' || recorded) && <Evidence draft={current} />}
    {current.missingPrices.length > 0 && <MessageBar intent="warning"><MessageBarBody><MessageBarTitle>{current.missingPrices.length} mappings have no newly sourced rate</MessageBarTitle>
      Prior usable rates and their original source dates are retained; otherwise these combinations remain unpriced.
      <details className={styles.disclosure}><summary>Missing sourced prices</summary><ul>{current.missingPrices.map((item) => <li key={item}>{item}</li>)}</ul></details>
    </MessageBarBody></MessageBar>}
    {stage === 'review' && !recorded && <>
      {preview.isPending && validDate && <Loading label="Preparing the current pricing preview" />}
      {preview.isError && <><ErrorState error={preview.error} /><p className={styles.muted}>If another catalog was activated, return to Public catalog and create a draft against that revision. No stale draft can replace it.</p><Button onClick={() => { void reload() }}>Reload and review</Button></>}
      {preview.data && !preview.isError && <div className={styles.reviewGrid}>
        <div className={styles.reviewMain}>
          <Evidence draft={current} />
          <Changes preview={preview.data} />
          {preview.data.totalGaps > 0 && <MessageBar intent="warning"><MessageBarBody><MessageBarTitle>{preview.data.totalGaps} deployments remain unpriced</MessageBarTitle>
            Missing facts, unsupported dimensions and absent rates stay visible. No zero price or cross-cloud fallback will be used.
          </MessageBarBody></MessageBar>}
          <details className={styles.disclosure}><summary>Unpriced deployment details ({preview.data.totalGaps})</summary>
            <ul className={styles.gaps}>{preview.data.gaps.map((gap) => <li key={gap.key}><strong>{gap.model ?? 'Unknown model'} · {cloudName(gap.cloud)}</strong><span>{gap.region ?? 'Region unknown'} · {gap.deploymentType ?? 'Type unknown'}</span><span>{gap.message}</span></li>)}</ul>
          </details>
          {preview.data.nextOffset !== null || offset > 0 ? <div className={styles.actions}>
            <Button disabled={offset === 0 || uncertain} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous rows</Button>
            <Button disabled={preview.data.nextOffset === null || uncertain} onClick={() => { if (preview.data.nextOffset !== null) setOffset(preview.data.nextOffset) }}>Next rows</Button>
          </div> : null}
        </div>
        <Effects preview={preview.data} />
      </div>}
      <section className={styles.confirmation} aria-label="Public catalog activation controls">
        <Field label="Effective from (UTC)" required validationMessage={!validDate ? 'Choose a valid UTC date.' : undefined} validationState={!validDate ? 'error' : 'none'}>
          <Input type="date" value={effectiveFrom} disabled={pending || uncertain} onChange={(_, data) => {
            setEffectiveFrom(data.value); setAcknowledged(false); setBackdateAcknowledged(false); setOffset(0); activate.reset()
          }} />
        </Field>
        <Field label="Activation note" required><Textarea value={note} maxLength={500} disabled={pending || uncertain} onChange={(_, data) => setNote(data.value)} resize="vertical" /></Field>
        <div className={styles.confirmationText}>
          <p className={styles.muted}>{!validDate ? 'Choose a valid UTC effective date before reviewing activation.' : effectiveFrom === today() ? "Today's whole-day cost estimates may change; earlier days retain their rates." : backdated ? `This correction can reprice ${dateLabel(effectiveFrom)} through today. Historical reports and budget status may change.` : 'Today and earlier days keep their rates. Public changes apply no earlier than the selected and source effective dates.'}</p>
          <Checkbox checked={acknowledged} disabled={pending || uncertain} onChange={(_, data) => setAcknowledged(data.checked === true)} label="I reviewed the source evidence, protected rates, and remaining coverage gaps." />
          {backdated && <Checkbox checked={backdateAcknowledged} disabled={pending || uncertain} onChange={(_, data) => setBackdateAcknowledged(data.checked === true)} label="I acknowledge that this backdated correction changes historical costs and can change budget blocking." />}
        </div>
      </section>
      {stale && <Button onClick={() => { void reload() }}>Reload and review</Button>}
      {uncertain && <MessageBar intent="warning"><MessageBarBody><MessageBarTitle>Activation outcome is not confirmed</MessageBarTitle>
        Check the saved draft before another write. Editable approval fields are locked.
        <div className={styles.actions}><Button onClick={() => checking.mutate()} disabled={checking.isPending}>Check activation status</Button>
          {statusMessage.startsWith('No activation') && submitted.current && <Button disabled={pending} onClick={() => { if (submitted.current) activate.mutate(submitted.current) }}>Retry identical reviewed activation</Button>}</div>
      </MessageBarBody></MessageBar>}
      <div className={styles.activationFooter}>
        <p className={styles.footerNote}>Activation is audited. Restore previous public rates as a new dated activation, without erasing intervening history. The existing budget worker evaluates new spend at its next check; this action does not write gateway access policies.</p>
        <div className={styles.actions}>
          <Button disabled={pending} onClick={() => requestLeave(() => onSelectDraft(null))}>Cancel</Button>
          {!uncertain && <Button appearance="primary" disabled={activationBlocked} disabledFocusable={pending}
            onClick={() => { if (!pending && !activationBlocked) submit() }}>Activate catalog</Button>}
        </div>
      </div>
    </>}
    </>}
  </WorkspaceEditorFrame>
}

export function PricingCatalogWorkspace({ draftId, stage, onSelectDraft, onWorkspaceChange }: Props) {
  const api = useMosaicApi()
  const client = useQueryClient()
  const [acquiring, setAcquiring] = useState(false)
  const [mode, setMode] = useState<'import' | 'retail'>('import')
  const [file, setFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState('')
  const [offset, setOffset] = useState(0)
  const status = useQuery({ queryKey: ['pricing', 'catalog', offset], queryFn: () => api.getPublicCatalog(offset) })
  const template = useQuery({ queryKey: ['pricing', 'catalog-template'], queryFn: api.getPublicCatalogTemplate, enabled: acquiring && mode === 'import' })
  useEffect(() => {
    onWorkspaceChange(acquiring || Boolean(draftId))
    return () => onWorkspaceChange(false)
  }, [acquiring, draftId, onWorkspaceChange])
  async function openDraft(created: PublicCatalogDraft) {
    await client.invalidateQueries({ queryKey: ['pricing', 'catalog'] })
    completeWorkspaceNavigation(() => {
      setAcquiring(false); setFile(null)
      onSelectDraft(created.id, 'validate')
    })
  }
  const acquisition = useMutation({
    mutationFn: async () => {
      if (!status.data) throw new Error('Load the current catalog before acquisition.')
      const baseRevision = status.data.revision
      if (mode === 'retail') return api.refreshPublicCatalog(baseRevision)
      if (!file || fileError) throw new Error(fileError || 'Choose a JSON pricing package.')
      return api.importPublicCatalog(await file.text(), baseRevision)
    },
    retry: false, onSuccess: openDraft,
  })
  const recover = useMutation({
    mutationFn: (revision: string) => api.recoverPublicCatalog(revision), retry: false, onSuccess: openDraft,
  })
  function start(nextMode: 'import' | 'retail') {
    if (acquisition.isPending) return
    setMode(nextMode); setAcquiring(true); setFile(null); setFileError(''); acquisition.reset()
  }
  function download(kind: 'example' | 'schema') {
    if (!template.data) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(template.data[kind], null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = `mosaic-public-catalog-${kind}.json`; anchor.click()
    URL.revokeObjectURL(url)
  }
  if (draftId) return <GuidedReview key={draftId} draftId={draftId} stage={stage} onSelectDraft={onSelectDraft} />
  if (acquiring) return <WorkspaceEditorFrame
    className={styles.workspace} title="Acquire public pricing data"
    description="Prepare a draft from a fixed public source or a versioned package. Acquisition never activates prices."
    backLabel="Back to catalog" onBack={() => setAcquiring(false)} onCancel={() => setAcquiring(false)}
    dirty={Boolean(file) || mode === 'retail'} pending={acquisition.isPending}
    pendingLabel={mode === 'retail' ? 'Fetching and validating bounded Commercial public data…' : 'Importing and validating the pricing package…'}
    error={acquisition.error?.message} errorKey={acquisition.submittedAt}
    primaryAction={{ label: mode === 'import' ? 'Import & validate' : 'Fetch & validate', onClick: () => acquisition.mutate(), disabled: !status.data || status.isError || (mode === 'import' && (!file || Boolean(fileError))) }}
  >
    <Steps step={1} />
    <Field label="Acquisition method"><RadioGroup value={mode} disabled={acquisition.isPending} onChange={(_, data) => { if (!acquisition.isPending) { setMode(data.value === 'retail' ? 'retail' : 'import'); acquisition.reset() } }}>
      <Radio value="import" label="Import pricing package" /><Radio value="retail" label="Fetch Commercial prices" />
    </RadioGroup></Field>
    <MessageBar intent="info"><MessageBarBody>The documented Retail API scope is Commercial. New Government numeric rates require explicit authoritative Government price evidence, not availability documentation or a matching Commercial meter. Existing Government records and provenance remain intact.</MessageBarBody></MessageBar>
    {mode === 'import' ? <>
      <Field label="Pricing package (.json)" required validationMessage={fileError || 'UTF-8 JSON, at most 15 MiB. No archives, scripts, billing exports, or credentials.'} validationState={fileError ? 'error' : 'none'}>
        <input type="file" className={styles.fileInput} aria-label="Pricing package (.json)" accept=".json,application/json" disabled={acquisition.isPending} onChange={(event) => {
          if (acquisition.isPending) return
          const selected = event.target.files?.[0] ?? null
          setFile(selected)
          setFileError(selected && selected.size > 15 * 1024 * 1024 ? 'The JSON file exceeds 15 MiB.' : selected && !selected.name.toLowerCase().endsWith('.json') ? 'Choose a .json pricing package.' : '')
          acquisition.reset()
        }} />
      </Field>
      <p className={styles.muted}>Use the versioned schema for exact cloud, model/version, publisher, deployment type, region, token/PTU units, sources and dates. Imported source-read dates are administrator assertions, not independent freshness verification.</p>
      {template.isError && <ErrorState error={template.error} />}
      <div className={styles.actions}><Button icon={<ArrowDownloadRegular />} disabled={!template.data} onClick={() => download('example')}>Download fictional example</Button><Button disabled={!template.data} onClick={() => download('schema')}>Download JSON schema</Button></div>
    </> : <section className={styles.panel}><h3>Explicit public-source acquisition</h3><p>The API fetches the current Commercial model/product mappings from prices.azure.com with bounded paging, time, rows and response size. No subscription credential, billing account, or scheduler is used.</p><p className={styles.muted}>New mappings can be imported and validated first, then used for a Commercial fetch. Unknown meters or missing prices are not guessed.</p></section>}
  </WorkspaceEditorFrame>
  if (status.isPending) return <Loading label="Loading public pricing catalog" />
  if (status.isError) return <><ErrorState error={status.error} /><Button onClick={() => { void status.refetch() }}>Retry catalog status</Button></>
  const data = status.data
  return <div className={styles.overview}>
    <div className={styles.overviewHeading}>
      <div><h2>Public catalog</h2><p className={styles.muted}>Maintain sourced reference prices without redeploying MOSAIC. Manual and approved billing-derived rates remain protected.</p></div>
      <div className={styles.actions}><Button icon={<CloudArrowDownRegular />} onClick={() => start('retail')}>Fetch Commercial prices</Button><Button appearance="primary" icon={<ArrowUploadRegular />} onClick={() => start('import')}>Import pricing package</Button></div>
    </div>
    <MessageBar intent="info"><MessageBarBody>{data.revision ? 'A reviewed runtime catalog is active.' : 'The packaged baseline is active; no runtime public catalog has been activated.'} An import or failed attempt never replaces the last usable catalog. Pricing data does not deploy or publish models.</MessageBarBody></MessageBar>
    <dl className={styles.facts}><div><dt>Catalog state</dt><dd>{data.revision ? 'Reviewed runtime catalog' : 'Packaged baseline'}</dd></div><div><dt>Packaged seed read</dt><dd>{dateLabel(data.seedLastUpdated)}</dd></div><div><dt>Model/meter definitions</dt><dd>{data.definitions}</dd></div><div><dt>Deployed coverage today</dt><dd>{data.pricedDeployments} / {data.deployments}</dd></div></dl>
    <section className={styles.panel}><h3>Source evidence & freshness</h3><Sources sources={data.sources} /></section>
    <details className={styles.disclosure}><summary>Reference models and mappings ({data.definitions})</summary>
      <p className={styles.muted}>Availability evidence is not a numeric rate or an end-user offering. Definitions without supported public meters remain visibly unmapped; manual prices are separate.</p>
      <div className={styles.scroll} tabIndex={0} role="region" aria-label="Reference model definitions"><table className={styles.table}><thead><tr><th>Model / cloud</th><th>Deployment type</th><th>Public mapping / availability evidence</th></tr></thead><tbody>
        {data.mappings.map((mapping, index) => <tr key={index}><td><strong>{mapping.model}</strong><small>{cloudName(mapping.cloud)} · {mapping.version ?? 'Any version'}</small></td><td>{mapping.deploymentType ?? 'Any supported type'}</td><td>{mapping.meters?.product ?? 'No public meter mapping'}{mapping.availabilitySource && <small><Link href={mapping.availabilitySource.url} target="_blank" rel="noreferrer">Availability evidence only</Link> · {dateLabel(mapping.availabilitySource.retrievedOn)}</small>}</td></tr>)}
      </tbody></table></div><p className={styles.muted}>{data.mappings.length} definitions shown on this page.</p>
    </details>
    <section><h3>Acquisition attempts</h3><div className={styles.scroll} tabIndex={0} role="region" aria-label="Public catalog acquisitions">
      <table className={styles.table} aria-label="Public catalog acquisitions"><thead><tr><th>Acquired</th><th>Method / state</th><th>Result</th><th>Action</th></tr></thead><tbody>
        {data.drafts.map((draft) => <tr key={draft.id}><td>{dateLabel(draft.acquiredAt)}</td><td>{draft.method === 'retail' ? 'Commercial Retail fetch' : draft.method === 'recovery' ? 'Recovery draft' : 'JSON import'}<small>{draft.status === 'readyForReview' ? 'Ready for review' : draft.status === 'invalid' ? 'Invalid acquisition' : 'Activated'}</small></td><td>{draft.error ?? (draft.method === 'supplied' ? 'Administrator-supplied evidence; review required.' : draft.method === 'recovery' ? 'Prior content, proposed as a new dated activation.' : 'Bounded source acquisition; not automatic activation.')}</td><td><Button onClick={() => onSelectDraft(draft.id, 'validate')}>{draft.status === 'activated' ? 'Inspect evidence' : draft.status === 'invalid' ? 'Inspect issue' : 'Validate & review'}</Button></td></tr>)}
      </tbody></table>
    </div>{data.drafts.length === 0 && <p className={styles.muted}>No acquisition attempts. Start with a source-evidenced package or an explicit Commercial fetch.</p>}</section>
    <section><h3>Dated activation history</h3><p className={styles.muted}>Recovery restores prior public content from a newly reviewed date. It does not erase the intervening cost history.</p>
      {recover.isError && <ErrorState error={recover.error} />}
      <div className={styles.scroll} tabIndex={0} role="region" aria-label="Public catalog activation history"><table className={styles.table} aria-label="Public catalog activation history"><thead><tr><th>Recorded / effective date</th><th>Review note</th><th>Operation</th><th>Recovery</th></tr></thead><tbody>
        {data.history.map((item) => <tr key={item.id}><td>{dateLabel(item.createdAt)}<small>Effective {dateLabel(item.effectiveFrom)}{item.effectiveFrom > today() ? ' · Scheduled' : ''}{item.backdated ? ' · Acknowledged correction' : ''}</small></td><td>{item.note}</td><td><code data-sensitive>{item.id}</code><small>{item.recoveredFrom ? 'Recovery activation' : 'Public catalog activation'}</small></td><td><Button icon={<HistoryRegular />} disabled={recover.isPending || item.id === data.revision} onClick={() => recover.mutate(item.id)}>Restore as new activation</Button></td></tr>)}
      </tbody></table></div>
      {data.history.length === 0 && <p className={styles.muted}>No runtime activations. Packaged prices retain their original source dates.</p>}
    </section>
    {(data.nextOffset !== null || offset > 0) && <div className={styles.actions}><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous rows</Button><Button disabled={data.nextOffset === null} onClick={() => { if (data.nextOffset !== null) setOffset(data.nextOffset) }}>Next rows</Button><span className={styles.muted}>Pages cover model definitions, attempts, and activation history.</span></div>}
  </div>
}
