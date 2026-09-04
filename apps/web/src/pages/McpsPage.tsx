import {
  Badge,
  Button,
  Card,
  MessageBar,
  MessageBarBody,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Title3,
} from '@fluentui/react-components'
import { AddRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { ImportFromGatewayDialog } from '../components/ImportFromGatewayDialog'
import { PageHeader } from '../components/PageHeader'
import type { CatalogVisibility, Gateway, McpServer } from '../types'
import styles from './McpsPage.module.css'

function transportLabel(server: McpServer): string {
  if (server.kind === 'passthrough') {
    return server.transportType === 'unknown'
      ? 'Passthrough'
      : `Passthrough · ${server.transportType === 'sse' ? 'SSE' : 'Streamable HTTP'}`
  }
  return 'Backed by REST APIs'
}

export function McpsPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const hasConsumedImportQueryRef = useRef(false)
  const [importOpen, setImportOpen] = useState(false)
  const [banner, setBanner] = useState<string | null>(null)

  const requestedGatewayId = useMemo(
    () => new URLSearchParams(location.search).get('import'),
    [location.search],
  )

  const servers = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => api.listMcpServers(),
  })
  const gateways = useQuery({
    queryKey: ['gateways'],
    queryFn: () => api.listGateways(),
  })

  const gatewayNames = useMemo(() => {
    const map = new Map<string, Gateway>()
    for (const gateway of gateways.data ?? []) {
      map.set(gateway.id, gateway)
    }
    return map
  }, [gateways.data])

  useEffect(() => {
    if (requestedGatewayId && !hasConsumedImportQueryRef.current) {
      hasConsumedImportQueryRef.current = true
      setImportOpen(true)
    }
    if (!requestedGatewayId) {
      hasConsumedImportQueryRef.current = false
    }
  }, [requestedGatewayId])

  function clearImportQuery() {
    const params = new URLSearchParams(location.search)
    if (!params.has('import')) {
      return
    }
    params.delete('import')
    const nextSearch = params.toString()
    navigate(
      { pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '' },
      { replace: true },
    )
  }

  const removeMutation = useMutation({
    mutationFn: (id: string) => api.deleteMcpServer(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      setBanner('Stopped governing that MCP server. Nothing changed in API Management.')
    },
  })

  const visibilityMutation = useMutation({
    mutationFn: ({ id, visibility }: { id: string; visibility: CatalogVisibility }) =>
      api.updateMcpServerCatalog(id, { visibility }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      setBanner('Updated who can discover this MCP server in the portal catalog.')
    },
  })

  return (
    <section className={styles.page}>
      <PageHeader
        title="MCPs"
        description="Model Context Protocol servers hosted by your API Management gateways that MOSAIC governs."
        source="live"
        actions={
          <Button appearance="primary" icon={<AddRegular />} onClick={() => setImportOpen(true)}>
            Import from gateway
          </Button>
        }
      />

      {banner && (
        <MessageBar intent="success">
          <MessageBarBody>{banner}</MessageBarBody>
        </MessageBar>
      )}
      {removeMutation.isError && <ErrorState error={removeMutation.error} />}

      <Card className={styles.panel}>
        <div className={styles.panelHeader}>
          <div className={styles.panelHeading}>
            <Title3 as="h2">Imported MCP servers</Title3>
            <Text size={200}>
              Importing records that MOSAIC governs a server. It never creates or changes one in
              Azure.
            </Text>
          </div>
        </div>

        {servers.isPending && <Loading label="Loading MCP servers..." />}
        {servers.isError && <ErrorState error={servers.error} />}
        {servers.isSuccess &&
          (servers.data.length === 0 ? (
            <EmptyState title="No MCP servers imported yet">
              Import the MCP servers you want MOSAIC to govern from a synchronised gateway. MCP
              servers require an API Management service on a management API version that supports
              them.
            </EmptyState>
          ) : (
            <Table aria-label="Imported MCP servers">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Server</TableHeaderCell>
                  <TableHeaderCell>Transport</TableHeaderCell>
                  <TableHeaderCell>Tools</TableHeaderCell>
                  <TableHeaderCell>Gateway</TableHeaderCell>
                  <TableHeaderCell>Catalog</TableHeaderCell>
                  <TableHeaderCell>Actions</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {servers.data.map((server) => (
                  <TableRow key={server.id}>
                    <TableCell>
                      <div className={styles.nameCell}>
                        <Text weight="semibold">{server.displayName}</Text>
                        <Text size={200} className={styles.pathText}>
                          /{server.path}
                        </Text>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge appearance="tint" className={styles.transportBadge}>
                        {transportLabel(server)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className={styles.nameCell}>
                        <Text size={200}>
                          {server.toolCount} tool{server.toolCount === 1 ? '' : 's'}
                        </Text>
                        {server.tools.length > 0 && (
                          <ul className={styles.toolList}>
                            {server.tools.slice(0, 3).map((tool) => (
                              <li key={tool.name}>
                                <Text size={200}>{tool.displayName}</Text>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Link className={styles.gatewayLink} to={`/gateways/${server.gatewayId}`}>
                        {gatewayNames.get(server.gatewayId)?.name ?? server.gatewayId}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Select
                        aria-label={`Catalog visibility for ${server.displayName}`}
                        value={server.visibility}
                        disabled={visibilityMutation.isPending}
                        onChange={(_, data) =>
                          visibilityMutation.mutate({
                            id: server.id,
                            visibility: data.value as CatalogVisibility,
                          })
                        }
                      >
                        <option value="catalog">Discoverable</option>
                        <option value="private">Entitled users only</option>
                      </Select>
                    </TableCell>
                    <TableCell>
                      <div className={styles.rowActions}>
                        <Button
                          appearance="subtle"
                          disabled={removeMutation.isPending}
                          onClick={() => removeMutation.mutate(server.id)}
                        >
                          Remove
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ))}
      </Card>

      <ImportFromGatewayDialog
        kind="mcpServers"
        open={importOpen}
        initialGatewayId={requestedGatewayId}
        onClose={() => {
          setImportOpen(false)
          clearImportQuery()
        }}
        onImported={(count) =>
          setBanner(
            `Imported ${count} MCP server${count === 1 ? '' : 's'}. MOSAIC recorded them as ` +
              'governed; API Management is unchanged.',
          )
        }
      />
    </section>
  )
}
