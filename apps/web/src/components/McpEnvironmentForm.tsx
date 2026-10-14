import { Button, Checkbox, MessageBar, MessageBarBody, Text } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useMosaicApi } from '../api'
import {
  environmentLabel, grantsAcknowledgmentDetails, invalidateEnvironmentQueries,
  publicationsBlockedDetails, useEnvironmentCatalog,
} from '../environments'
import { environmentAssignmentResourceNoun } from '../labels'
import type { EnvironmentAssignment, McpEndpoint } from '../types'
import { EnvironmentPicker } from './EnvironmentPicker'
import { GrantsAcknowledgmentRefusal, PublicationsBlockedRefusal } from './EnvironmentRefusal'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from '../pages/McpsPage.module.css'

export function McpEnvironmentForm({ endpoint, mutationKey, onClose, onChanged }: {
  endpoint: McpEndpoint
  mutationKey: readonly unknown[]
  onClose: () => void
  onChanged: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [environment, setEnvironment] = useState(endpoint.environment)
  const [suggested, setSuggested] = useState<EnvironmentAssignment[]>([])
  const [acknowledged, setAcknowledged] = useState(false)
  const mutation = useMutation({
    mutationKey,
    mutationFn: ({ includeSuggestions, acknowledgeGrants }: {
      includeSuggestions?: boolean; acknowledgeGrants?: boolean
    }) => api.assignEnvironments({
      assignments: [
        { resourceKind: 'mcpEndpoint', resourceId: endpoint.id, environment },
        ...(includeSuggestions ? suggested : []),
      ],
      acknowledgeGrants,
    }),
    onSuccess: async () => {
      invalidateEnvironmentQueries(queryClient)
      await queryClient.invalidateQueries({ queryKey: ['mcp-endpoints'] })
      onChanged()
    },
  })
  const blocked = publicationsBlockedDetails(mutation.error)
  const grants = grantsAcknowledgmentDetails(mutation.error)
  useEffect(() => { if (blocked) setSuggested(blocked.suggestedAssignments) }, [blocked])
  const suggestionNoun = environmentAssignmentResourceNoun(suggested)
  return (
    <WorkspaceEditorFrame title={`Edit ${endpoint.name}`}
      description="Change the registered server's environment without changing its URL or credentials."
      onBack={onClose} onCancel={onClose} dirty={environment !== endpoint.environment}
      pending={mutation.isPending}
      primaryAction={{
        label: grants ? 'Confirm and save' : 'Save',
        disabled: Boolean(grants) && !acknowledged,
        onClick: () => mutation.mutate({
          includeSuggestions: suggested.length > 0 && Boolean(grants), acknowledgeGrants: acknowledged,
        }),
      }}>
      <div className={styles.editorForm}>
        <Text>Change the environment for {endpoint.name}.</Text>
        <EnvironmentPicker catalog={catalog.data} value={environment} onChange={setEnvironment}
          allowUnclassified label="Environment" />
        {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog.data} />}
        {blocked && suggested.length > 0 && (
          <Button onClick={() => mutation.mutate({ includeSuggestions: true })} disabled={mutation.isPending}>
            {suggested.length === 1 ? `Also classify this ${suggestionNoun}` : `Also classify these ${suggested.length} ${suggestionNoun}`}{' '}
            as {environmentLabel(catalog.data, suggested[0]?.environment)}
          </Button>
        )}
        {grants && <GrantsAcknowledgmentRefusal details={grants} catalog={catalog.data} />}
        {grants && <Checkbox checked={acknowledged}
          onChange={(_, data) => setAcknowledged(Boolean(data.checked))}
          label="I understand these active grants will carry to the new environment." />}
        {mutation.error && !blocked && !grants && (
          <MessageBar intent="error" role="alert"><MessageBarBody>
            {mutation.error instanceof Error ? mutation.error.message : 'Unable to change this environment.'}
          </MessageBarBody></MessageBar>
        )}
      </div>
    </WorkspaceEditorFrame>
  )
}
