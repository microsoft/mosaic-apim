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
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useMemo, useState } from 'react'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { DEFAULT_COUNTER_KEY, QUOTA_PERIODS, describeLimits } from '../entitlement-limits'
import type {
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementResourceKind,
  EntitlementSubject,
  EntitlementSubjectKind,
  QuotaPeriod,
} from '../types'
import styles from './EntitlementsPage.module.css'

interface SubjectOption {
  id: string
  kind: EntitlementSubjectKind
  label: string
}

interface ResourceOption {
  id: string
  kind: EntitlementResourceKind
  label: string
}

interface GrantForm {
  subject: string
  resource: string
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: QuotaPeriod
  calls: string
  renewalPeriodSeconds: string
  notes: string
}

const emptyForm: GrantForm = {
  subject: '',
  resource: '',
  tokensPerMinute: '',
  tokenQuota: '',
  tokenQuotaPeriod: 'Monthly',
  calls: '',
  renewalPeriodSeconds: '60',
  notes: '',
}

function buildEnforcement(form: GrantForm): EntitlementEnforcement | null {
  const tokensPerMinute = Number(form.tokensPerMinute) || undefined
  const tokenQuota = Number(form.tokenQuota) || undefined
  const calls = Number(form.calls) || undefined
  const renewalPeriodSeconds = Number(form.renewalPeriodSeconds) || undefined

  const enforcement: EntitlementEnforcement = {}
  if (tokensPerMinute || tokenQuota) {
    enforcement.tokens = {
      counterKeyExpression: DEFAULT_COUNTER_KEY,
      estimatePromptTokens: true,
      ...(tokensPerMinute ? { tokensPerMinute } : {}),
      ...(tokenQuota ? { tokenQuota, tokenQuotaPeriod: form.tokenQuotaPeriod } : {}),
    }
  }
  if (calls && renewalPeriodSeconds) {
    enforcement.requests = {
      counterKeyExpression: DEFAULT_COUNTER_KEY,
      calls,
      renewalPeriodSeconds,
    }
  }
  return enforcement.tokens || enforcement.requests ? enforcement : null
}

export function EntitlementsPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [banner, setBanner] = useState<string | null>(null)
  const [inspectedPrincipal, setInspectedPrincipal] = useState('')
  const [form, setForm] = useState<GrantForm>(emptyForm)

  const entitlements = useQuery({
    queryKey: ['entitlements'],
    queryFn: () => api.listEntitlements(),
  })
  const principals = useQuery({ queryKey: ['principals'], queryFn: () => api.listPrincipals() })
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api.listGroups() })
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: () => api.listModelApis() })
  const mcpServers = useQuery({ queryKey: ['mcp-servers'], queryFn: () => api.listMcpServers() })
  const accessRequests = useQuery({
    queryKey: ['access-requests', 'pending'],
    queryFn: () => api.listAccessRequests('pending'),
  })

  const subjectOptions = useMemo<SubjectOption[]>(
    () => [
      ...(groups.data ?? []).map<SubjectOption>((group) => ({
        id: group.id,
        kind: 'group',
        label: `${group.name} (group)`,
      })),
      ...(principals.data ?? []).map<SubjectOption>((principal) => ({
        id: principal.id,
        kind: principal.kind === 'user' ? 'user' : 'application',
        label: `${principal.label ?? principal.objectId} (${
          principal.kind === 'user' ? 'user' : 'application'
        })`,
      })),
    ],
    [groups.data, principals.data],
  )

  const resourceOptions = useMemo<ResourceOption[]>(
    () => [
      ...(modelApis.data ?? []).map<ResourceOption>((item) => ({
        id: item.id,
        kind: 'modelApi',
        label: `${item.displayName} (model API)`,
      })),
      ...(mcpServers.data ?? []).map<ResourceOption>((item) => ({
        id: item.id,
        kind: 'mcpServer',
        label: `${item.displayName} (MCP server)`,
      })),
    ],
    [mcpServers.data, modelApis.data],
  )

  const labels = useMemo(() => {
    const map = new Map<string, string>()
    for (const option of [...subjectOptions, ...resourceOptions]) {
      map.set(option.id, option.label)
    }
    return map
  }, [resourceOptions, subjectOptions])

  const resolved = useQuery({
    queryKey: ['entitlements', 'resolve', inspectedPrincipal],
    queryFn: () => api.resolveEntitlements(inspectedPrincipal),
    enabled: Boolean(inspectedPrincipal),
  })

  const createMutation = useMutation({
    mutationFn: (payload: {
      subject: EntitlementSubject
      resource: EntitlementResource
      enforcement: EntitlementEnforcement | null
      notes: string | null
    }) => api.createEntitlement(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      setDialogOpen(false)
      setForm(emptyForm)
      setBanner('Granted access. MOSAIC recorded the grant; API Management is unchanged.')
    },
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.updateEntitlement(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
  })

  const revokeMutation = useMutation({
    mutationFn: (id: string) => api.deleteEntitlement(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      setBanner('Revoked that grant.')
    },
  })

  const decideMutation = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      approve ? api.approveAccessRequest(id) : api.denyAccessRequest(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['access-requests'] }),
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    const subject = subjectOptions.find((option) => option.id === form.subject)
    const resource = resourceOptions.find((option) => option.id === form.resource)
    if (!subject || !resource) {
      return
    }
    createMutation.mutate({
      subject: { kind: subject.kind, id: subject.id },
      resource: { kind: resource.kind, id: resource.id },
      enforcement: buildEnforcement(form),
      notes: form.notes.trim() || null,
    })
  }

  const rows = entitlements.data ?? []
  const unbound = rows.filter((item) => !item.binding).length

  return (
    <section className={styles.page}>
      <PageHeader
        title="Entitlements"
        description="Who may use a governed model API or MCP server, and the limits that apply. MOSAIC stores this intent; it does not write to API Management."
        source="live"
        actions={
          <Button
            appearance="primary"
            icon={<AddRegular />}
            disabled={subjectOptions.length === 0 || resourceOptions.length === 0}
            onClick={() => setDialogOpen(true)}
          >
            Add entitlement
          </Button>
        }
      />

      {banner && (
        <MessageBar intent="success">
          <MessageBarBody>{banner}</MessageBarBody>
        </MessageBar>
      )}
      {revokeMutation.isError && <ErrorState error={revokeMutation.error} />}
      {toggleMutation.isError && <ErrorState error={toggleMutation.error} />}

      <div className={styles.summaryGrid}>
        <Card className={styles.summaryCard}>
          <Text size={200}>Grants</Text>
          <strong>{rows.length}</strong>
        </Card>
        <Card className={styles.summaryCard}>
          <Text size={200}>Enabled</Text>
          <strong>{rows.filter((item) => item.enabled).length}</strong>
        </Card>
        <Card className={styles.summaryCard}>
          <Text size={200}>Without a binding</Text>
          <strong>{unbound}</strong>
          <Text size={200}>Consumption cannot be attributed until one is recorded.</Text>
        </Card>
        <Card className={styles.summaryCard}>
          <Text size={200}>Pending requests</Text>
          <strong>{accessRequests.data?.length ?? 0}</strong>
        </Card>
      </div>

      <Card className={styles.tableCard}>
        <div className={styles.tableHeader}>
          <div>
            <Title3 as="h2">Grants</Title3>
            <Text size={200}>
              A grant reaches a person directly or through a group. Limits are shown as sentences,
              never as policy markup.
            </Text>
          </div>
        </div>
        <div className={styles.tableWrap}>
          {entitlements.isPending && <Loading label="Loading entitlements..." />}
          {entitlements.isError && <ErrorState error={entitlements.error} />}
          {entitlements.isSuccess &&
            (rows.length === 0 ? (
              <EmptyState title="Nothing has been granted yet">
                Import a model API or MCP server, register the people or groups who need it, then
                grant access here.
              </EmptyState>
            ) : (
              <Table aria-label="Entitlements">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Subject</TableHeaderCell>
                    <TableHeaderCell>Resource</TableHeaderCell>
                    <TableHeaderCell>Limits</TableHeaderCell>
                    <TableHeaderCell>Binding</TableHeaderCell>
                    <TableHeaderCell>Actions</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((entitlement) => (
                    <TableRow key={entitlement.id}>
                      <TableCell>
                        <div className={styles.cellStack}>
                          <Text className={styles.primaryCell}>
                            {labels.get(entitlement.subject.id) ?? entitlement.subject.id}
                          </Text>
                          <Text className={styles.secondaryCell}>{entitlement.subject.kind}</Text>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className={styles.cellStack}>
                          <Text className={styles.primaryCell}>
                            {labels.get(entitlement.resource.id) ?? entitlement.resource.id}
                          </Text>
                          <Text className={styles.secondaryCell}>{entitlement.resource.kind}</Text>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className={styles.cellStack}>
                          {describeLimits(entitlement).map((sentence) => (
                            <Text key={sentence} className={styles.secondaryCell}>
                              {sentence}
                            </Text>
                          ))}
                        </div>
                      </TableCell>
                      <TableCell>
                        {entitlement.binding ? (
                          <Badge appearance="tint" className={styles.statusReady}>
                            {entitlement.binding.apimSubscriptionName ?? 'Recorded'} ·{' '}
                            {entitlement.binding.source}
                          </Badge>
                        ) : (
                          <Badge appearance="tint" className={styles.statusAttention}>
                            Not bound
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className={styles.rowActions}>
                          <Switch
                            checked={entitlement.enabled}
                            label={entitlement.enabled ? 'Enabled' : 'Disabled'}
                            disabled={toggleMutation.isPending}
                            onChange={(_, data) =>
                              toggleMutation.mutate({
                                id: entitlement.id,
                                enabled: data.checked,
                              })
                            }
                          />
                          <Button
                            appearance="subtle"
                            disabled={revokeMutation.isPending}
                            onClick={() => revokeMutation.mutate(entitlement.id)}
                          >
                            Revoke
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ))}
        </div>
      </Card>

      <div className={styles.contentGrid}>
        <Card className={styles.tableCard}>
          <div className={styles.tableHeader}>
            <div>
              <Title3 as="h2">Access requests</Title3>
              <Text size={200}>
                What portal users asked for. A request can be decided once; a decision is final.
              </Text>
            </div>
          </div>
          <div className={styles.tableWrap}>
            {accessRequests.isPending && <Loading label="Loading access requests..." />}
            {accessRequests.isError && <ErrorState error={accessRequests.error} />}
            {accessRequests.isSuccess &&
              (accessRequests.data.length === 0 ? (
                <EmptyState title="No pending requests">
                  Requests appear here when a portal user asks for a resource they can see but are
                  not entitled to.
                </EmptyState>
              ) : (
                <Table aria-label="Pending access requests">
                  <TableHeader>
                    <TableRow>
                      <TableHeaderCell>Requester</TableHeaderCell>
                      <TableHeaderCell>Resource</TableHeaderCell>
                      <TableHeaderCell>Justification</TableHeaderCell>
                      <TableHeaderCell>Actions</TableHeaderCell>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {accessRequests.data.map((accessRequest) => (
                      <TableRow key={accessRequest.id}>
                        <TableCell>
                          <Text className={styles.codeValue}>
                            {accessRequest.requesterObjectId}
                          </Text>
                        </TableCell>
                        <TableCell>
                          {labels.get(accessRequest.resource.id) ?? accessRequest.resource.id}
                        </TableCell>
                        <TableCell>
                          <Text className={styles.secondaryCell}>
                            {accessRequest.justification ?? 'No justification given'}
                          </Text>
                        </TableCell>
                        <TableCell>
                          <div className={styles.rowActions}>
                            <Button
                              appearance="primary"
                              disabled={decideMutation.isPending}
                              onClick={() =>
                                decideMutation.mutate({ id: accessRequest.id, approve: true })
                              }
                            >
                              Approve
                            </Button>
                            <Button
                              appearance="subtle"
                              disabled={decideMutation.isPending}
                              onClick={() =>
                                decideMutation.mutate({ id: accessRequest.id, approve: false })
                              }
                            >
                              Deny
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ))}
          </div>
        </Card>

        <Card className={styles.detailCard}>
          <div className={styles.detailHeader}>
            <Title3 as="h2">Effective access</Title3>
          </div>
          <Text size={200}>
            What one person can actually use, including everything a group grant contributes.
          </Text>
          <Field label="Principal">
            <Select
              value={inspectedPrincipal}
              onChange={(_, data) => setInspectedPrincipal(data.value)}
            >
              <option value="">Select a principal</option>
              {(principals.data ?? []).map((principal) => (
                <option key={principal.id} value={principal.id}>
                  {principal.label ?? principal.objectId}
                </option>
              ))}
            </Select>
          </Field>
          {resolved.isError && <ErrorState error={resolved.error} />}
          {resolved.isSuccess &&
            (resolved.data.length === 0 ? (
              <Text size={200}>Nothing has been granted to this principal.</Text>
            ) : (
              <dl className={styles.detailList}>
                {resolved.data.map((item) => (
                  <div key={item.entitlement.id}>
                    <dt>
                      {labels.get(item.entitlement.resource.id) ?? item.entitlement.resource.id}
                    </dt>
                    <dd>
                      {item.via === 'direct'
                        ? 'Granted directly'
                        : `Granted through ${item.viaGroupName ?? 'a group'}`}
                    </dd>
                  </div>
                ))}
              </dl>
            ))}
        </Card>
      </div>

      <Dialog open={dialogOpen} onOpenChange={(_, data) => setDialogOpen(data.open)}>
        <DialogSurface>
          <form onSubmit={submit}>
            <DialogBody>
              <DialogTitle>Add entitlement</DialogTitle>
              <DialogContent className={styles.dialogForm}>
                {createMutation.isError && <ErrorState error={createMutation.error} />}
                <Field label="Subject" required>
                  <Select
                    value={form.subject}
                    onChange={(_, data) => setForm({ ...form, subject: data.value })}
                  >
                    <option value="">Select a user, group, or application</option>
                    {subjectOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Resource" required>
                  <Select
                    value={form.resource}
                    onChange={(_, data) => setForm({ ...form, resource: data.value })}
                  >
                    <option value="">Select a model API or MCP server</option>
                    {resourceOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Text size={200}>
                  Leave every limit empty to grant unrestricted access. MOSAIC reports that
                  honestly rather than showing a limit of zero.
                </Text>
                <div className={styles.dialogGrid}>
                  <Field label="Tokens per minute">
                    <Input
                      type="number"
                      min={1}
                      value={form.tokensPerMinute}
                      onChange={(_, data) => setForm({ ...form, tokensPerMinute: data.value })}
                    />
                  </Field>
                  <Field label="Token quota">
                    <Input
                      type="number"
                      min={1}
                      value={form.tokenQuota}
                      onChange={(_, data) => setForm({ ...form, tokenQuota: data.value })}
                    />
                  </Field>
                  <Field label="Quota period">
                    <Select
                      value={form.tokenQuotaPeriod}
                      onChange={(_, data) =>
                        setForm({ ...form, tokenQuotaPeriod: data.value as QuotaPeriod })
                      }
                    >
                      {QUOTA_PERIODS.map((period) => (
                        <option key={period} value={period}>
                          {period}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </div>
                <div className={styles.switchGrid}>
                  <Field label="Calls">
                    <Input
                      type="number"
                      min={1}
                      value={form.calls}
                      onChange={(_, data) => setForm({ ...form, calls: data.value })}
                    />
                  </Field>
                  <Field label="Per how many seconds">
                    <Input
                      type="number"
                      min={1}
                      value={form.renewalPeriodSeconds}
                      onChange={(_, data) =>
                        setForm({ ...form, renewalPeriodSeconds: data.value })
                      }
                    />
                  </Field>
                </div>
                <Field label="Notes">
                  <Input
                    value={form.notes}
                    onChange={(_, data) => setForm({ ...form, notes: data.value })}
                  />
                </Field>
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" onClick={() => setDialogOpen(false)}>
                  Cancel
                </Button>
                <Button
                  appearance="primary"
                  type="submit"
                  disabled={!form.subject || !form.resource || createMutation.isPending}
                >
                  Grant access
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>
    </section>
  )
}
