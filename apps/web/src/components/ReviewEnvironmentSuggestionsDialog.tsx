import {
  Button,
  AnnounceProvider,
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
import { type RefObject, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import {
  environmentSuggestionsQueryKey,
  grantsAcknowledgmentDetails,
  invalidateEnvironmentQueries,
  publicationsBlockedDetails,
  useEnvironmentCatalog,
} from '../environments'
import { environmentAssignmentResourceNoun } from '../labels'
import type { EnvironmentAssignment, EnvironmentAssignmentResult } from '../types'
import { EnvironmentPicker } from './EnvironmentPicker'
import { GrantsAcknowledgmentRefusal, PublicationsBlockedRefusal } from './EnvironmentRefusal'
import { ErrorState } from './AsyncState'
import { useFocusAwareAnnounce } from './useFocusAwareAnnounce'
import { useDialogFocusReturn } from './useDialogFocusReturn'
import styles from './EnvironmentComponents.module.css'

interface ReviewEnvironmentSuggestionsProps {
  open: boolean
  onClose: () => void
  fallbackFocusRef?: RefObject<HTMLElement | null>
}

export function ReviewEnvironmentSuggestionsDialog(props: ReviewEnvironmentSuggestionsProps) {
  const [session, setSession] = useState({ open: props.open, key: 0 })
  if (props.open && !session.open) setSession({ open: true, key: session.key + 1 })
  else if (!props.open && session.open) setSession({ ...session, open: false })
  return <ReviewEnvironmentSuggestionsSession key={session.key} {...props} />
}

function ReviewEnvironmentSuggestionsSession({
  open,
  onClose,
  fallbackFocusRef,
}: ReviewEnvironmentSuggestionsProps) {
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
  const surfaceRef = useRef<HTMLDivElement>(null)
  const outcomeRef = useRef<HTMLDivElement>(null)
  const announcement = useFocusAwareAnnounce(outcomeRef)
  const surfaceMotion = useDialogFocusReturn({ open, surfaceRef, fallbackRef: fallbackFocusRef })
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
    onMutate: () => setError(null),
    mutationFn: (payload: { extra?: EnvironmentAssignment[]; acknowledgeGrants?: boolean }) =>
      api.assignEnvironments({
        assignments: [...assignments(), ...(payload.extra ?? [])],
        acknowledgeGrants: payload.acknowledgeGrants,
      }),
    onSuccess: (result) => {
      setError(null)
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
  const blockedSuggestionNoun = environmentAssignmentResourceNoun(blocked?.suggestedAssignments ?? [])
  const message =
    error instanceof ApiError ? error.message : error instanceof Error ? error.message : null
  const outcomeKey = mutation.isSuccess || error ? mutation.submittedAt : null
  const shownOutcome = useRef<number | null>(null)
  useLayoutEffect(() => {
    const previous = shownOutcome.current
    shownOutcome.current = outcomeKey
    if (open && outcomeKey !== null && outcomeKey !== previous) outcomeRef.current?.focus()
  }, [open, outcomeKey])
  return (
    <Dialog
      open={open}
      surfaceMotion={surfaceMotion}
      onOpenChange={(_, data) => {
        if (!data.open && !mutation.isPending) onClose()
      }}
    >
      <DialogSurface ref={surfaceRef}>
        <DialogBody>
          <DialogTitle>Review environment suggestions</DialogTitle>
          <DialogContent className={styles.dialogContent}>
            {suggestions.isLoading && <Spinner label="Loading suggestions" />}
            {suggestions.isError && <ErrorState error={suggestions.error} />}
            {suggestions.isSuccess && rows.length === 0 && (
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
            <AnnounceProvider value={announcement}>
            {blocked && <div ref={outcomeRef} tabIndex={-1}><PublicationsBlockedRefusal details={blocked} catalog={catalog.data} /></div>}
            {blocked && blocked.suggestedAssignments.length > 0 && (
              <Button disabledFocusable={mutation.isPending} onClick={() => {
                if (!mutation.isPending) mutation.mutate({ extra: blocked.suggestedAssignments })
              }}>
                Also classify suggested {blockedSuggestionNoun}
              </Button>
            )}
            {grants && <div ref={outcomeRef} tabIndex={-1}><GrantsAcknowledgmentRefusal details={grants} catalog={catalog.data} /></div>}
            {grants && (
              <Checkbox
                checked={acknowledged}
                onChange={(_, data) => setAcknowledged(Boolean(data.checked))}
                label="I understand these active grants will carry to the new environment."
              />
            )}
            {mutation.data && (
              <div ref={outcomeRef} tabIndex={-1}><MessageBar key={mutation.submittedAt} intent="success" politeness="polite">
                <MessageBarBody>
                  {mutation.data.results
                    .map(
                      (result) =>
                        `${result.resourceName}: ${result.status}${result.message ? ` — ${result.message}` : ''}`,
                    )
                    .join('; ')}
                </MessageBarBody>
              </MessageBar></div>
            )}
            {message && !blocked && !grants && (
              <div ref={outcomeRef} tabIndex={-1}><MessageBar key={mutation.submittedAt} intent="error">
                <MessageBarBody>{message}</MessageBarBody>
              </MessageBar></div>
            )}
            </AnnounceProvider>
          </DialogContent>
          <DialogActions>
            <Button disabledFocusable={mutation.isPending} onClick={onClose}>
              Close
            </Button>
            <Button
              appearance="primary"
              disabled={
                assignments().length === 0 ||
                (Boolean(grants) && !acknowledged)
              }
              disabledFocusable={mutation.isPending}
              onClick={() => {
                if (!mutation.isPending && assignments().length > 0 && (!grants || acknowledged)) {
                  mutation.mutate({ acknowledgeGrants: acknowledged })
                }
              }}
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
