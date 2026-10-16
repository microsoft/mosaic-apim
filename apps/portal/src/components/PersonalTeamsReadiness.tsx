import { Badge, Button } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { usePortalApi } from '../api'
import { useNotificationKeys } from '../notification-queries'
import { ErrorState, Loading } from './AsyncState'
import { NotificationCapabilityChecks } from './NotificationReadiness'

const stateLabels = { notInstalled: 'Not installed', installed: 'Installed', uninstalled: 'Uninstalled', blocked: 'Blocked' }

export function PersonalTeamsReadiness() {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const client = useQueryClient()
  const status = useQuery({ queryKey: [...keys.health, 'teams'], queryFn: api.getPersonalTeamsStatus })
  const test = useMutation({
    mutationFn: api.testPersonalTeams,
    onSuccess: () => { void client.invalidateQueries({ queryKey: keys.health }) },
  })
  return <section className="notification-section" aria-labelledby="personal-teams-heading">
    <div className="notification-section-heading">
      <h2 id="personal-teams-heading">Private Teams app</h2>
      <Button disabled={status.isFetching || test.isPending} onClick={() => { test.reset(); void status.refetch() }}>Refresh personal app status</Button>
    </div>
    <p>Only your authenticated personal installation can receive this test. No conversation ID or destination is accepted.
      Installing the app and opting in are separate from a functional delivery test.</p>
    {status.isPending && <Loading label="Loading your personal Teams app status" />}
    {status.isError && <ErrorState error={status.error} />}
    {status.isSuccess && <>
      <div className="notification-actions">
        <Badge appearance="outline">{stateLabels[status.data.state]}</Badge>
        {!status.data.enabled && <span>Private Teams is disabled by an administrator.</span>}
      </div>
      {status.data.checks.length > 0 && <NotificationCapabilityChecks checks={status.data.checks} />}
      {status.data.state !== 'installed' && <p>Install or unblock the approved MOSAIC personal app from your organization’s Teams catalog,
        then refresh this status. Ask your Teams administrator if the app is unavailable.</p>}
      <Button disabled={!status.data.enabled || status.data.state !== 'installed' || test.isPending}
        onClick={() => test.mutate()}>{test.isPending ? 'Sending test…' : 'Send a test to my private Teams'}</Button>
    </>}
    {test.isError && <p role="alert" className="form-error">The private Teams test failed. {test.error.message} Review the readiness guidance, then retry.</p>}
    {test.isSuccess && <div role="status" className="notification-test-result">
      <p>{test.data.outcome === 'confirmedDelivered' && test.data.confirmedDelivered
        ? 'Delivery was confirmed.'
        : test.data.outcome === 'providerAccepted' ? 'Teams accepted the test request; delivery is not confirmed.'
          : 'The test did not confirm delivery.'}</p>
      <p>{test.data.message}</p>
      {test.data.outcome === 'unknownOutcome' && <p>Check Teams before sending another test to avoid duplicate messages.</p>}
      <p className="notification-meta">Tested {new Date(test.data.testedAt).toLocaleString()}. Check your private Teams chat for receipt.</p>
    </div>}
  </section>
}
