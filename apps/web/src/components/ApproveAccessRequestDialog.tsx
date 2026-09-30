import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Checkbox,
  MessageBar,
  MessageBarBody,
  Select,
  Text,
} from '@fluentui/react-components'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import {
  QUOTA_PERIODS,
  buildEnforcement,
  callRateError,
  limitFormFrom,
  type LimitForm,
} from '../entitlement-limits'
import { EnvironmentBadge } from './EnvironmentBadge'
import {
  environmentChangedDetails,
  environmentLabel,
  findEnvironment,
} from '../environments'
import type { AccessRequest, AccessRequestApproval, EnvironmentCatalogView, Publication, QuotaPeriod } from '../types'
import { ErrorState } from './AsyncState'
import styles from '../pages/EntitlementsPage.module.css'

export interface ApprovalRequester {
  /**
   * The registered principal's label. Otherwise `objectId`: the requester isn't registered, or their
   * principal has no label.
   */
  label: string
  /** The requester's Entra object ID, as the request recorded it. */
  objectId: string
  registered: boolean
}

/**
 * Confirms an access request's grant limits before approval creates the grant. Render it only
 * while open, keyed on the request, so each request starts from its own prefilled limits.
 */
export function ApproveAccessRequestDialog({
  accessRequest,
  requester,
  resourceLabel,
  environmentCatalog,
  publication,
  governed,
  existingGrant,
  pending,
  error,
  onCancel,
  onApprove,
}: {
  accessRequest: AccessRequest
  requester: ApprovalRequester
  resourceLabel: string
  environmentCatalog?: EnvironmentCatalogView
  /** The publication of the requested model API, the only source of default grant limits. */
  publication?: Publication
  /** Whether MOSAIC applies this grant to API Management through the model's plan. */
  governed: boolean
  /** The requester already holds a direct grant for this resource, so approval would conflict. */
  existingGrant: boolean
  pending: boolean
  error: unknown
  onCancel: () => void
  onApprove: (approval: AccessRequestApproval) => void
}) {
  const [limits, setLimits] = useState<LimitForm>(() => limitFormFrom(publication?.enforcement))
  const [note, setNote] = useState('')
  const [confirmedMove, setConfirmedMove] = useState(false)
  const [currentEnvironment, setCurrentEnvironment] = useState<string | null>(
    () => accessRequest.resourceSummary?.environment ?? null,
  )
  const rateError = callRateError(limits)
  // MCP servers are limited by calls, never tokens, so their grants offer no token limits.
  const mcp = accessRequest.resource.kind === 'mcpServer'
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

  function submit(event: FormEvent) {
    event.preventDefault()
    // Approve stays focusable while pending, and a focusable submit button still submits the form.
    if (rateError || existingGrant || pending || (environmentMoved && !confirmedMove)) return
    onApprove({
      note: note.trim() || null,
      enforcement: buildEnforcement(limits, governed),
      ...(environmentMoved ? { confirmedEnvironment: currentEnvironment ?? 'unclassified' } : {}),
    })
  }

  // An approval that fails shows why at the top of the dialog. Move focus there, so a screen reader reads it:
  // the message bar doesn't announce itself. The failure only ever follows a submission, so it takes focus
  // from a field too, such as the one Enter submitted from. Fluent places focus as the dialog opens.
  const errorRef = useRef<HTMLDivElement>(null)
  const shownError = useRef(error)
  useEffect(() => {
    const shown = shownError.current
    shownError.current = error
    if (!error || Object.is(error, shown)) return
    errorRef.current?.focus()
  }, [error])

  return (
    <Dialog
      open
      onOpenChange={(_, data) => {
        // Stay open while approving, so the outcome is always shown here or in the page banner.
        if (!data.open && !pending) onCancel()
      }}
    >
      <DialogSurface>
        <form onSubmit={submit}>
          <DialogBody>
            <DialogTitle>Approve access request</DialogTitle>
            <DialogContent className={styles.dialogForm}>
              {Boolean(error) && (
                <div ref={errorRef} tabIndex={-1}>
                  <ErrorState error={error} />
                </div>
              )}
              <dl className={styles.detailList}>
                <div>
                  <dt>Requester</dt>
                  <dd className={styles.cellStack}>
                    {/* Entra object IDs are GUIDs, so a label that matches the ID in any letter case is the ID again. */}
                    {requester.label.toLowerCase() !== requester.objectId.toLowerCase() && (
                      <Text className={styles.primaryCell}>{requester.label}</Text>
                    )}
                    <Text className={styles.codeValue}>{requester.objectId}</Text>
                    {!requester.registered && (
                      <Text size={200}>
                        Not registered in MOSAIC yet. Approving registers them as a user principal.
                      </Text>
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Resource</dt>
                  <dd>{resourceLabel}</dd>
                </div>
                <div>
                  <dt>Requested environment</dt>
                  <dd>
                    <EnvironmentBadge environment={requestedEnvironment} catalog={environmentCatalog} />
                  </dd>
                </div>
                <div>
                  <dt>Current environment</dt>
                  <dd>
                    <EnvironmentBadge environment={currentEnvironment} catalog={environmentCatalog} />
                  </dd>
                </div>
                <div>
                  <dt>Justification</dt>
                  <dd>{justification || 'No justification given'}</dd>
                </div>
              </dl>
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
                {governed && mcp
                  ? 'Approving creates grant intent only. API Management is unchanged until this MCP server is planned and applied from the MCPs page.'
                  : governed && publication
                    ? `Approving creates grant intent only. API Management is unchanged until the ${publication.displayName} model plan is reviewed and applied.`
                    : 'Approving creates grant intent only. MOSAIC does not apply grants for this resource to API Management, so the grant stays desired state.'}
              </Text>
              {mcp ? (
                <Text size={200}>
                  MCP servers are limited by calls, not tokens. Leave the call rate empty to add no
                  grant-specific limit.
                </Text>
              ) : (
                <>
                  <Text size={200}>
                    {prefilled && publication
                      ? `Limits are prefilled from the ${publication.displayName} publication's token limit. Change or clear them before approving.`
                      : 'This resource has no default limits to prefill.'}{' '}
                    Leave a limit empty to add no grant-specific restriction. Inherited publication
                    safeguards still apply; this does not mean unrestricted gateway access.
                  </Text>
                  <div className={styles.dialogGrid}>
                    <Field label="Tokens per minute">
                      <Input
                        type="number"
                        min={1}
                        value={limits.tokensPerMinute}
                        onChange={(_, data) => setLimits({ ...limits, tokensPerMinute: data.value })}
                      />
                    </Field>
                    <Field label="Token quota">
                      <Input
                        type="number"
                        min={1}
                        value={limits.tokenQuota}
                        onChange={(_, data) => setLimits({ ...limits, tokenQuota: data.value })}
                      />
                    </Field>
                    <Field label="Quota period">
                      <Select
                        value={limits.tokenQuotaPeriod}
                        onChange={(_, data) =>
                          setLimits({ ...limits, tokenQuotaPeriod: data.value as QuotaPeriod })
                        }
                      >
                        {QUOTA_PERIODS.map((period) => (
                          <option key={period} value={period}>
                            {period}
                          </option>
                        ))}
                      </Select>
                    </Field>
                  </div>
                </>
              )}
              <div className={styles.switchGrid}>
                <Field label="Calls" validationMessage={rateError ?? undefined}>
                  <Input
                    type="number"
                    min={1}
                    value={limits.calls}
                    onChange={(_, data) => setLimits({ ...limits, calls: data.value })}
                  />
                </Field>
                <Field label="Per how many seconds">
                  <Input
                    type="number"
                    min={1}
                    value={limits.renewalPeriodSeconds}
                    onChange={(_, data) =>
                      setLimits({ ...limits, renewalPeriodSeconds: data.value })
                    }
                  />
                </Field>
              </div>
              <Field label="Decision note" hint="Optional. Recorded with the approval.">
                <Input value={note} onChange={(_, data) => setNote(data.value)} />
              </Field>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" disabled={pending} onClick={onCancel}>
                Cancel
              </Button>
              {/* A browser takes focus off a button that becomes disabled, so a busy button stays focusable. */}
              <Button
                appearance="primary"
                type="submit"
                disabled={existingGrant || Boolean(rateError) || (environmentMoved && !confirmedMove)}
                disabledFocusable={pending}
              >
                Approve and create grant
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  )
}
