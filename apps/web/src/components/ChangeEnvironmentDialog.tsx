import {
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Text,
} from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import {
  grantsAcknowledgmentDetails,
  invalidateEnvironmentQueries,
  publicationsBlockedDetails,
  useEnvironmentCatalog,
} from '../environments'
import type {
  EnvironmentAssignment,
  EnvironmentAssignmentResult,
  EnvironmentResourceKind,
} from '../types'
import { EnvironmentPicker } from './EnvironmentPicker'
import { GrantsAcknowledgmentRefusal, PublicationsBlockedRefusal } from './EnvironmentRefusal'
import styles from './EnvironmentComponents.module.css'

export interface EnvironmentChangeResource {
  resourceKind: EnvironmentResourceKind
  resourceId: string
  resourceName: string
  environment: string | null
}

export function ChangeEnvironmentDialog({
  resource,
  open,
  onClose,
  onChanged,
}: {
  resource: EnvironmentChangeResource
  open: boolean
  onClose: () => void
  onChanged: (result: EnvironmentAssignmentResult) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const [environment, setEnvironment] = useState<string | null>(resource.environment)
  const [error, setError] = useState<unknown>(null)
  const [suggested, setSuggested] = useState<EnvironmentAssignment[]>([])
  const [acknowledged, setAcknowledged] = useState(false)
  useEffect(() => {
    if (open) {
      setEnvironment(resource.environment)
      setError(null)
      setSuggested([])
      setAcknowledged(false)
    }
  }, [open, resource.environment])
  const mutation = useMutation({
    mutationFn: async ({
      includeSuggestions,
      acknowledgeGrants,
    }: {
      includeSuggestions?: boolean
      acknowledgeGrants?: boolean
    }) => {
      const assignments = [
        { resourceKind: resource.resourceKind, resourceId: resource.resourceId, environment },
        ...(includeSuggestions ? suggested : []),
      ]
      return api.assignEnvironments({ assignments, acknowledgeGrants })
    },
    onSuccess: (result) => {
      invalidateEnvironmentQueries(queryClient)
      onChanged(result)
    },
    onError: (err) => setError(err),
  })
  const blocked = publicationsBlockedDetails(error)
  const grants = grantsAcknowledgmentDetails(error)
  useEffect(() => {
    if (blocked) setSuggested(blocked.suggestedAssignments)
  }, [blocked])
  const message =
    error instanceof ApiError ? error.message : error instanceof Error ? error.message : null
  return (
    <Dialog
      open={open}
      onOpenChange={(_, data) => {
        if (!data.open && !mutation.isPending) onClose()
      }}
    >
      <DialogSurface>
        <DialogBody>
          <DialogTitle>Change environment</DialogTitle>
          <DialogContent className={styles.dialogContent}>
            <Text>Change the environment for {resource.resourceName}.</Text>
            <EnvironmentPicker
              catalog={catalog.data}
              value={environment}
              onChange={setEnvironment}
              allowUnclassified
              label="Environment"
            />
            {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog.data} />}
            {blocked && suggested.length > 0 && (
              <Button
                onClick={() => mutation.mutate({ includeSuggestions: true })}
                disabled={mutation.isPending}
              >
                Also classify these {suggested.length} endpoints as {environment ?? 'Unclassified'}
              </Button>
            )}
            {grants && <GrantsAcknowledgmentRefusal details={grants} catalog={catalog.data} />}
            {grants && (
              <Checkbox
                checked={acknowledged}
                onChange={(_, data) => setAcknowledged(Boolean(data.checked))}
                label="I understand these active grants will carry to the new environment."
              />
            )}
            {mutation.data && (
              <MessageBar intent="success">
                <MessageBarBody>
                  <MessageBarTitle>Environment updated</MessageBarTitle>
                  {mutation.data.results
                    .map(
                      (result) =>
                        `${result.resourceName}: ${result.status}${result.message ? ` — ${result.message}` : ''}`,
                    )
                    .join('; ')}
                  {mutation.data.warnings.length > 0
                    ? ` Warnings: ${mutation.data.warnings.join('; ')}`
                    : ''}
                </MessageBarBody>
              </MessageBar>
            )}
            {message && !blocked && !grants && (
              <MessageBar intent="error" role="alert">
                <MessageBarBody>{message}</MessageBarBody>
              </MessageBar>
            )}
          </DialogContent>
          <DialogActions>
            <Button disabled={mutation.isPending} onClick={onClose}>
              Close
            </Button>
            <Button
              appearance="primary"
              disabled={mutation.isPending || (Boolean(grants) && !acknowledged)}
              onClick={() =>
                mutation.mutate({
                  includeSuggestions: suggested.length > 0 && Boolean(grants),
                  acknowledgeGrants: acknowledged,
                })
              }
            >
              {mutation.isPending ? 'Saving…' : grants ? 'Confirm and save' : 'Save'}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
