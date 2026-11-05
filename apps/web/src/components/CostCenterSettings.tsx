import { Button, Card, MessageBar, MessageBarBody, Switch, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import { useCostCenterCapabilities } from '../cost-center-setup'
import type { CostCenterSettingsUpdate } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { CostCenterCapabilityNotice } from './CostCenterSetupForm'
import styles from './CostCenterSetup.module.css'

export function CostCenterSettings() {
  const api = useMosaicApi()
  const client = useQueryClient()
  const settings = useQuery({ queryKey: ['cost-center-settings'], queryFn: () => api.getCostCenterSettings() })
  const capabilities = useCostCenterCapabilities()
  const [saved, setSaved] = useState(false)
  const update = useMutation({
    mutationFn: (payload: CostCenterSettingsUpdate) => api.updateCostCenterSettings(payload),
    retry: false,
    onSuccess: (value) => {
      client.setQueryData(['cost-center-settings'], value)
      setSaved(true)
    },
    onError: () => setSaved(false),
  })
  return <Card className={styles.stack}>
    <Title3 as="h2">Cost centers</Title3>
    <Text>Changes save immediately. These controls affect new portal requests only; existing memberships and requests remain available for administrator review.</Text>
    {settings.isPending && <Loading label="Loading cost-center settings" />}
    {settings.isError && <><ErrorState title="Cost-center settings unavailable" error={settings.error} /><Button onClick={() => void settings.refetch()}>Retry settings</Button></>}
    {settings.data && <>
      <Switch label="Allow cost-center creation requests" checked={settings.data.creationRequestsEnabled ?? true}
        disabled={update.isPending || settings.isFetching} onChange={(_, data) => update.mutate({ creationRequestsEnabled: data.checked })} />
      <Text>On by default. People can propose a new center; administrators still review its configuration and group setup.</Text>
      <Switch label="Allow cost-center membership requests" checked={settings.data.membershipRequestsEnabled ?? false}
        disabled={update.isPending || settings.isFetching} onChange={(_, data) => update.mutate({ membershipRequestsEnabled: data.checked })} />
      <Text>Off by default. Only individually published, ready custom centers can be found for join requests. Administrators may still add members directly.</Text>
    </>}
    {update.isPending && <Text role="status">Saving cost-center settings…</Text>}
    {saved && !update.isPending && <Text role="status">Cost-center settings saved.</Text>}
    {update.isError && <ErrorState title="Settings were not confirmed saved. Retry the switch or reload settings." error={update.error} />}
    <section className={styles.section} aria-label="Directory capability status">
      <Title3 as="h3">Directory capability status</Title3>
      <MessageBar intent="info"><MessageBarBody>Enabling requests does not grant Graph consent, change the supported cloud boundary, or guarantee a membership write. Azure Government remains unsupported for this Entra-backed workflow.</MessageBarBody></MessageBar>
      {capabilities.isPending && <Text role="status">Checking directory capabilities…</Text>}
      {capabilities.isError && <ErrorState title="Directory capabilities unavailable" error={capabilities.error} />}
      {capabilities.data && <CostCenterCapabilityNotice capabilities={capabilities.data} />}
      <Button disabled={capabilities.isFetching} onClick={() => void capabilities.refetch()}>Refresh directory capabilities</Button>
    </section>
  </Card>
}
