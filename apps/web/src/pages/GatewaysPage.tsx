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
  MessageBarTitle,
  Select,
  Text,
  Title3,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { EnvironmentPicker } from '../components/EnvironmentPicker'
import { PageHeader } from '../components/PageHeader'
import { useEnvironmentCatalog } from '../environments'
import type { Gateway, GatewayStatus, GatewaySuggestion } from '../types'
import styles from './GatewaysPage.module.css'

const statusLabels: Record<GatewayStatus, string> = {
  pending: 'Not checked',
  connected: 'Connected',
  degraded: 'Partial data',
  unauthorized: 'Access needed',
  unreachable: 'Unreachable',
}

const statusClasses: Record<GatewayStatus, string> = {
  pending: styles.pendingBadge,
  connected: styles.connectedBadge,
  degraded: styles.degradedBadge,
  unauthorized: styles.attentionBadge,
  unreachable: styles.attentionBadge,
}

export function GatewayStatusBadge({ status }: { status: GatewayStatus }) {
  return (
    <Badge appearance="tint" className={statusClasses[status]}>
      {statusLabels[status]}
    </Badge>
  )
}

export function AccessPanel({ gateway }: { gateway: Gateway }) {
  const { access } = gateway
  const [copied, setCopied] = useState(false)

  async function copyCommand(command: string) {
    try {
      await navigator.clipboard?.writeText(command)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <Card className={styles.accessCard}>
      <MessageBar intent={access.canRead ? 'success' : 'error'}>
        <MessageBarBody>
          <MessageBarTitle>
            {access.canRead ? 'MOSAIC can read this gateway' : 'MOSAIC cannot read this gateway'}
          </MessageBarTitle>
          {access.message}
        </MessageBarBody>
      </MessageBar>
      <div className={styles.accessFacts}>
        <Text size={200}>
          Read access: <strong>{access.canRead ? 'granted' : 'missing'}</strong>
        </Text>
        <Text size={200}>
          Write access, needed for manage mode:{' '}
          <strong>{access.canWrite ? 'granted' : 'not granted'}</strong>
        </Text>
      </div>
      {access.remediation && (
        <div className={styles.remediation}>
          <Text block>
            Grant the <strong>{access.remediation.roleName}</strong> role on this API Management
            service. Someone with permission to assign roles must run:
          </Text>
          <pre className={styles.commandBlock}>{access.remediation.command}</pre>
          <Button
            appearance="secondary"
            onClick={() => void copyCommand(access.remediation!.command)}
          >
            {copied ? 'Copied' : 'Copy command'}
          </Button>
        </div>
      )}
    </Card>
  )
}

export function GatewaysPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [resourceId, setResourceId] = useState('')
  const [name, setName] = useState('')
  const [environment, setEnvironment] = useState<string | null>(null)
  const [environmentTouched, setEnvironmentTouched] = useState(false)
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [selectedSuggestion, setSelectedSuggestion] = useState<GatewaySuggestion | null>(null)
  const [lastRegistered, setLastRegistered] = useState<Gateway | null>(null)

  const gateways = useQuery({ queryKey: ['gateways'], queryFn: api.listGateways })
  const catalog = useEnvironmentCatalog()
  const suggestions = useQuery({
    queryKey: ['gateway-suggestions'],
    queryFn: api.listSuggestedGateways,
  })

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ['gateways'] })
    await queryClient.invalidateQueries({ queryKey: ['gateway-suggestions'] })
  }

  const register = useMutation({
    mutationFn: api.registerGateway,
    onSuccess: async (gateway) => {
      closeDialog()
      setLastRegistered(gateway)
      await refresh()
    },
  })

  const sync = useMutation({ mutationFn: api.syncGateway, onSuccess: refresh })

  const recheck = useMutation({
    mutationFn: api.preflightGateway,
    onSuccess: async (gateway) => {
      setLastRegistered(gateway)
      await refresh()
    },
  })

  const remove = useMutation({
    mutationFn: api.deleteGateway,
    onSuccess: async () => {
      setLastRegistered(null)
      await refresh()
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    setEnvironmentTouched(true)
    if (!environment) return
    register.mutate({
      azureResourceId: resourceId.trim(),
      name: name.trim() || undefined,
      environment,
    })
  }

  function suggestionEvidence(item: GatewaySuggestion) {
    return item.suggestedEnvironment && item.azureEnvironmentTag
      ? `Azure tag environment = "${item.azureEnvironmentTag}"`
      : undefined
  }

  function openDialog(prefill?: GatewaySuggestion) {
    register.reset()
    setSelectedSuggestion(prefill ?? null)
    setResourceId(prefill?.azureResourceId ?? '')
    setName(prefill?.serviceName ?? '')
    setEnvironment(prefill?.suggestedEnvironment ?? null)
    setEnvironmentTouched(false)
    setDialogOpen(true)
  }

  function closeDialog() {
    setDialogOpen(false)
    setResourceId('')
    setName('')
    setEnvironment(null)
    setEnvironmentTouched(false)
    setSelectedSuggestion(null)
    register.reset()
  }

  const pending = (suggestions.data ?? []).filter((item) => !item.alreadyRegistered)
  const filteredGateways = (gateways.data ?? []).filter((gateway) => {
    if (environmentFilter === 'all') return true
    if (environmentFilter === '__unclassified__') return gateway.environment == null
    return gateway.environment === environmentFilter
  })
  useEffect(() => {
    if (dialogOpen && !environment && !selectedSuggestion && catalog.data?.environments[0]) {
      setEnvironment(catalog.data.environments[0].key)
    }
  }, [dialogOpen, environment, selectedSuggestion, catalog.data])

  return (
    <div className={styles.page}>
      <PageHeader
        title="Gateways"
        description="Bring an existing Azure API Management service under MOSAIC. MOSAIC reads what is already there and describes it in plain language. It does not change your gateway unless an administrator switches it to manage mode and publishes a model."
        source="live"
        actions={
          <Button
            appearance="primary"
            icon={<AddRegular />}
            onClick={() => openDialog()}
          >
            Onboard gateway
          </Button>
        }
      />

      {pending.length > 0 && (
        <Card className={styles.suggestionCard}>
          <Title3 as="h2">Detected gateway</Title3>
          {pending.map((item) => (
            <div key={item.azureResourceId} className={styles.suggestionRow}>
              <div className={styles.suggestionText}>
                <Text weight="semibold">{item.serviceName}</Text>
                <Text size={200}>
                  {item.reason} Resource group {item.resourceGroup}.
                </Text>
              </div>
              <Button
                appearance="primary"
                onClick={() => openDialog(item)}
              >
                Onboard
              </Button>
            </div>
          ))}
        </Card>
      )}

      {!dialogOpen && register.isError && <ErrorState error={register.error} />}
      {lastRegistered && <AccessPanel gateway={lastRegistered} />}

      {gateways.isPending && <Loading label="Loading gateways" />}
      {gateways.isError && <ErrorState error={gateways.error} />}
      {gateways.data?.length === 0 && (
        <EmptyState title="No gateways yet">
          Onboard an API Management service to see its APIs, products, subscriptions, and policies.
        </EmptyState>
      )}

      {gateways.data && gateways.data.length > 0 && (
        <Card className={styles.listCard}>
          <div className={styles.cardHeader}>
            <div className={styles.cardHeaderText}>
              <Title3 as="h2">Registered gateways</Title3>
              <Text size={200} className={styles.muted}>
                {filteredGateways.length} of {gateways.data.length} gateway{gateways.data.length === 1 ? '' : 's'} under MOSAIC
                observation.
              </Text>
            </div>
            <Field label="Environment filter">
              <Select
                aria-label="Filter gateways by environment"
                value={environmentFilter}
                onChange={(event) => setEnvironmentFilter(event.target.value)}
              >
                <option value="all">All</option>
                {catalog.data?.environments.map((item) => (
                  <option key={item.key} value={item.key}>{item.displayName}</option>
                ))}
                <option value="__unclassified__">Unclassified</option>
              </Select>
            </Field>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Gateway</th>
                  <th>Environment</th>
                  <th>Status</th>
                  <th>AI APIs</th>
                  <th>Last synced</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredGateways.map((gateway) => (
                  <tr key={gateway.id}>
                    <td>
                      <div className={styles.gatewayName}>
                        <Link className={styles.gatewayLink} to={`/gateways/${gateway.id}`}>
                          {gateway.name}
                        </Link>
                        <Text size={200} className={styles.muted}>
                          {gateway.serviceName}
                          {gateway.environmentLabel ? ` · Legacy label: ${gateway.environmentLabel}` : ''}
                        </Text>
                      </div>
                    </td>
                    <td><EnvironmentBadge environment={gateway.environment} catalog={catalog.data} /></td>
                    <td>
                      <GatewayStatusBadge status={gateway.status} />
                    </td>
                    <td>
                      {gateway.inventory.aiApis} of {gateway.inventory.apis}
                    </td>
                    <td>
                      {gateway.lastSyncedAt
                        ? new Date(gateway.lastSyncedAt).toLocaleString()
                        : 'Never'}
                    </td>
                    <td>
                      <div className={styles.inlineActions}>
                        <Button
                          appearance="secondary"
                          onClick={() => recheck.mutate(gateway.id)}
                          disabled={recheck.isPending}
                        >
                          Check access
                        </Button>
                        <Button
                          appearance="secondary"
                          onClick={() => sync.mutate(gateway.id)}
                          disabled={sync.isPending || !gateway.access.canRead}
                        >
                          Sync
                        </Button>
                        <Button
                          appearance="subtle"
                          onClick={() => remove.mutate(gateway.id)}
                          disabled={remove.isPending}
                        >
                          Remove
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Text size={200} className={styles.footnote}>
            Removing a gateway deletes only what MOSAIC stored about it. The API Management service
            itself is never modified.
          </Text>
        </Card>
      )}

      <Dialog open={dialogOpen} onOpenChange={(_, data) => data.open ? setDialogOpen(true) : closeDialog()}>
        <DialogSurface>
          <form onSubmit={submit}>
            <DialogBody>
              <DialogTitle>Onboard a gateway</DialogTitle>
              <DialogContent className={styles.dialogForm}>
                <Text>
                  MOSAIC checks that its managed identity can read the service before registering
                  it. If access is missing, it will tell you exactly which role to grant.
                </Text>
                <Field
                  label="API Management resource ID"
                  required
                  hint="/subscriptions/{id}/resourceGroups/{group}/providers/Microsoft.ApiManagement/service/{name}"
                >
                  <Input value={resourceId} onChange={(_, data) => setResourceId(data.value)} />
                </Field>
                <Field label="Display name" hint="Defaults to the service name">
                  <Input value={name} onChange={(_, data) => setName(data.value)} />
                </Field>
                <EnvironmentPicker
                  catalog={catalog.data}
                  value={environment}
                  onChange={(value) => { setEnvironment(value); setEnvironmentTouched(true) }}
                  required
                  label="Environment"
                  suggestion={selectedSuggestion?.suggestedEnvironment && suggestionEvidence(selectedSuggestion)
                    ? { environment: selectedSuggestion.suggestedEnvironment, evidence: suggestionEvidence(selectedSuggestion)! }
                    : undefined}
                  validationMessage={environmentTouched && !environment ? 'Choose an environment.' : undefined}
                />
                {selectedSuggestion?.azureEnvironmentTag && !selectedSuggestion.suggestedEnvironment && (
                  <Text size={200} className={styles.muted}>Azure tag: {selectedSuggestion.azureEnvironmentTag}</Text>
                )}
                {register.isError && <ErrorState error={register.error} />}
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" type="button" onClick={closeDialog}>
                  Cancel
                </Button>
                <Button appearance="primary" type="submit" disabled={register.isPending}>
                  {register.isPending ? 'Checking access…' : 'Check access and onboard'}
                </Button>
              </DialogActions>
            </DialogBody>
          </form>
        </DialogSurface>
      </Dialog>
    </div>
  )
}
