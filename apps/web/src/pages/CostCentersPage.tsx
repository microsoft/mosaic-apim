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
  Switch,
  Text,
  Textarea,
  Title3,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { BUDGET_LEVEL_LABELS, budgetBadgeColor } from '../budgets'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { BudgetDetails, BudgetForm, BudgetMeter } from '../components/BudgetEditor'
import { PageHeader } from '../components/PageHeader'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { BudgetUpdate, BudgetView, CostCenter, CostCenterLimit, QuotaPeriod } from '../types'
import styles from './CostCentersPage.module.css'

const CODE_PATTERN = /^[A-Za-z0-9._-]{1,64}$/
const PERIODS: QuotaPeriod[] = ['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly']

interface CostCenterForm {
  name: string
  code: string
  description: string
  owners: string
  keysAllowed: boolean
}

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

const emptyForm: CostCenterForm = {
  name: '',
  code: '',
  description: '',
  owners: '',
  keysAllowed: true,
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

function owners(value: string) {
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function costCenterPayload(form: CostCenterForm) {
  return {
    name: form.name.trim(),
    code: form.code.trim(),
    description: form.description.trim() || null,
    owners: owners(form.owners),
    keysAllowed: form.keysAllowed,
  }
}

function ownerText(costCenter: Pick<CostCenter, 'owners'>) {
  return costCenter.owners.length ? costCenter.owners.join(', ') : 'No owners'
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
  return `${which} may no longer be chargeable here, and MOSAIC couldn't finish checking them because their models were busy. Applies leave them out until it does.`
}

function CostCenterBadges({ costCenter, showKeys = true }: { costCenter: CostCenter; showKeys?: boolean }) {
  return (
    <span className={styles.badges}>
      {costCenter.isTenantDefault && <Badge appearance="tint" color="brand">Tenant default</Badge>}
      {costCenter.builtIn && <Badge appearance="tint">Built in</Badge>}
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

function CostCenterTable({ costCenters, budgets }: { costCenters: CostCenter[]; budgets: Map<string, BudgetView> }) {
  return (
    <div className={styles.tableWrap}>
      <table aria-label="Cost centers">
        <thead>
          <tr>
            <th>Name</th>
            <th>Code</th>
            <th>Members</th>
            <th>Grants</th>
            <th>Keys allowed</th>
            <th>Owners</th>
          </tr>
        </thead>
        <tbody>
          {costCenters.map((costCenter) => {
            const budget = budgets.get(costCenter.id)
            return (
              <tr key={costCenter.id}>
                <td>
                  <div className={styles.nameCell}>
                    <RouterLink to={`/cost-centers/${costCenter.id}`}>{costCenter.name}</RouterLink>
                    <span className={styles.badges}>
                      <CostCenterBadges costCenter={costCenter} showKeys={false} />
                      {budget && budget.status.level !== 'ok' && (
                        <Badge appearance="tint" color={budgetBadgeColor(budget.status.level)}>
                          {BUDGET_LEVEL_LABELS[budget.status.level]}
                        </Badge>
                      )}
                    </span>
                  </div>
                </td>
                <td><code className={styles.code}>{costCenter.code}</code></td>
                <td>{costCenter.isTenantDefault ? 'Everyone' : costCenter.members.length}</td>
                <td>{costCenter.enabledGrantCount} / {costCenter.grantCount}</td>
                <td>{costCenter.keysAllowed ? 'Yes' : 'No'}</td>
                <td>{ownerText(costCenter)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function CostCentersPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<CostCenterForm>(emptyForm)
  const [defaultCostCenterId, setDefaultCostCenterId] = useState('')
  const costCenters = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters })
  const settings = useQuery({ queryKey: ['cost-center-settings'], queryFn: api.getCostCenterSettings })
  const budgets = useQuery({ queryKey: ['budgets'], queryFn: api.getBudgets })
  const codeInvalid = Boolean(form.code) && !CODE_PATTERN.test(form.code)
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

  const create = useMutation({
    mutationFn: () => api.createCostCenter(costCenterPayload(form)),
    onSuccess: async () => {
      setForm(emptyForm)
      await queryClient.invalidateQueries({ queryKey: ['cost-centers'] })
    },
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

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!form.name.trim() || !form.code.trim() || codeInvalid) return
    create.mutate()
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Cost centers"
        description="Charge grants and keys to named cost centers across gateways."
        source="live"
      />
      {(costCenters.isPending || settings.isPending) && <Loading label="Loading cost centers" />}
      {(costCenters.isError || settings.isError) && <ErrorState error={costCenters.error ?? settings.error} />}
      {costCenters.data && settings.data && (
        <div className={styles.grid}>
          <div className={styles.stack}>
            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <Title3 as="h2">Cost centers</Title3>
                <Text className={styles.muted}>
                  Every grant, and so every call, is charged to one. Everyone may charge the tenant
                  default; others only their members.
                </Text>
              </div>
              {costCenters.data.length ? (
                <CostCenterTable costCenters={costCenters.data} budgets={budgetsByCostCenter} />
              ) : (
                <EmptyState title="No cost centers">
                  Create a cost center to charge grants separately.
                </EmptyState>
              )}
            </div>

            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <Title3 as="h2">Organization budget</Title3>
                <Text className={styles.muted}>
                  Every priced call this month, across every cost center and gateway, including calls MOSAIC couldn&apos;t
                  link to a grant and reserved capacity nobody called. It emails at each threshold and never blocks.
                </Text>
              </div>
              {budgets.isError && <ErrorState error={budgets.error} />}
              {budgets.data?.organization && (
                <BudgetMeter budget={budgets.data.organization} label="Organization" />
              )}
              {budgets.data && (
                <BudgetForm
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
                />
              )}
            </div>
          </div>

          <div className={styles.card}>
            <form className={styles.form} onSubmit={submit}>
              <Title3 as="h2">New cost center</Title3>
              {create.isError && (
                <MessageBar intent="error">
                  <MessageBarBody>
                    <MessageBarTitle>Unable to create cost center</MessageBarTitle>
                    {errorMessage(create.error, 'Review the fields and try again.')}
                  </MessageBarBody>
                </MessageBar>
              )}
              <Field label="Name" required>
                <Input aria-label="Name" value={form.name} onChange={(_, data) => setForm({ ...form, name: data.value })} />
              </Field>
              <Field
                label="Code"
                hint="Use 1 to 64 letters, numbers, dots, underscores, or hyphens."
                required
                validationMessage={codeInvalid ? 'Use only A-Z, a-z, 0-9, dot, underscore, or hyphen.' : undefined}
              >
                <Input aria-label="Code" value={form.code} onChange={(_, data) => setForm({ ...form, code: data.value })} />
              </Field>
              <Field label="Description">
                <Textarea aria-label="Description" value={form.description} onChange={(_, data) => setForm({ ...form, description: data.value })} />
              </Field>
              <Field label="Owners" hint="Enter email addresses separated by commas or new lines.">
                <Textarea aria-label="Owners" value={form.owners} onChange={(_, data) => setForm({ ...form, owners: data.value })} />
              </Field>
              <Switch
                checked={form.keysAllowed}
                label="Keys allowed"
                onChange={(_, data) => setForm({ ...form, keysAllowed: data.checked })}
              />
              <Button appearance="primary" icon={<AddRegular />} type="submit" disabled={create.isPending || codeInvalid}>
                Create cost center
              </Button>
            </form>

            <form className={styles.form} onSubmit={(event) => { event.preventDefault(); saveSettings.mutate() }}>
              <Title3 as="h2">Tenant default</Title3>
              {saveSettings.isError && (
                <MessageBar intent="error">
                  <MessageBarBody>{errorMessage(saveSettings.error, 'Unable to save the default cost center.')}</MessageBarBody>
                </MessageBar>
              )}
              <Text className={styles.muted}>
                New principals are charged here unless you pick another default when you onboard them. Everyone may charge it, so moving it revokes grants under the old default that their subjects may charge no other way.
              </Text>
              <Field label="Default cost center">
                <Select value={defaultCostCenterId} onChange={(_, data) => setDefaultCostCenterId(data.value)}>
                  {costCenters.data.map((costCenter) => (
                    <option key={costCenter.id} value={costCenter.id}>{costCenter.name} ({costCenter.code})</option>
                  ))}
                </Select>
              </Field>
              <Button appearance="primary" type="submit" disabled={!defaultCostCenterId || saveSettings.isPending}>
                Save default
              </Button>
            </form>
          </div>
        </div>
      )}
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
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { costCenterId = '' } = useParams()
  const [form, setForm] = useState<CostCenterForm>(emptyForm)
  const [memberId, setMemberId] = useState('')
  const [removeMember, setRemoveMember] = useState<{ id: string; label: string; securityGroup: boolean } | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [limitDraft, setLimitDraft] = useState<LimitDraft>(emptyLimitDraft)
  const [limitValidation, setLimitValidation] = useState<string | null>(null)
  const [limits, setLimits] = useState<CostCenterLimit[]>([])

  const costCenter = useQuery({ queryKey: ['cost-centers', costCenterId], queryFn: () => api.getCostCenter(costCenterId), enabled: Boolean(costCenterId) })
  const budget = useQuery({
    queryKey: ['budgets', costCenterId],
    queryFn: () => api.getCostCenterBudget(costCenterId),
    enabled: Boolean(costCenterId),
  })
  const principals = useQuery({ queryKey: ['principals'], queryFn: api.listPrincipals })
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
    setForm({
      name: data.name,
      code: data.code,
      description: data.description ?? '',
      owners: data.owners.join(', '),
      keysAllowed: data.keysAllowed,
    })
    setLimits(data.limits)
  }, [data])

  const update = useMutation({
    mutationFn: () => api.updateCostCenter(costCenterId, costCenterPayload(form)),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['cost-centers'] }),
        queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenterId] }),
      ])
    },
  })
  const addMember = useMutation({
    mutationFn: () => api.addCostCenterMember(costCenterId, memberId),
    onSuccess: async () => {
      setMemberId('')
      await queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenterId] })
    },
  })
  const remove = useMutation({
    mutationFn: (principalId: string) => api.removeCostCenterMember(costCenterId, principalId),
    onSuccess: async () => {
      setRemoveMember(null)
      await queryClient.invalidateQueries({ queryKey: ['cost-centers'] })
    },
  })
  const saveLimits = useMutation({
    mutationFn: () => api.updateCostCenterLimits(costCenterId, limits),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenterId] }),
  })
  const deleteCostCenter = useMutation({
    mutationFn: () => api.deleteCostCenter(costCenterId),
    onSuccess: () => navigate('/cost-centers'),
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
  if (costCenter.isError) return <ErrorState error={costCenter.error} />
  if (!data) return null

  const nonMembers = (principals.data ?? []).filter((principal) =>
    !data.memberDetails.some((member) => member.principalId === principal.id && member.explicit),
  )
  const codeInvalid = !CODE_PATTERN.test(form.code)
  const selectedLimitResource = resources.find((item) => item.key === limitDraft.resource)
  const limitResourceIsMcp = selectedLimitResource?.kind === 'mcpServer'

  return (
    <section className={styles.page}>
      <PageHeader
        title={data.name}
        description={`${data.code} · ${data.description ?? 'No description'}`}
        source="live"
        actions={
          <Button onClick={() => navigate(`/entitlements?costCenter=${encodeURIComponent(data.id)}`)}>
            View grants
          </Button>
        }
      />
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
      <div className={styles.grid}>
        <div className={styles.card}>
          <form className={styles.form} onSubmit={(event) => { event.preventDefault(); if (!codeInvalid) update.mutate() }}>
            <Title3 as="h2">Details</Title3>
            {update.isError && (
              <MessageBar intent="error"><MessageBarBody>{errorMessage(update.error, 'Unable to save the cost center.')}</MessageBarBody></MessageBar>
            )}
            <div className={styles.formGrid}>
              <Field label="Name" required><Input aria-label="Name" value={form.name} onChange={(_, data) => setForm({ ...form, name: data.value })} /></Field>
              <Field label="Code" hint="Changing the code marks grants pending until their models are applied." validationMessage={codeInvalid ? 'Use only A-Z, a-z, 0-9, dot, underscore, or hyphen.' : undefined} required>
                <Input aria-label="Code" value={form.code} onChange={(_, data) => setForm({ ...form, code: data.value })} />
              </Field>
            </div>
            <Field label="Description"><Textarea aria-label="Description" value={form.description} onChange={(_, data) => setForm({ ...form, description: data.value })} /></Field>
            <Field label="Owners" hint="Email addresses separated by commas or new lines."><Textarea aria-label="Owners" value={form.owners} onChange={(_, data) => setForm({ ...form, owners: data.value })} /></Field>
            <Switch
              checked={form.keysAllowed}
              label="Keys allowed"
              onChange={(_, switchData) => setForm({ ...form, keysAllowed: switchData.checked })}
            />
            <Text className={styles.muted}>
              Turning keys off suspends this cost center&apos;s keys on the next apply. Changing the code or this switch marks grants pending until their models are applied.
            </Text>
            <Button appearance="primary" type="submit" disabled={update.isPending || codeInvalid}>Save changes</Button>
          </form>
        </div>

        <div className={styles.card}>
          <Title3 as="h2">Summary</Title3>
          <dl className={styles.detailList}>
            <div><dt>Status</dt><dd><CostCenterBadges costCenter={data} /></dd></div>
            <div><dt>Grants</dt><dd>{data.enabledGrantCount} enabled of {data.grantCount}</dd></div>
            <div><dt>Owners</dt><dd>{ownerText(data)}</dd></div>
            <div><dt>Defaults</dt><dd>{data.defaultFor} principal{data.defaultFor === 1 ? '' : 's'}</dd></div>
          </dl>
          {!data.builtIn && (
            <>
              {deleteCostCenter.isError && (
                <MessageBar intent="error"><MessageBarBody>{errorMessage(deleteCostCenter.error, 'Unable to delete this cost center.')}</MessageBarBody></MessageBar>
              )}
              <Button className={styles.dangerButton} appearance="subtle" onClick={() => setDeleteOpen(true)}>Delete cost center</Button>
            </>
          )}
        </div>
      </div>

      <div className={styles.card}>
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
      </div>

      <div className={styles.stack}>
        <div className={styles.card}>
          <Title3 as="h2">Members</Title3>
          <Text className={styles.muted}>Removing a member revokes their grants under this cost center, unless they may still charge it another way, such as through a listed security group. The next apply deletes their keys. Removing a security group revokes the grants that relied on it.</Text>
          <form className={styles.addMember} onSubmit={(event) => { event.preventDefault(); if (memberId) addMember.mutate() }}>
            <Field label="Add member">
              <Select aria-label="Add member" value={memberId} onChange={(_, selectData) => setMemberId(selectData.value)}>
                <option value="">Select a MOSAIC principal</option>
                {nonMembers.map((principal) => (
                  <option key={principal.id} value={principal.id}>{principal.label ?? principal.objectId} — {PRINCIPAL_KIND_LABELS[principal.kind]}</option>
                ))}
              </Select>
            </Field>
            <Button appearance="primary" type="submit" disabled={!memberId || addMember.isPending}>Add member</Button>
          </form>
          {(addMember.isError || remove.isError) && (
            <MessageBar intent="error"><MessageBarBody>{errorMessage(addMember.error ?? remove.error, 'Unable to update members.')}</MessageBarBody></MessageBar>
          )}
          <div className={styles.tableWrap}>
            <table aria-label="Cost center members">
              <thead><tr><th>Member</th><th>Kind</th><th>Object ID</th><th>Membership</th><th>Actions</th></tr></thead>
              <tbody>
                {data.memberDetails.length === 0 ? (
                  <tr><td colSpan={5}><EmptyState title="No members">Add a principal or security group to let it charge this cost center.</EmptyState></td></tr>
                ) : data.memberDetails.map((member) => (
                  <tr key={`${member.principalId}:${member.explicit}`}>
                    <td>
                      <span className={styles.primary}>{member.label ?? member.principalId}</span>
                      {member.isDefault && <div><Badge appearance="tint" color="brand">Default</Badge></div>}
                    </td>
                    <td>{member.kind ? PRINCIPAL_KIND_LABELS[member.kind] : 'Unknown'}</td>
                    <td><code className={styles.code}>{member.objectId ?? '—'}</code></td>
                    <td>{member.explicit ? 'Listed' : 'Default only'}</td>
                    <td>
                      {member.explicit && (
                        <Button
                          appearance="subtle"
                          className={styles.dangerButton}
                          onClick={() => setRemoveMember({ id: member.principalId, label: member.label ?? member.principalId, securityGroup: member.kind === 'securityGroup' })}
                        >
                          Remove
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className={styles.card}>
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
        </div>
      </div>

      <Dialog open={removeMember !== null} onOpenChange={(_, dialogData) => !dialogData.open && setRemoveMember(null)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Remove member</DialogTitle>
            <DialogContent>
              <Text>
                Remove <strong>{removeMember?.label}</strong>? This revokes their grants under this cost center, unless they may still charge it another way, and the next apply deletes their keys.
                {removeMember?.securityGroup ? ' Grants people charged only through this group are revoked too.' : ''}
              </Text>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setRemoveMember(null)}>Cancel</Button>
              <Button appearance="primary" onClick={() => removeMember && remove.mutate(removeMember.id)}>Remove</Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={(_, dialogData) => !dialogData.open && setDeleteOpen(false)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Delete cost center</DialogTitle>
            <DialogContent>
              <Text>Delete <strong>{data.name}</strong>? This is refused while it is the tenant default, a principal default, or has grants.</Text>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setDeleteOpen(false)}>Cancel</Button>
              <Button appearance="primary" onClick={() => deleteCostCenter.mutate()} disabled={deleteCostCenter.isPending}>Delete</Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </section>
  )
}
