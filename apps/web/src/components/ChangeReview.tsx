import { MessageBar, MessageBarBody, Text, Title3 } from '@fluentui/react-components'
import type { ReactNode } from 'react'
import { classifyChanges } from '../change-review'
import type { EditContext } from '../change-review'
import type { PublishPlan, ReviewChange } from '../types'
import { plural } from '../labels'
import styles from './ChangeReview.module.css'

function ChangeList({ title, changes, replaced }: {
  title: string
  changes: ReviewChange[]
  replaced: Map<string, string>
}) {
  if (!changes.length) return null
  return <section aria-label={title} className={styles.section}>
    <Title3 as="h3">{title} ({changes.length})</Title3>
    {[...new Set(['Access', 'Limits', 'Authentication', 'Routing', 'Configuration', ...changes.map((change) => change.category)])].map((category) => {
      const rows = changes.filter((change) => change.category === category)
      return rows.length > 0 && <div key={category}>
        <Text as="h4" weight="semibold">{category}</Text>
        <ul className={styles.list}>
          {rows.map((change) => <li key={change.key} className={styles.change}>
            <Text weight="semibold">{change.label}</Text>
            <div className={styles.values}>
              <div><Text size={200}>Before</Text><Text block>{
                !change.previousKnown ? 'Previous value unavailable' : change.before ?? 'Not configured'
              }</Text></div>
              <div><Text size={200}>After apply</Text><Text block>{change.after ?? 'Removed'}</Text></div>
            </div>
            {replaced.has(change.key) && <Text block size={200}>
              Replaces the previously saved value: {replaced.get(change.key)}
            </Text>}
          </li>)}
        </ul>
      </div>
    })}
  </section>
}

export function ChangeReview({ plan, context }: { plan: PublishPlan; context?: EditContext }) {
  const review = plan.review
  if (!review) return <MessageBar intent="warning" layout="multiline"><MessageBarBody>
    This older plan has no before/after comparison. Its complete target and execution steps remain
    below. Request a fresh plan for a change summary; previous values may still be unavailable.
  </MessageBarBody></MessageBar>
  const trustedContext = context?.targetId === plan.publicationId ? context : undefined
  const { yours, pending, unattributed, replaced } = classifyChanges(review.changes, trustedContext)
  const unpublish = plan.operation === 'unpublish'
  return <div className={styles.section}>
    <Text block>{unpublish
      ? 'Review the access and configuration this unpublish removes.'
      : 'Applies all pending changes for this target, including previously saved changes.'}</Text>
    <Text block size={200}>Compared with MOSAIC's recorded configuration, not a fresh verification
      of live gateway behavior.{review.appliedAt
        ? ` Last successful apply: ${new Date(review.appliedAt).toLocaleString()}.` : ''}</Text>
    <Text block size={200}>This comparison covers recorded configuration and compiled access.
      Live endpoint details and manual policy edits may also affect execution; inspect the warnings
      and technical details. Counter expressions are shown only as fingerprints.</Text>
    {review.baseline === 'partial' || review.baseline === 'unavailable'
      ? <MessageBar intent="warning" layout="multiline"><MessageBarBody>
        Some previous values are unavailable. Unknown configuration is not treated as unchanged.
        Review the proposed values and technical execution before applying.
      </MessageBarBody></MessageBar> : null}
    {unpublish ? <Text block>The gateway route and its recorded configuration are removed.
      Saved grants stay in MOSAIC. The full recorded removal comparison is in Technical execution.</Text> : <>
      <ChangeList title="Your changes" changes={yours} replaced={replaced} />
      <ChangeList title="Previously saved changes included" changes={yours.length ? pending : []} replaced={replaced} />
      {unattributed.length > 0 && <Text block size={200}>Edit attribution is unavailable for some compiled or
        inherited grant values. They are included below without assuming when they were saved.</Text>}
      <ChangeList title="Pending changes" changes={yours.length ? unattributed : [...pending, ...unattributed]} replaced={replaced} />
    </>}
    {!unpublish && !review.changes.length && <Text block>No intended configuration changes. The execution batch may
      still write configuration again or interrupt calls; review its impact below.</Text>}
    <section aria-label="Impact of applying" className={styles.section}>
      <Title3 as="h3">{unpublish ? 'Impact of unpublishing' : 'Impact of applying'}</Title3>
      {review.impact.length > 0 && <MessageBar intent="warning" layout="multiline">
        <MessageBarBody><ul className={styles.impactList}>
          {review.impact.map((impact) => <li key={impact}>{impact}</li>)}
        </ul></MessageBarBody>
      </MessageBar>}
    </section>
  </div>
}

export function TechnicalExecution({ plan, children }: { plan: PublishPlan; children: ReactNode }) {
  const writes = plan.steps.filter((step) => step.action !== 'noChange').length
  const resources = new Set(plan.steps.map((step) => step.resourceId)).size
  return <details className={styles.technical} open={!plan.review}>
    <summary>Technical execution ({plural(writes, 'planned operation')} across {plural(resources, 'resource')})</summary>
    <Text block size={200}>Execution steps are not a count of intended behavior changes. Repeated
      resources represent ordered safety stages. Shared dependencies can be rechecked at apply.</Text>
    {plan.operation !== 'unpublish' && <Text block size={200}>Per-person limits use the grant's
      override or its cost-center defaults. Shared monthly quotas and publication safeguards are
      additional; limits do not authorize a disabled grant.</Text>}
    {plan.review && plan.operation === 'unpublish' && <details>
      <summary>Recorded configuration removed ({plan.review.changes.length} values)</summary>
      <ChangeList title="Changes on unpublish" changes={plan.review.changes} replaced={new Map()} />
    </details>}
    {plan.review && plan.operation !== 'unpublish' && <details>
      <summary>Complete proposed configuration ({plan.review.target.fields.length} values)</summary>
      <dl>
        {plan.review.target.fields.map((field) => <div key={field.key}>
          <dt>{field.label}</dt><dd>{field.value}</dd>
        </div>)}
      </dl>
    </details>}
    {children}
  </details>
}
