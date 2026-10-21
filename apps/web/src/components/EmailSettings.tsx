import { Badge, Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, MessageBar, MessageBarBody, Text, Title3, useRestoreFocusTarget } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useMosaicApi } from '../api'
import type { EmailSettingsUpdate } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { CapabilityEvidence } from './NotificationChecks'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './AdminGovernance.module.css'

const mailboxPattern = /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i

export function EmailSettings() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const restoreFocus = useRestoreFocusTarget()
  const settings = useQuery({ queryKey: ['email-settings'], queryFn: () => api.getEmailSettings(), retry: false })
  const [draft, setDraft] = useState<EmailSettingsUpdate | null>(null)
  const [testTo, setTestTo] = useState('')
  const [confirmTest, setConfirmTest] = useState(false)
  const checks = useMutation({ mutationFn: (_revision: number) => api.checkEmailAccess() })
  const save = useMutation({
    mutationFn: (body: EmailSettingsUpdate) => api.saveEmailSettings(body),
    onSuccess: value => {
      client.setQueryData(['email-settings'], value)
      void client.invalidateQueries({ queryKey: ['notification-health'] })
      void client.invalidateQueries({ queryKey: ['budgets'] })
      checks.reset()
      setDraft(null)
    },
  })
  const test = useMutation({
    mutationFn: (to: string) => api.sendTestEmail(to),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['email-settings'] })
      void client.invalidateQueries({ queryKey: ['notification-health'] })
      checks.reset()
    },
  })
  const data = settings.data
  if (draft) {
    const invalid = Boolean(draft.senderMailboxId && !mailboxPattern.test(draft.senderMailboxId))
      || (draft.enabled && !draft.senderMailboxId)
      || Boolean(draft.sender && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(draft.sender))
    const original: EmailSettingsUpdate = {
      enabled: data?.enabled ?? false, senderMailboxId: data?.senderMailboxId ?? null,
      sender: data?.sender ?? null, expectedRevision: data?.revision,
    }
    return <WorkspaceEditorFrame title="Configure Microsoft Graph email"
      description="Use one provisioned Microsoft 365 mailbox and the API's app-only managed identity. Saving does not send a message or grant permissions."
      onBack={() => setDraft(null)} onCancel={() => setDraft(null)}
      dirty={JSON.stringify(draft) !== JSON.stringify(original)} pending={save.isPending}
      primaryAction={{ label: 'Save email settings', disabled: Boolean(invalid), onClick: () => save.mutate(draft) }}
      error={save.isError ? <ErrorState error={save.error} /> : undefined}>
      <div className={styles.section}>
        <Field label="Sender mailbox object ID" required={draft.enabled}
          validationMessage={draft.senderMailboxId && !mailboxPattern.test(draft.senderMailboxId) ? 'Enter the mailbox user object ID as a GUID, not an address or application ID.' : undefined}
          hint="The Entra user object ID of an existing Exchange Online mailbox. Do not use the API app ID or managed identity ID.">
          <Input value={draft.senderMailboxId ?? ''} onChange={(_, d) => setDraft({ ...draft, senderMailboxId: d.value.trim() || null })} />
        </Field>
        <Field label="Sender address label (optional)" hint="For administrators to recognize the mailbox. Graph sends as the mailbox object ID above; this label cannot override From.">
          <Input type="email" value={draft.sender ?? ''} onChange={(_, d) => setDraft({ ...draft, sender: d.value.trim() || null })} />
        </Field>
        <Checkbox label="Send budget email" checked={draft.enabled} onChange={(_, d) => setDraft({ ...draft, enabled: Boolean(d.checked) })} />
        <Text>This switch controls budget notices only. General email uses the Channels switch, routing, and each recipient's preferences.</Text>
        <Text>A Microsoft 365 administrator must authorize the API managed identity using mailbox-scoped Exchange Application RBAC. MOSAIC does not create mailboxes or grant tenant-wide Mail.Send.</Text>
      </div>
    </WorkspaceEditorFrame>
  }
  const stale = Boolean(data?.lastTestAt && data.lastTestRevision !== data.revision)
  return <div className={styles.stack}>
    <section className={styles.section} aria-label="Microsoft Graph email setup">
      <div className={styles.toolbar}><Title3 as="h2">Microsoft Graph email</Title3>
        {data && <Badge appearance="tint" color={data.migrationRequired || !data.configured ? 'warning' : 'informative'}>
          {data.migrationRequired ? 'Migration required' : data.configured ? 'Sender saved' : 'Not configured'}
        </Badge>}
      </div>
      <Text>Send notifications from a Microsoft 365 mailbox using the API's managed identity. Configuration, runtime access, and an accepted send are separate checks.</Text>
      {settings.isPending && <Loading label="Loading email settings" />}
      {settings.isError && <><ErrorState error={settings.error} /><Button onClick={() => void settings.refetch()}>Retry email settings</Button></>}
      {data && <>
        {data.migrationRequired && <MessageBar intent="warning"><MessageBarBody>
          Legacy Azure Communication Services settings are no longer used. Select a Graph sender mailbox; pending notices will resume after setup.
        </MessageBarBody></MessageBar>}
        <dl className={styles.facts}>
          <div><dt>Sender mailbox</dt><dd>{data.senderMailboxId ?? 'Not selected'}</dd></div>
          <div><dt>Address label</dt><dd>{data.sender ?? 'Not supplied'}</dd></div>
          <div><dt>Budget email</dt><dd>{data.enabled ? 'Enabled' : 'Disabled'}</dd></div>
          <div><dt>Configuration</dt><dd>Revision {data.revision}</dd></div>
        </dl>
        <div className={styles.toolbar}>
          <Button appearance="primary" onClick={() => { save.reset(); setDraft({ enabled: data.enabled, senderMailboxId: data.senderMailboxId, sender: data.sender, expectedRevision: data.revision }) }}>Configure sender</Button>
          <RouterLink to="/settings?tab=notifications&notificationView=channels">Manage channel enablement</RouterLink>
        </div>
      </>}
    </section>
    <details className={styles.section}>
      <summary className={styles.setupSummary}>Mailbox authorization instructions</summary>
      <ol className={styles.setupSteps}>
        <li><strong>Provision the sender.</strong> Choose an existing Exchange Online mailbox and save its Entra user object ID above.</li>
        <li><strong>Scope the API identity.</strong> An Exchange administrator registers the API managed identity's service principal in Exchange, creates a scope containing only that mailbox, and assigns <code>Application Mail.Send</code>. Do not add unscoped Entra <code>Mail.Send</code>; permissions are additive.</li>
        <li><strong>Check without sending.</strong> Verify runtime identity and available directory data. Use <code>Test-ServicePrincipalAuthorization</code> in Exchange to simulate mailbox authorization; token acquisition alone is not proof.</li>
        <li><strong>Send an explicit test.</strong> Confirm Graph acceptance, then check the recipient's mailbox. Normal notifications additionally require channel enablement, routing, and opt-in.</li>
      </ol>
      <a href="https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac" target="_blank" rel="noreferrer">Exchange Application RBAC setup reference</a>
    </details>
    <div className={styles.setupColumns}>
    <section className={styles.section} aria-label="Email access checks">
      <div className={styles.toolbar}><Title3 as="h2">Check access</Title3>
        <Button disabled={!data || checks.isPending || test.isPending} onClick={() => data && checks.mutate(data.revision)}>
          {checks.isPending ? 'Checking access...' : 'Check email access'}
        </Button>
      </div>
      <Text>Read-only: no message is sent and no permissions are changed. Send-only Graph access has no harmless sendMail dry run.</Text>
      {checks.isPending && <Loading label="Checking saved email access" />}
      {checks.isError && <ErrorState error={checks.error} />}
      <div role="status" aria-live="polite">
        {checks.variables === data?.revision && checks.data?.map(check => <CapabilityEvidence key={check.feature} check={check} />)}
      </div>
    </section>
    <section className={styles.section} aria-label="Test email">
      <Title3 as="h2">Send a test email</Title3>
      <Text>Uses the saved sender, even when budget email is off. HTTP 202 means Graph accepted the request, not that it was delivered or read.</Text>
      <Field label="Test recipient" hint="Send only to an address authorized for this test.">
        <Input type="email" value={testTo} onChange={(_, d) => { setTestTo(d.value); test.reset() }} />
      </Field>
      <Button {...restoreFocus} disabled={!data?.configured || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(testTo.trim()) || test.isPending}
        onClick={() => { test.reset(); setConfirmTest(true) }}>Send test email</Button>
      {!data?.configured && <Text>Save a Graph sender mailbox before testing a send.</Text>}
      {data?.lastTestAt && <div role="status" className={styles.stack}>
        <Badge color={stale ? 'warning' : data.lastTestOutcome === 'providerAccepted' ? 'success' : 'danger'}>
          {stale ? 'Stale test result' : data.lastTestOutcome === 'providerAccepted' ? 'Graph accepted the test' : 'Test needs attention'}
        </Badge>
        <Text>Last test: {new Date(data.lastTestAt).toLocaleString()} · Revision {data.lastTestRevision ?? 'unknown'}</Text>
        <Text>{stale ? 'Configuration changed. Check access and send a new test using the saved sender.' : data.lastTestError ?? 'Check the recipient mailbox and junk folder to confirm receipt.'}</Text>
        {data.lastTestRequestId && <Text className={styles.evidence}>Graph request ID: {data.lastTestRequestId}</Text>}
      </div>}
    </section>
    </div>
    <Dialog open={confirmTest} onOpenChange={(_, d) => { if (!test.isPending) setConfirmTest(d.open) }}>
      <DialogSurface><DialogBody><DialogTitle>Send a test email?</DialogTitle><DialogContent>
        <Text>Send one synthetic MOSAIC test from the saved mailbox to {testTo.trim()}. This does not enable normal notifications.</Text>
        {test.isPending && <Loading label="Sending test email" />}
        {test.isError && <ErrorState error={test.error} />}
        {test.data && <div role="status" className={styles.stack}>
          <Text>{test.data.sent ? 'Graph accepted the test request. Check the recipient mailbox for receipt.' : test.data.error ?? 'Graph did not accept the test.'}</Text>
          <Text>Outcome: {test.data.outcome} · {new Date(test.data.testedAt).toLocaleString()}</Text>
          {test.data.errorCode && <Text>Diagnostic: {test.data.errorCode}</Text>}
          {test.data.requestId && <Text>Graph request ID: {test.data.requestId}</Text>}
          {test.data.evidenceRecorded === false && <MessageBar intent="warning"><MessageBarBody>
            The test ran, but its evidence could not be saved because settings changed. Refresh the sender configuration and check access again.
          </MessageBarBody></MessageBar>}
          <Text>This is a single explicit test; normal queued notifications retry automatically within their attempt limits.</Text>
        </div>}
      </DialogContent><DialogActions>
        <Button disabled={test.isPending} onClick={() => setConfirmTest(false)}>Close</Button>
        <Button appearance="primary" disabled={test.isPending || test.isSuccess} onClick={() => test.mutate(testTo.trim())}>Send synthetic email</Button>
      </DialogActions></DialogBody></DialogSurface>
    </Dialog>
  </div>
}
