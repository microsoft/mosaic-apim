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
import { EntitlementAccessState } from '../components/EntitlementAccessState'
import { EntitlementConnectionDialog } from '../components/EntitlementConnectionDialog'
import { ModelAccessSettingsPanel } from '../components/ModelAccessSettingsPanel'
import { PublishModelDialog } from '../components/PublishModelDialog'
import { DEFAULT_COUNTER_KEY, GOVERNED_COUNTER_KEY, QUOTA_PERIODS, callRateError, describeLimits } from '../entitlement-limits'
import { runtimeConfig } from '../runtime-config'
import type {
  Entitlement,
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementResourceKind,
  EntitlementSubject,
  EntitlementSubjectKind,
  QuotaPeriod,
  Publication,
  PublishPlan,
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

function buildEnforcement(form: GrantForm, governed: boolean): EntitlementEnforcement | null {
  const tokensPerMinute = Number(form.tokensPerMinute) || undefined
  const tokenQuota = Number(form.tokenQuota) || undefined
  const calls = Number(form.calls) || undefined
  const renewalPeriodSeconds = Number(form.renewalPeriodSeconds) || undefined
  const counterKeyExpression = governed ? GOVERNED_COUNTER_KEY : DEFAULT_COUNTER_KEY

  const enforcement: EntitlementEnforcement = {}
  if (tokensPerMinute || tokenQuota) {
    enforcement.tokens = {
      counterKeyExpression,
      estimatePromptTokens: true,
      ...(tokensPerMinute ? { tokensPerMinute } : {}),
      ...(tokenQuota ? { tokenQuota, tokenQuotaPeriod: form.tokenQuotaPeriod } : {}),
    }
  }
  if (calls && renewalPeriodSeconds) {
    enforcement.requests = {
      counterKeyExpression,
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
  const [selectedPublicationId, setSelectedPublicationId] = useState('')
  const [directModelId, setDirectModelId] = useState<string | null>(null)
  const [connectionGrantId, setConnectionGrantId] = useState<string | null>(null)
  const [review, setReview] = useState<{ publication: Publication; plan: PublishPlan } | null>(null)

  const entitlements = useQuery({
    queryKey: ['entitlements'],
    queryFn: () => api.listEntitlements(),
  })
  const principals = useQuery({ queryKey: ['principals'], queryFn: () => api.listPrincipals() })
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api.listGroups() })
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: () => api.listModelApis() })
  const mcpServers = useQuery({ queryKey: ['mcp-servers'], queryFn: () => api.listMcpServers() })
  const publications = useQuery({ queryKey: ['publications'], queryFn: () => api.listPublications() })
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
      setDirectModelId(null)
      await queryClient.invalidateQueries({ queryKey: ['publications'] })
      setBanner('Saved grant intent. API Management is unchanged; review and apply the model plan to activate a supported direct grant.')
    },
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.updateEntitlement(id, { enabled }),
    onSuccess: async (_, variables) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: ['publications'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlement-connection'] }),
      ])
      setBanner(variables.enabled
        ? 'Saved enabled intent. Runtime access changes only after a supported model plan is reviewed and applied.'
        : 'Saved disabled intent. Managed revocation is pending until the model plan is reviewed, applied, and propagated; API Management is unchanged by this save.')
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (id: string) => api.deleteEntitlement(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['entitlements'] })
      setBanner('Removed the desired-state grant. API Management is unchanged.')
    },
  })

  const decideMutation = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      approve ? api.approveAccessRequest(id) : api.denyAccessRequest(id),
    onSuccess: async (_, variables) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['access-requests'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: ['publications'] }),
      ])
      setBanner(variables.approve
        ? 'Approved the request and saved grant intent. This does not activate APIM access; supported model changes still need review and apply.'
        : 'Denied the access request.')
    },
  })

  const reviewMutation = useMutation({
    mutationFn: async (publicationId: string) => {
      const plan = await api.createPublishPlan(publicationId)
      const publication = await api.getPublication(publicationId)
      if (!plan.accessSnapshot) throw new Error('The service did not return a governed-access snapshot. No model access changes can be applied from this review.')
      return { publication, plan }
    },
    onSuccess: (result) => setReview(result),
  })

  const publishedModels = (publications.data ?? []).filter(
    (publication) => publication.status === 'published' || Boolean(publication.lastAppliedAt),
  )
  const selectedPublication = publishedModels.find((publication) => publication.id === selectedPublicationId)

  function publicationFor(entitlement: Pick<Entitlement, 'resource'>) {
    if (entitlement.resource.kind !== 'modelApi') return undefined
    const modelApi = modelApis.data?.find((model) => model.id === entitlement.resource.id)
    return publications.data?.find(
      (publication) => publication.modelApiId === entitlement.resource.id || publication.id === modelApi?.publicationId,
    )
  }

  function managedGrant(entitlement: Pick<Entitlement, 'resource' | 'subject' | 'runtime'>) {
    const publication = publicationFor(entitlement)
    return entitlement.resource.kind === 'modelApi' && entitlement.subject.kind !== 'group'
      && Boolean(entitlement.runtime || publication?.governedAccess || publication?.appliedAccess)
  }

  function addDirectGrant(modelApiId: string) {
    setDirectModelId(modelApiId)
    setForm({ ...emptyForm, resource: modelApiId })
    createMutation.reset()
    setDialogOpen(true)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    const subject = subjectOptions.find((option) => option.id === form.subject)
    const resource = resourceOptions.find((option) => option.id === form.resource)
    if (!subject || !resource || callRateError(form) || (directModelId && subject.kind === 'group')) {
      return
    }
    const target = {
      subject: { kind: subject.kind, id: subject.id },
      resource: { kind: resource.kind, id: resource.id },
    }
    createMutation.mutate({
      ...target,
      enforcement: buildEnforcement(form, managedGrant(target)),
      notes: form.notes.trim() || null,
    })
  }

  const rows = entitlements.data ?? []
  const unbound = rows.filter((item) => !item.binding).length
  const rateError = callRateError(form)
  const connectionGrant = rows.find((entitlement) => entitlement.id === connectionGrantId)

  return (
    <section className={styles.page}>
      <PageHeader
        title="Entitlements"
        description="Save desired grants and limits, then review and explicitly apply model-wide changes for MOSAIC-published models. Saving alone never changes API Management. Group, MCP, and imported-only grants remain desired state."
        source="live"
        actions={
          <Button
            appearance="primary"
            icon={<AddRegular />}
            disabled={subjectOptions.length === 0 || resourceOptions.length === 0}
            onClick={() => {
              setDirectModelId(null)
              setForm(emptyForm)
              createMutation.reset()
              setDialogOpen(true)
            }}
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
      {decideMutation.isError && <ErrorState error={decideMutation.error} />}
      {principals.isError && <ErrorState error={principals.error} />}
      {groups.isError && <ErrorState error={groups.error} />}
      {modelApis.isError && <ErrorState error={modelApis.error} />}
      {mcpServers.isError && <ErrorState error={mcpServers.error} />}
      {runtimeConfig.authMode === 'local' && (
        <MessageBar intent="warning">
          <MessageBarBody>Local development mode: service responses may be simulated and do not verify live APIM enforcement.</MessageBarBody>
        </MessageBar>
      )}

      <Card className={styles.detailCard}>
        <Title3 as="h2">Governed model access</Title3>
        <Text>Choose a successfully published MOSAIC model. Direct users and applications are supported; groups, MCP servers, and imported-only APIs are not orchestrated.</Text>
        {publications.isPending && <Loading label="Loading published models..." />}
        {publications.isError && <ErrorState error={publications.error} />}
        <Field label="Published model">
          <Select
            value={selectedPublicationId}
            onChange={(_, data) => {
              setSelectedPublicationId(data.value)
              reviewMutation.reset()
              setConnectionGrantId(null)
            }}
          >
            <option value="">Select a published model</option>
            {publishedModels.map((publication) => (
              <option key={publication.id} value={publication.id}>
                {publication.displayName} · {publication.deploymentName} · {publication.gatewayId}
              </option>
            ))}
          </Select>
        </Field>
        {publications.isSuccess && publishedModels.length === 0 && <Text>No successfully published models are available. Publish a model before configuring runtime grants.</Text>}
        {selectedPublication && (
          <ModelAccessSettingsPanel
            key={`${selectedPublication.id}:${JSON.stringify(selectedPublication.governedAccess)}`}
            publication={selectedPublication}
            reviewing={reviewMutation.isPending}
            onReview={() => reviewMutation.mutate(selectedPublication.id)}
            onAddGrant={addDirectGrant}
            onMessage={setBanner}
          />
        )}
        {reviewMutation.isError && <ErrorState error={reviewMutation.error} />}
      </Card>

      <div className={styles.summaryGrid}>
        <Card className={styles.summaryCard}>
          <Text size={200}>Grants</Text>
          <strong>{rows.length}</strong>
        </Card>
        <Card className={styles.summaryCard}>
          <Text size={200}>Desired enabled</Text>
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
              Desired intent and last recorded runtime state are separate. Model-wide review above
              includes every saved change for that model. Applied metadata is not a live invocation test.
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
                    <TableHeaderCell>Desired / applied</TableHeaderCell>
                    <TableHeaderCell>Binding</TableHeaderCell>
                    <TableHeaderCell>Actions</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((entitlement) => {
                    const relatedPublication = publicationFor(entitlement)
                    const appliedGrant = relatedPublication?.appliedAccess?.grants.find(
                      (grant) => grant.entitlementId === entitlement.id,
                    )
                    const managed = managedGrant(entitlement)
                    return (
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
                          <Text size={200} weight="semibold">Desired limits</Text>
                          {describeLimits(entitlement, relatedPublication?.enforcement).map((sentence) => (
                            <Text key={sentence} className={styles.secondaryCell}>
                              {sentence}
                            </Text>
                          ))}
                          {appliedGrant && (
                            <>
                              <Text size={200} weight="semibold">Last applied limits {appliedGrant.enabled ? '' : '(grant disabled)'}</Text>
                              {describeLimits(appliedGrant, relatedPublication?.appliedAccess?.publicationEnforcement).map((sentence) => (
                                <Text key={`applied:${sentence}`} className={styles.secondaryCell}>{sentence}</Text>
                              ))}
                            </>
                          )}
                        </div>
                      </TableCell>
                      <TableCell><EntitlementAccessState entitlement={entitlement} /></TableCell>
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
                          {!managed && <Switch
                            checked={entitlement.enabled}
                            label={entitlement.enabled ? 'Enabled' : 'Disabled'}
                            disabled={toggleMutation.isPending}
                            onChange={(_, data) =>
                              toggleMutation.mutate({
                                id: entitlement.id,
                                enabled: data.checked,
                              })
                            }
                          />}
                          {managed && !entitlement.enabled && (
                            <Button disabled={toggleMutation.isPending} onClick={() => toggleMutation.mutate({ id: entitlement.id, enabled: true })}>
                              Re-enable
                            </Button>
                          )}
                          <Button
                            appearance="subtle"
                            disabled={revokeMutation.isPending || toggleMutation.isPending || (managed && !entitlement.enabled)}
                            onClick={() => {
                              setConnectionGrantId(null)
                              if (managed) toggleMutation.mutate({ id: entitlement.id, enabled: false })
                              else revokeMutation.mutate(entitlement.id)
                            }}
                          >
                            Revoke
                          </Button>
                          {managed && relatedPublication && (
                            <Button appearance="subtle" onClick={() => setSelectedPublicationId(relatedPublication.id)}>Manage model</Button>
                          )}
                          {managed && entitlement.runtime && (
                            <Button onClick={() => setConnectionGrantId(entitlement.id)}>Connection info</Button>
                          )}
                        </div>
                      </TableCell>
                      </TableRow>
                    )
                  })}
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
            <Title3 as="h2">Resolved desired access</Title3>
          </div>
          <Text size={200}>
            Recorded direct and group intent for a principal. This is not proof of active gateway
            access; group grants remain desired state only.
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
                {directModelId && <Text>Direct model grant: saving does not activate access. Review and apply the complete model plan afterward.</Text>}
                <Field label="Subject" required>
                  <Select
                    value={form.subject}
                    onChange={(_, data) => setForm({ ...form, subject: data.value })}
                  >
                    <option value="">Select a user, group, or application</option>
                    {subjectOptions.filter((option) => !directModelId || option.kind !== 'group').map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Resource" required>
                  <Select
                    value={form.resource}
                    disabled={Boolean(directModelId)}
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
                  Leave a limit empty to add no grant-specific restriction. Inherited publication
                  safeguards still apply; this does not mean unrestricted gateway access.
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
                  <Field label="Calls" validationMessage={rateError ?? undefined}>
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
                  disabled={
                    !form.subject ||
                    !form.resource ||
                    Boolean(rateError) ||
                    createMutation.isPending
                  }
                >
                  Grant access
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>
      <PublishModelDialog
        open={review !== null}
        initialReview={review}
        onClose={() => setReview(null)}
        onPublished={setBanner}
      />
      {connectionGrant && (
        <EntitlementConnectionDialog
          open
          entitlement={connectionGrant}
          onClose={() => setConnectionGrantId(null)}
        />
      )}
    </section>
  )
}
