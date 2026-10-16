import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Badge, Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface,
  DialogTitle, Field, Input, MessageBar, MessageBarBody, Select, Text,
} from '@fluentui/react-components'
import { useMosaicApi } from '../api'
import type { AzureConnection } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import styles from './AzureConnectionsPanel.module.css'

type Editor = { kind: 'create' } | { kind: 'name' | 'secret'; connection: AzureConnection }

function ConnectionEditor({ editor, onClose }: { editor: Editor; onClose: () => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [name, setName] = useState(editor.kind === 'create' ? '' : editor.connection.name)
  const [cloud, setCloud] = useState<'commercial' | 'government'>('government')
  const [tenant, setTenant] = useState('')
  const [clientId, setClientId] = useState('')
  const [secret, setSecret] = useState('')
  const [dirty, setDirty] = useState(false)
  const [save, setSave] = useState<{ pending: boolean; error: unknown }>({ pending: false, error: null })
  async function saveConnection() {
    if (save.pending) return
    setSave({ pending: true, error: null })
    try {
      if (editor.kind === 'create') {
        await api.createAzureConnection({
          name: name.trim(), azureCloud: cloud, azureTenantId: tenant.trim(),
          clientId: clientId.trim(), clientSecret: secret,
        })
      } else if (editor.kind === 'secret') {
        await api.replaceAzureConnectionSecret(editor.connection.id, secret)
      } else {
        await api.renameAzureConnection(editor.connection.id, { name: name.trim() })
      }
      setSecret('')
      void queryClient.invalidateQueries({ queryKey: ['azure-connections'] })
      completeWorkspaceNavigation(onClose)
    } catch (error) {
      setSave({ pending: false, error })
    }
  }
  const valid = editor.kind === 'create' ? name.trim() && tenant.trim() && clientId.trim() && secret
    : editor.kind === 'secret' ? Boolean(secret) : Boolean(name.trim())
  return <WorkspaceEditorFrame
    title={editor.kind === 'create' ? 'Add Azure connection'
      : editor.kind === 'secret' ? 'Replace connection secret' : 'Rename Azure connection'}
    description="Management credentials stay in the separate MOSAIC credentials vault. They are never given to a gateway or end user."
    backLabel="Back to Azure connections" onBack={onClose} onCancel={onClose}
    dirty={dirty} pending={save.pending}
    error={save.error === null ? undefined : save.error instanceof Error
      ? save.error.message : 'Unable to save the Azure connection.'}
    primaryAction={{ label: editor.kind === 'secret' ? 'Verify and replace secret' : 'Save connection',
      disabled: !valid, onClick: () => { void saveConnection() } }}>
    <form className={styles.form} onChangeCapture={() => setDirty(true)}
      onSubmit={(event) => { event.preventDefault(); if (valid) void saveConnection() }}>
      {editor.kind !== 'secret' && <Field label="Connection name" required>
        <Input value={name} disabled={save.pending} onChange={(_, data) => setName(data.value)} maxLength={120} />
      </Field>}
      {editor.kind === 'create' && <>
        <Field label="Azure cloud" required>
          <Select value={cloud} disabled={save.pending} onChange={(_, data) => setCloud(data.value === 'government' ? 'government' : 'commercial')}>
            <option value="government">Azure Government</option>
            <option value="commercial">Azure Commercial</option>
          </Select>
        </Field>
        <Field label="Target tenant ID" required hint="The service principal's tenant in the selected cloud, not the MOSAIC sign-in tenant.">
          <Input value={tenant} disabled={save.pending} onChange={(_, data) => setTenant(data.value)} />
        </Field>
        <Field label="Application (client) ID" required>
          <Input value={clientId} disabled={save.pending} onChange={(_, data) => setClientId(data.value)} />
        </Field>
      </>}
      {editor.kind !== 'name' && <Field label="Client secret value" required
        hint="Paste the value, not its secret ID. It is write-only and never shown again.">
        <Input type="password" autoComplete="new-password" value={secret} disabled={save.pending}
          onChange={(_, data) => setSecret(data.value)} />
      </Field>}
      {editor.kind === 'secret' && <Text>
        MOSAIC checks the replacement's authentication before activating it. Existing resources keep this connection.
      </Text>}
    </form>
  </WorkspaceEditorFrame>
}

export function AzureConnectionsPanel() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [editor, setEditor] = useState<Editor | null>(null)
  const [removing, setRemoving] = useState<AzureConnection | null>(null)
  const connections = useQuery({
    queryKey: ['azure-connections'], queryFn: () => api.listAzureConnections(),
  })
  const test = useMutation({
    mutationFn: (id: string) => api.testAzureConnection(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['azure-connections'] }),
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteAzureConnection(id),
    onSuccess: () => {
      setRemoving(null)
      void queryClient.invalidateQueries({ queryKey: ['azure-connections'] })
    },
  })
  if (editor) return <ConnectionEditor key={editor.kind === 'create' ? 'create' : `${editor.kind}:${editor.connection.id}`}
    editor={editor} onClose={() => setEditor(null)} />
  return <section className={styles.panel} aria-labelledby="azure-connections-heading">
    <div className={styles.toolbar}>
      <div>
        <h2 id="azure-connections-heading">Azure connections</h2>
        <Text>Choose the management identity used to discover and manage each Azure estate.</Text>
      </div>
      <Button appearance="primary" onClick={() => setEditor({ kind: 'create' })}>Add connection</Button>
    </div>
    <MessageBar intent="info" layout="multiline"><MessageBarBody>
      Government connections manage resources, not user sign-in. Government offerings accept grant-scoped subscription keys,
      not commercial Entra tokens. Management metadata and usage are stored in this commercial MOSAIC deployment;
      your organization must approve that data boundary.
    </MessageBarBody></MessageBar>
    {connections.isPending && <Loading label="Loading Azure connections" />}
    {connections.isError && <ErrorState error={connections.error} />}
    {test.isError && <ErrorState error={test.error} />}
    <div className={styles.list}>
      {connections.data?.map((connection) => <article className={styles.connection} key={connection.id}>
        <div className={styles.heading}>
          <h3>{connection.name}</h3>
          <Badge appearance="outline">{connection.azureCloud === 'government' ? 'AzGov' : 'Commercial'}</Badge>
          <Badge appearance="tint" color={connection.status === 'failed' ? 'danger' : connection.status === 'ready' ? 'success' : 'informative'}>
            {connection.status === 'ready' ? 'Authentication verified' : connection.status === 'failed' ? 'Access needed' : 'Not tested'}
          </Badge>
        </div>
        <dl className={styles.facts}>
          <dt>Authentication</dt><dd>{connection.authentication === 'managedIdentity' ? 'MOSAIC managed identity' : 'Service principal'}</dd>
          <dt>Target tenant</dt><dd>{connection.azureTenantId}</dd>
          {connection.clientId && <><dt>Application ID</dt><dd>{connection.clientId}</dd></>}
        </dl>
        {connection.statusMessage && <Text role="status">{connection.statusMessage}</Text>}
        {connection.authentication === 'clientSecret' && <div className={styles.actions}>
          <Button disabled={test.isPending} onClick={() => test.mutate(connection.id)}>Test authentication</Button>
          <Button onClick={() => setEditor({ kind: 'name', connection })}>Rename</Button>
          <Button onClick={() => setEditor({ kind: 'secret', connection })}>Replace secret</Button>
          <Button disabled={connection.inUse} onClick={() => { remove.reset(); setRemoving(connection) }}>Remove</Button>
          {connection.inUse && <Text size={200}>Registered resources use this connection.</Text>}
        </div>}
      </article>)}
    </div>
    <Dialog open={removing !== null} onOpenChange={(_, data) => { if (!data.open && !remove.isPending) setRemoving(null) }}>
      <DialogSurface><DialogBody>
        <DialogTitle>Remove Azure connection?</DialogTitle>
        <DialogContent>
          Remove {removing?.name} from MOSAIC and delete its stored management secret.
          This does not remove the service principal or any Azure resource.
          {remove.isError && <ErrorState error={remove.error} />}
        </DialogContent>
        <DialogActions>
          <Button disabled={remove.isPending} onClick={() => setRemoving(null)}>Cancel</Button>
          <Button appearance="primary" disabled={remove.isPending}
            onClick={() => { if (removing) remove.mutate(removing.id) }}>Remove connection</Button>
        </DialogActions>
      </DialogBody></DialogSurface>
    </Dialog>
  </section>
}
