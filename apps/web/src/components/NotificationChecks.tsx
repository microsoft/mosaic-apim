import { Badge, Subtitle2, Text } from '@fluentui/react-components'
import { useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import type { CapabilityCheck } from '../admin-types'
import styles from './AdminGovernance.module.css'

const labels: Record<string, string> = {
  emailConfiguration: 'Saved sender mailbox',
  emailIdentity: 'Runtime Graph identity',
  emailMailbox: 'Mailbox directory lookup',
  emailSendAuthorization: 'Mailbox send authorization',
  teamsWorkflowConfiguration: 'Shared destination configuration',
  teamsWorkflowAccess: 'Secret and endpoint access',
  teamsWorkflowRouting: 'Normal notification routing',
  teamsWorkflowExecution: 'Workflow execution and receipt',
  teamsBotConfiguration: 'Private bot configuration',
  teamsBotOAuth: 'Bot runtime authentication',
  teamsPersonalInstallation: 'Your personal installation',
  teamsPersonalPreferences: 'Your channel opt-in',
  teamsTest: 'Last explicit send test',
}
const owners: Record<string, string> = {
  mosaicAdmin: 'MOSAIC administrator', azureAdmin: 'Azure administrator',
  entraAdmin: 'Microsoft 365 / Entra administrator', teamsAdmin: 'Teams administrator', user: 'You',
}

export function CapabilityEvidence({ check }: { check: CapabilityCheck }) {
  const [, refresh] = useState(0)
  useEffect(() => {
    if (!check.freshUntil) return
    const interval = window.setInterval(() => refresh(value => value + 1), 30_000)
    return () => window.clearInterval(interval)
  }, [check.freshUntil])
  const expired = Boolean(check.freshUntil && new Date(check.freshUntil).getTime() <= Date.now())
  const verified = check.state === 'verified' && Boolean(check.checkedAt) && check.evidence.length > 0 && !expired
  const title = labels[check.feature] ?? check.feature
  return <section className={styles.evidenceSection} aria-label={`${title} readiness`}>
    <div className={styles.toolbar}><Subtitle2 as="h3">{title}</Subtitle2>
      <Badge color={verified ? 'success' : check.state === 'failed' ? 'danger' : 'warning'}>
        {expired ? 'Stale evidence' : check.state === 'verified' && !verified ? 'Unverified' : check.state === 'needsAction' ? 'Action needed' : check.state}
      </Badge>
    </div>
    <Text>{check.reason ?? 'No reason was supplied.'}</Text>
    <Text size={200} className={styles.muted}>
      {check.required ? 'Required' : 'Optional'} · Checked: {check.checkedAt ? new Date(check.checkedAt).toLocaleString() : 'Not checked'}
      {check.lastTestAt && ` · Last send test: ${new Date(check.lastTestAt).toLocaleString()}`}
    </Text>
    {check.evidence.length > 0 && <ul>{check.evidence.map((e, i) => <li key={i} className={styles.evidence}>{e}</li>)}</ul>}
    {check.remediation && !verified && <div className={styles.stack}>
      {check.remediation.action !== check.reason && <Text>{check.remediation.action}</Text>}
      <Text size={200} className={styles.muted}>Who can fix it: {owners[check.remediation.owner] ?? check.remediation.owner}</Text>
      {check.remediation.path?.startsWith('/') && !check.remediation.path.startsWith('//') && <RouterLink to={check.remediation.path}>Open setup</RouterLink>}
      {check.remediation.command && <code className={styles.evidence}>{check.remediation.command}</code>}
    </div>}
  </section>
}
