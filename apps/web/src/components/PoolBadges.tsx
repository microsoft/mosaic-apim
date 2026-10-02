import { Badge } from '@fluentui/react-components'
import { POOL_CAPACITY_LABELS, POOL_READINESS_LABELS, POOL_RUN_STATUS_LABELS, readinessSummary } from '../pools'
import type { ReadinessTone } from '../pools'
import { isUnpublished, publicationStatusLabel } from '../publication-state'
import type { PublicationRecord } from '../publication-state'
import type { PoolCapacityBadge, PoolReadiness, PublishRunStatus } from '../types'
import styles from './PoolDialogs.module.css'

const READINESS_CLASS: Record<PoolReadiness, string> = {
  ready: styles.readyBadge,
  notConfirmed: styles.cautionBadge,
  cannotInvoke: styles.dangerBadge,
}

const TONE_CLASS: Record<ReadinessTone, string> = {
  success: styles.readyBadge,
  warning: styles.cautionBadge,
  danger: styles.dangerBadge,
  muted: styles.quietBadge,
}

const CAPACITY_CLASS: Record<PoolCapacityBadge, string> = {
  provisioned: styles.primaryBadge,
  provisionedWithOverflow: styles.primaryBadge,
  payAsYouGo: styles.quietBadge,
  unknown: styles.cautionBadge,
}

/** Where a pool stands with its gateway: the same states, and colors, as a model's publication. */
export function PoolStatusBadge({ pool }: { pool: PublicationRecord }) {
  const { status } = pool
  const className =
    status === 'failed' || status === 'rolledBack'
      ? styles.dangerBadge
      : status === 'applying' || status === 'planned'
        ? styles.primaryBadge
        : status === 'draft'
          ? styles.quietBadge
          : styles.readyBadge
  return (
    <Badge appearance="tint" className={isUnpublished(pool) ? styles.quietBadge : className}>
      {publicationStatusLabel(pool)}
    </Badge>
  )
}

/** Whether one member can serve requests, as far as MOSAIC can tell before it publishes. */
export function PoolReadinessBadge({ readiness }: { readiness: PoolReadiness }) {
  return (
    <Badge appearance="tint" className={READINESS_CLASS[readiness]}>
      {POOL_READINESS_LABELS[readiness]}
    </Badge>
  )
}

/** The worst readiness among a pool's active members, with how many share it. */
export function ReadinessSummaryBadge({ readiness }: { readiness: Partial<Record<PoolReadiness, number>> }) {
  const { label, tone } = readinessSummary(readiness)
  return (
    <Badge appearance="tint" className={TONE_CLASS[tone]}>
      {label}
    </Badge>
  )
}

/** What a pool model's capacity adds up to, as users are told it. */
export function PoolCapacityBadgeView({ capacity }: { capacity: PoolCapacityBadge }) {
  return (
    <Badge appearance="tint" className={CAPACITY_CLASS[capacity]}>
      {POOL_CAPACITY_LABELS[capacity]}
    </Badge>
  )
}

/** Saved changes the gateway doesn't run yet. They take effect when a plan is applied. */
export function UnappliedChangesBadge() {
  return (
    <Badge appearance="tint" className={styles.cautionBadge}>
      Changes not applied
    </Badge>
  )
}

const RUN_STATUS_CLASS: Record<PublishRunStatus, string> = {
  running: styles.primaryBadge,
  succeeded: styles.readyBadge,
  failed: styles.dangerBadge,
  rolledBack: styles.cautionBadge,
  rollbackFailed: styles.dangerBadge,
  interrupted: styles.cautionBadge,
}

/** How one publish or unpublish run of a pool ended. */
export function PoolRunStatusBadge({ status }: { status: PublishRunStatus }) {
  return (
    <Badge appearance="tint" className={RUN_STATUS_CLASS[status]}>
      {POOL_RUN_STATUS_LABELS[status]}
    </Badge>
  )
}
