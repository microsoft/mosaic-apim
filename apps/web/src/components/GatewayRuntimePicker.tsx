import { Field, Select, Text } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import type { Gateway, GatewayRuntimeChoice, SelfHostedGateway } from '../types'
import { ErrorState, Loading } from './AsyncState'

export const azureManaged = { kind: 'azureManaged' } as const
export const runtimeQueryKey = (id: string) => ['self-hosted-gateways', id] as const

export function useGatewayRuntimes(id: string, enabled = true) {
  const api = useMosaicApi()
  return useQuery({
    queryKey: runtimeQueryKey(id),
    queryFn: () => api.listSelfHostedGateways(id),
    enabled: Boolean(id) && enabled,
  })
}

export function runtimeRefusal(gateway: Gateway | undefined, runtime: SelfHostedGateway) {
  if (!gateway || gateway.id !== runtime.parentGatewayId) return 'Not registered on this APIM service'
  if ((gateway.azureCloud ?? 'commercial') !== 'commercial'
    || gateway.capabilities.skuName?.toLowerCase() !== 'developer'
    || runtime.imageVersion !== '2.13.2') return 'Outside the first self-hosted profile'
  return ''
}

export function GatewayRuntimePicker({ gateway, choice, onChange, runtimes, path = 'model' }: {
  gateway: Gateway
  choice: GatewayRuntimeChoice
  onChange: (choice: GatewayRuntimeChoice) => void
  runtimes: ReturnType<typeof useGatewayRuntimes>
  path?: 'model' | 'mcp'
}) {
  const rows = runtimes.data ?? []
  const selected = choice.kind === 'selfHosted' ? choice.runtimeId : ''
  const missing = selected && !rows.some((item) => item.id === selected)
  return <>
    <Field label="Gateway runtime" validationState={missing ? 'warning' : 'none'}
      validationMessage={missing ? 'Your previous choice is not available here. Choose a runtime explicitly.' : undefined}>
      <Select value={selected} onChange={(event) => {
        const runtimeId = event.target.value
        onChange(runtimeId ? { kind: 'selfHosted', runtimeId } : azureManaged)
      }}>
        <option value="">Azure-managed gateway (default)</option>
        {missing && <option value={selected} disabled>Previously selected self-hosted gateway</option>}
        {rows.map((runtime) => {
          const reason = runtimeRefusal(gateway, runtime)
          return <option key={runtime.id} value={runtime.id} disabled={Boolean(reason)}>
            {runtime.gatewayName} (self-hosted){reason ? ` — ${reason}` : ''}
          </option>
        })}
      </Select>
    </Field>
    {runtimes.isPending && <Loading label="Loading self-hosted gateways" />}
    {runtimes.isError && <ErrorState error={runtimes.error} />}
    {path === 'mcp' && selected && !missing && <Text block size={200}>
      Tools inherit their server’s gateway. Apply needs proof of local MCP traffic and OAuth discovery.
    </Text>}
    {path === 'model' && selected && !missing && <Text block size={200}>
      Self-hosted publishing supports governed Azure OpenAI Chat Completions with Entra. Review verifies the selected runtime before Apply.
    </Text>}
  </>
}
