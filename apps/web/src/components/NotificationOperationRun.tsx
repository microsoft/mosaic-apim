import { Badge, Button, Text, Title3 } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { ErrorState, Loading } from './AsyncState'
import styles from './AdminGovernance.module.css'

function CloseRun({ parameter }: { parameter: 'runId' | 'syncRun' }) {
  const [, setParams] = useSearchParams()
  return <Button onClick={() => setParams(current => {
    const next = new URLSearchParams(current)
    next.delete(parameter)
    return next
  })}>Close notified run</Button>
}

export function PublishNotificationRun({ publicationId, target }: { publicationId: string; target: 'model' | 'mcp' | 'pool' }) {
  const api = useMosaicApi()
  const [params] = useSearchParams()
  const runId = params.get('runId')
  const run = useQuery({
    queryKey: ['notification-publish-run', target, publicationId, runId],
    queryFn: async () => {
      const exact = target === 'mcp'
        ? await api.getMcpPublishRun(publicationId, runId!)
        : target === 'pool'
          ? await api.getModelPoolRun(publicationId, runId!)
          : await api.getPublishRun(publicationId, runId!)
      if (exact.id !== runId || exact.publicationId !== publicationId) throw new Error('The returned run does not match the notified publication and run. No other run is substituted.')
      return exact
    },
    enabled: Boolean(runId),
    retry: false,
  })
  if (!runId) return null
  return <section className={styles.evidenceSection} aria-label="Notified publication run">
    <div className={styles.toolbar}><Title3 as="h3">Notified publication run</Title3><CloseRun parameter="runId" /></div>
    <Text>This exact historical {target === 'pool' ? 'pool' : target === 'mcp' ? 'MCP publication' : 'model publication'} run is the notification's evidence. It is not a new apply or proof of current runtime readiness.</Text>
    {run.isPending && <Loading label="Loading exact notified publication run" />}
    {run.isError && <><ErrorState error={run.error} /><Button onClick={() => void run.refetch()}>Retry notified publication run</Button></>}
    {run.data && <><Badge appearance="outline">{run.data.status}</Badge>
      <dl className={styles.facts}>
        <div><dt>Run ID</dt><dd>{run.data.id}</dd></div>
        <div><dt>Publication</dt><dd>{run.data.publicationId}</dd></div>
        <div><dt>Plan</dt><dd>{run.data.planId}</dd></div>
        <div><dt>Started</dt><dd>{run.data.startedAt}</dd></div>
        <div><dt>Completed</dt><dd>{run.data.completedAt ?? 'No completion recorded'}</dd></div>
        <div><dt>Rollback recorded</dt><dd>{run.data.rolledBack ? 'Yes' : 'No'}</dd></div>
      </dl>
      {run.data.errors.map((error, i) => <Text key={i}>{error}</Text>)}
      {!run.data.errors.length && <Text>No errors were recorded on this run.</Text>}
      {run.data.steps.length > 0 && <div className={styles.scroll}><table className={styles.table}>
        <caption>Recorded run steps</caption><thead><tr><th>Resource</th><th>Action</th><th>Status</th><th>Error</th></tr></thead>
        <tbody>{run.data.steps.map((step, i) => <tr key={`${step.resourceId}-${i}`}><th scope="row">{step.name}</th><td>{step.action}</td><td>{step.status}</td><td>{step.error ?? 'None recorded'}</td></tr>)}</tbody>
      </table></div>}
    </>}
  </section>
}

export function EndpointNotificationRun({ endpointId, kind }: { endpointId: string; kind: 'model' | 'mcp' }) {
  const api = useMosaicApi()
  const [params] = useSearchParams()
  const runId = params.get('syncRun')
  const run = useQuery({
    queryKey: ['notification-endpoint-run', kind, endpointId, runId],
    queryFn: async () => {
      const exact = kind === 'model' ? await api.getModelEndpointSyncRun(endpointId, runId!) : await api.getMcpEndpointSyncRun(endpointId, runId!)
      if (exact.id !== runId || exact.endpointId !== endpointId) throw new Error('The returned run does not match the notified endpoint and run. No other run is substituted.')
      return exact
    },
    enabled: Boolean(runId),
    retry: false,
  })
  if (!runId) return null
  return <section className={styles.evidenceSection} aria-label="Notified endpoint sync run">
    <div className={styles.toolbar}><Title3 as="h3">Notified endpoint sync run</Title3><CloseRun parameter="syncRun" /></div>
    <Text>This is the exact historical {kind === 'mcp' ? 'MCP' : 'model'} endpoint sync run, not a fresh connectivity or discovery test.</Text>
    {run.isPending && <Loading label="Loading exact notified endpoint sync run" />}
    {run.isError && <><ErrorState error={run.error} /><Button onClick={() => void run.refetch()}>Retry notified endpoint run</Button></>}
    {run.data && <><Badge appearance="outline">{run.data.status}</Badge>
      <dl className={styles.facts}>
        <div><dt>Run ID</dt><dd>{run.data.id}</dd></div><div><dt>Endpoint</dt><dd>{run.data.endpointId}</dd></div>
        <div><dt>Started</dt><dd>{run.data.startedAt}</dd></div><div><dt>Completed</dt><dd>{run.data.completedAt ?? 'No completion recorded'}</dd></div>
      </dl>
      {run.data.errors.map((error, i) => <Text key={i}>{error}</Text>)}
      {!run.data.errors.length && <Text>No errors were recorded on this run.</Text>}
    </>}
  </section>
}
