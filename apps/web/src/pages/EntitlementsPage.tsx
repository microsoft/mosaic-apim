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
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  Select,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { Loading } from '../components/AsyncState'
import { PageHeader, PreviewNotice } from '../components/PageHeader'
import type { Group, TokenEnforcement } from '../types'
import styles from './EntitlementsPage.module.css'

type QuotaPeriod = NonNullable<TokenEnforcement['tokenQuotaPeriod']>
type DeploymentStatus = 'ready' | 'syncing' | 'attention'

interface PreviewModelDeployment {
  id: string
  deploymentName: string
  modelName: string
  provider: string
  endpoint: string
  status: DeploymentStatus
}

interface LocalEntitlement {
  id: string
  groupId: string
  modelDeploymentId: string
  enabled: boolean
  enforcement: TokenEnforcement
  createdAt: string
  updatedAt: string
}

interface EntitlementDraft {
  id: string | null
  groupId: string
  modelDeploymentId: string
  enabled: boolean
  counterKeyExpression: string
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: QuotaPeriod | ''
  estimatePromptTokens: boolean
}

interface DraftValidationResult {
  enforcement: TokenEnforcement | null
  errors: string[]
}

interface BannerState {
  intent: 'success' | 'warning' | 'error'
  message: string
}

const quotaPeriods = ['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly'] as const

const previewDeployments: PreviewModelDeployment[] = [
  {
    id: 'dep-gpt41-prod',
    deploymentName: 'gpt-4.1-prod',
    modelName: 'gpt-4.1',
    provider: 'Azure AI Foundry',
    endpoint:
      'https://mosaic-eastus.services.ai.azure.com/openai/deployments/gpt-4.1-prod/chat/completions',
    status: 'ready',
  },
  {
    id: 'dep-embed-3-large',
    deploymentName: 'text-embedding-3-large',
    modelName: 'text-embedding-3-large',
    provider: 'Azure AI Foundry',
    endpoint:
      'https://mosaic-eastus.services.ai.azure.com/openai/deployments/text-embedding-3-large/embeddings',
    status: 'syncing',
  },
  {
    id: 'dep-gpt4o-mini',
    deploymentName: 'gpt-4o-mini',
    modelName: 'gpt-4o-mini',
    provider: 'Azure OpenAI',
    endpoint:
      'https://mosaic-weu.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions',
    status: 'attention',
  },
] satisfies PreviewModelDeployment[]

function createInitialEntitlements(groups: Group[]): LocalEntitlement[] {
  return groups.slice(0, 2).map((group, index) => {
    const isPrimary = index === 0
    return {
      id: `ent-local-seed-${index + 1}`,
      groupId: group.id,
      modelDeploymentId: isPrimary ? 'dep-gpt41-prod' : 'dep-embed-3-large',
      enabled: isPrimary,
      enforcement: isPrimary
        ? {
            counterKeyExpression: '@(context.Subscription?.Id ?? "group")',
            tokensPerMinute: 180000,
            estimatePromptTokens: true,
          }
        : {
            counterKeyExpression: '@(context.Subscription?.Name ?? "group-preview")',
            tokenQuota: 1500000,
            tokenQuotaPeriod: 'Daily',
            estimatePromptTokens: false,
          },
      createdAt: '2026-08-11T08:00:00Z',
      updatedAt: '2026-08-12T15:40:00Z',
    }
  })
}

function isQuotaPeriod(value: string): value is QuotaPeriod {
  return quotaPeriods.some((period) => period === value)
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function createEmptyDraft(groups: Group[]): EntitlementDraft {
  return {
    id: null,
    groupId: groups[0]?.id ?? '',
    modelDeploymentId: previewDeployments[0]?.id ?? '',
    enabled: true,
    counterKeyExpression: '@(context.Subscription?.Id ?? "group")',
    tokensPerMinute: '',
    tokenQuota: '',
    tokenQuotaPeriod: '',
    estimatePromptTokens: true,
  }
}

function createDraftFromEntitlement(
  entitlement: LocalEntitlement,
): EntitlementDraft {
  return {
    id: entitlement.id,
    groupId: entitlement.groupId,
    modelDeploymentId: entitlement.modelDeploymentId,
    enabled: entitlement.enabled,
    counterKeyExpression: entitlement.enforcement.counterKeyExpression,
    tokensPerMinute: entitlement.enforcement.tokensPerMinute?.toString() ?? '',
    tokenQuota: entitlement.enforcement.tokenQuota?.toString() ?? '',
    tokenQuotaPeriod: entitlement.enforcement.tokenQuotaPeriod ?? '',
    estimatePromptTokens: entitlement.enforcement.estimatePromptTokens,
  }
}

function parsePositiveInteger(value: string, label: string, errors: string[]): number | undefined {
  const trimmed = value.trim()
  if (!trimmed) {
    return undefined
  }

  const numericValue = Number(trimmed)
  if (!Number.isInteger(numericValue) || numericValue <= 0) {
    errors.push(`${label} must be a positive whole number.`)
    return undefined
  }
  return numericValue
}

function validateDraft(
  draft: EntitlementDraft,
  existingEntitlements: LocalEntitlement[],
): DraftValidationResult {
  const errors: string[] = []
  const counterKeyExpression = draft.counterKeyExpression.trim()
  if (!counterKeyExpression) {
    errors.push('Counter key is required.')
  }

  if (!draft.groupId) {
    errors.push('Choose a MOSAIC group.')
  }
  if (!draft.modelDeploymentId) {
    errors.push('Choose a model deployment.')
  }

  const tokensPerMinute = parsePositiveInteger(
    draft.tokensPerMinute,
    'Tokens per minute',
    errors,
  )
  const tokenQuota = parsePositiveInteger(draft.tokenQuota, 'Quota', errors)
  const tokenQuotaPeriod = draft.tokenQuotaPeriod || undefined

  if (tokensPerMinute === undefined && tokenQuota === undefined) {
    errors.push('At least one rate limit or quota must be configured.')
  }
  if (tokenQuota !== undefined && tokenQuotaPeriod === undefined) {
    errors.push('Quota period is required when a quota is configured.')
  }
  if (tokenQuota === undefined && tokenQuotaPeriod !== undefined) {
    errors.push('Quota is required when a quota period is configured.')
  }

  const duplicate = existingEntitlements.find(
    (entitlement) =>
      entitlement.id !== draft.id &&
      entitlement.groupId === draft.groupId &&
      entitlement.modelDeploymentId === draft.modelDeploymentId,
  )
  if (duplicate) {
    errors.push('A local preview grant already exists for this group and deployment.')
  }

  if (errors.length > 0) {
    return { enforcement: null, errors }
  }

  return {
    enforcement: {
      counterKeyExpression,
      tokensPerMinute,
      tokenQuota,
      tokenQuotaPeriod,
      estimatePromptTokens: draft.estimatePromptTokens,
    },
    errors,
  }
}

function statusClass(status: DeploymentStatus): string {
  switch (status) {
    case 'ready':
      return styles.statusReady
    case 'syncing':
      return styles.statusSyncing
    case 'attention':
      return styles.statusAttention
  }
}

function statusLabel(status: DeploymentStatus): string {
  switch (status) {
    case 'ready':
      return 'Ready'
    case 'syncing':
      return 'Preview syncing'
    case 'attention':
      return 'Needs attention'
  }
}

export function EntitlementsPage() {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: api.listGroups })

  const hasSeededEntitlements = useRef(false)
  const [entitlements, setEntitlements] = useState<LocalEntitlement[]>([])
  const [dialogOpen, setDialogOpen] = useState(false)
  const [draft, setDraft] = useState<EntitlementDraft>(createEmptyDraft([]))
  const [validationErrors, setValidationErrors] = useState<string[]>([])
  const [selectedEntitlementId, setSelectedEntitlementId] = useState('')
  const [banner, setBanner] = useState<BannerState | null>(null)

  const groups = useMemo(() => groupsQuery.data ?? [], [groupsQuery.data])

  useEffect(() => {
    if (!groups.length) {
      return
    }
    if (!hasSeededEntitlements.current) {
      hasSeededEntitlements.current = true
      const seededEntitlements = createInitialEntitlements(groups)
      setEntitlements(seededEntitlements)
      setSelectedEntitlementId(seededEntitlements[0]?.id ?? '')
    } else {
      setEntitlements((current) =>
        current.filter((entitlement) =>
          groups.some((group) => group.id === entitlement.groupId),
        ),
      )
    }
    setDraft((current) => ({
      ...current,
      groupId:
        current.groupId && groups.some((group) => group.id === current.groupId)
          ? current.groupId
          : groups[0].id,
      modelDeploymentId:
        current.modelDeploymentId &&
        previewDeployments.some((deployment) => deployment.id === current.modelDeploymentId)
          ? current.modelDeploymentId
          : previewDeployments[0]?.id ?? '',
    }))
  }, [groups])

  useEffect(() => {
    if (!entitlements.some((entitlement) => entitlement.id === selectedEntitlementId)) {
      setSelectedEntitlementId(entitlements[0]?.id ?? '')
    }
  }, [entitlements, selectedEntitlementId])

  const selectedEntitlement = useMemo(
    () => entitlements.find((entitlement) => entitlement.id === selectedEntitlementId) ?? null,
    [entitlements, selectedEntitlementId],
  )

  const enabledCount = entitlements.filter((entitlement) => entitlement.enabled).length
  const quotaGuardedCount = entitlements.filter(
    (entitlement) => entitlement.enforcement.tokenQuota !== undefined,
  ).length

  function openCreateDialog() {
    setDraft(createEmptyDraft(groups))
    setValidationErrors([])
    setDialogOpen(true)
  }

  function openEditDialog(entitlement: LocalEntitlement) {
    setDraft(createDraftFromEntitlement(entitlement))
    setValidationErrors([])
    setSelectedEntitlementId(entitlement.id)
    setDialogOpen(true)
  }

  function closeDialog() {
    setDialogOpen(false)
    setValidationErrors([])
  }

  function submitDialog(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const validation = validateDraft(draft, entitlements)
    if (!validation.enforcement) {
      setValidationErrors(validation.errors)
      return
    }

    const now = new Date().toISOString()
    const nextEntitlement: LocalEntitlement = {
      id: draft.id ?? `ent-local-${Date.now()}`,
      groupId: draft.groupId,
      modelDeploymentId: draft.modelDeploymentId,
      enabled: draft.enabled,
      enforcement: validation.enforcement,
      createdAt:
        entitlements.find((entitlement) => entitlement.id === draft.id)?.createdAt ?? now,
      updatedAt: now,
    }

    setEntitlements((current) => {
      const existingIndex = current.findIndex((entitlement) => entitlement.id === nextEntitlement.id)
      if (existingIndex === -1) {
        return [nextEntitlement, ...current]
      }
      return current.map((entitlement) =>
        entitlement.id === nextEntitlement.id ? nextEntitlement : entitlement,
      )
    })
    setSelectedEntitlementId(nextEntitlement.id)
    setDialogOpen(false)
    setValidationErrors([])
    setBanner({
      intent: 'success',
      message:
        draft.id === null
          ? 'Added a browser-only entitlement preview. No backend grant or APIM policy changed.'
          : 'Updated the browser-only entitlement preview. Runtime enforcement is unchanged.',
    })
  }

  function removeEntitlement(entitlementId: string) {
    setEntitlements((current) => current.filter((entitlement) => entitlement.id !== entitlementId))
    setBanner({
      intent: 'warning',
      message: 'Removed the local preview grant. MOSAIC did not delete a backend entitlement.',
    })
  }

  function toggleEntitlement(entitlementId: string, enabled: boolean) {
    setEntitlements((current) =>
      current.map((entitlement) =>
        entitlement.id === entitlementId
          ? {
              ...entitlement,
              enabled,
              updatedAt: new Date().toISOString(),
            }
          : entitlement,
      ),
    )
    setBanner({
      intent: 'success',
      message: enabled
        ? 'Enabled the local preview grant. Enforcement was not applied to Azure yet.'
        : 'Disabled the local preview grant in the browser only.',
    })
  }

  return (
    <section className={styles.page}>
      <PageHeader
        title="Entitlements"
        description="Combine live MOSAIC groups with local deployment grants and token enforcement previews."
        source="local"
        actions={
          <Button
            appearance="primary"
            disabled={groupsQuery.isLoading || Boolean(groupsQuery.error) || groups.length === 0}
            onClick={openCreateDialog}
          >
            Add entitlement
          </Button>
        }
      />

      <PreviewNotice kind="local">
        MOSAIC groups below are loaded live. Entitlement grants, toggles, and token enforcement on
        this page remain browser-only until backend persistence and enforcement apply flows are
        connected.
      </PreviewNotice>

      {banner && (
        <MessageBar intent={banner.intent}>
          <MessageBarBody>{banner.message}</MessageBarBody>
        </MessageBar>
      )}

      {groupsQuery.isLoading ? (
        <Loading label="Loading MOSAIC groups" />
      ) : groupsQuery.error ? (
        <MessageBar intent="error">
          <MessageBarBody>{groupsQuery.error.message}</MessageBarBody>
        </MessageBar>
      ) : (
        <>
          <div className={styles.summaryGrid}>
            <Card className={styles.summaryCard}>
              <Text>Live groups</Text>
              <strong>{groups.length}</strong>
              <Text>Fetched from MOSAIC now.</Text>
            </Card>
            <Card className={styles.summaryCard}>
              <Text>Preview grants</Text>
              <strong>{entitlements.length}</strong>
              <Text>Stored in this browser session only.</Text>
            </Card>
            <Card className={styles.summaryCard}>
              <Text>Enabled now</Text>
              <strong>{enabledCount}</strong>
              <Text>Local toggle state, not runtime enforcement.</Text>
            </Card>
            <Card className={styles.summaryCard}>
              <Text>Quota guarded</Text>
              <strong>{quotaGuardedCount}</strong>
              <Text>Grants with a paired token quota and period.</Text>
            </Card>
          </div>

          {groups.length === 0 ? (
            <Card className={styles.emptyCard}>
              <Title3 as="h2">No MOSAIC groups are available</Title3>
              <Text>
                Entitlements need live groups first. Go to Identity to create or import the groups
                you want to grant.
              </Text>
              <div>
                <Button
                  appearance="primary"
                  onClick={() => navigate('/identity?tab=groups')}
                >
                  Open Identity
                </Button>
              </div>
            </Card>
          ) : (
            <div className={styles.contentGrid}>
              <Card className={styles.tableCard}>
                <div className={styles.tableHeader}>
                  <div>
                    <Title3 as="h2">Group-to-deployment grants</Title3>
                    <Text>Live groups with local entitlement and TokenEnforcement preview state.</Text>
                  </div>
                </div>
                <div className={styles.tableWrap}>
                  <Table aria-label="Entitlement preview grants">
                    <TableHeader>
                      <TableRow>
                        <TableHeaderCell>Group</TableHeaderCell>
                        <TableHeaderCell>Deployment</TableHeaderCell>
                        <TableHeaderCell>Rate / quota</TableHeaderCell>
                        <TableHeaderCell>Counter key</TableHeaderCell>
                        <TableHeaderCell>Enabled</TableHeaderCell>
                        <TableHeaderCell>Updated</TableHeaderCell>
                        <TableHeaderCell>
                          <span className={styles.srOnly}>Actions</span>
                        </TableHeaderCell>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {entitlements.map((entitlement) => {
                        const group = groups.find((item) => item.id === entitlement.groupId)
                        const deployment = previewDeployments.find(
                          (item) => item.id === entitlement.modelDeploymentId,
                        )
                        const isSelected = entitlement.id === selectedEntitlementId
                        return (
                          <TableRow
                            key={entitlement.id}
                            aria-selected={isSelected}
                            className={isSelected ? styles.selectedRow : undefined}
                          >
                            <TableCell>
                              <button
                                className={styles.rowButton}
                                type="button"
                                onClick={() => setSelectedEntitlementId(entitlement.id)}
                              >
                                <span className={styles.primaryCell}>{group?.name ?? entitlement.groupId}</span>
                                <span className={styles.secondaryCell}>
                                  {group?.description || 'Live group description unavailable.'}
                                </span>
                              </button>
                            </TableCell>
                            <TableCell>
                              <div className={styles.cellStack}>
                                <span>{deployment?.deploymentName ?? entitlement.modelDeploymentId}</span>
                                <span className={styles.secondaryCell}>{deployment?.modelName ?? 'Unknown model'}</span>
                                {deployment && (
                                  <Badge appearance="filled" className={statusClass(deployment.status)}>
                                    {statusLabel(deployment.status)}
                                  </Badge>
                                )}
                              </div>
                            </TableCell>
                            <TableCell>
                              <div className={styles.cellStack}>
                                <span>
                                  {entitlement.enforcement.tokensPerMinute
                                    ? `${entitlement.enforcement.tokensPerMinute.toLocaleString()} TPM`
                                    : 'No TPM limit'}
                                </span>
                                <span className={styles.secondaryCell}>
                                  {entitlement.enforcement.tokenQuota &&
                                  entitlement.enforcement.tokenQuotaPeriod
                                    ? `${entitlement.enforcement.tokenQuota.toLocaleString()} / ${entitlement.enforcement.tokenQuotaPeriod}`
                                    : 'No quota'}
                                </span>
                              </div>
                            </TableCell>
                            <TableCell>
                              <code className={styles.codeValue}>
                                {entitlement.enforcement.counterKeyExpression}
                              </code>
                            </TableCell>
                            <TableCell>
                              <Switch
                                aria-label={`Toggle entitlement for ${group?.name ?? entitlement.groupId}`}
                                checked={entitlement.enabled}
                                label={entitlement.enabled ? 'Enabled' : 'Disabled'}
                                onChange={(_, data) => toggleEntitlement(entitlement.id, data.checked)}
                              />
                            </TableCell>
                            <TableCell>{formatTimestamp(entitlement.updatedAt)}</TableCell>
                            <TableCell>
                              <div className={styles.rowActions}>
                                <Button appearance="subtle" onClick={() => openEditDialog(entitlement)}>
                                  Edit
                                </Button>
                                <Button appearance="subtle" onClick={() => removeEntitlement(entitlement.id)}>
                                  Remove
                                </Button>
                              </div>
                            </TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                  {entitlements.length === 0 && (
                    <div className={styles.emptyState}>
                      <Title3 as="h3">No preview entitlements yet</Title3>
                      <Text>Create a browser-only grant for a live MOSAIC group.</Text>
                    </div>
                  )}
                </div>
              </Card>

              {selectedEntitlement && (
                <Card className={styles.detailCard}>
                  <div className={styles.detailHeader}>
                    <div>
                      <Title3 as="h2">
                        {groups.find((group) => group.id === selectedEntitlement.groupId)?.name ??
                          selectedEntitlement.groupId}
                      </Title3>
                      <Text>
                        {previewDeployments.find(
                          (deployment) => deployment.id === selectedEntitlement.modelDeploymentId,
                        )?.deploymentName ?? selectedEntitlement.modelDeploymentId}
                      </Text>
                    </div>
                    <Badge appearance="filled" className={selectedEntitlement.enabled ? styles.statusReady : styles.statusMuted}>
                      {selectedEntitlement.enabled ? 'Enabled locally' : 'Disabled locally'}
                    </Badge>
                  </div>
                  <dl className={styles.detailList}>
                    <div>
                      <dt>Counter key</dt>
                      <dd className={styles.codeValue}>
                        {selectedEntitlement.enforcement.counterKeyExpression}
                      </dd>
                    </div>
                    <div>
                      <dt>Tokens per minute</dt>
                      <dd>
                        {selectedEntitlement.enforcement.tokensPerMinute?.toLocaleString() ?? 'Not set'}
                      </dd>
                    </div>
                    <div>
                      <dt>Quota</dt>
                      <dd>
                        {selectedEntitlement.enforcement.tokenQuota &&
                        selectedEntitlement.enforcement.tokenQuotaPeriod
                          ? `${selectedEntitlement.enforcement.tokenQuota.toLocaleString()} / ${selectedEntitlement.enforcement.tokenQuotaPeriod}`
                          : 'Not set'}
                      </dd>
                    </div>
                    <div>
                      <dt>Estimate prompt tokens</dt>
                      <dd>
                        {selectedEntitlement.enforcement.estimatePromptTokens ? 'Enabled' : 'Disabled'}
                      </dd>
                    </div>
                  </dl>
                  <div className={styles.noteCard}>
                    <Text>
                      This entitlement preview does not create backend grants, APIM products, or
                      runtime enforcement yet.
                    </Text>
                  </div>
                  <div className={styles.rowActions}>
                    <Button appearance="primary" onClick={() => openEditDialog(selectedEntitlement)}>
                      Edit preview
                    </Button>
                    <Button
                      appearance="secondary"
                      onClick={() =>
                        toggleEntitlement(selectedEntitlement.id, !selectedEntitlement.enabled)
                      }
                    >
                      {selectedEntitlement.enabled ? 'Disable locally' : 'Enable locally'}
                    </Button>
                  </div>
                </Card>
              )}
            </div>
          )}
        </>
      )}

      <Dialog
        open={dialogOpen}
        onOpenChange={(_, data) => {
          if (!data.open) {
            closeDialog()
          }
        }}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>{draft.id ? 'Edit entitlement' : 'Add entitlement'}</DialogTitle>
            <DialogContent>
              <form className={styles.dialogForm} onSubmit={submitDialog}>
                <Field label="MOSAIC group" required>
                  <Select
                    aria-label="Choose a MOSAIC group"
                    value={draft.groupId}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        groupId: event.target.value,
                      }))
                    }
                  >
                    {groups.map((group) => (
                      <option key={group.id} value={group.id}>
                        {group.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Model deployment" required>
                  <Select
                    aria-label="Choose a model deployment"
                    value={draft.modelDeploymentId}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        modelDeploymentId: event.target.value,
                      }))
                    }
                  >
                    {previewDeployments.map((deployment) => (
                      <option key={deployment.id} value={deployment.id}>
                        {deployment.deploymentName} — {deployment.modelName}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Counter key expression" required>
                  <Input
                    aria-label="Counter key expression"
                    value={draft.counterKeyExpression}
                    onChange={(_, data) =>
                      setDraft((current) => ({
                        ...current,
                        counterKeyExpression: data.value,
                      }))
                    }
                  />
                </Field>
                <div className={styles.dialogGrid}>
                  <Field label="Tokens per minute">
                    <Input
                      aria-label="Tokens per minute"
                      inputMode="numeric"
                      value={draft.tokensPerMinute}
                      onChange={(_, data) =>
                        setDraft((current) => ({
                          ...current,
                          tokensPerMinute: data.value,
                        }))
                      }
                    />
                  </Field>
                  <Field label="Quota">
                    <Input
                      aria-label="Token quota"
                      inputMode="numeric"
                      value={draft.tokenQuota}
                      onChange={(_, data) =>
                        setDraft((current) => ({
                          ...current,
                          tokenQuota: data.value,
                        }))
                      }
                    />
                  </Field>
                  <Field label="Quota period">
                    <Select
                      aria-label="Token quota period"
                      value={draft.tokenQuotaPeriod}
                      onChange={(event) => {
                        const nextValue = event.target.value
                        setDraft((current) => ({
                          ...current,
                          tokenQuotaPeriod:
                            nextValue === '' || isQuotaPeriod(nextValue) ? nextValue : '',
                        }))
                      }}
                    >
                      <option value="">None</option>
                      {quotaPeriods.map((period) => (
                        <option key={period} value={period}>
                          {period}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </div>
                <div className={styles.switchGrid}>
                  <Switch
                    aria-label="Toggle entitlement enabled state"
                    checked={draft.enabled}
                    label={draft.enabled ? 'Enabled locally' : 'Disabled locally'}
                    onChange={(_, data) =>
                      setDraft((current) => ({
                        ...current,
                        enabled: data.checked,
                      }))
                    }
                  />
                  <Switch
                    aria-label="Estimate prompt tokens"
                    checked={draft.estimatePromptTokens}
                    label={
                      draft.estimatePromptTokens
                        ? 'Estimate prompt tokens enabled'
                        : 'Estimate prompt tokens disabled'
                    }
                    onChange={(_, data) =>
                      setDraft((current) => ({
                        ...current,
                        estimatePromptTokens: data.checked,
                      }))
                    }
                  />
                </div>
                {validationErrors.length > 0 && (
                  <MessageBar intent="error">
                    <MessageBarBody>
                      <ul className={styles.errorList}>
                        {validationErrors.map((error) => (
                          <li key={error}>{error}</li>
                        ))}
                      </ul>
                    </MessageBarBody>
                  </MessageBar>
                )}
                <DialogActions>
                  <Button appearance="secondary" type="button" onClick={closeDialog}>
                    Cancel
                  </Button>
                  <Button appearance="primary" type="submit">
                    Save local preview
                  </Button>
                </DialogActions>
              </form>
            </DialogContent>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </section>
  )
}
