import {
  Badge,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMosaicApi } from '../api'
import { AI_KIND_LABELS } from '../labels'
import type { Gateway } from '../types'
import { ErrorState, Loading } from './AsyncState'
import styles from './ImportFromGatewayDialog.module.css'

export type ImportKind = 'apis' | 'mcpServers'

/** The subset of a candidate this dialog renders, shared by both resource kinds. */
interface Candidate {
  apiName: string
  displayName: string
  path: string
  recommended: boolean
  alreadyImported: boolean
  /** Right-hand detail column: an AI kind for APIs, a transport for MCP servers. */
  detail: string
  signals: string[]
}

const COPY: Record<
  ImportKind,
  { title: string; noun: string; plural: string; empty: string; unsynced: string }
> = {
  apis: {
    title: 'Import APIs from a gateway',
    noun: 'API',
    plural: 'APIs',
    empty: 'MOSAIC did not observe any APIs on this gateway.',
    unsynced:
      'This gateway has not been synchronised yet. Sync it so MOSAIC imports what the gateway ' +
      'actually contains.',
  },
  mcpServers: {
    title: 'Import MCP servers from a gateway',
    noun: 'MCP server',
    plural: 'MCP servers',
    empty: 'MOSAIC did not observe any MCP servers on this gateway.',
    unsynced:
      'This gateway has not been synchronised yet. Sync it so MOSAIC imports what the gateway ' +
      'actually contains.',
  },
}

export function ImportFromGatewayDialog({
  kind,
  open,
  initialGatewayId,
  onClose,
  onImported,
}: {
  kind: ImportKind
  open: boolean
  initialGatewayId?: string | null
  onClose: () => void
  onImported: (count: number) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const copy = COPY[kind]
  const [gatewayId, setGatewayId] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [touched, setTouched] = useState(false)
  const appliedInitialGatewayRef = useRef(false)

  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
    enabled: open,
  })

  const gatewayOptions: Gateway[] = useMemo(() => gateways.data ?? [], [gateways.data])

  useEffect(() => {
    if (!open) {
      appliedInitialGatewayRef.current = false
      return
    }
    // The requested gateway is applied once per opening. Re-applying it on every gateway refetch
    // would drag the administrator back after they picked a different one from the list.
    const requested =
      !appliedInitialGatewayRef.current &&
      initialGatewayId &&
      gatewayOptions.some((item) => item.id === initialGatewayId)
        ? initialGatewayId
        : null
    if (requested) {
      appliedInitialGatewayRef.current = true
    }
    // Prefer a gateway that has actually been synced; an unsynced one can import nothing.
    const fallback =
      gatewayOptions.find((item) => item.lastSyncedAt)?.id ?? gatewayOptions[0]?.id ?? ''
    setGatewayId((current) => requested ?? (current || fallback))
  }, [open, initialGatewayId, gatewayOptions])

  const candidatesQuery = useQuery({
    queryKey: ['import-candidates', kind, gatewayId],
    queryFn: async (): Promise<{ candidates: Candidate[]; unsupported: boolean }> => {
      if (kind === 'apis') {
        const result = await api.listImportableApis(gatewayId)
        return {
          unsupported: false,
          candidates: result.candidates.map((item) => ({
            apiName: item.apiName,
            displayName: item.displayName,
            path: item.path,
            recommended: item.recommended,
            alreadyImported: item.alreadyImported,
            detail: AI_KIND_LABELS[item.aiKind] || 'Not recognised',
            signals: item.aiSignals,
          })),
        }
      }
      const result = await api.listImportableMcpServers(gatewayId)
      return {
        unsupported: result.support === 'unavailable',
        candidates: result.candidates.map((item) => ({
          apiName: item.apiName,
          displayName: item.displayName,
          path: item.path,
          recommended: item.recommended,
          alreadyImported: item.alreadyImported,
          detail:
            item.kind === 'passthrough'
              ? `Passthrough · ${item.transportType}`
              : `${item.toolCount} tool${item.toolCount === 1 ? '' : 's'}`,
          signals: [],
        })),
      }
    },
    enabled: open && gatewayId !== '',
  })

  const candidates = useMemo(
    () => candidatesQuery.data?.candidates ?? [],
    [candidatesQuery.data],
  )

  useEffect(() => {
    // Detection pre-checks; it does not decide. Reset only when a fresh list arrives, so an
    // administrator's manual changes are never quietly undone.
    setSelected(
      new Set(
        candidates
          .filter((item) => item.recommended && !item.alreadyImported)
          .map((item) => item.apiName),
      ),
    )
    setTouched(false)
  }, [candidates])

  const importMutation = useMutation({
    mutationFn: async (apiNames: string[]) =>
      kind === 'apis'
        ? await api.importModelApis(gatewayId, apiNames)
        : await api.importMcpServers(gatewayId, apiNames),
    onSuccess: async (imported) => {
      await queryClient.invalidateQueries({
        queryKey: [kind === 'apis' ? 'model-apis' : 'mcp-servers'],
      })
      await queryClient.invalidateQueries({ queryKey: ['import-candidates'] })
      onImported(imported.length)
      onClose()
    },
  })

  function toggle(apiName: string, checked: boolean) {
    setTouched(true)
    setSelected((current) => {
      const next = new Set(current)
      if (checked) {
        next.add(apiName)
      } else {
        next.delete(apiName)
      }
      return next
    })
  }

  const selectable = candidates.filter((item) => !item.alreadyImported)
  const selectedCount = selected.size
  const selectedGateway = gatewayOptions.find((item) => item.id === gatewayId)
  const neverSynced = selectedGateway !== undefined && !selectedGateway.lastSyncedAt
  const unsupported = candidatesQuery.data?.unsupported === true

  return (
    <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogContent>
            <div className={styles.intro}>
              <Text>
                Importing records that MOSAIC governs {selectedCount === 1 ? 'this' : 'these'}{' '}
                {copy.plural}. It creates nothing in Azure and changes no API Management
                configuration.
              </Text>
            </div>

            <div className={styles.controls}>
              <Field label="Gateway" className={styles.gatewayField}>
                <Select
                  aria-label="Gateway to import from"
                  value={gatewayId}
                  onChange={(event) => setGatewayId(event.target.value)}
                >
                  {gatewayOptions.length === 0 && <option value="">No gateways registered</option>}
                  {gatewayOptions.map((gateway) => (
                    <option key={gateway.id} value={gateway.id}>
                      {gateway.name}
                      {gateway.environmentLabel ? ` · ${gateway.environmentLabel}` : ''}
                    </option>
                  ))}
                </Select>
              </Field>
              <div className={styles.selectionActions}>
                <Button
                  appearance="secondary"
                  disabled={selectable.length === 0}
                  onClick={() => {
                    setTouched(true)
                    setSelected(new Set(selectable.map((item) => item.apiName)))
                  }}
                >
                  Select all
                </Button>
                <Button
                  appearance="secondary"
                  disabled={selectedCount === 0}
                  onClick={() => {
                    setTouched(true)
                    setSelected(new Set())
                  }}
                >
                  Clear
                </Button>
              </div>
            </div>

            {gateways.isError && <ErrorState error={gateways.error} />}

            {neverSynced && (
              <MessageBar intent="warning">
                <MessageBarBody>
                  <MessageBarTitle>Not synchronised</MessageBarTitle>
                  {copy.unsynced}
                </MessageBarBody>
              </MessageBar>
            )}

            {unsupported && (
              <MessageBar intent="info">
                <MessageBarBody>
                  <MessageBarTitle>MCP servers are not available here</MessageBarTitle>
                  This API Management service does not support MCP servers on the management API
                  version they require.
                </MessageBarBody>
              </MessageBar>
            )}

            {importMutation.isError && <ErrorState error={importMutation.error} />}

            {candidatesQuery.isPending && gatewayId !== '' && (
              <Loading label={`Loading ${copy.plural}...`} />
            )}
            {candidatesQuery.isError && <ErrorState error={candidatesQuery.error} />}

            {candidatesQuery.isSuccess &&
              (candidates.length === 0 ? (
                <Text>{copy.empty}</Text>
              ) : (
                <div className={styles.tableScroll}>
                  <Table size="small" aria-label={`${copy.plural} available to import`}>
                    <TableHeader>
                      <TableRow>
                        <TableHeaderCell>Import</TableHeaderCell>
                        <TableHeaderCell>{copy.noun}</TableHeaderCell>
                        <TableHeaderCell>Detail</TableHeaderCell>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {candidates.map((candidate) => (
                        <TableRow key={candidate.apiName}>
                          <TableCell>
                            <Checkbox
                              aria-label={`Import ${candidate.displayName}`}
                              checked={
                                candidate.alreadyImported || selected.has(candidate.apiName)
                              }
                              disabled={candidate.alreadyImported}
                              onChange={(_, data) =>
                                toggle(candidate.apiName, data.checked === true)
                              }
                            />
                          </TableCell>
                          <TableCell>
                            <div className={styles.nameCell}>
                              <Text weight="semibold">{candidate.displayName}</Text>
                              <Text size={200} className={styles.pathText}>
                                /{candidate.path}
                              </Text>
                              {candidate.signals.length > 0 && (
                                <div className={styles.signals}>
                                  {candidate.signals.map((signal) => (
                                    <Text key={signal} size={200}>
                                      {signal}
                                    </Text>
                                  ))}
                                </div>
                              )}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className={styles.nameCell}>
                              <Text size={200}>{candidate.detail}</Text>
                              {candidate.alreadyImported ? (
                                <Badge appearance="tint" className={styles.importedBadge}>
                                  Already imported
                                </Badge>
                              ) : (
                                candidate.recommended && (
                                  <Badge appearance="tint" className={styles.recommendedBadge}>
                                    Recommended
                                  </Badge>
                                )
                              )}
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              ))}
          </DialogContent>
          <DialogActions>
            <Text size={200} className={styles.footerNote}>
              {selectedCount} selected
              {!touched && selectedCount > 0 ? ' by detection' : ''}
            </Text>
            <Button appearance="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button
              appearance="primary"
              disabled={selectedCount === 0 || importMutation.isPending}
              onClick={() => importMutation.mutate([...selected])}
            >
              {importMutation.isPending ? 'Importing...' : `Import ${selectedCount}`}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
