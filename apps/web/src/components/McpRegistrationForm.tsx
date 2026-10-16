import { Field, Input, Tab, TabList } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { useEnvironmentCatalog } from '../environments'
import type { McpAuthMode, McpEndpoint } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { EnvironmentPicker } from './EnvironmentPicker'
import { AzureConnectionPicker, type ConnectionSelection } from './AzureConnectionPicker'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from '../pages/McpsPage.module.css'

export function McpRegistrationForm({
  mutationKey, onClose, onRegistered,
}: {
  mutationKey: readonly unknown[]
  onClose: () => void
  onRegistered: (endpoint: McpEndpoint) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const formRef = useRef<HTMLFormElement>(null)
  const [mode, setMode] = useState<McpAuthMode>('none')
  const [connection, setConnection] = useState<ConnectionSelection>({ azureCloud: 'commercial', azureConnectionId: null })
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [environment, setEnvironment] = useState<string | null>(null)
  const [environmentTouched, setEnvironmentTouched] = useState(false)
  const [secretUri, setSecretUri] = useState('')
  const [audience, setAudience] = useState('')
  useEffect(() => {
    if (!environmentTouched && !environment && catalog.data?.environments[0]) {
      setEnvironment(catalog.data.environments[0].key)
    }
  }, [environmentTouched, environment, catalog.data])
  const register = useMutation({
    mutationKey,
    mutationFn: () => api.registerMcpEndpoint({
      endpoint: url.trim(),
      name: name.trim() || undefined,
      environment,
      authMode: mode,
      ...(connection.azureConnectionId || connection.azureCloud !== 'commercial' ? connection : {}),
      credentialSecretUri: mode === 'apiKey' ? secretUri.trim() : undefined,
      resourceAudience: mode === 'managedIdentity' ? audience.trim() : undefined,
    }),
    onSuccess: async (endpoint) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['mcp-endpoints'] }),
        queryClient.invalidateQueries({ queryKey: ['mcp-endpoint-tools'] }),
      ])
      onRegistered(endpoint)
    },
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    setEnvironmentTouched(true)
    if (!environment || !url.trim() || (mode === 'apiKey' && !secretUri.trim()) ||
      (mode === 'managedIdentity' && !audience.trim()) || register.isPending) return
    register.mutate()
  }
  return (
    <WorkspaceEditorFrame title="Register MCP server"
      description="Record a server and discover its tools. Nothing is created in Azure, and MOSAIC never calls a tool."
      onBack={onClose} onCancel={onClose}
      dirty={Boolean(url || name || secretUri || audience || mode !== 'none' || environmentTouched)}
      pending={register.isPending} pendingLabel="Registering server…"
      primaryAction={{ label: 'Register', onClick: () => formRef.current?.requestSubmit() }}>
      <form ref={formRef} onSubmit={submit} className={styles.editorForm}>
        <AzureConnectionPicker value={connection} onChange={(selection) => {
          setConnection(selection)
          if (selection.azureCloud === 'government' && mode === 'apiKey') setMode('none')
        }} />
        <Field label="Server URL" required
          hint="The Streamable HTTP endpoint, usually ending in /mcp. MOSAIC will not connect to a private or loopback address.">
          <Input required type="url" value={url} onChange={(_, data) => setUrl(data.value)}
            placeholder="https://contoso.azure-api.net/tools-mcp/mcp" />
        </Field>
        <TabList aria-label="Server authentication" selectedValue={mode}
          onTabSelect={(_, data) => setMode(data.value as McpAuthMode)}>
          <Tab value="none">No auth</Tab>
          <Tab value="apiKey" disabled={connection.azureCloud === 'government'}>Key Vault secret</Tab>
          <Tab value="managedIdentity">{connection.azureCloud === 'government' ? 'Selected connection' : 'Managed identity'}</Tab>
        </TabList>
        {mode === 'apiKey' && (
          <Field label="Key Vault secret URI" required
            hint="Store the bearer token in Key Vault and paste its secret identifier. MOSAIC stores only this URI, never the token.">
            <Input required type="url" value={secretUri} onChange={(_, data) => setSecretUri(data.value)}
              placeholder="https://my-vault.vault.azure.net/secrets/mcp-token" />
          </Field>
        )}
        {mode === 'managedIdentity' && (
          <Field label="Token audience" required
            hint="Who the token is for. MOSAIC will not infer this: a token is only ever sent to an audience you named.">
            <Input required value={audience} onChange={(_, data) => setAudience(data.value)}
              placeholder="api://contoso-mcp" />
          </Field>
        )}
        <Field label="Display name">
          <Input value={name} onChange={(_, data) => setName(data.value)} />
        </Field>
        {catalog.isPending && <Loading label="Loading environments" />}
        {catalog.isError && <ErrorState error={catalog.error} />}
        <EnvironmentPicker catalog={catalog.data} value={environment}
          onChange={(value) => { setEnvironment(value); setEnvironmentTouched(true) }}
          required label="Environment"
          validationMessage={environmentTouched && !environment ? 'Choose an environment.' : undefined} />
        {register.isError && <ErrorState error={register.error} />}
      </form>
    </WorkspaceEditorFrame>
  )
}
