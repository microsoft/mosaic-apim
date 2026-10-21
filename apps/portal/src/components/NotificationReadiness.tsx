import { Badge, Button } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { usePortalApi } from '../api'
import { capabilityStatus, safeRemediationPath } from '../notification-format'
import { useNotificationKeys } from '../notification-queries'
import type { CapabilityCheck } from '../notification-types'
import { EmptyState, ErrorState, Loading } from './AsyncState'

const ownerLabels = {
  mosaicAdmin: 'MOSAIC administrator', azureAdmin: 'Azure administrator',
  entraAdmin: 'Entra administrator', teamsAdmin: 'Teams administrator', user: 'You',
}

const featureLabels: Record<string, string> = {
  inApp: 'In-app inbox', email: 'Email', teamsPersonal: 'Private Teams',
  teamsBotConfiguration: 'Bot identity', teamsBotOAuth: 'Runtime bot access',
  teamsPersonalInstallation: 'Your personal installation',
  teamsPersonalPreferences: 'Private channel and preferences', teamsTest: 'Last explicit test',
}

export function NotificationCapabilityChecks({ checks }: { checks: CapabilityCheck[] }) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(interval)
  }, [])
  return <ul className="readiness-list">
        {checks.map((check) => {
          const status = capabilityStatus(check, now)
          const path = safeRemediationPath(check.remediation?.path ?? null)
          return <li key={`${check.feature}:${check.scope}`}>
            <div className="notification-item-heading">
              <h3>{featureLabels[check.feature] ?? check.feature.replace(/([a-z])([A-Z])/g, '$1 $2')}</h3>
              <Badge appearance="outline" color={status === 'Verified' ? 'success' : status === 'Failed' ? 'danger' : 'informative'}>{status}</Badge>
            </div>
            <p>{check.reason ?? 'No readiness explanation is available.'}</p>
            {check.evidence.length > 0 && <ul>{check.evidence.map((evidence, index) => <li key={index}>{evidence}</li>)}</ul>}
            {check.checkedAt && <p className="notification-meta">Checked <time dateTime={check.checkedAt}>{new Date(check.checkedAt).toLocaleString()}</time>
              {check.freshUntil && <> · Evidence expires {new Date(check.freshUntil).toLocaleString()}</>}</p>}
            {check.lastTestAt && <p className="notification-meta">Last test: {new Date(check.lastTestAt).toLocaleString()}. Provider acceptance is not confirmed delivery.</p>}
            {check.remediation && status !== 'Verified' && <div className="notification-remediation">
              <strong>{ownerLabels[check.remediation.owner]}</strong>
              <p>{check.remediation.action}</p>
              {path && (check.remediation.owner === 'user' || path.startsWith('https:')
                ? <a href={path} {...(path.startsWith('https:') ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>Open remediation guidance</a>
                : <p>Administrator view: <code>{path}</code></p>)}
              {check.remediation.command && <pre><code>{check.remediation.command}</code></pre>}
            </div>}
          </li>
        })}
      </ul>
}

export function NotificationReadiness() {
  const api = usePortalApi()
  const keys = useNotificationKeys()
  const health = useQuery({ queryKey: keys.health, queryFn: api.getNotificationHealth, staleTime: 0 })
  return (
    <section className="notification-section" aria-labelledby="notification-readiness-heading">
      <div className="notification-section-heading">
        <h2 id="notification-readiness-heading">Delivery readiness</h2>
        <Button onClick={() => void health.refetch()} disabled={health.isFetching}>Refresh readiness</Button>
      </div>
      <p>Preferences are saved intent, not proof of delivery. Email needs your resolved directory contact.
        Private Teams requires the MOSAIC personal app installed for your signed-in account.
        Shared administrator channels are never a fallback.</p>
      {health.isPending && <Loading label="Checking notification readiness" />}
      {health.isError && <ErrorState error={health.error} />}
      {health.isSuccess && health.data.length === 0 &&
        <EmptyState title="Readiness unavailable">No capability evidence was returned. Ask your MOSAIC administrator to check notification setup.</EmptyState>}
      {health.isSuccess && health.data.length > 0 && <NotificationCapabilityChecks checks={health.data} />}
    </section>
  )
}
