import {
  Badge,
  Card,
  Field,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Select,
  Text,
  Title3,
} from '@fluentui/react-components'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import { percentOf } from '../pools'
import { formatTimestamp } from '../publication-state'
import type { PoolHealth, PoolMemberHealth, PoolMemberView, PoolModelHealth, PoolModelView } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { CopyButton } from './CopyButton'
import { PoolMemberAccessBadges } from './PoolBadges'
import styles from '../pages/PoolDetailPage.module.css'

/** The windows the card reads a pool's health over, in hours. The API takes 1 to 168. */
const RANGES = [
  { hours: 1, label: 'Last hour' },
  { hours: 6, label: 'Last 6 hours' },
  { hours: 24, label: 'Last 24 hours' },
  { hours: 168, label: 'Last 7 days' },
]

const DEFAULT_HOURS = 24

/** A count and its noun, such as "1,240 calls". */
function count(value: number, noun: string): string {
  return `${value.toLocaleString()} ${noun}${value === 1 ? '' : 's'}`
}

function memberKey(member: Pick<PoolMemberHealth, 'modelEndpointId' | 'deploymentName'>): string {
  return `${member.modelEndpointId}::${member.deploymentName}`
}

function Figure({ label, value, share }: { label: string; value: number; share?: string | null }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {value.toLocaleString()}
        {share && <span className={styles.figureShare}>{` ${share}`}</span>}
      </dd>
    </div>
  )
}

function breakerTrips(minutes: number | null | undefined) {
  if (minutes == null) return <span className={styles.muted}>No breaker</span>
  return minutes === 0 ? 'None' : count(minutes, 'minute')
}

function MemberHealthRow({
  member,
  view,
  breakers,
}: {
  member: PoolMemberHealth
  view?: PoolMemberView
  breakers: boolean
}) {
  const place = [member.endpointName ?? member.modelEndpointId, member.region].filter(Boolean).join(' · ')
  const succeeded = percentOf(member.succeeded, member.attempts)
  const tagged = member.drained || member.overflow || member.apiKey || view?.provider === 'awsBedrock'
  return (
    <tr className={member.drained ? styles.drainedRow : undefined}>
      <td>
        <div className={styles.cellStack}>
          <Text weight="semibold">{member.deploymentName}</Text>
          <Text size={200} className={styles.muted}>{place}</Text>
          {tagged && (
            <span className={styles.healthBadges}>
              {member.drained && (
                <Badge appearance="outline" size="small">
                  Drained
                </Badge>
              )}
              {member.overflow && (
                <Badge appearance="outline" size="small">
                  Overflow
                </Badge>
              )}
              <PoolMemberAccessBadges apiKey={member.apiKey} provider={view?.provider} />
            </span>
          )}
        </div>
      </td>
      <td className={styles.number}>{member.attempts.toLocaleString()}</td>
      <td className={styles.number}>
        {member.succeeded.toLocaleString()}
        {succeeded && <span className={styles.muted}>{` ${succeeded}`}</span>}
      </td>
      <td className={styles.number}>{member.throttled.toLocaleString()}</td>
      <td className={styles.number}>{member.failed.toLocaleString()}</td>
      <td className={styles.number}>{member.clientErrors.toLocaleString()}</td>
      {breakers && <td className={styles.number}>{breakerTrips(member.trippedMinutes)}</td>}
      <td>{member.lastSeen ? formatTimestamp(member.lastSeen) : '—'}</td>
    </tr>
  )
}

function ModelHealth({ model, views }: { model: PoolModelHealth; views: Map<string, PoolMemberView> }) {
  const headingId = `pool-health-${model.modelId}`
  const breakers = model.members.some((member) => member.trippedMinutes != null)
  const overflow = model.members.some((member) => member.overflow)
  return (
    <section className={styles.healthModel} aria-labelledby={headingId}>
      <Title3 as="h3" id={headingId}>{model.displayName}</Title3>
      {model.requests === 0 ? (
        <Text>No calls reached {model.displayName} in this range.</Text>
      ) : (
        <>
          <dl className={styles.figures}>
            <Figure label="Calls" value={model.requests} />
            <Figure label="Succeeded" value={model.succeeded} share={percentOf(model.succeeded, model.requests)} />
            <Figure label="Unavailable" value={model.unavailable} share={percentOf(model.unavailable, model.requests)} />
            <Figure label="Client errors" value={model.clientErrors} />
            <Figure label="Retried" value={model.retried} />
            {overflow && <Figure label="Answered by overflow" value={model.overflowed} />}
          </dl>
          {model.exhausted > 0 && (
            <Text size={200}>
              The gateway answered {count(model.exhausted, 'attempt')} itself, because no deployment in the backend
              pool was available.
            </Text>
          )}
          {model.unplaced > 0 && (
            <Text size={200}>MOSAIC couldn’t tell which deployment answered {count(model.unplaced, 'attempt')}.</Text>
          )}
          <div className="table-scroll">
            <table aria-label={`How each deployment answered ${model.displayName}`}>
              <thead>
                <tr>
                  <th>Deployment</th>
                  <th className={styles.number}>Attempts</th>
                  <th className={styles.number}>Succeeded</th>
                  <th className={styles.number}>Throttled</th>
                  <th className={styles.number}>Failed</th>
                  <th className={styles.number}>Client errors</th>
                  {breakers && <th className={styles.number}>Breaker tripped</th>}
                  <th>Last attempt</th>
                </tr>
              </thead>
              <tbody>
                {model.members.map((member) => (
                  <MemberHealthRow
                    key={memberKey(member)}
                    member={member}
                    view={views.get(memberKey(member))}
                    breakers={breakers}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}

function HealthReport({ health, views }: { health: PoolHealth; views: Map<string, PoolMemberView> }) {
  switch (health.status) {
    case 'accessDenied':
      return (
        <>
          <MessageBar intent="warning">
            <MessageBarBody>
              <MessageBarTitle>MOSAIC can’t read the gateway’s logs</MessageBarTitle>
              {health.message}
            </MessageBarBody>
          </MessageBar>
          {health.command && (
            <div className={styles.example}>
              <Text>Someone with permission to assign roles can run:</Text>
              <pre className={styles.request}>{health.command}</pre>
              <div>
                <CopyButton value={health.command} label="command" />
              </div>
            </div>
          )}
        </>
      )
    case 'error':
      return (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>MOSAIC couldn’t read the gateway’s logs</MessageBarTitle>
            {health.message}
          </MessageBarBody>
        </MessageBar>
      )
    case 'ok':
      break
    default:
      return <Text>{health.message}</Text>
  }
  if (health.message) {
    // Calls reached the pool, but none from a policy that writes attempt traces.
    return (
      <MessageBar intent="warning">
        <MessageBarBody>{health.message}</MessageBarBody>
      </MessageBar>
    )
  }
  const breakers = health.models.some((model) =>
    model.requests > 0 && model.members.some((member) => member.trippedMinutes != null),
  )
  const overflow = health.models.some((model) => model.requests > 0 && model.members.some((member) => member.overflow))
  const notes = [
    'Throttled attempts got a 429, and failed ones a server error or no response. A call is unavailable when it ended on one of those.',
    overflow && 'Overflow deployments take a call only once the others can’t.',
    breakers &&
      'Breaker trips are estimates: MOSAIC counts the minutes in which a deployment failed often enough to trip its breaker.',
  ]
  return (
    <>
      <Text size={200} className={styles.muted}>
        Since {formatTimestamp(health.start)}. The newest calls can take a few minutes to show.
      </Text>
      {health.untraced > 0 && (
        <Text size={200}>
          {count(health.untraced, 'call')} reached a deployment without leaving an attempt trace, so these figures
          leave {health.untraced === 1 ? 'it' : 'them'} out.
        </Text>
      )}
      {health.models.map((model) => (
        <ModelHealth key={model.modelId} model={model} views={views} />
      ))}
      <Text size={200} className={styles.footnote}>
        {notes.filter(Boolean).join(' ')}
      </Text>
    </>
  )
}

/**
 * How the gateway's calls to each pool model ended, and how each deployment answered them, from
 * the traces the pool's policy writes to the gateway's logs.
 */
export function PoolHealthCard({ poolId, models }: { poolId: string; models: PoolModelView[] }) {
  const api = useMosaicApi()
  const [hours, setHours] = useState(DEFAULT_HOURS)
  const health = useQuery({
    queryKey: ['model-pools', 'health', poolId, hours],
    queryFn: () => api.getModelPoolHealth(poolId, hours),
    placeholderData: keepPreviousData,
    // Each read is a Log Analytics query over whole hours, so a minute-old answer is fresh enough.
    staleTime: 60_000,
  })
  const views = new Map(models.flatMap((model) => model.members.map((member) => [memberKey(member), member] as const)))
  return (
    <Card className={styles.card} aria-labelledby="pool-health-heading">
      <div className={styles.healthHeader}>
        <div className={styles.sectionHeading}>
          <Title3 as="h2" id="pool-health-heading">Health</Title3>
          <Text size={200} className={styles.muted}>
            How the gateway’s calls to each model ended, and how each deployment answered, from the traces the pool’s
            policy writes to the gateway’s logs.
          </Text>
        </div>
        <Field label="Range">
          <Select value={String(hours)} onChange={(_, data) => setHours(Number(data.value))}>
            {RANGES.map((range) => (
              <option key={range.hours} value={range.hours}>
                {range.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <div className={styles.healthBody} aria-busy={health.isPlaceholderData || undefined}>
        {health.isPending && <Loading label="Loading health" />}
        {health.isError && <ErrorState error={health.error} title="Unable to load the pool’s health" />}
        {health.data && <HealthReport health={health.data} views={views} />}
      </div>
    </Card>
  )
}
