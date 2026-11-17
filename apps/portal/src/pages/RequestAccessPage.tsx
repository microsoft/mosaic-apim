import { AnnounceProvider, Badge, Button, Checkbox, Field, Input, Select, Textarea, useAnnounce } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, usePortalApi } from '../api'
import { ErrorState } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { RequestFormAnswers } from '../components/RequestFormAnswers'
import { UnsavedChanges } from '../components/UnsavedChanges'
import { useFocusAwareAnnounce } from '../components/useFocusAwareAnnounce'
import { resourceFromCatalog, sameResource } from '../entitlement-format'
import { invalidateRequests } from '../request-queries'
import {
  compatibleRequestAnswers, requestAnswerErrors, requestAnswers, requestFormWarning, requestFormErrorMessage,
  type RequestAnswerDraft,
} from '../request-form'
import type { CatalogEntry, PortalEnvironment, RequestFormResolution } from '../types'

export function RequestAccessPage({ entry, environments, initialCostCenterId, initialJustification = '', onBack }: {
  entry: CatalogEntry; environments?: PortalEnvironment[]; initialCostCenterId?: string; initialJustification?: string; onBack: (costCenterId?: string) => void
}) {
  const api = usePortalApi()
  const client = useQueryClient()
  const navigate = useNavigate()
  const { announce } = useAnnounce()
  const profile = useQuery({ queryKey: ['portal', 'profile'], queryFn: api.getProfile })
  const [selectedCenter, setSelectedCenter] = useState(initialCostCenterId)
  const centerId = selectedCenter ?? profile.data?.defaultCostCenter?.id ??
    entry.costCenterAccess?.find(access => access.isDefault)?.costCenter.id ?? ''
  const access = entry.costCenterAccess?.find(item => item.costCenter.id === centerId)
  const eligible = access?.mode === 'requestOnly' && !(entry.azureCloud === 'government' && !access.keysAllowed)
  const [step, setStep] = useState(0)
  const [resolution, setResolution] = useState<RequestFormResolution | null>(null)
  const [latest, setLatest] = useState<RequestFormResolution | null>(null)
  const [values, setValues] = useState<RequestAnswerDraft>({})
  const [justification, setJustification] = useState(initialJustification)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [invalidAttempt, setInvalidAttempt] = useState(0)
  const [stale, setStale] = useState(false)
  const [notice, setNotice] = useState('')
  const [retryChecked, setRetryChecked] = useState(false)
  const [recoveredId, setRecoveredId] = useState<string | null>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const form = useRef<HTMLFormElement>(null)
  const outcome = useRef<HTMLDivElement>(null)
  const announcement = useFocusAwareAnnounce(outcome)
  const resource = resourceFromCatalog(entry)
  const resolver = useMutation({
    mutationFn: () => api.resolveRequestForm(resource, centerId),
    onSuccess: next => {
      if (resolution && next.contextRevision !== resolution.contextRevision && Object.keys(values).length) {
        setLatest(next); setStep(1)
      } else {
        setResolution(next)
        setStale(false)
        setStep(next.status === 'configured' ? 1 : 2)
      }
    },
  })
  const create = useMutation({
    mutationFn: () => {
      if (!resolution || !eligible || stale || latest) throw new Error('Review the current offering and form before submitting.')
      return api.createAccessRequest({
        resource, costCenterId: centerId, justification: justification.trim() || undefined,
        requestForm: { revision: resolution.revision, contextRevision: resolution.contextRevision,
          answers: requestAnswers(resolution.questions, values) },
      })
    },
    retry: false,
    onSuccess: () => {
      announce('Request submitted. Track it in My requests. Approval saves grant intent; gateway apply remains separate.', { polite: true })
      return invalidateRequests(client)
    },
    onError: error => {
      setRetryChecked(false)
      if (error instanceof ApiError) {
        const details = error.body?.details
        if (details?.reason === 'requestFormChanged' || details?.reason === 'requestFormRequired') {
          setStale(true); setStep(1)
        }
        let fields: Record<string, string> = {}
        if (Array.isArray(details?.fields)) for (const item of details.fields) {
          if (typeof item === 'object' && item !== null && 'questionId' in item && 'message' in item &&
            typeof item.questionId === 'string' && typeof item.message === 'string') fields = { ...fields, [item.questionId]: item.message }
        }
        setErrors(fields)
        if (Object.keys(fields).length) setStep(1)
      }
    },
  })
  const unknownOutcome = create.isError && (!(create.error instanceof ApiError) || create.error.status >= 500)
  const check = useMutation({
    mutationFn: api.listAccessRequests,
    onSuccess: requests => {
      const found = requests.find(request => request.state === 'pending' &&
        sameResource(request.resource, resource) && request.costCenterId === centerId)
      if (found) setRecoveredId(found.id)
      else { setRetryChecked(true); setNotice('No pending request was found at this check. You may explicitly retry; no automatic resubmission was made.') }
    },
  })
  const busy = resolver.isPending || create.isPending || check.isPending
  const dirty = !create.isSuccess && !recoveredId && (Object.keys(values).length > 0 || justification.length > 0 ||
    selectedCenter !== undefined && selectedCenter !== initialCostCenterId)
  useLayoutEffect(() => { heading.current?.focus() }, [step])
  useLayoutEffect(() => {
    if (invalidAttempt) form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
  }, [invalidAttempt])
  useLayoutEffect(() => {
    if (create.isError || resolver.isError || check.isError) outcome.current?.focus()
  }, [create.isError, create.error, create.submittedAt, resolver.isError, resolver.error, resolver.submittedAt, check.isError, check.error])
  useEffect(() => {
    if (create.isSuccess) navigate(`/requests?request=${encodeURIComponent(create.data.id)}`, { replace: true })
    else if (recoveredId) navigate(`/requests?request=${encodeURIComponent(recoveredId)}`, { replace: true })
  }, [create.isSuccess, create.data, recoveredId, navigate])

  function review() {
    const next = requestAnswerErrors(resolution?.questions ?? [], values)
    setErrors(next)
    if (Object.keys(next).length) setInvalidAttempt(attempt => attempt + 1)
    else setStep(2)
  }
  function answer(id: string, value: string | boolean) {
    setValues(old => ({ ...old, [id]: value }))
    setErrors(old => { const next = { ...old }; delete next[id]; return next })
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    if (step === 2 && !busy && eligible && !stale && !latest && (!unknownOutcome || retryChecked)) create.mutate()
  }
  function acceptLatest(keep: boolean) {
    if (!latest) return
    setValues(keep ? compatibleRequestAnswers(resolution?.questions ?? [], latest.questions, values) : {})
    setResolution(latest); setLatest(null); setStale(false); setErrors({})
    setNotice('The latest form is loaded. Removed, incompatible or over-limit answers were not carried forward. Changed acknowledgment wording must be acknowledged again; review all questions.')
    setStep(latest.status === 'configured' ? 1 : 2)
  }
  const error = create.isError ? create.error : resolver.isError ? resolver.error : check.isError ? check.error : null
  return <section className="governance-request" data-request-form>
    <UnsavedChanges dirty={dirty} title="Discard this access request?" />
    <Button appearance="subtle" onClick={() => onBack(centerId)} disabled={busy}>Back to Catalog</Button>
    <PageHeader title="Request access" description="Provide context for human review. Approval records grant intent; gateway apply remains separate." />
    <ol className="governance-steps" aria-label="Request steps">
      {['Offering', 'Questions', 'Review'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined}>{index + 1}. {label}</li>)}
    </ol>
    <h2 ref={heading} tabIndex={-1}>{['Confirm offering', 'Governance questions', 'Review request'][step]}</h2>
    <div className="governance-context"><strong>{entry.displayName}</strong>
      <EnvironmentBadge azureCloud={entry.azureCloud ?? 'commercial'} environment={entry.environment} environments={environments} />
      {access && <span>Charged to {access.costCenter.name} ({access.costCenter.code})</span>}
    </div>
    {error && <AnnounceProvider value={announcement}><div ref={outcome} tabIndex={-1}>
      <ErrorState context="action" title="Request not confirmed" error={new Error(requestFormErrorMessage(error))} announcementKey={create.submittedAt || resolver.submittedAt || check.submittedAt} />
    </div></AnnounceProvider>}
    {notice && <p role="status">{notice}</p>}
    <form ref={form} onSubmit={submit} className="governance-form" autoComplete="off" noValidate>
      {step === 0 && <>
        <Field label="Cost center" required hint="Only an eligible Request-only cost center can receive this individual request. A default is not permission.">
          <Select value={centerId} onChange={(_, data) => setSelectedCenter(data.value)}>
            <option value="">Choose a cost center</option>
            {centerId && !entry.costCenterAccess?.some(item => item.costCenter.id === centerId) && <option value={centerId}>Selected cost center unavailable</option>}
            {(entry.costCenterAccess ?? []).map(item => <option key={item.costCenter.id} value={item.costCenter.id}>
              {item.costCenter.name} ({item.costCenter.code}){item.mode === 'automatic' ? ' - Automatic, no individual request' : ''}
            </option>)}
          </Select>
        </Field>
        {profile.isError && <p>Your default could not load. Choose an eligible cost center explicitly.</p>}
        {!eligible && <p role="alert">Choose an eligible Request-only center. Existing membership, publication and caller/key requirements still apply.</p>}
        <p>Model and MCP grants are independent. Answering questions never grants identity, membership, funding eligibility or gateway access.</p>
      </>}
      {step === 1 && <>
        <p className="governance-warning">{requestFormWarning}</p>
        {stale && <div role="alert"><p>The form or offering context changed. Your draft is retained; reload and review before submitting.</p>
          <Button type="button" onClick={() => resolver.mutate()} disabledFocusable={busy}>Reload current form</Button></div>}
        {latest && <section aria-label="Review changed form"><h3>The form changed</h3>
          <p>Nothing was submitted. Choose how to load the latest version; only compatible answers can be retained.</p>
          <Button type="button" onClick={() => acceptLatest(true)}>Keep compatible answers and review</Button>
          <Button type="button" onClick={() => acceptLatest(false)}>Load latest with blank answers</Button>
        </section>}
        {resolution && <Badge appearance="outline">Form v{resolution.revision}</Badge>}
        {(resolution?.questions ?? []).map(question => <Field key={question.id} label={question.label}
          required={question.required} hint={question.helpText ?? undefined}
          validationState={Object.hasOwn(errors, question.id) ? 'error' : 'none'} validationMessage={Object.hasOwn(errors, question.id) ? errors[question.id] : undefined}>
          {question.kind === 'acknowledgment' ? <Checkbox checked={values[question.id] === true}
            label="I acknowledge this statement" aria-invalid={Object.hasOwn(errors, question.id)}
            onChange={(_, data) => answer(question.id, data.checked === true)} />
            : question.kind === 'singleChoice' ? <Select value={typeof values[question.id] === 'string' ? String(values[question.id]) : ''}
              aria-invalid={Object.hasOwn(errors, question.id)} onChange={(_, data) => answer(question.id, data.value)}>
              <option value="">Choose an answer</option>{question.options.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
            </Select> : question.kind === 'shortText'
              ? <Input value={typeof values[question.id] === 'string' ? String(values[question.id]) : ''}
                aria-invalid={Object.hasOwn(errors, question.id)} onChange={(_, data) => answer(question.id, data.value)} />
              : <Textarea value={typeof values[question.id] === 'string' ? String(values[question.id]) : ''}
                rows={3} resize="vertical" aria-invalid={Object.hasOwn(errors, question.id)}
                onChange={(_, data) => answer(question.id, data.value)} />}
          {question.maxLength && <small>Maximum {question.maxLength.toLocaleString()} characters. No secrets or datasets.</small>}
        </Field>)}
        <Field label="Justification (optional)" hint="The existing optional note remains separate from configured questions.">
          <Textarea value={justification} onChange={(_, data) => setJustification(data.value)} resize="vertical" />
        </Field>
      </>}
      {step === 2 && resolution && <>
        {resolution.questions.length > 0 ? <RequestFormAnswers draft form={{
          formatVersion: 1, revision: resolution.revision, questions: resolution.questions,
          answers: requestAnswers(resolution.questions, values),
        }} /> : <p>No additional questions apply. The selected cost center and optional justification are still reviewed.</p>}
        <p><strong>Justification:</strong> {justification.trim() || 'Not provided'}</p>
        <p>Submission records this version for human review. It does not create membership, an entitlement, a relaxed limit or an applied gateway change.</p>
        {unknownOutcome && !retryChecked && <Button type="button" disabledFocusable={check.isPending}
          onClick={() => { if (!check.isPending) check.mutate() }}>Check My requests before retry</Button>}
      </>}
      <footer className="governance-actions">
        {step > 0 && <Button type="button" disabled={busy} onClick={() => setStep(step - 1)}>Back</Button>}
        {step === 0 && <Button type="button" appearance="primary" disabled={!eligible} disabledFocusable={busy}
          onClick={() => { if (!busy && eligible) resolver.mutate() }}>Next: Questions</Button>}
        {step === 1 && <Button type="button" appearance="primary" disabled={stale || Boolean(latest) || !resolution} disabledFocusable={busy}
          onClick={() => { if (!busy) review() }}>Next: Review</Button>}
        {step === 2 && <Button type="submit" appearance="primary" disabled={!eligible || stale || Boolean(latest) || unknownOutcome && !retryChecked}
          disabledFocusable={busy}>Request access</Button>}
      </footer>
    </form>
  </section>
}
