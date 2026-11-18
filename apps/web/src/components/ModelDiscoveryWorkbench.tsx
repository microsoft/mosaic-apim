import { Badge, Button, Field, MessageBar, MessageBarBody, MessageBarTitle, Select } from '@fluentui/react-components'
import { ArrowLeftRegular, ArrowSyncRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { useEnvironmentCatalog } from '../environments'
import { formatTimestamp } from '../publication-state'
import type { ModelEndpoint, ModelProposalContext, ModelProposalDetail, ModelProposalSummary } from '../types'
import { EmptyState, ErrorState, Loading } from './AsyncState'
import { ResourceWorkbench } from './ResourceWorkbench'
import { WorkspaceDialog } from './WorkspaceDialog'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import styles from './ModelDiscoveryWorkbench.module.css'

const EMPTY_CONTEXT: ModelProposalContext = { environment: null, gatewayId: null, costCenterId: null }
const changes: Record<ModelProposalSummary['change'], string> = {
  baseline: 'Baseline listing', new: 'New catalog version', changed: 'Catalog facts changed',
  unchanged: 'Listing unchanged', unlisted: 'No longer listed', deploymentOnly: 'Deployment only',
}
const outcomes = {
  running: 'Discovery in progress', complete: 'Discovery recorded',
  partial: 'Discovery incomplete', failed: 'Discovery failed', interrupted: 'Discovery interrupted',
}
const lifecycleLabels: Record<string, string> = {
  GenerallyAvailable: 'Generally available', Preview: 'Preview', Deprecated: 'Deprecated',
}

function ModelVersion({ item }: { item: ModelProposalSummary }) {
  return <>{item.model.modelPublisher ?? item.model.modelFormat} · {item.model.modelVersion}</>
}

function Findings({ proposal }: { proposal: ModelProposalDetail }) {
  const { deployments, deploymentStatus, publications } = proposal
  const applied = publications.filter((item) => item.appliedApiRecorded).length
  return <dl className={styles.facts}>
    <div><dt>Provider catalog</dt><dd>{proposal.listed ? 'Version listed' : 'Not listed'}</dd>
      {proposal.lifecycleStatus && <dd>{Object.hasOwn(lifecycleLabels, proposal.lifecycleStatus)
        ? lifecycleLabels[proposal.lifecycleStatus] : proposal.lifecycleStatus}</dd>}</div>
    <div><dt>Azure deployment</dt><dd>{deploymentStatus === 'observed' ? 'Deployment observed' :
      deploymentStatus === 'unconfirmed' ? 'Not confirmed' : 'No exact deployment observed'}</dd>
      {deployments.map((item) => <dd key={item.name} data-resource-name={item.name}>
        {item.name} · {item.provisioningState ?? 'State unknown'}
      </dd>)}</div>
    <div><dt>Governed publication</dt><dd>{publications.length
      ? `${applied} applied API${applied === 1 ? '' : 's'} recorded · ${publications.length} intent record${publications.length === 1 ? '' : 's'}`
      : deploymentStatus === 'unconfirmed' ? 'Not confirmed' : 'No matching publication recorded'}</dd></div>
    <div><dt>Caller eligibility</dt><dd>No access created</dd>
      <dd>Existing publication and exact-resource grants still govern access.</dd></div>
  </dl>
}

function IntentSummary({ proposal }: { proposal: ModelProposalDetail }) {
  const api = useMosaicApi()
  const catalog = useEnvironmentCatalog()
  const context = proposal.savedContext?.context
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: api.listGateways, enabled: Boolean(context?.gatewayId) })
  const centers = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters, enabled: Boolean(context?.costCenterId) })
  const lookup = (key?: string | null, label?: string) => key ? label ?? 'Selection unavailable' : 'Not selected'
  return <>
    <h4>Intended context</h4>
    <dl className={styles.facts}>
      <div><dt>Environment</dt><dd>{lookup(context?.environment, catalog.data?.environments.find(item => item.key === context?.environment)?.displayName)}</dd></div>
      <div><dt>Gateway</dt><dd>{lookup(context?.gatewayId, gateways.data?.find(item => item.id === context?.gatewayId)?.name)}</dd></div>
      <div><dt>Cost center</dt><dd>{lookup(context?.costCenterId, centers.data?.find(item => item.id === context?.costCenterId)?.name)}</dd></div>
    </dl>
    <p>{proposal.contextStatus === 'outOfDate' ? 'Saved context is out of date. Review it again.' :
      proposal.contextStatus === 'incomplete' ? 'Draft saved with missing context.' :
        proposal.contextStatus === 'recorded' ? 'Draft context recorded.' : 'No context draft saved.'}{' '}
      Planning context only. No assignments or access settings change.</p>
    {proposal.contextNotes.map(note => <p key={note}>{note}</p>)}
  </>
}

function Evidence({ proposal }: { proposal: ModelProposalDetail }) {
  const { evidence } = proposal
  return <>
    <h4>Source and freshness</h4>
    <p>Azure account catalog · {evidence.source.location ?? 'Unknown region'} · Commercial</p>
    <p>Catalog observed {formatTimestamp(evidence.catalogObservedAt)}. Deployments observed {formatTimestamp(evidence.deploymentsObservedAt)}.</p>
    <p>Discovery rules: {evidence.rulesRevision}. Runtime access: {
      proposal.runtimeFreshness === 'stale' ? 'Check out of date' :
        proposal.runtimeFreshness === 'current' ? `Checked ${formatTimestamp(proposal.runtimeCheckedAt)} under ${proposal.runtimeRulesRevision}` :
          'No check recorded for the intended gateway'
    }. Discovery does not refresh runtime access or evaluate quota or capacity.</p>
    <details>
      <summary>Source details</summary>
      <p data-resource-name={evidence.source.accountScope}>{evidence.source.accountScope}</p>
      <p>Azure management API {evidence.source.apiVersion}</p>
      {proposal.model.source && <p data-resource-name={proposal.model.source}>Model source: {proposal.model.source}</p>}
      {proposal.model.sourceAccount && <p data-resource-name={proposal.model.sourceAccount}>Model source account: {proposal.model.sourceAccount}</p>}
    </details>
  </>
}

function ProposalEditor({
  endpoint, proposal, onClose, onSaved,
}: {
  endpoint: ModelEndpoint
  proposal: ModelProposalDetail
  onClose: () => void
  onSaved: (proposal: ModelProposalDetail) => void
}) {
  const api = useMosaicApi()
  const catalog = useEnvironmentCatalog()
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: api.listGateways })
  const centers = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters })
  // Keep the reviewed evidence and expected revision fixed even if the list refetches.
  const [review] = useState(proposal)
  const initial = review.savedContext?.context ?? EMPTY_CONTEXT
  const [context, setContext] = useState(initial)
  const save = useMutation({
    mutationFn: () => api.saveModelProposalContext(endpoint.id, review.id, {
      ...context, evidenceRunId: review.evidence.id, evidenceDigest: review.evidenceDigest,
      expectedRevision: review.contextRevision,
    }),
    onSuccess: saved => completeWorkspaceNavigation(() => onSaved(saved)),
  })
  const field = (label: string, key: keyof ModelProposalContext, options: { id: string; name: string }[]) =>
    <Field label={label}>
      <Select value={context[key] ?? ''} disabled={save.isPending}
        onChange={event => setContext(current => ({ ...current, [key]: event.target.value || null }))}>
        <option value="">Not selected</option>
        {context[key] && !options.some(item => item.id === context[key]) &&
          <option value={context[key]}>Previous selection unavailable</option>}
        {options.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </Select>
    </Field>
  const error = catalog.error ?? gateways.error ?? centers.error
  return <WorkspaceDialog open title="Review onboarding proposal"
    description={<>{review.model.modelName} · <ModelVersion item={review} /> · {endpoint.name}</>}
    onOpenChange={(_, state) => { if (!state.open) onClose() }}
    dirty={context !== initial} pending={save.isPending}
    error={save.isError ? <ErrorState context="action" title="Proposal context was not saved" error={save.error} /> : undefined}
    actions={requestClose => <>
      <Button onClick={requestClose} disabled={save.isPending}>Cancel</Button>
      <Button appearance="primary" onClick={() => save.mutate()}
        disabled={save.isPending || !proposal.evidenceCurrent || Boolean(error)
          || catalog.isPending || gateways.isPending || centers.isPending}
      >{save.isPending ? 'Saving proposal…' : 'Save proposal'}</Button>
    </>}>
    <div className={styles.review}>
      <p>Save a draft for this exact version. You can leave context unspecified. No deployment, publication, pool, or access changes will be made.</p>
      {!proposal.evidenceCurrent && <p role="status">Evidence changed or is incomplete. Return to discovery and review a complete read.</p>}
      {error && <ErrorState error={error} />}
      {(catalog.isPending || gateways.isPending || centers.isPending) && <Loading label="Loading proposal context choices" />}
      <div className={styles.facts}>
        {field('Intended environment', 'environment', (catalog.data?.environments ?? []).map(item => ({ id: item.key, name: item.displayName })))}
        {field('Intended gateway', 'gatewayId', (gateways.data ?? []).filter(item => (item.azureCloud ?? 'commercial') === (endpoint.azureCloud ?? 'commercial')))}
        {field('Intended cost center', 'costCenterId', centers.data ?? [])}
      </div>
      <Findings proposal={review} />
      <Evidence proposal={review} />
    </div>
  </WorkspaceDialog>
}

export function ModelDiscoveryWorkbench({ endpoint, onClose }: { endpoint: ModelEndpoint; onClose: () => void }) {
  const api = useMosaicApi()
  const cache = useQueryClient()
  const back = useRef<HTMLButtonElement>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editing, setEditing] = useState<ModelProposalDetail | null>(null)
  const [saved, setSaved] = useState(false)
  const lastRequest = useRef<string | null>(null)
  const listKey = ['model-discovery', endpoint.id]
  const list = useQuery({ queryKey: listKey, queryFn: () => api.listModelProposals(endpoint.id) })
  const detail = useQuery({
    queryKey: [...listKey, selectedId],
    queryFn: () => api.getModelProposal(endpoint.id, selectedId!),
    enabled: selectedId !== null,
  })
  useEffect(() => { back.current?.focus() }, [])
  const discover = useMutation({
    mutationFn: () => api.discoverModelChanges(endpoint.id, lastRequest.current ??= crypto.randomUUID()),
    onSuccess: run => { if (run.status !== 'running') lastRequest.current = null },
    onSettled: async () => { await cache.invalidateQueries({ queryKey: listKey }) },
  })
  const latest = list.data?.latestAttempt
  const historical = Boolean(list.data?.lastComplete && !list.data.evidenceCurrent)
  const details = detail.data
  const busy = discover.isPending
  return <>
    <Button className={styles.back} ref={back} appearance="subtle" icon={<ArrowLeftRegular />} onClick={onClose}>Back to model endpoint</Button>
    <ResourceWorkbench
      title="Discover model changes"
      description={`${endpoint.name} · ${endpoint.provider === 'azureAiFoundry' ? 'Azure AI Foundry' : endpoint.provider === 'azureOpenAi' ? 'Azure OpenAI' : 'Registered endpoint'} · Discovery only`}
      actions={list.data?.supported && <Button appearance="primary" icon={<ArrowSyncRegular />}
        aria-busy={busy} disabled={busy} onClick={() => {
          setSaved(false)
          if (latest?.status === 'running') void cache.invalidateQueries({ queryKey: listKey })
          else discover.mutate()
        }}
      >{busy ? 'Discovering changes…' : latest?.status === 'running' ? 'Check discovery status' : 'Discover changes'}</Button>}
      toolbar={<div className={styles.messages}>
        {list.isPending && <Loading label="Loading saved discovery" />}
        {list.isError && <ErrorState error={list.error} />}
        {discover.isError && <ErrorState context="action" title="MOSAIC could not complete discovery" error={discover.error} />}
        {saved && <p role="status">Proposal context saved. No operational changes made.</p>}
        {latest && <MessageBar intent={latest.status === 'complete' ? 'info' : 'warning'}>
          <MessageBarBody><MessageBarTitle>{outcomes[latest.status]}</MessageBarTitle>
            Attempted {formatTimestamp(latest.startedAt)}. {historical && 'Showing prior evidence; review and save are unavailable until a complete discovery.'}
            {latest.findings.map(finding => <p key={finding}>{finding}</p>)}
          </MessageBarBody>
        </MessageBar>}
      </div>}
      items={list.data?.items ?? []} getId={item => item.id}
      selectedId={selectedId} onSelect={id => { setSelectedId(id); setSaved(false) }}
      listLabel="Discovered model versions" detailLabel="Selected version evidence" boundedList
      emptyList={<EmptyState title={list.data?.supported === false ? 'Discovery unsupported' :
        list.data?.lastComplete ? 'No model versions observed' : latest ? 'No complete discovery yet' : 'No discovery recorded'}>
        {list.data?.supported === false ? 'Use a Commercial Azure OpenAI or Foundry endpoint registered by ARM resource ID.' :
          list.data?.lastComplete ? 'The last complete account read contained no catalog entries or matching deployments.' :
            'Choose Discover changes to read the account catalog and deployments. Opening this view only reads MOSAIC’s saved records.'}
      </EmptyState>}
      renderListItem={item => <div className={styles.row}>
        <strong>{item.model.modelName}</strong><span><ModelVersion item={item} /></span>
        <div className={styles.tags}>
          <Badge appearance="tint" color={item.change === 'new' ? 'brand' : 'informative'}>{changes[item.change]}</Badge>
          {item.deploymentStatus === 'observed' && <Badge appearance="tint" color="informative">Already deployed</Badge>}
          {item.deploymentStatus === 'unconfirmed' && <Badge appearance="tint" color="warning">Deployment not confirmed</Badge>}
        </div>
      </div>}
      renderDetail={item => <article className={styles.detail}>
        <h3>{item.model.modelName}</h3>
        <p><ModelVersion item={item} /> · {changes[item.change]}</p>
        {detail.isPending && <Loading label="Loading exact-version evidence" />}
        {detail.isError && <ErrorState error={detail.error} />}
        {details && <>
          <MessageBar><MessageBarBody><MessageBarTitle>Discovery does not create access</MessageBarTitle>
            A catalog listing is not a deployment or permission to call a model. Existing deployments, publications, pools, and grants stay unchanged.
          </MessageBarBody></MessageBar>
          <Findings proposal={details} />
          <IntentSummary proposal={details} />
          <Evidence proposal={details} />
          <Button appearance="primary" onClick={() => setEditing(details)}
            disabled={busy || historical || !details.evidenceCurrent}>Review proposal</Button>
        </>}
      </article>}
    />
    {editing && <ProposalEditor endpoint={endpoint} proposal={editing}
      onClose={() => setEditing(null)}
      onSaved={result => {
        cache.setQueryData([...listKey, result.id], result)
        void cache.invalidateQueries({ queryKey: listKey })
        setEditing(null)
        setSaved(true)
      }} />}
  </>
}
