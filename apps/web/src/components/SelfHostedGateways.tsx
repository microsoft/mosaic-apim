import { Badge, Button, Field, Input, Select, Text } from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import type { Gateway, SelfHostedGateway } from '../types'
import { EmptyState, ErrorState, Loading } from './AsyncState'
import { runtimeQueryKey, runtimeRefusal, useGatewayRuntimes } from './GatewayRuntimePicker'
import { ResourceWorkbench } from './ResourceWorkbench'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import styles from './SelfHostedGateways.module.css'

export function SelfHostedGateways({ parents }: { parents: Gateway[] }) {
  const [parentId, setParentId] = useState('')
  const [creating, setCreating] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<string | null>(null)
  const api = useMosaicApi()
  const cache = useQueryClient()
  const parent = parents.find((item) => item.id === parentId)
  const runtimes = useGatewayRuntimes(parentId)
  const remove = useMutation({
    mutationFn: (id: string) => api.removeSelfHostedGateway(parentId, id),
    onSuccess: () => {
      setConfirm(null)
      setSelected(null)
      void cache.invalidateQueries({ queryKey: runtimeQueryKey(parentId) })
    },
  })
  if (creating && parent) return <OnboardRuntime parent={parent} onBack={() => setCreating(false)}
    onSaved={(runtime) => { setSelected(runtime.id); setCreating(false) }} />
  return <ResourceWorkbench title="Self-hosted gateways"
    description="Register an existing runtime under its API Management service. Registration does not deploy a gateway or change any API."
    toolbar={<Field label="Parent APIM service"><Select value={parentId} onChange={(event) => {
      setParentId(event.target.value)
      setSelected(null)
      setConfirm(null)
      remove.reset()
    }}>
      <option value="">Choose an APIM service</option>
      {parents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
    </Select></Field>}
    actions={<Button appearance="primary" icon={<AddRegular />} disabled={!parent}
      onClick={() => setCreating(true)}>Onboard self-hosted gateway</Button>}
    items={runtimes.data ?? []} getId={(item) => item.id}
    selectedId={selected} onSelect={(id) => { setSelected(id); setConfirm(null); remove.reset() }}
    listLabel="Registered self-hosted gateways"
    emptyList={!parent
      ? <EmptyState title="Choose a parent APIM service">Only its registered self-hosted runtimes appear here.</EmptyState>
      : runtimes.isPending ? <Loading label="Loading self-hosted gateways" />
      : runtimes.isError ? <ErrorState error={runtimes.error} />
      : <EmptyState title="No self-hosted gateways registered">Onboard an existing gateway from this service.</EmptyState>}
    renderListItem={(item) => <div className={styles.row}>
      <Text weight="semibold">{item.gatewayName}</Text>
      <Text size={200}>Self-hosted {item.imageVersion}</Text>
    </div>}
    renderDetail={(item) => <section className={styles.details}>
      <h2>{item.gatewayName}</h2>
      <Badge appearance="tint" color="warning">{runtimeRefusal(parent, item) || 'Runtime unconfirmed'}</Badge>
      <dl>
        <div><dt>Parent APIM service</dt><dd>{parent?.name} · {parent?.managementMode === 'manage' ? 'Manage' : 'Observe'}</dd></div>
        <div><dt>Local caller ingress</dt><dd><code>{item.ingressOrigin}</code></dd></div>
        <div><dt>Registered image</dt><dd>{item.imageVersion}{item.imageDigest && <code>{item.imageDigest}</code>}</dd></div>
        <div><dt>Registered</dt><dd>{new Date(item.createdAt).toLocaleString()}</dd></div>
      </dl>
      <Text block size={200}>These are registered facts, not proof of reachability, policy, identity, or metering.</Text>
      {remove.isError && <ErrorState error={remove.error} />}
      {confirm === item.id ? <div role="group" aria-label="Confirm removing the runtime record">
        <Text block>Remove the MOSAIC record only? API Management and callers are not changed.</Text>
        <Button onClick={() => setConfirm(null)}>Cancel</Button>{' '}
        <Button disabledFocusable={remove.isPending} onClick={() => {
          if (!remove.isPending) remove.mutate(item.id)
        }}>Remove runtime record</Button>
      </div> : <Button onClick={() => setConfirm(item.id)}>Forget runtime</Button>}
    </section>}
  />
}

function OnboardRuntime({ parent, onBack, onSaved }: {
  parent: Gateway; onBack: () => void; onSaved: (runtime: SelfHostedGateway) => void
}) {
  const api = useMosaicApi()
  const cache = useQueryClient()
  const [name, setName] = useState('')
  const [origin, setOrigin] = useState('')
  const [version, setVersion] = useState('')
  const [digest, setDigest] = useState('')
  const candidates = useQuery({
    queryKey: ['self-hosted-candidates', parent.id],
    queryFn: () => api.listSelfHostedGatewayCandidates(parent.id),
  })
  const register = useMutation({
    mutationFn: () => api.registerSelfHostedGateway(parent.id, {
      gatewayName: name, ingressOrigin: origin.trim(), imageVersion: version.trim(),
      ...(digest.trim() ? { imageDigest: digest.trim() } : {}),
    }),
    onSuccess: (runtime) => {
      void cache.invalidateQueries({ queryKey: runtimeQueryKey(parent.id) })
      completeWorkspaceNavigation(() => onSaved(runtime))
    },
  })
  return <WorkspaceEditorFrame title="Onboard a self-hosted gateway"
    description={`Record an existing runtime under ${parent.name}. This does not deploy or attach anything in APIM.`}
    onBack={onBack} onCancel={onBack} dirty={Boolean(name || origin || version || digest)}
    error={register.error?.message} pending={register.isPending}
    primaryAction={{ label: 'Register runtime', disabled: !(name && origin.trim() && version.trim()), onClick: () => register.mutate() }}>
    <div className={styles.fields}>
      <Field label="Existing self-hosted gateway" required>
        <Select value={name} onChange={(event) => setName(event.target.value)}>
          <option value="">Select a gateway on this service</option>
          {candidates.data?.map((item) => <option key={item.resourceId} value={item.gatewayName}>{item.gatewayName}</option>)}
        </Select>
      </Field>
      {candidates.isPending && <Loading label="Reading existing gateway inventory" />}
      {candidates.isError && <ErrorState error={candidates.error} />}
      {candidates.isSuccess && !candidates.data.length && <Text>No existing self-hosted gateways were found on this service.</Text>}
      <Field label="Local HTTPS ingress origin" hint="Use the address callers reach. Do not enter the APIM configuration endpoint." required>
        <Input type="url" value={origin} onChange={(_, data) => setOrigin(data.value)} />
      </Field>
      <Field label="Running image version" hint="Enter the exact major.minor.patch, not a rolling tag." required>
        <Input value={version} onChange={(_, data) => setVersion(data.value)} />
      </Field>
      <Field label="Running image digest" hint="Required before this runtime can be qualified.">
        <Input value={digest} onChange={(_, data) => setDigest(data.value)} />
      </Field>
    </div>
  </WorkspaceEditorFrame>
}
