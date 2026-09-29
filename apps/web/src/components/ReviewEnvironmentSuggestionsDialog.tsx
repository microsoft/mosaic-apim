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
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import {
  environmentSuggestionsQueryKey,
  grantsAcknowledgmentDetails,
  invalidateEnvironmentQueries,
  publicationsBlockedDetails,
  useEnvironmentCatalog,
} from '../environments'
import type { EnvironmentAssignment, EnvironmentAssignmentResult } from '../types'
import { EnvironmentPicker } from './EnvironmentPicker'
import { GrantsAcknowledgmentRefusal, PublicationsBlockedRefusal } from './EnvironmentRefusal'
import styles from './EnvironmentComponents.module.css'

export function ReviewEnvironmentSuggestionsDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const catalog = useEnvironmentCatalog()
  const suggestions = useQuery({
    queryKey: environmentSuggestionsQueryKey,
    queryFn: () => api.listEnvironmentSuggestions(),
    enabled: open,
  })
  const [selected, setSelected] = useState<Record<string, boolean>>({})
  const [environments, setEnvironments] = useState<Record<string, string | null>>({})
  const [error, setError] = useState<unknown>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const rows = useMemo(() => suggestions.data?.items ?? [], [suggestions.data?.items])
  useEffect(() => {
    if (rows.length === 0) return
    setSelected((current) =>
      Object.fromEntries(
        rows.map((row) => [
          row.resourceId,
          current[row.resourceId] ?? Boolean(row.suggestedEnvironment),
        ]),
      ),
    )
    setEnvironments((current) =>
      Object.fromEntries(
        rows.map((row) => [row.resourceId, current[row.resourceId] ?? row.suggestedEnvironment]),
      ),
    )
  }, [rows])
  const assignments = (): EnvironmentAssignment[] =>
    rows.flatMap((row) =>
      selected[row.resourceId]
        ? [
            {
              resourceKind: row.resourceKind,
              resourceId: row.resourceId,
              environment: environments[row.resourceId] ?? null,
            },
          ]
        : [],
    )
  const mutation = useMutation({
    mutationFn: (payload: { extra?: EnvironmentAssignment[]; acknowledgeGrants?: boolean }) =>
      api.assignEnvironments({
        assignments: [...assignments(), ...(payload.extra ?? [])],
        acknowledgeGrants: payload.acknowledgeGrants,
      }),
    onSuccess: (result) => {
      invalidateEnvironmentQueries(queryClient)
      queryClient.setQueryData<EnvironmentAssignmentResult>(
        ['last-environment-assignment-result'],
        result,
      )
    },
    onError: (err) => setError(err),
  })
  const blocked = publicationsBlockedDetails(error)
  const grants = grantsAcknowledgmentDetails(error)
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
          <DialogTitle>Review environment suggestions</DialogTitle>
          <DialogContent className={styles.dialogContent}>
            {suggestions.isLoading && <Spinner label="Loading suggestions" />}
            {!suggestions.isLoading && rows.length === 0 && (
              <Text>No unclassified resources need review.</Text>
            )}
            {rows.length > 0 && (
              <Table aria-label="Environment suggestions">
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Apply</TableHeaderCell>
                    <TableHeaderCell>Resource</TableHeaderCell>
                    <TableHeaderCell>Environment</TableHeaderCell>
                    <TableHeaderCell>Evidence</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={row.resourceId}>
                      <TableCell>
                        <Checkbox
                          aria-label={`Select ${row.resourceName}`}
                          checked={selected[row.resourceId] ?? false}
                          onChange={(_, data) =>
                            setSelected((current) => ({
                              ...current,
                              [row.resourceId]: Boolean(data.checked),
                            }))
                          }
                        />
                      </TableCell>
                      <TableCell>
                        {row.resourceKind}: {row.resourceName}
                      </TableCell>
                      <TableCell>
                        <EnvironmentPicker
                          catalog={catalog.data}
                          label={`Environment for ${row.resourceName}`}
                          value={environments[row.resourceId] ?? null}
                          onChange={(value) =>
                            setEnvironments((current) => ({ ...current, [row.resourceId]: value }))
                          }
                          allowUnclassified
                        />
                      </TableCell>
                      <TableCell>{row.evidence ?? 'No suggestion'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {blocked && <PublicationsBlockedRefusal details={blocked} catalog={catalog.data} />}
            {blocked && blocked.suggestedAssignments.length > 0 && (
              <Button onClick={() => mutation.mutate({ extra: blocked.suggestedAssignments })}>
                Also classify suggested endpoints
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
                  {mutation.data.results
                    .map(
                      (result) =>
                        `${result.resourceName}: ${result.status}${result.message ? ` — ${result.message}` : ''}`,
                    )
                    .join('; ')}
                </MessageBarBody>
              </MessageBar>
            )}
            {message && !blocked && !grants && (
              <MessageBar intent="error">
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
              disabled={
                mutation.isPending ||
                assignments().length === 0 ||
                (Boolean(grants) && !acknowledged)
              }
              onClick={() => mutation.mutate({ acknowledgeGrants: acknowledged })}
            >
              {mutation.isPending
                ? 'Submitting…'
                : grants
                  ? 'Confirm and submit'
                  : 'Submit selections'}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
