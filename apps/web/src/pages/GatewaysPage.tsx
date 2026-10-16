import { Badge, Button, Card, Field, MessageBar, MessageBarBody, MessageBarTitle, Select, Text } from '@fluentui/react-components'
import { AddRegular, CloudRegular, LightbulbRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { AzureConnectionPicker, type ConnectionSelection } from '../components/AzureConnectionPicker'
import { GatewayRegistrationEditor } from '../components/GatewayEditors'
import { PageHeader } from '../components/PageHeader'
import { ResourceWorkbench } from '../components/ResourceWorkbench'
import { SectionLayout, usePageSection } from '../components/SectionLayout'
import { useWorkbenchLocation } from '../components/useWorkbenchLocation'
import { useEnvironmentCatalog } from '../environments'
import { MANAGEMENT_MODE_LABELS } from '../labels'
import type { Gateway, GatewayStatus } from '../types'
import { GatewayDetailPage } from './GatewayDetailPage'
import styles from './GatewaysPage.module.css'

const statusLabels: Record<GatewayStatus, string> = {
  pending: 'Not checked', connected: 'Connected', degraded: 'Partial data',
  unauthorized: 'Access needed', unreachable: 'Unreachable',
}
const statusClasses: Record<GatewayStatus, string> = {
  pending: styles.pendingBadge, connected: styles.connectedBadge, degraded: styles.degradedBadge,
  unauthorized: styles.attentionBadge, unreachable: styles.attentionBadge,
}
export function GatewayStatusBadge({ status }: { status: GatewayStatus }) {
  return <Badge appearance="tint" className={statusClasses[status]}>{statusLabels[status]}</Badge>
}

export function AccessPanel({ gateway }: { gateway: Gateway }) {
  const { access } = gateway
  const [copied, setCopied] = useState(false)
  async function copyCommand(command: string) {
    try { await navigator.clipboard?.writeText(command); setCopied(true) } catch { setCopied(false) }
  }
  return <Card className={styles.accessCard}>
    <MessageBar intent={access.canRead ? 'success' : 'error'}><MessageBarBody>
      <MessageBarTitle>{access.canRead ? 'MOSAIC can read this gateway' : 'MOSAIC cannot read this gateway'}</MessageBarTitle>
      {access.message}
    </MessageBarBody></MessageBar>
    <div className={styles.accessFacts}>
      <Text size={200}>Read access: <strong>{access.canRead ? 'granted' : 'missing'}</strong></Text>
      <Text size={200}>Write access, needed for manage mode: <strong>{access.canWrite ? 'granted' : 'not granted'}</strong></Text>
    </div>
    {access.remediation && <div className={styles.remediation}>
      <Text block>Grant the <strong>{access.remediation.roleName}</strong> role on this API Management service.
        Someone with permission to assign roles must run:</Text>
      <pre className={styles.commandBlock}>{access.remediation.command}</pre>
      <Button onClick={() => void copyCommand(access.remediation!.command)}>{copied ? 'Copied' : 'Copy command'}</Button>
    </div>}
  </Card>
}

const gatewaySections = [
  { value: 'registered', label: 'Registered gateways', icon: <CloudRegular /> },
  { value: 'suggestions', label: 'Detected gateways', icon: <LightbulbRegular /> },
]

export function GatewaysPage() {
  const api = useMosaicApi()
  const catalog = useEnvironmentCatalog()
  const [section, selectSection] = usePageSection(gatewaySections)
  const [params, setParams] = useSearchParams()
  const workbench = useWorkbenchLocation({ section })
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: api.listGateways })
  const [connection, setConnection] = useState<ConnectionSelection>({ azureCloud: 'commercial', azureConnectionId: null })
  const suggestions = useQuery({ queryKey: ['gateway-suggestions', connection], queryFn: () => api.listSuggestedGateways(connection) })
  const environmentFilter = params.get('environment') ?? 'all'
  const pending = (suggestions.data ?? []).filter((item) => !item.alreadyRegistered)
  const suggestion = pending.find((item) => item.azureResourceId === workbench.selectedId)
  const filteredGateways = (gateways.data ?? []).filter((gateway) =>
    environmentFilter === 'all' || (environmentFilter === '__unclassified__'
      ? gateway.environment == null : gateway.environment === environmentFilter))

  function saved(gateway: Gateway) {
    setParams((current) => {
      const next = new URLSearchParams(current)
      next.set('tab', 'registered')
      next.set('selected', gateway.id)
      next.delete('mode')
      next.delete('workbench')
      if (environmentFilter !== 'all' && gateway.environment !== environmentFilter) next.delete('environment')
      return next
    }, { replace: true, preventScrollReset: true })
  }
  const filter = <Field label="Environment filter">
    <Select aria-label="Filter gateways by environment" value={environmentFilter} onChange={(event) => {
      const next = new URLSearchParams(params)
      next.set('environment', event.target.value)
      setParams(next, { preventScrollReset: true })
    }}>
      <option value="all">All</option>
      {catalog.data?.environments.map((item) => <option key={item.key} value={item.key}>{item.displayName}</option>)}
      <option value="__unclassified__">Unclassified</option>
    </Select>
  </Field>
  let body
  if (workbench.mode === 'new') {
    body = section === 'suggestions' && workbench.selectedId && !suggestion
      ? suggestions.isPending ? <Loading label="Loading detected gateway" /> : <><Button onClick={() => workbench.close()}>Back to detected gateways</Button>
        {suggestions.isError ? <ErrorState error={suggestions.error} /> : <EmptyState title="Detected gateway no longer available">It may already be registered. Return to the inventory.</EmptyState>}</>
      : <GatewayRegistrationEditor key={suggestion?.azureResourceId ?? 'new'} suggestion={suggestion}
          onClose={() => workbench.close()} onSaved={saved} />
  } else if (workbench.mode === 'edit' && workbench.selectedId && section === 'registered') {
    body = <GatewayDetailPage gatewayId={workbench.selectedId} embedded />
  } else if (section === 'registered') {
    body = <ResourceWorkbench title="Registered gateways" toolbar={filter}
      description={`${filteredGateways.length} of ${gateways.data?.length ?? 0} gateways under MOSAIC observation.`}
      actions={<Button appearance="primary" icon={<AddRegular />} onClick={() => workbench.open('new', null)}>Onboard gateway</Button>}
      items={filteredGateways} getId={(item) => item.id} selectedId={workbench.selectedId} onSelect={workbench.select}
      listLabel="Registered gateways"
      emptyList={gateways.isPending ? <Loading label="Loading gateways" /> : gateways.isError ? <ErrorState error={gateways.error} />
        : gateways.data?.length ? <EmptyState title="No gateways match this environment">Choose another environment or show all gateways.</EmptyState>
          : <EmptyState title="No gateways yet">Onboard an API Management service to see its read-only inventory.</EmptyState>}
      renderListItem={(item) => <div className={styles.resourceRow}>
        <Text weight="semibold">{item.name}</Text>
        <Text size={200} className={styles.muted}>{item.serviceName} · {MANAGEMENT_MODE_LABELS[item.managementMode]}</Text>
        <div className={styles.rowBadges}><GatewayStatusBadge status={item.status} />
          <EnvironmentBadge azureCloud={item.azureCloud ?? 'commercial'} environment={item.environment} catalog={catalog.data} size="small" /></div>
      </div>}
      renderDetail={(item) => <GatewayDetailPage key={item.id} gatewayId={item.id} embedded />}
      missingSelection={<EmptyState title="Gateway not in this list">It may have been removed or excluded by the environment filter. Clear the selection or change the filter.</EmptyState>}
    />
  } else {
    body = <ResourceWorkbench title="Detected gateways"
      toolbar={<AzureConnectionPicker value={connection} onChange={(value) => { setConnection(value); workbench.close() }} />}
      description="Existing API Management services available for registration. Nothing changes in Azure."
      items={pending} getId={(item) => item.azureResourceId} selectedId={workbench.selectedId} onSelect={workbench.select}
      listLabel="Detected gateways"
      emptyList={suggestions.isPending ? <Loading label="Loading detected gateways" /> : suggestions.isError
        ? <ErrorState error={suggestions.error} /> : <EmptyState title="No detected gateways">You can onboard a service by its resource ID from Registered gateways.</EmptyState>}
      renderListItem={(item) => <div className={styles.resourceRow}><Text weight="semibold">{item.serviceName}</Text>
        <EnvironmentBadge azureCloud={item.azureCloud ?? 'commercial'} environment={item.suggestedEnvironment ?? null} catalog={catalog.data} size="small" />
        <Text size={200} className={styles.muted}>{item.resourceGroup}</Text></div>}
      renderDetail={(item) => <section className={styles.suggestionDetail}>
        <div className={styles.detailHeading}><h2>{item.serviceName}</h2>
          <Button appearance="primary" onClick={() => workbench.open('new', item.azureResourceId)}>Onboard</Button></div>
        <Text>{item.reason} Resource group {item.resourceGroup}.</Text>
        <Text className={styles.muted}>Registration records this service in MOSAIC and checks access. Observe-only is the default; it does not change APIM resources.</Text>
      </section>}
    />
  }
  return <div className={styles.page}>
    <PageHeader title="Gateways" description="Register an existing API Management service and sync its read-only inventory. Changes require managed mode and a reviewed plan." />
    {gateways.isError && gateways.data && <ErrorState error={gateways.error} />}
    {section === 'suggestions' && suggestions.isError && suggestions.data && <ErrorState error={suggestions.error} />}
    <SectionLayout label="Gateways" sections={gatewaySections} active={section} onSelect={selectSection}>
      {body}
    </SectionLayout>
  </div>
}
