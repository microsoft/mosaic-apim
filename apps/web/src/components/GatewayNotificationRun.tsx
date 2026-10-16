import { Badge, Button, Text, Title3 } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { ErrorState, Loading } from './AsyncState'
import styles from './AdminGovernance.module.css'

export function GatewayNotificationRun({ gatewayId }: { gatewayId: string }) {
  const api = useMosaicApi()
  const [params, setParams] = useSearchParams()
  const runId = params.get('syncRun')
  const run = useQuery({
    queryKey: ['gateway-notification-run', gatewayId, runId],
    queryFn: async () => {
      const exact = await api.getSyncRun(gatewayId, runId!)
      if (exact.id !== runId || exact.gatewayId !== gatewayId) throw new Error('The returned run does not match the notified gateway and run. No other run is substituted.')
      return exact
    },
    enabled: Boolean(runId),
    retry: false,
  })
  if (!runId) return null
  return <section className={styles.section} aria-label="Notified gateway sync run">
    <div className={styles.toolbar}><Title3 as="h2">Notified gateway sync run</Title3><Button onClick={() => setParams(current => { const next = new URLSearchParams(current); next.delete('syncRun'); return next })}>Close run details</Button></div>
    <Text>This is the exact historical run referenced by the notification, not a fresh connectivity test.</Text>
    {run.isPending && <Loading label="Loading notified gateway sync run" />}
    {run.isError && <><ErrorState error={run.error} /><Button onClick={() => void run.refetch()}>Retry run details</Button></>}
    {run.data && <><Badge appearance="outline">{run.data.status}</Badge><dl className={styles.facts}>
      <div><dt>Run ID</dt><dd>{run.data.id}</dd></div><div><dt>Started</dt><dd>{run.data.startedAt}</dd></div><div><dt>Completed</dt><dd>{run.data.completedAt ?? 'No completion recorded'}</dd></div>
    </dl>{run.data.errors.map((error, index) => <Text key={index}>{error}</Text>)}
      {!run.data.errors.length && <Text>No errors were recorded on this run.</Text>}</>}
  </section>
}
