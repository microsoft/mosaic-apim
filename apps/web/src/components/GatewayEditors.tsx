import { Button, Checkbox, Field, Input, MessageBar, MessageBarBody, Text } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { environmentLabel, grantsAcknowledgmentDetails, invalidateEnvironmentQueries, publicationsBlockedDetails, useEnvironmentCatalog } from '../environments'
import type { EnvironmentAssignment, Gateway, GatewaySuggestion } from '../types'
import { ErrorState } from './AsyncState'
import { EnvironmentPicker } from './EnvironmentPicker'
import { GrantsAcknowledgmentRefusal, PublicationsBlockedRefusal } from './EnvironmentRefusal'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import styles from './GatewayEditors.module.css'

export function GatewayRegistrationEditor({ suggestion, onClose, onSaved }: {
  suggestion?: GatewaySuggestion
  onClose: () => void
  onSaved: (gateway: Gateway) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [resourceId, setResourceId] = useState(suggestion?.azureResourceId ?? '')
  const [name, setName] = useState(suggestion?.serviceName ?? '')
  const [environment, setEnvironment] = useState<string | null>(suggestion?.suggestedEnvironment ?? null)
  const [touched, setTouched] = useState(false)
  const [dirty, setDirty] = useState(false)
  const initialized = useRef(false)
  useEffect(() => {
    if (!initialized.current && !suggestion && catalog.data?.environments[0]) {
      setEnvironment(catalog.data.environments[0].key)
      initialized.current = true
    }
  }, [catalog.data, suggestion])
  const register = useMutation({
    mutationFn: () => api.registerGateway({ azureResourceId: resourceId.trim(), name: name.trim() || undefined, environment }),
    onSuccess: (gateway) => {
      queryClient.setQueryData(['gateway', gateway.id], gateway)
      queryClient.setQueryData<Gateway[]>(['gateways'], (items = []) => [...items.filter((item) => item.id !== gateway.id), gateway])
      void queryClient.invalidateQueries({ queryKey: ['gateways'] })
      void queryClient.invalidateQueries({ queryKey: ['gateway-suggestions'] })
      completeWorkspaceNavigation(() => onSaved(gateway))
    },
  })
  function submit() {
    setTouched(true)
    if (environment && resourceId.trim()) register.mutate()
  }
  return <WorkspaceEditorFrame title="Onboard a gateway" onBack={onClose} onCancel={onClose}
    backLabel="Back to gateways" dirty={dirty} pending={register.isPending} pendingLabel="Checking access…"
    description="Register an existing API Management service in MOSAIC. Registration saves intent and never changes APIM."
    primaryAction={{ label: 'Check access and onboard', onClick: submit, disabled: register.isPending }}>
    <form className={styles.form} onSubmit={(event) => { event.preventDefault(); submit() }}
      onChangeCapture={() => setDirty(true)}>
      <Text>MOSAIC checks whether its managed identity can read the service. New gateways are observe-only.
        If access is missing, MOSAIC shows the exact role to grant.</Text>
      <Field label="API Management resource ID" required
        hint="/subscriptions/{id}/resourceGroups/{group}/providers/Microsoft.ApiManagement/service/{name}"
        validationMessage={touched && !resourceId.trim() ? 'Enter the API Management resource ID.' : undefined}>
        <Input value={resourceId} onChange={(_, data) => setResourceId(data.value)} />
      </Field>
      <Field label="Display name" hint="Defaults to the service name">
        <Input value={name} onChange={(_, data) => setName(data.value)} />
      </Field>
      <EnvironmentPicker catalog={catalog.data} value={environment} required label="Environment"
        onChange={(value) => { setDirty(true); setEnvironment(value); setTouched(true) }}
        suggestion={suggestion?.suggestedEnvironment && suggestion.azureEnvironmentTag
          ? { environment: suggestion.suggestedEnvironment, evidence: `Azure tag environment = "${suggestion.azureEnvironmentTag}"` } : undefined}
        validationMessage={touched && !environment ? 'Choose an environment.' : undefined} />
      {suggestion?.azureEnvironmentTag && !suggestion.suggestedEnvironment &&
        <Text size={200}>Azure tag: {suggestion.azureEnvironmentTag}</Text>}
      {catalog.isError && <ErrorState error={catalog.error} />}
      {register.isError && <ErrorState error={register.error} />}
    </form>
  </WorkspaceEditorFrame>
}

export function GatewayRecordEditor({ gateway, onClose, onSaved }: {
  gateway: Gateway
  onClose: () => void
  onSaved: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [name, setName] = useState(gateway.name)
  const [environment, setEnvironment] = useState(gateway.environment)
  const [acknowledged, setAcknowledged] = useState(false)
  const [suggested, setSuggested] = useState<EnvironmentAssignment[]>([])
  const [savedEnvironment, setSavedEnvironment] = useState<string | null>(gateway.environment)
  const dirty = name !== gateway.name || environment !== gateway.environment
  const save = useMutation({
    mutationFn: async (includeSuggestions: boolean) => {
      if (environment !== savedEnvironment) {
        await api.assignEnvironments({
          assignments: [{ resourceKind: 'gateway', resourceId: gateway.id, environment }, ...(includeSuggestions ? suggested : [])],
          acknowledgeGrants: acknowledged,
        })
        setSavedEnvironment(environment)
        invalidateEnvironmentQueries(queryClient)
      }
      if (name.trim() !== gateway.name) await api.updateGateway(gateway.id, { name: name.trim() })
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['gateways'] })
      void queryClient.invalidateQueries({ queryKey: ['gateway', gateway.id] })
      completeWorkspaceNavigation(onSaved)
    },
  })
  const blocked = publicationsBlockedDetails(save.error)
  const grants = grantsAcknowledgmentDetails(save.error)
  useEffect(() => { if (blocked) setSuggested(blocked.suggestedAssignments) }, [blocked])
  return <WorkspaceEditorFrame title={`Edit ${gateway.name}`} onBack={onClose} onCancel={onClose}
    backLabel="Back to gateway" description="Update MOSAIC metadata. This does not rename or change the Azure service."
    dirty={dirty} pending={save.isPending}
    primaryAction={{ label: 'Save changes', disabled: !name.trim() || Boolean(grants && !acknowledged),
      onClick: () => save.mutate(Boolean(grants && suggested.length)) }}>
    <div className={styles.form}>
      <Field label="Display name" required><Input value={name} onChange={(_, data) => setName(data.value)} /></Field>
      <EnvironmentPicker catalog={catalog.data} value={environment} allowUnclassified label="Environment"
        onChange={(value) => { setEnvironment(value); setAcknowledged(false); setSuggested([]); save.reset() }} />
      {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog.data} />}
      {blocked && suggested.length > 0 && <Button disabled={save.isPending} onClick={() => save.mutate(true)}>
        Also classify {suggested.length === 1 ? 'this resource' : `these ${suggested.length} resources`} as {environmentLabel(catalog.data, suggested[0]?.environment)}
      </Button>}
      {grants && <GrantsAcknowledgmentRefusal details={grants} catalog={catalog.data} />}
      {grants && <Checkbox checked={acknowledged} onChange={(_, data) => setAcknowledged(Boolean(data.checked))}
        label="I understand these active grants will carry to the new environment." />}
      {savedEnvironment !== gateway.environment && save.isError && <MessageBar intent="warning"><MessageBarBody>
        The environment was saved, but the display name was not. Retry Save changes to finish.
      </MessageBarBody></MessageBar>}
      {save.isError && !blocked && !grants && <ErrorState error={save.error} />}
      {catalog.isError && <ErrorState error={catalog.error} />}
    </div>
  </WorkspaceEditorFrame>
}
