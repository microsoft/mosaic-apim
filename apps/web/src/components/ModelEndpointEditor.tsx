import { Button, Checkbox, Field, Input, MessageBar, MessageBarBody, Text } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import {
  environmentLabel, grantsAcknowledgmentDetails, invalidateEnvironmentQueries,
  publicationsBlockedDetails, useEnvironmentCatalog,
} from '../environments'
import { usesBackendKey } from '../key-endpoint'
import { environmentAssignmentResourceNoun } from '../labels'
import type { EnvironmentAssignment, ModelEndpoint } from '../types'
import { ErrorState } from './AsyncState'
import { EnvironmentPicker } from './EnvironmentPicker'
import { GrantsAcknowledgmentRefusal, PublicationsBlockedRefusal } from './EnvironmentRefusal'
import { WorkspaceDialog } from './WorkspaceDialog'
import styles from '../pages/ModelsPage.module.css'

export function ModelEndpointEditor({ endpoint, mutationKey, onClose, onSaved }: {
  endpoint: ModelEndpoint
  mutationKey?: readonly unknown[]
  onClose: () => void
  onSaved: (endpoint: ModelEndpoint) => void
}) {
  const api = useMosaicApi()
  const [name, setName] = useState(endpoint.name)
  const [secretUri, setSecretUri] = useState('')
  const externalKey = usesBackendKey(endpoint) && !endpoint.keyStoredByMosaic
  const mutation = useMutation({
    mutationKey,
    mutationFn: () => api.updateModelEndpoint(endpoint.id, {
      name: name.trim(),
      ...(externalKey && secretUri.trim() ? { credentialSecretUri: secretUri.trim() } : {}),
    }),
    onSuccess: onSaved,
  })
  return <WorkspaceDialog open title={`Edit ${endpoint.name}`} pending={mutation.isPending}
    dirty={name !== endpoint.name || Boolean(secretUri)}
    description="Update MOSAIC configuration only. Provider resources and API Management are unchanged."
    onOpenChange={(_, data) => !data.open && onClose()}>
    <form className={styles.dialogForm} onSubmit={(event) => {
      event.preventDefault()
      if (!mutation.isPending && name.trim()) mutation.mutate()
    }}>
      <Field label="Display name" required><Input value={name} required onChange={(_, data) => setName(data.value)} /></Field>
      {externalKey && <Field label="Replacement Key Vault secret URI"
        hint="Leave empty to keep the current reference. MOSAIC stores only the URI; this does not change the key or grant Key Vault access.">
        <Input value={secretUri} type="url" onChange={(_, data) => setSecretUri(data.value)} />
      </Field>}
      {mutation.isError && <ErrorState error={mutation.error} />}
      <div className={styles.formActions}><Button appearance="primary" type="submit"
        disabled={!name.trim()} disabledFocusable={mutation.isPending}>
        {mutation.isPending ? 'Saving…' : 'Save endpoint'}
      </Button></div>
    </form>
  </WorkspaceDialog>
}

export function ModelEndpointEnvironmentEditor({ endpoint, mutationKey, onClose, onSaved }: {
  endpoint: ModelEndpoint
  mutationKey?: readonly unknown[]
  onClose: () => void
  onSaved: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [environment, setEnvironment] = useState(endpoint.environment)
  const [suggested, setSuggested] = useState<EnvironmentAssignment[]>([])
  const [acknowledged, setAcknowledged] = useState(false)
  const mutation = useMutation({
    mutationKey,
    mutationFn: ({ includeSuggestions, acknowledgeGrants }: { includeSuggestions?: boolean; acknowledgeGrants?: boolean }) =>
      api.assignEnvironments({
        assignments: [
          { resourceKind: 'modelEndpoint', resourceId: endpoint.id, environment },
          ...(includeSuggestions ? suggested : []),
        ],
        acknowledgeGrants,
      }),
    onSuccess: () => { invalidateEnvironmentQueries(queryClient); onSaved() },
    onError: (error) => {
      const blocked = publicationsBlockedDetails(error)
      if (blocked) setSuggested(blocked.suggestedAssignments)
    },
  })
  const blocked = publicationsBlockedDetails(mutation.error)
  const grants = grantsAcknowledgmentDetails(mutation.error)
  const suggestionNoun = environmentAssignmentResourceNoun(suggested)
  const message = mutation.error instanceof ApiError || mutation.error instanceof Error ? mutation.error.message : null
  return <WorkspaceDialog open title="Change environment" description={`Change the environment for ${endpoint.name}.`}
    pending={mutation.isPending} dirty={environment !== endpoint.environment || acknowledged}
    onOpenChange={(_, data) => !data.open && onClose()}>
    <div className={styles.dialogForm}>
      <EnvironmentPicker catalog={catalog.data} value={environment} onChange={setEnvironment} allowUnclassified label="Environment" />
      {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog.data} />}
      {blocked && suggested.length > 0 && <Button disabled={mutation.isPending}
        onClick={() => mutation.mutate({ includeSuggestions: true })}>
        {suggested.length === 1 ? `Also classify this ${suggestionNoun}` : `Also classify these ${suggested.length} ${suggestionNoun}`}
        {' '}as {environmentLabel(catalog.data, suggested[0]?.environment)}
      </Button>}
      {grants && <GrantsAcknowledgmentRefusal details={grants} catalog={catalog.data} />}
      {grants && <Checkbox checked={acknowledged} onChange={(_, data) => setAcknowledged(Boolean(data.checked))}
        label="I understand these active grants will carry to the new environment." />}
      {message && !blocked && !grants && <MessageBar intent="error" role="alert"><MessageBarBody>{message}</MessageBarBody></MessageBar>}
      <Text size={200}>Classification is saved intent. Runtime enforcement still requires review and explicit Apply.</Text>
      <Button appearance="primary" disabled={Boolean(grants) && !acknowledged} disabledFocusable={mutation.isPending}
        onClick={() => !mutation.isPending && mutation.mutate({ includeSuggestions: suggested.length > 0 && Boolean(grants), acknowledgeGrants: acknowledged })}>
        {mutation.isPending ? 'Saving…' : grants ? 'Confirm and save' : 'Save'}
      </Button>
    </div>
  </WorkspaceDialog>
}
