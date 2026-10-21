import { Badge, Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Text, Title3, useRestoreFocusTarget } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import { ErrorState, Loading } from './AsyncState'
import { CapabilityEvidence } from './NotificationChecks'
import styles from './AdminGovernance.module.css'

export function PersonalTeamsAccess() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const status = useQuery({ queryKey: ['admin-personal-teams'], queryFn: () => api.getMyTeamsStatus(), retry: false })
  const access = useMutation({ mutationFn: (_revision: string) => api.checkMyTeamsAccess() })
  const revision = status.data?.checks.find(check => check.feature === 'teamsTest')?.configurationRevision
    ?? `${status.data?.enabled}:${status.data?.state}`
  const accessRevision = access.data?.find(check => check.feature === 'teamsTest')?.configurationRevision
  const currentAccess = access.variables === revision && (!accessRevision || accessRevision === revision) ? access.data : undefined
  const [confirm, setConfirm] = useState(false)
  const test = useMutation({
    mutationFn: () => api.testMyTeamsConnection(),
    onSuccess: () => {
      access.reset()
      void client.invalidateQueries({ queryKey: ['admin-personal-teams'] })
      void client.invalidateQueries({ queryKey: ['notification-health'] })
    },
  })
  return <section className={styles.section} aria-label="My private Teams access">
    <div className={styles.toolbar}><Title3 as="h2">My private Teams access</Title3>
      {status.data && <Badge appearance="outline">{status.data.state === 'notInstalled' ? 'Not installed' : status.data.state}</Badge>}
      <Button disabled={!status.data || access.isPending || test.isPending} onClick={() => { access.mutate(revision); void status.refetch() }}>
        {access.isPending ? 'Checking access...' : 'Check my Teams access'}
      </Button>
    </div>
    <Text>Checks the runtime bot identity and your authenticated personal installation without sending. This view applies only to your administrator persona, not your portal preferences.</Text>
    {status.isPending && <Loading label="Loading private Teams status" />}
    {status.isError && <><ErrorState error={status.error} /><Button onClick={() => void status.refetch()}>Retry my Teams status</Button></>}
    {access.isPending && <Loading label="Checking private Teams access without sending" />}
    {access.isError && <ErrorState error={access.error} />}
    <div role="status" aria-live="polite">
      {(currentAccess ?? status.data?.checks)?.map(check => <CapabilityEvidence key={check.feature} check={check} />)}
    </div>
    {status.data?.lastTest && <Text>
      Last explicit test: {new Date(status.data.lastTest.testedAt).toLocaleString()} · {status.data.lastTest.outcome}. {status.data.lastTest.message}
    </Text>}
    <div><Button {...restoreFocus} disabled={!status.data?.enabled || status.data.state !== 'installed' || test.isPending}
      onClick={() => { test.reset(); setConfirm(true) }}>Send a test to my private Teams</Button></div>
    <Text>Install the approved MOSAIC personal app and opt in to the categories you want. Private messages are never redirected to a shared channel.</Text>
    <Dialog open={confirm} onOpenChange={(_, d) => { if (!test.isPending) setConfirm(d.open) }}>
      <DialogSurface><DialogBody><DialogTitle>Send a private Teams test?</DialogTitle><DialogContent>
        <Text>Sends a synthetic message only to your authenticated personal installation.</Text>
        {test.isPending && <Loading label="Sending private Teams test" />}
        {test.isError && <ErrorState error={test.error} />}
        {test.data && <div role="status" className={styles.stack}>
          <Text>{test.data.message}</Text><Text>{test.data.outcome} · {new Date(test.data.testedAt).toLocaleString()}</Text>
          {test.data.errorCode && <Text>Diagnostic: {test.data.errorCode}</Text>}
          <Text>Provider acceptance is not proof of receipt. Check your private Teams chat.</Text>
        </div>}
      </DialogContent><DialogActions>
        <Button disabled={test.isPending} onClick={() => setConfirm(false)}>Close</Button>
        <Button appearance="primary" disabled={test.isPending || test.isSuccess} onClick={() => test.mutate()}>Send private synthetic test</Button>
      </DialogActions></DialogBody></DialogSurface>
    </Dialog>
  </section>
}
