import { Badge, Button } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { usePortalApi } from '../api'
import { notificationCategories } from '../notification-format'
import { useNotificationKeys } from '../notification-queries'
import { EmptyState, ErrorState, Loading } from './AsyncState'
import { NotificationCapabilityChecks } from './NotificationReadiness'

const channelLabels = { inApp: 'In-app', email: 'Email', teamsPersonal: 'Private Teams' }

export function EffectiveNotificationPreferences() {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const preview = useQuery({ queryKey: [...keys.preferences, 'preview'], queryFn: api.getNotificationPreferencePreview })
  return <section className="notification-section" aria-labelledby="effective-preferences-heading">
    <div className="notification-section-heading">
      <h2 id="effective-preferences-heading">Effective saved preferences</h2>
      <Button onClick={() => void preview.refetch()} disabled={preview.isFetching}>Refresh effective preferences</Button>
    </div>
    <p>This preview uses your saved preferences, not unsaved edits. Availability and next scheduled delivery are estimates,
      rechecked before sending; neither is confirmation of delivery.</p>
    {preview.isPending && <Loading label="Loading effective preferences" />}
    {preview.isError && <ErrorState error={preview.error} />}
    {preview.isSuccess && <>
      {preview.data.channels.filter((channel) => channel.channel !== 'teamsChannel' && channel.mode !== 'off').length === 0 &&
        <EmptyState title="No saved channels are opted in">Enable a category and save your preferences to see its effective delivery choices.</EmptyState>}
      <ul className="readiness-list">
        {preview.data.channels.filter((channel) => channel.channel !== 'teamsChannel' && channel.mode !== 'off').map((channel) =>
          <li key={`${channel.category}:${channel.channel}`}>
            <div className="notification-item-heading">
              <h3>{notificationCategories.find((item) => item.value === channel.category)?.label} · {channelLabels[channel.channel as keyof typeof channelLabels]}</h3>
              <Badge appearance="outline">{channel.available ? 'Available for sending' : 'Unavailable'}</Badge>
            </div>
            <p>{channel.mode === 'digest' ? `Digests: ${channel.digestPeriods.join(', ')}` : 'Immediate'}</p>
            {channel.reasons.map((reason, index) => <p key={index}>{reason}</p>)}
            {channel.nextDeliveryAt && <p className="notification-meta">Next scheduled delivery: <time dateTime={channel.nextDeliveryAt}>
              {new Date(channel.nextDeliveryAt).toLocaleString()}</time></p>}
          </li>)}
      </ul>
      {preview.data.checks.length > 0 && <NotificationCapabilityChecks checks={preview.data.checks} />}
    </>}
  </section>
}
