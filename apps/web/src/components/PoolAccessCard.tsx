import { Badge, Button, Card, MessageBar, MessageBarBody, MessageBarTitle, Switch, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { describeAccessMethods, describeLimits } from '../entitlement-limits'
import { ENTITLEMENT_SUBJECT_KIND_LABELS, plural } from '../labels'
import { poolAccessLabel } from '../pools'
import type { ModelAccessSettings, ModelPool, PoolAccessGrant, PoolModelQuota } from '../types'
import { ErrorState } from './AsyncState'
import styles from '../pages/PoolDetailPage.module.css'

const BOTH_METHODS: ModelAccessSettings = { keysEnabled: true, entraEnabled: true }

function grantState(grant: PoolAccessGrant): string {
  if (grant.revoked) return 'Revoked when its subject left the cost center'
  if (!grant.enabled) return 'Off'
  if (grant.keyName && grant.keysAllowed === false) return 'On, but its cost center turned keys off'
  return 'On'
}

function describeQuota(quota: PoolModelQuota): string {
  const parts: string[] = []
  if (quota.monthlyTokens) parts.push(`${quota.monthlyTokens.toLocaleString()} tokens`)
  if (quota.monthlyCalls) parts.push(`${quota.monthlyCalls.toLocaleString()} calls`)
  return `${quota.costCenterCode} shares ${parts.join(' and ')} a month`
}

/**
 * Who may call a pool. Before governed access, every caller shares the pool's subscription. Opting
 * in is one-way: the apply suspends that subscription, and each caller needs their own grant.
 */
export function PoolAccessCard({
  pool,
  onSaved,
}: {
  pool: ModelPool
  onSaved: (message: string) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [settings, setSettings] = useState<ModelAccessSettings>(pool.governedAccess ?? BOTH_METHODS)
  const governed = Boolean(pool.governedAccess)
  const dirty =
    !pool.governedAccess ||
    settings.keysEnabled !== pool.governedAccess.keysEnabled ||
    settings.entraEnabled !== pool.governedAccess.entraEnabled
  const busy = pool.status === 'applying' || pool.accessState === 'applying' || pool.accessState === 'unknown'
  const applied = pool.appliedAccess
  const modelName = (id: string) => pool.models.find((model) => model.id === id)?.displayName ?? id

  const save = useMutation({
    mutationFn: () => api.updateModelPool(pool.id, { governedAccess: settings }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['model-pools'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
      ])
      onSaved(
        governed
          ? 'Saved the pool’s access settings. API Management is unchanged until you review and apply a plan.'
          : 'Saved governed access for the pool. Callers keep using its shared subscription until you review and apply a plan.',
      )
    },
  })

  return (
    <Card className={styles.card} aria-labelledby="pool-access-heading">
      <div className={styles.cardHeading}>
        <Title3 as="h2" id="pool-access-heading">Who can call it</Title3>
        <Badge appearance="tint">{poolAccessLabel(pool)}</Badge>
      </div>
      {!governed && (
        <Text>
          Every caller shares the pool’s subscription, <span className={styles.code}>{pool.subscriptionName}</span>.
          Turn on governed access to give each person, application, or security group their own grant to the pool’s
          models, with their own limits and cost center.
        </Text>
      )}
      {!applied && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Governed access can’t be turned off</MessageBarTitle>
            When a plan applies it, MOSAIC suspends the shared subscription, and only callers with a grant can call
            the pool. Grant access on the Entitlements page before you apply. Saving changes nothing in API
            Management.
          </MessageBarBody>
        </MessageBar>
      )}
      {(pool.accessState === 'unknown' || pool.accessState === 'failed') && (
        <MessageBar intent="warning">
          <MessageBarBody>
            {pool.accessState === 'unknown'
              ? 'MOSAIC doesn’t know which grants the gateway enforces. An interrupted run may have changed API Management, so don’t assume access was given or taken away.'
              : 'The last apply failed, so the saved access isn’t confirmed active.'}
          </MessageBarBody>
        </MessageBar>
      )}
      <Text weight="semibold">How callers sign in</Text>
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
      <Text size={200} className={styles.muted}>
        A key serves every model its holder is granted in this pool under one cost center. Security groups always
        sign in with Entra tokens. Turning both off denies every call.
      </Text>
      <dl className={styles.summaryList}>
        <dt>Saved</dt>
        <dd>{governed ? describeAccessMethods(pool.governedAccess) : 'Shared subscription'}</dd>
        <dt>Applied</dt>
        <dd>
          {applied
            ? `${describeAccessMethods(applied.settings)}, version ${applied.version}`
            : 'Shared subscription'}
        </dd>
      </dl>
      {save.isError && <ErrorState error={save.error} title="MOSAIC didn’t save the access settings" />}
      <div className={styles.accessActions}>
        <Button appearance="primary" disabled={!dirty || busy || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : governed ? 'Save access settings' : 'Turn on governed access'}
        </Button>
        {governed && <Link to="/entitlements">Grant access on the Entitlements page</Link>}
      </div>

      {applied && (
        <>
          <Text weight="semibold">
            Grants in force: {plural(applied.grants.filter((grant) => grant.enabled).length, 'grant')}
          </Text>
          {applied.grants.length === 0 ? (
            <Text>No one can call the pool yet. Grant access on the Entitlements page, then apply a plan.</Text>
          ) : (
            <div className="table-scroll">
              <table aria-label="Grants in force">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Who</th>
                    <th>Signs in with</th>
                    <th>Cost center</th>
                    <th>Limits</th>
                    <th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {applied.grants.map((grant) => (
                    <tr key={grant.entitlementId}>
                      <td>{modelName(grant.poolModelId)}</td>
                      <td>
                        <div className={styles.cellStack}>
                          <Text>{grant.displayName}</Text>
                          <Text size={200} className={styles.muted}>
                            {ENTITLEMENT_SUBJECT_KIND_LABELS[grant.subject.kind]}
                          </Text>
                        </div>
                      </td>
                      <td>
                        {grant.keyName ? <span className={styles.code}>{grant.keyName}</span> : 'Entra token'}
                      </td>
                      <td>{grant.costCenterCode ?? '—'}</td>
                      <td>
                        <div className={styles.cellStack}>
                          {describeLimits(grant).map((sentence) => (
                            <Text key={sentence} size={200}>{sentence}</Text>
                          ))}
                        </div>
                      </td>
                      <td>{grantState(grant)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(applied.quotas ?? []).length > 0 && (
            <div className={styles.cellStack}>
              <Text weight="semibold">Pooled quotas</Text>
              {(applied.quotas ?? []).map((quota) => (
                <Text key={`${quota.poolModelId}:${quota.costCenterId}`} size={200}>
                  {modelName(quota.poolModelId)}: {describeQuota(quota)}
                </Text>
              ))}
            </div>
          )}
          {applied.tokenMetering === false && (
            <Text size={200} className={styles.muted}>
              This gateway’s tier can’t count the pool’s tokens, so grants limit calls, not tokens.
            </Text>
          )}
        </>
      )}
    </Card>
  )
}
