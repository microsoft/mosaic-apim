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
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Select,
  Text,
  Title3,
} from '@fluentui/react-components'
import { AddRegular, BuildingRegular, MoneyRegular, PeopleRegular, SettingsRegular, GaugeRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { BUDGET_LEVEL_LABELS, budgetBadgeColor } from '../budgets'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { BudgetDetails, BudgetForm, BudgetMeter } from '../components/BudgetEditor'
import { PageHeader } from '../components/PageHeader'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { ResourceWorkbench } from '../components/ResourceWorkbench'
import { CostCenterEditor } from '../components/CostCenterSetupForm'
import { CostCenterDirectoryMembers } from '../components/CostCenterDirectoryMembers'
import { CostCenterDirectoryOperations } from '../components/CostCenterDirectoryOperations'
import { CostCenterResourceAccessFields } from '../components/CostCenterResourceAccess'
import { WorkspaceLeaveGuard, completeWorkspaceNavigation } from '../components/WorkspaceLeaveGuard'
import { useWorkbenchLocation } from '../components/useWorkbenchLocation'
import { COST_CENTER_SETUP_LABELS, costCenterSetupState } from '../cost-centers'
import type { BudgetUpdate, CostCenter, CostCenterLimit, QuotaPeriod } from '../types'
import styles from './CostCentersPage.module.css'

const PERIODS: QuotaPeriod[] = ['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly']

interface LimitDraft {
  /** The chosen resource's `limitKey`. */
  resource: string
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: QuotaPeriod
  callsPerMinute: string
  callQuota: string
  callQuotaPeriod: QuotaPeriod
  monthlyTokens: string
  monthlyCalls: string
}

interface LimitResourceOption {
  key: string
  kind: CostCenterLimit['resource']['kind']
  id: string
  /** A pool model's pool. */
  scopeId?: string
  label: string
}

/** Pool model IDs are unique only within their pool, so a pool model's key names its pool too. */
function limitKey(resource: CostCenterLimit['resource']): string {
  return resource.kind === 'poolModel'
    ? `poolModel:${resource.scopeId ?? ''}:${resource.id}`
    : `${resource.kind}:${resource.id}`
}

const emptyLimitDraft: LimitDraft = {
  resource: '',
  tokensPerMinute: '',
  tokenQuota: '',
  tokenQuotaPeriod: 'Monthly',
  callsPerMinute: '',
  callQuota: '',
  callQuotaPeriod: 'Monthly',
  monthlyTokens: '',
  monthlyCalls: '',
}

function ownerText(costCenter: Pick<CostCenter, 'owners'>) {
  return costCenter.owners.length ? costCenter.owners.join(', ') : 'No contact owners'
}

function errorMessage(error: unknown, fallback: string) {
  const reason = error instanceof ApiError ? error.body?.details?.reason : undefined
  if (reason === 'codeInUse') return 'That code is already used by another cost center. Choose a unique code.'
  if (reason === 'builtIn') return 'General is built in and cannot be deleted.'
  if (reason === 'tenantDefault') return 'This is the tenant default. Choose another default before deleting it.'
  if (reason === 'hasGrants') return 'This cost center still has grants. Revoke or move them before deleting it.'
  if (reason === 'isDefault') return 'People still use this as their default cost center. Move them before deleting it.'
  if (reason === 'notACostCenterMember') return 'That principal cannot charge this cost center. Add them as a member first.'
  return error instanceof Error ? error.message : fallback
}

function pendingRecheckText(costCenter: CostCenter) {
  const rechecks = costCenter.pendingRechecks ?? []
  const grants = new Set(rechecks.flatMap((item) => item.entitlementIds ?? []))
  const counted = rechecks.every((item) => item.entitlementIds !== null)
  const which = counted && grants.size > 0
    ? `${grants.size.toLocaleString()} grant${grants.size === 1 ? '' : 's'} under this cost center`
    : 'Some grants under this cost center'
  return `${which} may no longer be chargeable here. Eligibility verification or a busy resource prevented the check from finishing. Applies leave affected grants out until the recheck completes.`
}

function CostCenterBadges({ costCenter, showKeys = true }: { costCenter: CostCenter; showKeys?: boolean }) {
  return (
    <span className={styles.badges}>
      {costCenter.isTenantDefault && <Badge appearance="tint" color="brand">Tenant default</Badge>}
      <Badge appearance="tint" color={costCenterSetupState(costCenter) === 'setupRequired' ? 'warning' : 'informative'}>
        {COST_CENTER_SETUP_LABELS[costCenterSetupState(costCenter)]}
      </Badge>
      {(costCenter.pendingRechecks?.length ?? 0) > 0 && (
        <Badge appearance="tint" color="warning">Recheck pending</Badge>
      )}
      {showKeys && (
        <Badge appearance={costCenter.keysAllowed ? 'tint' : 'outline'} color={costCenter.keysAllowed ? 'success' : 'warning'}>
          {costCenter.keysAllowed ? 'Keys allowed' : 'Keys off'}
        </Badge>
      )}
    </span>
  )
}

export function CostCentersPage() {
  const sections = [
    { value: 'centers', label: 'Cost centers', icon: <BuildingRegular /> },
    { value: 'organization', label: 'Organization-wide budget', icon: <MoneyRegular />, description: 'Aggregate monthly spending across all cost centers and gateways. Warn-only: this budget never blocks calls.' },
  ]
  const [section, selectSection] = usePageSection(sections)
  const workbench = useWorkbenchLocation({ section })
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [defaultCostCenterId, setDefaultCostCenterId] = useState('')
  const costCenters = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters })
  const settings = useQuery({ queryKey: ['cost-center-settings'], queryFn: api.getCostCenterSettings })
  const budgets = useQuery({ queryKey: ['budgets'], queryFn: api.getBudgets })
  const budgetsByCostCenter = useMemo(
    () => new Map((budgets.data?.costCenters ?? []).flatMap((item) => (item.costCenter ? [[item.costCenter.id, item] as const] : []))),
    [budgets.data],
  )

  useEffect(() => {
    if (settings.data) setDefaultCostCenterId(settings.data.defaultCostCenterId)
  }, [settings.data])

  const saveOrganizationBudget = useMutation({
    mutationFn: (payload: BudgetUpdate) => api.setOrganizationBudget(payload),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['budgets'] }),
  })
  const removeOrganizationBudget = useMutation({
    mutationFn: () => api.deleteOrganizationBudget(),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['budgets'] }),
  })

  const saveSettings = useMutation({
    mutationFn: () => api.updateCostCenterSettings({ defaultCostCenterId }),
    // Moving the default changes which cost center is badged as the tenant default, and the
    // recheck it runs can revoke grants.
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['cost-center-settings'] }),
        queryClient.invalidateQueries({ queryKey: ['cost-centers'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
      ])
    },
  })

  return (
    <section className={styles.page}>
      <PageHeader
        title="Cost centers"
        description="Connect dedicated Entra groups, choose exact resource access, and charge grants and individual keys across gateways."
      />
      <SectionLayout label="Cost centers" sections={sections} active={section} onSelect={(value) => { if (value !== section) selectSection(value) }}>
      {section === 'centers' && (costCenters.isPending || settings.isPending) && <Loading label="Loading cost centers" />}
      {section === 'centers' && (costCenters.isError || settings.isError) && <ErrorState error={costCenters.error ?? settings.error} />}
      {section === 'centers' && costCenters.data && settings.data && (
        workbench.mode === 'new' ? (
          <CostCenterEditor onCancel={() => workbench.close()} onSaved={(id) => workbench.close(id)} />
        ) : workbench.mode === 'edit' && workbench.selectedId ? (
          <CostCenterWorkspace key={workbench.selectedId} costCenterId={workbench.selectedId} embedded />
        ) : (
          <div className={styles.stack}>
            <ResourceWorkbench
              title="Cost centers"
              description="Custom centers require a verified exclusive Entra group. General is exempt; a default preference is not a custom-center membership grant."
              actions={<Button appearance="primary" icon={<AddRegular />}
                onClick={() => workbench.open('new')}>New cost center</Button>}
              items={costCenters.data}
              getId={(costCenter) => costCenter.id}
              selectedId={workbench.selectedId}
              onSelect={workbench.select}
              listLabel="Cost centers"
              detailLabel="Selected cost center"
              renderListItem={(costCenter) => {
                const budget = budgetsByCostCenter.get(costCenter.id)
                return <span className={styles.resourceRow}>
                  <span className={styles.primary}>{costCenter.name}</span>
                  <span className={styles.muted}>{costCenter.code} · {costCenter.enabledGrantCount} / {costCenter.grantCount} grants</span>
                  <span className={styles.badges}>
                    <CostCenterBadges costCenter={costCenter} />
                    {budget && budget.status.level !== 'ok' && <Badge appearance="tint" color={budgetBadgeColor(budget.status.level)}>
                      {BUDGET_LEVEL_LABELS[budget.status.level]}
                    </Badge>}
                  </span>
                </span>
              }}
              renderDetail={(costCenter) => <CostCenterWorkspace key={costCenter.id} costCenterId={costCenter.id} embedded />}
              emptyList={<EmptyState title="No cost centers">Create a cost center to charge grants separately.</EmptyState>}
              emptySelection={<EmptyState title="Select a cost center">Inspect group setup, actual directory members, resource access, limits, and budget.</EmptyState>}
            />
            <details className={styles.defaultSettings}>
              <summary>Tenant default</summary>
            <form className={styles.form} onSubmit={(event) => { event.preventDefault(); saveSettings.mutate() }}>
              {saveSettings.isError && (
                <MessageBar intent="error">
                  <MessageBarBody>{errorMessage(saveSettings.error, 'Unable to save the default cost center.')}</MessageBarBody>
                </MessageBar>
              )}
              <Text className={styles.muted}>
                This is a charging preference for eligible principals. Choosing a custom center never grants group membership. General retains its legacy eligibility rules; changing the default may require individual grant rechecks.
              </Text>
              <Field label="Default cost center">
                <Select value={defaultCostCenterId} onChange={(_, data) => setDefaultCostCenterId(data.value)}>
                  {costCenters.data.map((costCenter) => (
                    <option key={costCenter.id} value={costCenter.id} disabled={costCenterSetupState(costCenter) === 'setupRequired'}>{costCenter.name} ({costCenter.code}){costCenterSetupState(costCenter) === 'setupRequired' ? ' — Setup required' : ''}</option>
                  ))}
                </Select>
              </Field>
              <Button appearance="primary" type="submit" disabled={!defaultCostCenterId || saveSettings.isPending}>
                Save default
              </Button>
            </form>
            </details>
          </div>
        )
      )}
      {section === 'organization' && <div className={styles.card}>
        <div className={styles.cardHeader}>
          <Title3 as="h2">Organization-wide budget</Title3>
          <Badge appearance="tint" color="warning">Warn-only · Never blocks</Badge>
          <Text className={styles.muted}>
            Every priced call this month, including calls MOSAIC couldn&apos;t link to a grant
            and reserved capacity nobody called. Email at each threshold; blocking is available
            only on an individual cost-center budget.
          </Text>
        </div>
        {budgets.isPending && <Loading label="Loading organization budget" />}
        {budgets.isError && <ErrorState error={budgets.error} />}
        {budgets.data?.organization && <BudgetMeter budget={budgets.data.organization} label="Organization" />}
        {budgets.data && <BudgetForm
          budget={budgets.data.organization}
          organization
          saving={saveOrganizationBudget.isPending}
          removing={removeOrganizationBudget.isPending}
          error={
            saveOrganizationBudget.isError
              ? errorMessage(saveOrganizationBudget.error, 'Unable to save the organization budget.')
              : removeOrganizationBudget.isError
                ? errorMessage(removeOrganizationBudget.error, 'Unable to remove the organization budget.')
                : null
          }
          onSave={(payload) => saveOrganizationBudget.mutate(payload)}
          onRemove={() => removeOrganizationBudget.mutate()}
        />}
      </div>}
      </SectionLayout>
    </section>
  )
}

const PERIOD_NOUNS: Record<QuotaPeriod, string> = {
  Hourly: 'hour',
  Daily: 'day',
  Weekly: 'week',
  Monthly: 'month',
  Yearly: 'year',
}

function limitText(limit: CostCenterLimit, label: string) {
  const n = (value: number) => value.toLocaleString()
  const per = (period: QuotaPeriod | null | undefined) => PERIOD_NOUNS[period ?? 'Monthly']
  const pieces = [label]
  if (limit.person?.tokensPerMinute) pieces.push(`${n(limit.person.tokensPerMinute)} tokens per minute`)
  if (limit.person?.tokenQuota) pieces.push(`${n(limit.person.tokenQuota)} tokens per ${per(limit.person.tokenQuotaPeriod)}`)
  if (limit.person?.callsPerMinute) pieces.push(`${n(limit.person.callsPerMinute)} calls per minute`)
  if (limit.person?.callQuota) pieces.push(`${n(limit.person.callQuota)} calls per ${per(limit.person.callQuotaPeriod)}`)
  const pool = []
  if (limit.pool?.monthlyTokens) pool.push(`${n(limit.pool.monthlyTokens)} tokens per month`)
  if (limit.pool?.monthlyCalls) pool.push(`${n(limit.pool.monthlyCalls)} calls per month`)
  if (pool.length) pieces.push(`Pooled quota: ${pool.join(' · ')}`)
  return pieces.join(' · ')
}

function parsePositiveInteger(value: string) {
  const trimmed = value.trim()
  if (!trimmed) return { value: null, valid: true }
  if (!/^\d+$/.test(trimmed)) return { value: null, valid: false }
  const parsed = Number(trimmed)
  return { value: parsed, valid: Number.isSafeInteger(parsed) && parsed > 0 }
}

export function CostCenterDetailPage() {
  const { costCenterId = '' } = useParams()
  return <CostCenterWorkspace costCenterId={costCenterId} />
}

function CostCenterWorkspace({ costCenterId, embedded = false }: { costCenterId: string; embedded?: boolean }) {
  const sections = [
    { value: 'overview', label: 'Overview', icon: <BuildingRegular /> },
    { value: 'members', label: 'Members', icon: <PeopleRegular /> },
    { value: 'access', label: 'Resource access', icon: <GaugeRegular /> },
    { value: 'limits', label: 'Limits', icon: <GaugeRegular /> },
    { value: 'budget', label: 'Budget', icon: <MoneyRegular /> },
  ]
  const [section, selectSection] = usePageSection(sections, 'overview', embedded ? 'centerTab' : 'tab')
  const workbench = useWorkbenchLocation({ section: embedded ? 'centers' : section })
  const editing = workbench.mode === 'edit'
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [limitDraft, setLimitDraft] = useState<LimitDraft>(emptyLimitDraft)
  const [limitValidation, setLimitValidation] = useState<string | null>(null)
  const [limits, setLimits] = useState<CostCenterLimit[]>([])
  const limitsLoadedId = useRef<string | null>(null)
  const limitsBaseline = useRef<CostCenterLimit[]>([])
  const previousLimitSection = useRef(section)

  const costCenter = useQuery({ queryKey: ['cost-centers', costCenterId], queryFn: () => api.getCostCenter(costCenterId), enabled: Boolean(costCenterId) })
  const budget = useQuery({
    queryKey: ['budgets', costCenterId],
    queryFn: () => api.getCostCenterBudget(costCenterId),
    enabled: Boolean(costCenterId),
  })
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: () => api.listModelApis() })
  const mcpServers = useQuery({ queryKey: ['mcp-servers'], queryFn: () => api.listMcpServers() })
  const modelPools = useQuery({ queryKey: ['model-pools', 'list'], queryFn: () => api.listModelPools() })
  const data = costCenter.data

  const resources = useMemo<LimitResourceOption[]>(
    () => [
      ...(modelApis.data ?? []).map((item) => ({ key: `modelApi:${item.id}`, kind: 'modelApi' as const, id: item.id, label: `${item.displayName} (model API)` })),
      ...(modelPools.data ?? []).flatMap((pool) => pool.models.map((model) => ({
        key: limitKey({ kind: 'poolModel', id: model.id, scopeId: pool.id }),
        kind: 'poolModel' as const,
        id: model.id,
        scopeId: pool.id,
        label: `${model.displayName} in ${pool.displayName} (pool model)`,
      }))),
      ...(mcpServers.data ?? []).map((item) => ({ key: `mcpServer:${item.id}`, kind: 'mcpServer' as const, id: item.id, label: `${item.displayName} (MCP server)` })),
    ],
    [mcpServers.data, modelApis.data, modelPools.data],
  )
  const labels = useMemo(() => new Map(resources.map((item) => [item.key, item.label])), [resources])

  useEffect(() => {
    if (!data) return
    const keepDraft = limitsLoadedId.current === data.id && previousLimitSection.current === section && section === 'limits'
    const previousBaseline = limitsBaseline.current
    setLimits((current) => keepDraft && JSON.stringify(current) !== JSON.stringify(previousBaseline) ? current : data.limits)
    if (!keepDraft) {
      setLimitDraft(emptyLimitDraft)
      setLimitValidation(null)
    }
    limitsLoadedId.current = data.id
    limitsBaseline.current = data.limits
    previousLimitSection.current = section
  }, [data, section])

  const saveLimits = useMutation({
    mutationFn: () => api.updateCostCenterLimits(costCenterId, limits),
    onSuccess: async (saved) => {
      setLimits(saved.limits)
      limitsBaseline.current = saved.limits
      setLimitDraft(emptyLimitDraft)
      await queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenterId] })
    },
  })
  const deleteCostCenter = useMutation({
    mutationFn: () => api.deleteCostCenter(costCenterId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['cost-centers'] })
      completeWorkspaceNavigation(() => embedded ? workbench.close(null) : navigate('/cost-centers'))
    },
  })
  const recheck = useMutation({
    mutationFn: () => api.recheckCostCenter(costCenterId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['cost-centers'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
      ])
    },
  })
  // Saving judges the budget at once, so a raised budget lifts its block in the same request.
  const saveBudget = useMutation({
    mutationFn: (payload: BudgetUpdate) => api.setCostCenterBudget(costCenterId, payload),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['budgets'] }),
  })
  const removeBudget = useMutation({
    mutationFn: () => api.deleteCostCenterBudget(costCenterId),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['budgets'] }),
  })

  function addLimit(event: FormEvent) {
    event.preventDefault()
    const resource = resources.find((item) => item.key === limitDraft.resource)
    if (!resource) return
    const isMcp = resource.kind === 'mcpServer'
    const values = {
      tokensPerMinute: isMcp ? { value: null, valid: true } : parsePositiveInteger(limitDraft.tokensPerMinute),
      tokenQuota: isMcp ? { value: null, valid: true } : parsePositiveInteger(limitDraft.tokenQuota),
      callsPerMinute: parsePositiveInteger(limitDraft.callsPerMinute),
      callQuota: parsePositiveInteger(limitDraft.callQuota),
      monthlyTokens: isMcp ? { value: null, valid: true } : parsePositiveInteger(limitDraft.monthlyTokens),
      monthlyCalls: parsePositiveInteger(limitDraft.monthlyCalls),
    }
    if (Object.values(values).some((value) => !value.valid)) {
      setLimitValidation('Use positive whole numbers for limits.')
      return
    }
    const hasPerson = Boolean(values.tokensPerMinute.value || values.tokenQuota.value || values.callsPerMinute.value || values.callQuota.value)
    const hasPool = Boolean(values.monthlyTokens.value || values.monthlyCalls.value)
    if (!hasPerson && !hasPool) {
      setLimitValidation('Set per-person limits, a pooled quota, or both.')
      return
    }
    const next: CostCenterLimit = {
      resource: { kind: resource.kind, id: resource.id, ...(resource.scopeId ? { scopeId: resource.scopeId } : {}) },
      person: hasPerson ? {
        tokensPerMinute: values.tokensPerMinute.value,
        tokenQuota: values.tokenQuota.value,
        tokenQuotaPeriod: values.tokenQuota.value ? limitDraft.tokenQuotaPeriod : null,
        callsPerMinute: values.callsPerMinute.value,
        callQuota: values.callQuota.value,
        callQuotaPeriod: values.callQuota.value ? limitDraft.callQuotaPeriod : null,
      } : null,
      pool: hasPool ? {
        monthlyTokens: values.monthlyTokens.value,
        monthlyCalls: values.monthlyCalls.value,
      } : null,
    }
    setLimits((current) => [...current.filter((item) => limitKey(item.resource) !== limitDraft.resource), next])
    setLimitValidation(null)
    setLimitDraft(emptyLimitDraft)
  }

  if (costCenter.isPending) return <Loading label="Loading cost center" />
  if (costCenter.isError || !data) return <div className={styles.stack}>
    {costCenter.isError
      ? <ErrorState error={costCenter.error} />
      : <EmptyState title="Cost center unavailable">This record may have been removed or is no longer accessible.</EmptyState>}
    <Button onClick={() => embedded ? workbench.close(null) : navigate('/cost-centers')}>Back to cost centers</Button>
  </div>

  const selectedLimitResource = resources.find((item) => item.key === limitDraft.resource)
  const limitResourceIsMcp = selectedLimitResource?.kind === 'mcpServer'
  function selectCostCenterSection(value: string) {
    if (value === section) return
    if (!embedded) {
      selectSection(value)
      return
    }
    const next = new URLSearchParams(location.search)
    next.set('centerTab', value)
    navigate({ pathname: location.pathname, search: `?${next}` }, { preventScrollReset: true })
  }

  if (editing) return (
    <CostCenterEditor costCenter={data} onCancel={() => workbench.close()}
      onSaved={() => workbench.close(embedded ? costCenterId : undefined)} />
  )

  return (
    <WorkspaceLeaveGuard
      dirty={section === 'limits' && (JSON.stringify(limits) !== JSON.stringify(data.limits) || JSON.stringify(limitDraft) !== JSON.stringify(emptyLimitDraft))}
      pending={saveLimits.isPending}>
    {() => <section className={`${styles.page} ${embedded ? styles.embedded : ''}`}>
      {embedded ? <div className={styles.selectedHeader}>
        <div className={styles.cardHeader}><Title3 as="h3">{data.name}</Title3><Text className={styles.muted}>{data.code} · {data.description ?? 'No description'}</Text></div>
        <RouterLink to={`/cost-centers/${encodeURIComponent(data.id)}`}
          state={{ from: `${location.pathname}${location.search}` }}>Open cost center</RouterLink>
      </div> : <PageHeader
        title={data.name}
        description={`${data.code} · ${data.description ?? 'No description'}`}
        actions={
          <>
            <Button onClick={() => navigate(typeof location.state?.from === 'string' && location.state.from.startsWith('/cost-centers?')
              ? location.state.from : `/cost-centers?selected=${encodeURIComponent(data.id)}`)}>Back to cost centers</Button>
            <Button onClick={() => navigate(`/entitlements?costCenter=${encodeURIComponent(data.id)}`)}>View grants</Button>
          </>
        }
      />}
      {(data.pendingRechecks?.length ?? 0) > 0 && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Recheck pending</MessageBarTitle>
            {pendingRecheckText(data)}{' '}
            <Button size="small" onClick={() => recheck.mutate()} disabled={recheck.isPending}>
              Check grants again
            </Button>
          </MessageBarBody>
        </MessageBar>
      )}
      {recheck.isError && (
        <MessageBar intent="error">
          <MessageBarBody>{errorMessage(recheck.error, 'Unable to check the grants again.')}</MessageBarBody>
        </MessageBar>
      )}
      <SectionLayout label="Cost center" orientation={embedded ? 'horizontal' : 'vertical'}
        sections={sections} active={section} onSelect={selectCostCenterSection}>
      <Text className={styles.muted}>Scope: {data.name} ({data.code}) · This cost center only.</Text>
      {section === 'overview' && <div className={styles.stack}>
        <div className={styles.card}>
          <Title3 as="h2">Summary</Title3>
          <div className={styles.rowActions}>
            <Button appearance="primary" icon={<SettingsRegular />} onClick={() => workbench.open('edit', embedded ? data.id : undefined)}>Edit cost center</Button>
            {costCenterSetupState(data) === 'setupRequired' && <Button onClick={() => workbench.open('edit', embedded ? data.id : undefined)}>Finish group setup</Button>}
            {embedded && <Button onClick={() => navigate(`/entitlements?costCenter=${encodeURIComponent(data.id)}`)}>View grants</Button>}
          </div>
          <dl className={styles.detailList}>
            <div><dt>Status</dt><dd><CostCenterBadges costCenter={data} /></dd></div>
            <div><dt>Grants</dt><dd>{data.enabledGrantCount} enabled of {data.grantCount}</dd></div>
            <div><dt>Contact owners</dt><dd>{ownerText(data)} · Does not assign group ownership or membership</dd></div>
            <div><dt>Defaults</dt><dd>{data.defaultFor} principal{data.defaultFor === 1 ? '' : 's'}</dd></div>
            <div><dt>Membership authority</dt><dd>{data.setupState === 'exempt' ? 'General is exempt from dedicated group setup' : 'Actual Entra membership, read one page at a time in Members. No local shadow roster.'}</dd></div>
            {data.setupState !== 'exempt' && <>
              <div><dt>Portal publication</dt><dd>{data.discoverable ? 'Discoverable in My cost centers' : 'Private — not discoverable for new join requests'}</dd></div>
              <div><dt>Public purpose</dt><dd>{data.publicPurpose || 'Not supplied'}</dd></div>
            </>}
            {data.entraBinding && <>
              <div><dt>Directory tenant</dt><dd className={styles.code}>{data.entraBinding.directoryTenantId}</dd></div>
              <div><dt>Entra group object ID</dt><dd className={styles.code}>{data.entraBinding.groupObjectId}</dd></div>
              <div><dt>Group verification</dt><dd>{data.entraBinding.verificationState} · {data.entraBinding.provenance}</dd></div>
            </>}
            <div><dt>Resource access definitions</dt><dd>{data.resourceAccess?.length ?? 0} exact offerings · Saving intent is not gateway apply</dd></div>
            <div><dt>Resource limits</dt><dd>{data.limits.length} configured</dd></div>
          </dl>
          {budget.isPending && <Loading label="Loading budget" />}
          {budget.isError && <ErrorState error={budget.error} />}
          {budget.isSuccess && (budget.data ? <BudgetMeter budget={budget.data} label={data.name} /> : <Text className={styles.muted}>No budget set</Text>)}
          {costCenterSetupState(data) === 'setupRequired' && <MessageBar intent="warning"><MessageBarBody>
            This center is saved but not usable for custom-center access until its exclusive Entra group is verified.
            {data.entraBinding?.verificationError ? ` ${data.entraBinding.verificationError}` : ''}
          </MessageBarBody></MessageBar>}
          {data.setupState !== 'exempt' && <Button onClick={() => recheck.mutate()} disabled={recheck.isPending}>Recheck individual grant and key eligibility</Button>}
          {data.pendingRechecks?.map((check) => check.verificationError && <Text key={check.id}>{check.verificationError}</Text>)}
          {data.setupState !== 'exempt' && (
            <>
              {deleteCostCenter.isError && (
                <MessageBar intent="error"><MessageBarBody>{errorMessage(deleteCostCenter.error, 'Unable to delete this cost center.')}</MessageBarBody></MessageBar>
              )}
              <Button className={styles.dangerButton} appearance="subtle" onClick={() => setDeleteOpen(true)}>Delete cost center</Button>
            </>
          )}
        </div>
        {data.setupState !== 'exempt' && <CostCenterDirectoryOperations costCenterId={data.id} />}
      </div>}

      {section === 'access' && <div className={styles.stack}>
        {data.setupState === 'exempt' ? <Text>General retains legacy resource availability and credential rules; it is exempt by stable ID.</Text> : <>
          <div className={styles.rowActions}><Button appearance="primary" onClick={() => workbench.open('edit', embedded ? data.id : undefined)}>Edit resource access</Button></div>
          <CostCenterResourceAccessFields value={data.resourceAccess ?? []} costCenter={data} />
        </>}
      </div>}

      {section === 'members' && <CostCenterDirectoryMembers costCenter={data} onSetup={() => workbench.open('edit', embedded ? data.id : undefined)} />}

      {section === 'budget' && <div className={styles.card}>
        <div className={styles.cardHeader}>
          <Title3 as="h2">Budget</Title3>
          <Text className={styles.muted}>
            A monthly amount in US dollars for every grant under this cost center, across every gateway, priced as the
            Cost tab prices it. MOSAIC emails at each threshold, once a month, and can block calls at 100%.
          </Text>
        </div>
        {budget.isError && <ErrorState error={budget.error} />}
        {budget.isPending && <Loading label="Loading budget" />}
        {budget.isSuccess && (
          <div className={styles.budgetLayout}>
            <div className={styles.budgetStatus}>
              {budget.data ? (
                <>
                  <BudgetMeter budget={budget.data} label={data.name} />
                  <BudgetDetails budget={budget.data} />
                </>
              ) : (
                <EmptyState title="No budget">
                  Set a monthly amount to email this cost center&apos;s owners at 80% and 100% of it. Choose to block
                  its calls at 100% if spending past it must stop.
                </EmptyState>
              )}
            </div>
            <BudgetForm
              budget={budget.data}
              owners={data.owners}
              saving={saveBudget.isPending}
              removing={removeBudget.isPending}
              error={
                saveBudget.isError
                  ? errorMessage(saveBudget.error, 'Unable to save the budget.')
                  : removeBudget.isError
                    ? errorMessage(removeBudget.error, 'Unable to remove the budget.')
                    : null
              }
              onSave={(payload) => saveBudget.mutate(payload)}
              onRemove={() => removeBudget.mutate()}
            />
          </div>
        )}
      </div>}

      {section === 'limits' && <div className={styles.stack}>
        {section === 'limits' && <div className={styles.card} data-workspace-editor>
          <Title3 as="h2">Limits</Title3>
          <Text className={styles.muted}>
            Per-person limits apply to each grant under this cost center that sets none of its own. A pooled quota is shared by every grant under the cost center on that model, per gateway. MCP servers use calls only.
          </Text>
          {saveLimits.isError && (
            <MessageBar intent="error"><MessageBarBody>{errorMessage(saveLimits.error, 'Unable to save limits.')}</MessageBarBody></MessageBar>
          )}
          <div className={styles.limitList}>
            {limits.length === 0 ? <Text className={styles.muted}>No cost-center defaults are set.</Text> : limits.map((limit) => {
              const key = limitKey(limit.resource)
              return (
                <div key={key} className={styles.limitRow}>
                  <Text>{limitText(limit, labels.get(key) ?? limit.resource.id)}</Text>
                  <Button appearance="subtle" onClick={() => setLimits((current) => current.filter((item) => limitKey(item.resource) !== key))}>Remove</Button>
                </div>
              )
            })}
          </div>
          <form className={styles.form} onSubmit={addLimit}>
            {limitValidation && (
              <MessageBar intent="error">
                <MessageBarBody>{limitValidation}</MessageBarBody>
              </MessageBar>
            )}
            <Field label="Resource">
              <Select
                aria-label="Resource"
                value={limitDraft.resource}
                onChange={(_, selectData) => {
                  const nextResource = resources.find((item) => item.key === selectData.value)
                  setLimitValidation(null)
                  setLimitDraft(nextResource?.kind === 'mcpServer'
                    ? { ...limitDraft, resource: selectData.value, tokensPerMinute: '', tokenQuota: '', monthlyTokens: '' }
                    : { ...limitDraft, resource: selectData.value })
                }}
              >
                <option value="">Select a model API, pool model, or MCP server</option>
                {resources.map((resource) => <option key={resource.key} value={resource.key}>{resource.label}</option>)}
              </Select>
            </Field>
            <div className={styles.formGrid}>
              {!limitResourceIsMcp && (
                <>
                  <Field label="Tokens per minute"><Input aria-label="Tokens per minute" type="number" min={1} value={limitDraft.tokensPerMinute} onChange={(_, inputData) => setLimitDraft({ ...limitDraft, tokensPerMinute: inputData.value })} /></Field>
                  <Field label="Token quota"><Input type="number" min={1} value={limitDraft.tokenQuota} onChange={(_, inputData) => setLimitDraft({ ...limitDraft, tokenQuota: inputData.value })} /></Field>
                  <Field label="Token quota period"><Select value={limitDraft.tokenQuotaPeriod} onChange={(_, selectData) => setLimitDraft({ ...limitDraft, tokenQuotaPeriod: selectData.value as QuotaPeriod })}>{PERIODS.map((period) => <option key={period} value={period}>{period}</option>)}</Select></Field>
                </>
              )}
              <Field label="Calls per minute"><Input type="number" min={1} value={limitDraft.callsPerMinute} onChange={(_, inputData) => setLimitDraft({ ...limitDraft, callsPerMinute: inputData.value })} /></Field>
              <Field label="Call quota"><Input type="number" min={1} value={limitDraft.callQuota} onChange={(_, inputData) => setLimitDraft({ ...limitDraft, callQuota: inputData.value })} /></Field>
              <Field label="Call quota period"><Select value={limitDraft.callQuotaPeriod} onChange={(_, selectData) => setLimitDraft({ ...limitDraft, callQuotaPeriod: selectData.value as QuotaPeriod })}>{PERIODS.map((period) => <option key={period} value={period}>{period}</option>)}</Select></Field>
              {!limitResourceIsMcp && <Field label="Monthly pooled tokens"><Input type="number" min={1} value={limitDraft.monthlyTokens} onChange={(_, inputData) => setLimitDraft({ ...limitDraft, monthlyTokens: inputData.value })} /></Field>}
              <Field label="Monthly pooled calls"><Input type="number" min={1} value={limitDraft.monthlyCalls} onChange={(_, inputData) => setLimitDraft({ ...limitDraft, monthlyCalls: inputData.value })} /></Field>
            </div>
            <div className={styles.rowActions}>
              <Button type="submit" disabled={!limitDraft.resource}>Add limit</Button>
              <Button appearance="primary" type="button" onClick={() => saveLimits.mutate()} disabled={saveLimits.isPending}>Save limits</Button>
            </div>
          </form>
        </div>}
      </div>}
      </SectionLayout>

      <Dialog open={deleteOpen} onOpenChange={(_, dialogData) => !dialogData.open && setDeleteOpen(false)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Delete cost center</DialogTitle>
            <DialogContent>
              <Text>Delete <strong>{data.name}</strong>? This is refused while it is the tenant default, a principal default, or has grants. Deleting a MOSAIC record never deletes its Entra group, and does not remove previously applied gateway access.</Text>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setDeleteOpen(false)}>Cancel</Button>
              <Button appearance="primary" onClick={() => deleteCostCenter.mutate()} disabled={deleteCostCenter.isPending}>Delete</Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </section>}
    </WorkspaceLeaveGuard>
  )
}
