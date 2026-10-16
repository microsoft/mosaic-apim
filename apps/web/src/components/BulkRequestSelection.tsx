import { Button, Checkbox, Text } from '@fluentui/react-components'
import { useState } from 'react'
import type { AccessRequest, Principal } from '../types'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './AdminGovernance.module.css'

export function BulkRequestSelection({ requests, principals, onBack, onReview }: { requests: AccessRequest[]; principals: Principal[]; onBack: () => void; onReview: (ids: string[]) => void }) {
  const [ids, setIds] = useState<string[]>([])
  const pending = requests.filter(r => r.state === 'pending')
  return <WorkspaceEditorFrame title="Select requests for bulk approval" onBack={onBack} onCancel={onBack}
    description="Select up to 100 explicit requests. Each request is previewed and approved independently; selecting all never means an unbounded server query."
    primaryAction={{ label: `Review ${ids.length} requests`, onClick: () => onReview(ids), disabled: !ids.length }}>
    <div className={styles.stack}><div className={styles.toolbar}>
      <Button onClick={() => setIds(pending.slice(0, 100).map(r => r.id))}>Select first {Math.min(pending.length, 100)} pending requests</Button>
      <Button disabled={!ids.length} onClick={() => setIds([])}>Clear selection</Button><Text>{ids.length} of 100 selected</Text>
    </div>
      {pending.length ? pending.map(r => <div className={styles.row} key={r.id}>
        <Checkbox checked={ids.includes(r.id)} disabled={ids.length >= 100 && !ids.includes(r.id)}
          label={`${principals.find(p => p.id === r.requesterPrincipalId || p.objectId === r.requesterObjectId)?.label ?? 'Requester name unavailable'} · ${r.resourceSummary?.displayName ?? r.resourceSnapshot?.displayName ?? 'Resource unavailable'}`}
          onChange={(_, d) => setIds(current => d.checked ? [...current, r.id] : current.filter(id => id !== r.id))} />
        <Text>{r.costCenter?.name ?? 'Requester default'} · {r.resourceSummary?.environment ?? 'Unclassified'} · {new Date(r.createdAt).toLocaleString()}</Text>
      </div>) : <Text>No pending requests in the current queue. Return to requests and refresh.</Text>}
    </div>
  </WorkspaceEditorFrame>
}
