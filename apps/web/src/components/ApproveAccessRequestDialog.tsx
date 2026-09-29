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
import type { AccessRequest, AccessRequestApproval, Publication, QuotaPeriod } from '../types'
import { ErrorState } from './AsyncState'
import styles from '../pages/EntitlementsPage.module.css'

export interface ApprovalRequester {
  /** The principal's label when MOSAIC knows the requester, otherwise their Entra object ID. */
  label: string
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
  const rateError = callRateError(limits)
  const prefilled = Boolean(
    publication?.enforcement?.tokensPerMinute || publication?.enforcement?.tokenQuota,
  )
  const justification = accessRequest.justification?.trim()

  function submit(event: FormEvent) {
    event.preventDefault()
    // Approve stays focusable while pending, and a focusable submit button still submits the form.
    if (rateError || existingGrant || pending) return
    onApprove({ note: note.trim() || null, enforcement: buildEnforcement(limits, governed) })
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
                    {requester.label !== requester.objectId && (
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
                  <dt>Justification</dt>
                  <dd>{justification || 'No justification given'}</dd>
                </div>
              </dl>
              {existingGrant && (
                <MessageBar intent="warning">
                  <MessageBarBody>
                    {requester.label} already has a direct grant for {resourceLabel}. Deny this
                    request, or change the existing grant instead.
                  </MessageBarBody>
                </MessageBar>
              )}
              <Text>
                {governed && publication
                  ? `Approving creates grant intent only. API Management is unchanged until the ${publication.displayName} model plan is reviewed and applied.`
                  : 'Approving creates grant intent only. MOSAIC does not apply grants for this resource to API Management, so the grant stays desired state.'}
              </Text>
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
                disabled={existingGrant || Boolean(rateError)}
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
