import { Field, Select, Text } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import type { AzureCloud, AzureConnection } from '../types'
import { ErrorState } from './AsyncState'
import { cloudLabel } from '../cloud-label'

export interface ConnectionSelection {
  azureCloud: AzureCloud
  azureConnectionId: string | null
}

export function AzureConnectionPicker({ value, onChange }: {
  value: ConnectionSelection
  onChange: (selection: ConnectionSelection) => void
}) {
  const api = useMosaicApi()
  const connections = useQuery({
    queryKey: ['azure-connections'],
    queryFn: () => api.listAzureConnections(),
  })
  const selectedId = value.azureConnectionId ?? 'host'
  const selected = connections.data?.find((connection) => connection.id === selectedId)
  function select(connection: AzureConnection) {
    onChange({
      azureCloud: connection.azureCloud,
      azureConnectionId: connection.id === 'host' ? null : connection.id,
    })
  }
  return <Field label="Azure connection" required
    hint="Choose the management identity and cloud before selecting an Azure resource. Configure connections in Settings.">
    <Select value={selectedId} disabled={connections.isPending || connections.isError}
      onChange={(_, data) => {
        const connection = connections.data?.find((item) => item.id === data.value)
        if (connection) select(connection)
      }}>
      {!connections.data?.some((item) => item.id === 'host') &&
        <option value="host">Host managed identity · Azure Commercial</option>}
      {connections.data?.map((connection) => <option key={connection.id} value={connection.id}>
        {connection.name} · {cloudLabel(connection.azureCloud)}
        {connection.status === 'failed' ? ' · validation failed' : ''}
      </option>)}
    </Select>
    {connections.isPending && <Text size={200}>Loading connections…</Text>}
    {connections.isError && <ErrorState error={connections.error} />}
    {selected && <Text size={200}>
      {selected.authentication === 'managedIdentity' ? 'Host managed identity' : `Service principal ${selected.clientId ?? '(not configured)'}`}
      {' · '}{cloudLabel(selected.azureCloud)}{' · '}{selected.status === 'ready' ? 'Read access tested' : selected.status}
      {selected.statusMessage && ` — ${selected.statusMessage}`}
    </Text>}
  </Field>
}
