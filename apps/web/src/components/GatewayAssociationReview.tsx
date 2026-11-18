import { Badge, MessageBar, MessageBarBody, MessageBarTitle, Text } from '@fluentui/react-components'
import { ArrowRightRegular } from '@fluentui/react-icons'
import type { PublishPlan } from '../types'
import styles from './GatewayAssociationReview.module.css'

export function GatewayAssociationReview({ plan }: { plan: PublishPlan }) {
  const runtime = plan.runtimeReview
  if (!runtime) return null
  const association = plan.steps.find((step) => step.resourceId === runtime.associationId)
  const binding = plan.steps.find((step) => step.resourceId === runtime.bindingId)
  const blocked = runtime.blockers.length > 0
  const authEnabled = plan.accessSnapshot?.settings.entraEnabled
  return <section className={styles.review} aria-label="Self-hosted publication review">
    <div className={styles.panel}>
      <h3>Gateway–API association</h3>
      <Text block>These links describe APIM configuration, not the caller’s network route.</Text>
      {blocked && <MessageBar intent="warning" layout="multiline">
        <MessageBarBody><MessageBarTitle>Apply blocked</MessageBarTitle>
          <ul className={styles.blockers}>{runtime.blockers.map((reason) => <li key={reason}>{reason}</li>)}</ul>
        </MessageBarBody>
      </MessageBar>}
      <ol className={styles.map}>
        <li>
          <span>Parent APIM service</span>
          <strong>{runtime.parentName}</strong>
          <span>Developer (classic) · Manage</span>
          <span>Azure-managed ingress · not used</span>
          {runtime.parentIngress && <code>{runtime.parentIngress}</code>}
        </li>
        <li>
          <span className={styles.relation}><ArrowRightRegular aria-hidden /> Owns</span>
          <span>API in the parent service</span>
          <strong>{runtime.apiName}</strong>
          <span>Chat Completions operation and runtime guard</span>
        </li>
        <li>
          <span className={styles.relation}><ArrowRightRegular aria-hidden /> {association?.action === 'noChange' ? 'Retain link' : 'Attach'}</span>
          <span>Selected runtime</span>
          <strong>{runtime.gatewayName}</strong>
          <span>Self-hosted {runtime.imageVersion}</span>
          <code>{runtime.ingressOrigin}</code>
        </li>
      </ol>
      <dl className={styles.changes}>
        <div><dt>{association?.action === 'noChange' ? 'Retain association' : 'Add association'}</dt>
          <dd><code>{runtime.associationId}</code></dd></div>
        <div><dt>{binding?.action === 'noChange' ? 'Retain APIM binding' : 'Record APIM binding'}</dt>
          <dd><code>{runtime.bindingId}</code></dd></div>
      </dl>
      <Text block weight="semibold">Azure-managed ingress must deny this API. Existing managed publications stay unchanged.</Text>
    </div>
    <div className={`${styles.panel} ${styles.impact}`}>
      <div>
        <h3>Enforcement and usage</h3>
        <Text block>{blocked ? 'Documented policy support does not verify this runtime.' : 'Required evidence is current for this review. Apply checks it again.'}</Text>
        <Text block>Grant and cost-center checks still apply. Streaming tokens are estimated; counters do not synchronize across gateways.</Text>
      </div>
      <div>
        <h3>Caller connection <Badge appearance="tint">Preview only</Badge></h3>
        <code>{runtime.ingressOrigin}</code>
        <p><code>POST {runtime.operationPath}?api-version={runtime.apiVersion}</code></p>
        <Text block>{authEnabled ? 'Entra bearer token · Models.Invoke. Callers need an applied direct grant and the correct cost-center context.' : 'This plan does not enable callers.'}</Text>
        <Text block>Callers use the local URL only after verified Apply. No fallback to Azure ingress.</Text>
      </div>
    </div>
  </section>
}
