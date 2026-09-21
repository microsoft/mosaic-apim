import { Badge, Button, MessageBar, MessageBarBody, Switch, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import { describeAccessMethods, describePublicationLimits } from '../entitlement-limits'
import type { Publication } from '../types'
import { ErrorState } from './AsyncState'
import { ModelAccessRecovery } from './ModelAccessRecovery'
import styles from '../pages/EntitlementsPage.module.css'

export function ModelAccessSettingsPanel({
  publication,
  reviewing,
  onReview,
  onAddGrant,
  onMessage,
}: {
  publication: Publication
  reviewing: boolean
  onReview: () => void
  onAddGrant: (modelApiId: string) => void
  onMessage: (message: string) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [settings, setSettings] = useState(
    publication.governedAccess ?? { keysEnabled: true, entraEnabled: true },
  )
  const dirty = !publication.governedAccess
    || settings.keysEnabled !== publication.governedAccess.keysEnabled
    || settings.entraEnabled !== publication.governedAccess.entraEnabled
  const busy = publication.accessState === 'applying' || publication.accessState === 'unknown'
    || publication.status === 'applying'

  async function invalidate() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['publications'] }),
      queryClient.invalidateQueries({ queryKey: ['model-apis'] }),
      queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
    ])
  }

  const link = useMutation({
    mutationFn: () => api.linkPublicationModelApi(publication.id),
    onSuccess: async () => {
      await invalidate()
      onMessage('Linked the published model to its canonical model API. API Management is unchanged.')
    },
  })
  const save = useMutation({
    mutationFn: () => api.updatePublication(publication.id, { governedAccess: settings }),
    onSuccess: async () => {
      await invalidate()
      onMessage('Saved governed-access intent only. Review and apply this model’s plan to change API Management.')
    },
  })

  return (
    <div className={styles.accessPanel}>
      <div className={styles.detailHeader}>
        <Title3 as="h3">{publication.displayName}</Title3>
        <Badge appearance="tint">
          {publication.governedAccess ? `Access: ${publication.accessState}` : 'Legacy publication'}
        </Badge>
      </div>
      {!publication.appliedAccess && (
        <MessageBar intent="warning">
          <MessageBarBody>
            Opting into governed access retires this model&apos;s generic bootstrap key when the
            reviewed plan is applied. Existing clients must move to direct grants.
            Saving alone leaves the current API and legacy access unchanged.
          </MessageBarBody>
        </MessageBar>
      )}
      {(publication.accessState === 'unknown' || publication.accessState === 'failed') && (
        <MessageBar intent="warning">
          <MessageBarBody>
            {publication.accessState === 'unknown'
              ? 'Runtime state is unknown. An interrupted apply may have changed APIM; do not assume access or revocation succeeded.'
              : 'The last access apply failed. The target configuration is not confirmed active.'}
            {publication.lastError ? ` ${publication.lastError}` : ''}
          </MessageBarBody>
        </MessageBar>
      )}
      {publication.accessState === 'unknown' && (
        <ModelAccessRecovery publicationId={publication.id} runId={publication.lastRunId} />
      )}
      {!publication.modelApiId && (
        <div className={styles.cellStack}>
          <Text>Link this already-published API before adding direct grants. Linking writes MOSAIC metadata only.</Text>
          <Button disabled={link.isPending || busy} onClick={() => link.mutate()}>
            {link.isPending ? 'Linking…' : 'Link published model'}
          </Button>
        </div>
      )}
      <Text weight="semibold">Desired authentication methods</Text>
      <div className={styles.switchGrid}>
        <Switch
          label="Dedicated subscription keys"
          checked={settings.keysEnabled}
          disabled={save.isPending || busy}
          onChange={(_, data) => setSettings({ ...settings, keysEnabled: data.checked })}
        />
        <Switch
          label="Microsoft Entra bearer tokens"
          checked={settings.entraEnabled}
          disabled={save.isPending || busy}
          onChange={(_, data) => setSettings({ ...settings, entraEnabled: data.checked })}
        />
      </div>
      <Text>{describeAccessMethods(settings)}</Text>
      <Text size={200}>
        Either enabled method can be used independently. Both share the same grant and limits.
        Disabling both means deny all, never anonymous access. MOSAIC derives APIM&apos;s subscription requirement.
      </Text>
      <Text>Saved desired methods: {publication.governedAccess ? describeAccessMethods(publication.governedAccess) : 'Legacy — not opted in'}</Text>
      <Text>Last applied methods: {describeAccessMethods(publication.appliedAccess?.settings)}</Text>
      {publication.appliedAccess && (
        <Text size={200}>
          Last recorded access version: {publication.appliedAccess.version}. This snapshot is not a live invocation test.
        </Text>
      )}
      <div className={styles.switchGrid}>
        <div className={styles.cellStack}>
          <Text weight="semibold">Desired publication limits</Text>
          {describePublicationLimits(publication.enforcement).map((limit) => <Text key={limit}>{limit}</Text>)}
        </div>
        <div className={styles.cellStack}>
          <Text weight="semibold">Last applied publication limits</Text>
          {publication.appliedAccess
            ? describePublicationLimits(publication.appliedAccess.publicationEnforcement).map((limit) => <Text key={limit}>{limit}</Text>)
            : <Text>No governed-access snapshot has been applied.</Text>}
        </div>
      </div>
      <Text size={200}>Grant limits do not remove these inherited model-wide safeguards.</Text>
      {link.isError && <ErrorState error={link.error} />}
      {save.isError && <ErrorState error={save.error} />}
      <div className={styles.rowActions}>
        <Button appearance="primary" disabled={!dirty || busy || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : 'Save access settings'}
        </Button>
        <Button
          disabled={!publication.modelApiId || !publication.governedAccess || busy}
          onClick={() => publication.modelApiId && onAddGrant(publication.modelApiId)}
        >
          Add direct grant
        </Button>
        <Button
          disabled={dirty || !publication.governedAccess || !publication.modelApiId || busy || reviewing || save.isPending}
          onClick={onReview}
        >
          {reviewing ? 'Preparing review…' : 'Review model changes'}
        </Button>
      </div>
      <Text size={200}>Review applies all saved grants and settings for this model, not a single row.</Text>
    </div>
  )
}
