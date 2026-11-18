import {
  Button,
  AnnounceProvider,
  Field,
  Input,
  Checkbox,
  MessageBar,
  MessageBarBody,
  Select,
  Text,
} from '@fluentui/react-components'
import { type FormEvent, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  buildEnforcement,
  limitInputError,
  describePersonLimits,
  describeLimits,
  emptyLimitForm,
  limitFormFrom,
  personLimitsFor,
  type LimitForm,
} from '../entitlement-limits'
import { EnvironmentBadge } from './EnvironmentBadge'
import {
  environmentChangedDetails,
  environmentLabel,
  findEnvironment,
} from '../environments'
import type {
  AccessRequest,
  AccessRequestApproval,
  CostCenter,
  EnvironmentCatalogView,
  ModelPool,
  Publication,
} from '../types'
import { PolicyCapabilityError } from './PolicyCapabilityFeedback'
import { policyReviewFields } from '../policy-capabilities'
import { AccessRequestRequester } from './AccessRequestRequester'
import { RequestFormAnswers } from './RequestFormAnswers'
import { useRecordedRequestForm } from './useRecordedRequestForm'
import { ErrorState, Loading } from './AsyncState'
import { LimitFields, type LimitTargetContext } from './GovernanceFields'
import { useFocusAwareAnnounce } from './useFocusAwareAnnounce'
import type { RequesterDisplay } from '../requester-display'
import {
  WorkspaceDialog as Dialog,
  WorkspaceDialogActions as DialogActions,
  WorkspaceDialogBody as DialogBody,
  WorkspaceDialogContent as DialogContent,
  WorkspaceDialogSurface as DialogSurface,
} from './WorkspaceDialog'
import styles from '../pages/EntitlementsPage.module.css'

export type ApprovalRequester = RequesterDisplay

/**
 * Confirms an access request's grant limits before approval creates the grant. Render it only
 * while open, keyed on the request, so each request starts from its own prefilled limits.
 */
export function ApproveAccessRequestDialog({
  accessRequest,
  requester,
  resourceLabel,
  costCenters = [],
  environmentCatalog,
  publication,
  pool,
  governed,
  defaultCostCenterId,
  existingGrant,
  locked = false,
  pending,
  error,
  errorKey,
  targetContext = 'conditional',
  onCancel,
  onApprove,
}: {
  accessRequest: AccessRequest
  requester: ApprovalRequester
  resourceLabel: string
  costCenters?: CostCenter[]
  environmentCatalog?: EnvironmentCatalogView
  /** The publication of the requested model API, the only source of default grant limits. */
  publication?: Publication
  /** The pool of a requested pool model. */
  pool?: Pick<ModelPool, 'displayName' | 'governedAccess'>
  /** Whether MOSAIC applies this grant to API Management through the model's plan. */
  governed: boolean
  /** The requester's recorded default, or the recorded tenant default for a new principal. */
  defaultCostCenterId?: string
  /** The requester already holds a direct grant for this resource, so approval would conflict. */
  existingGrant: boolean
  /** The resource is applying or unknown, so approval must wait for recovery. */
  locked?: boolean
  pending: boolean
  error: unknown
  errorKey?: string | number
  targetContext?: LimitTargetContext
  onCancel: () => void
  onApprove: (approval: AccessRequestApproval) => void
}) {
  const requestedCostCenterId = accessRequest.costCenterId ?? ''
  const evidence = useRecordedRequestForm(accessRequest)
  const guided = evidence.hasForm
  const [step, setStep] = useState(0)
  const stepHeading = useRef<HTMLHeadingElement>(null)
  useLayoutEffect(() => { if (guided) stepHeading.current?.focus() }, [step, guided])
  const [note, setNote] = useState('')
  const [costCenterId, setCostCenterId] = useState(requestedCostCenterId)
  // The grant is charged to the cost center chosen here, or else the requested one, or else, for a
  // request that names none, the requester's default.
  const charged = costCenters.find((item) => item.id === (costCenterId || requestedCostCenterId || defaultCostCenterId))
  const personDefaults = personLimitsFor(charged, accessRequest.resource)
  // A grant that sets no limits takes its cost center's per-person defaults, so a publication's limit
  // prefills only when there are none. Until an administrator edits the limits, they follow the choice.
  const [editedLimits, setEditedLimits] = useState<LimitForm | null>(null)
  const limits =
    editedLimits ?? (personDefaults ? emptyLimitForm : limitFormFrom(publication?.enforcement))
  const [confirmedMove, setConfirmedMove] = useState(false)
  const [currentEnvironment, setCurrentEnvironment] = useState<string | null>(
    () => accessRequest.resourceSummary?.environment ?? null,
  )
  const rateError = limitInputError(limits)
  // MCP servers are limited by calls, never tokens, so their grants offer no token limits.
  const mcp = accessRequest.resource.kind === 'mcpServer'
  const pooled = accessRequest.resource.kind === 'poolModel'
  const inheritedNote = pooled
    ? "The pool's shared token limit still applies"
    : 'Inherited publication safeguards still apply'
  const prefilled = Boolean(
    publication?.enforcement?.tokensPerMinute || publication?.enforcement?.tokenQuota,
  )
  const justification = accessRequest.justification?.trim()
  const requestedEnvironment = accessRequest.requestedEnvironment ?? null
  const environmentMoved = requestedEnvironment !== currentEnvironment
  const requestedIsProduction = Boolean(findEnvironment(environmentCatalog, requestedEnvironment)?.production)
  const currentIsProduction = Boolean(findEnvironment(environmentCatalog, currentEnvironment)?.production)
  const currentEnvironmentName = environmentLabel(environmentCatalog, currentEnvironment)

  useEffect(() => {
    const changed = environmentChangedDetails(error)
    if (!changed) return
    setCurrentEnvironment(changed.currentEnvironment ?? null)
    setConfirmedMove(false)
  }, [error])

  useEffect(() => {
    setConfirmedMove(false)
  }, [requestedEnvironment, currentEnvironment])

  function editLimits(next: LimitForm) {
    setEditedLimits(next)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    // Approve stays focusable while pending, and a focusable submit button still submits the form.
    if (rateError || existingGrant || locked || accessRequest.state !== 'pending' || pending || (environmentMoved && !confirmedMove)) return
    if (guided && (step !== 2 || !evidence.form)) return
    onApprove({
      note: note.trim() || null,
      enforcement: buildEnforcement(limits, governed),
      ...(costCenterId ? { costCenterId } : {}),
      ...(environmentMoved ? { confirmedEnvironment: currentEnvironment ?? 'unclassified' } : {}),
    })
  }

  // An approval that fails shows why at the top of the dialog. Move focus there, so a screen reader reads it:
  // the message bar doesn't announce itself. The failure only ever follows a submission, so it takes focus
  // from a field too, such as the one Enter submitted from. Fluent places focus as the dialog opens.
  const errorRef = useRef<HTMLDivElement>(null)
  const formRef = useRef<HTMLFormElement>(null)
  const errorAnnouncement = useFocusAwareAnnounce(errorRef)
  const shownError = useRef({ error, key: errorKey })
  useLayoutEffect(() => {
    const shown = shownError.current
    shownError.current = { error, key: errorKey }
    if (!error || (Object.is(error, shown.error) && errorKey === shown.key)) return
    errorRef.current?.focus()
  }, [error, errorKey])
  useLayoutEffect(() => {
    if (evidence.detail.isError) errorRef.current?.focus()
  }, [evidence.detail.isError, evidence.detail.errorUpdatedAt])

  return (
    <Dialog
      open
      title="Approve access request"
      description="Review the request and grant limits. Approval is final and saves intent only; it does not activate runtime access."
      pending={pending}
      onOpenChange={(_, data) => {
        // Stay open while approving, so the outcome is always shown here or in the page banner.
        if (!data.open && !pending) onCancel()
      }}
    >
      <DialogSurface>
        <form ref={formRef} onSubmit={submit}>
          <DialogBody>
            <DialogContent className={styles.dialogForm}>
              {guided && <>
                <ol className="governance-steps" aria-label="Approval steps">
                  {['Evidence', 'Grant', 'Confirm'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined}>{index + 1}. {label}</li>)}
                </ol>
                <h3 ref={stepHeading} tabIndex={-1}>{['Review recorded evidence', 'Review grant settings', 'Confirm grant intent'][step]}</h3>
              </>}
              {(locked || accessRequest.state !== 'pending') && <MessageBar intent="warning"><MessageBarBody>
                {accessRequest.state !== 'pending'
                  ? 'This request has already been decided. Decisions are final; no further approval can be made.'
                  : 'Access is applying or its state is unknown. Recover the affected resource before approving.'}
              </MessageBarBody></MessageBar>}
              {Boolean(error) && (
                <AnnounceProvider value={errorAnnouncement}>
                  <div ref={errorRef} tabIndex={-1}>
                    <PolicyCapabilityError title="Unable to approve access request" error={error}
                      reviewFields={!guided || step === 1 ? policyReviewFields(formRef.current, error) : {}}
                      announcementKey={errorKey} />
                  </div>
                </AnnounceProvider>
              )}
              {guided && Boolean(error) && step !== 1 && <Button type="button" onClick={() => setStep(1)}>Review grant fields</Button>}
              {guided && !error && evidence.detail.isError && <AnnounceProvider value={errorAnnouncement}>
                <div ref={errorRef} tabIndex={-1}><ErrorState context="action" title="Recorded answers unavailable" error={evidence.detail.error} announcementKey={evidence.detail.errorUpdatedAt} />
                  <Button type="button" onClick={() => void evidence.detail.refetch()}>Retry recorded answers</Button></div>
              </AnnounceProvider>}
              <section className={styles.dialogForm} hidden={guided && step !== 0}>
              <dl className={styles.detailList}>
                <div>
                  <dt>Requester</dt>
                  <dd className={styles.cellStack}>
                    <AccessRequestRequester requester={requester} showRegistrationEffect />
                  </dd>
                </div>
                <div>
                  <dt>Resource</dt>
                  <dd>{resourceLabel}</dd>
                </div>
                <div>
                  <dt>Cost center</dt>
                  <dd>{accessRequest.costCenter ? `${accessRequest.costCenter.name} (${accessRequest.costCenter.code})` : 'Subject default'}</dd>
                </div>
                <div>
                  <dt>Requested environment</dt>
                  <dd>
                    <EnvironmentBadge azureCloud={accessRequest.resourceSnapshot?.azureCloud ?? 'commercial'} environment={requestedEnvironment} catalog={environmentCatalog} />
                  </dd>
                </div>
                <div>
                  <dt>Current environment</dt>
                  <dd>
                    <EnvironmentBadge azureCloud={accessRequest.resourceSummary?.azureCloud ?? 'commercial'} environment={currentEnvironment} catalog={environmentCatalog} />
                  </dd>
                </div>
                <div>
                  <dt>Justification</dt>
                  <dd>{justification || 'No justification given'}</dd>
                </div>
              </dl>
              {guided && (evidence.form ? <RequestFormAnswers form={evidence.form} />
                : evidence.detail.isSuccess ? <Text role="alert">Recorded answers are unavailable. Do not approve using today's form.</Text>
                : !evidence.detail.isError && <Loading label="Loading recorded governance answers" />)}
              </section>
              {(requestedIsProduction || currentIsProduction) && (
                <MessageBar intent="warning">
                  <MessageBarBody>This approval grants production-class access.</MessageBarBody>
                </MessageBar>
              )}
              {environmentMoved && (
                <MessageBar intent="warning">
                  <MessageBarBody>
                    This resource moved from {environmentLabel(environmentCatalog, requestedEnvironment)} to{' '}
                    {currentEnvironmentName} since the request was created. Confirm the current
                    environment before approving.
                  </MessageBarBody>
                </MessageBar>
              )}
              <section className={styles.dialogForm} hidden={guided && step !== 1}>
              {environmentMoved && (
                <Checkbox
                  checked={confirmedMove}
                  label={`Approve access to ${currentEnvironmentName}`}
                  onChange={(_, data) => setConfirmedMove(Boolean(data.checked))}
                />
              )}
              {existingGrant && (
                <MessageBar intent="warning">
                  <MessageBarBody>
                    {requester.label} already has a direct grant for {resourceLabel}. Deny this
                    request, or change the existing grant instead.
                  </MessageBarBody>
                </MessageBar>
              )}
              <Text>
                {pooled
                  ? pool?.governedAccess
                    ? `Approving creates grant intent only. API Management is unchanged until the ${pool.displayName} plan is reviewed and applied.`
                    : `Approving creates grant intent only. ${pool?.displayName ?? 'The pool'} doesn't use governed access yet, so the grant waits until it does and the pool's plan is applied.`
                  : governed && mcp
                  ? 'Approving creates grant intent only. API Management is unchanged until this MCP server is planned and applied from the MCPs page.'
                  : governed && publication
                    ? `Approving creates grant intent only. API Management is unchanged until the ${publication.displayName} model plan is reviewed and applied.`
                    : 'Approving creates grant intent only. MOSAIC does not apply grants for this resource to API Management, so the grant stays desired state.'}
              </Text>
              {personDefaults && charged ? (
                <Text size={200}>
                  {charged.name} sets per-person defaults here: {describePersonLimits(personDefaults)}.
                  Leave every limit empty to apply them; any limit you set replaces them all.
                  {` ${inheritedNote}.`}
                </Text>
              ) : mcp ? (
                <Text size={200}>
                  MCP servers are limited by calls, not tokens. Leave both call amounts empty to
                  add no grant-specific call restriction.
                </Text>
              ) : (
                <Text size={200}>
                  {prefilled && publication
                    ? `Limits are prefilled from the ${publication.displayName} publication's token limit. Change or clear them before approving.`
                    : 'This resource has no default limits to prefill.'}{' '}
                  Leave a limit empty to add no grant-specific restriction. {inheritedNote}; this
                  does not mean unrestricted gateway access.
                </Text>
              )}
              <LimitFields value={limits} onChange={editLimits} mcp={mcp}
                targetContext={targetContext}
                inherited={personDefaults} inheritedLabel={charged ? `${charged.name} per-person defaults` : 'Cost-center defaults (resolved on save)'} />
              <Field label="Cost center" hint="The grant is charged here. Choose another cost center the requester may charge.">
                <Select value={costCenterId} onChange={(_, data) => setCostCenterId(data.value)}>
                  {requestedCostCenterId ? (
                    !costCenters.some((costCenter) => costCenter.id === requestedCostCenterId) && (
                      <option value={requestedCostCenterId}>
                        {accessRequest.costCenter
                          ? `${accessRequest.costCenter.name} (${accessRequest.costCenter.code})`
                          : requestedCostCenterId}{' '}
                        — requested
                      </option>
                    )
                  ) : (
                    <option value="">Requester&apos;s default cost center</option>
                  )}
                  {costCenters.map((costCenter) => (
                    <option key={costCenter.id} value={costCenter.id}>
                      {costCenter.name} ({costCenter.code})
                      {costCenter.id === requestedCostCenterId ? ' — requested' : ''}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Decision note" hint="Optional. Recorded with the approval.">
                <Input value={note} onChange={(_, data) => setNote(data.value)} />
              </Field>
              </section>
              {guided && step === 2 && <>
                <p><strong>Requester:</strong> {requester.label} · <strong>Resource:</strong> {resourceLabel}</p>
                <p><strong>Charged cost center:</strong> {charged?.name ?? accessRequest.costCenter?.name ?? 'Requester default (revalidated on approval)'}</p>
                <p><strong>Grant limits:</strong> {personDefaults && editedLimits === null
                  ? describePersonLimits(personDefaults)
                  : describeLimits({ enforcement: buildEnforcement(limits, governed) }).join(' ')}</p>
                <p>{inheritedNote}. Approval records intent only; the affected gateway plan still requires separate review and apply.</p>
                {note && <p><strong>Decision note:</strong> {note}</p>}
                {evidence.form && <RequestFormAnswers form={evidence.form} />}
              </>}
            </DialogContent>
            <DialogActions>
              {guided && step > 0 && <Button type="button" disabled={pending} onClick={() => setStep(step - 1)}>Previous step</Button>}
              {guided && step < 2 ? <Button type="button" appearance="primary" disabledFocusable={pending}
                disabled={!evidence.form || locked || accessRequest.state !== 'pending' ||
                  step === 1 && (existingGrant || Boolean(rateError) || environmentMoved && !confirmedMove)}
                onClick={() => { if (!pending) setStep(step + 1) }}>{step === 0 ? 'Next: Grant settings' : 'Next: Confirm'}</Button> : <>
              {/* A browser takes focus off a button that becomes disabled, so a busy button stays focusable. */}
              <Button
                appearance="primary"
                type="submit"
                disabled={locked || accessRequest.state !== 'pending' || existingGrant || Boolean(rateError) || (environmentMoved && !confirmedMove)}
                disabledFocusable={pending}
              >
                Approve and create grant
              </Button>
              </>}
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  )
}
