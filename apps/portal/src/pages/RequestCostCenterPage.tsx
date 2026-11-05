import { Button, Field, Input, MessageBar, MessageBarBody, MessageBarTitle, Textarea } from '@fluentui/react-components'
import { ArrowLeftRegular } from '@fluentui/react-icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, usePortalApi } from '../api'
import { PageHeader } from '../components/PageHeader'
import { UnsavedChanges } from '../components/UnsavedChanges'
import { costCenterPolicyKey, invalidateRequests } from '../request-queries'
import { ErrorState, Loading } from '../components/AsyncState'
import type { CostCenterRequestCreate } from '../types'

type FormField = 'name' | 'code' | 'description' | 'justification' | 'suggestedGroupObjectId'
type FormErrors = Partial<Record<FormField, string>>
const groupIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const fields: FormField[] = ['name', 'code', 'description', 'justification', 'suggestedGroupObjectId']

function validationErrors(error: unknown): FormErrors {
  if (!(error instanceof ApiError) || !Array.isArray(error.body?.detail)) return {}
  const errors: FormErrors = {}
  for (const detail of error.body.detail) {
    if (typeof detail !== 'object' || detail === null || !('loc' in detail) || !('msg' in detail) ||
      !Array.isArray(detail.loc) || typeof detail.msg !== 'string') continue
    const field = fields.find((candidate) => detail.loc.includes(candidate))
    if (field) errors[field] = detail.msg.replace(/^Value error, /, '')
  }
  return errors
}

export function RequestCostCenterPage() {
  const api = usePortalApi()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const policy = useQuery({ queryKey: costCenterPolicyKey, queryFn: api.getCostCenterSelfServiceSettings })
  const [values, setValues] = useState<Record<FormField, string>>({
    name: '', code: '', description: '', justification: '', suggestedGroupObjectId: '',
  })
  const [errors, setErrors] = useState<FormErrors>({})
  const [invalidSubmission, setInvalidSubmission] = useState(0)
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const formRef = useRef<HTMLFormElement>(null)
  const errorRef = useRef<HTMLDivElement>(null)
  const create = useMutation({
    mutationFn: (payload: CostCenterRequestCreate) => api.createCostCenterRequest(payload),
    onSuccess: () => { void invalidateRequests(queryClient) },
    onError: (error) => setErrors(validationErrors(error)),
    retry: false,
  })
  const dirty = Object.values(values).some((value) => value.length > 0) && !create.isSuccess

  useEffect(() => {
    if (create.isSuccess) {
      navigate(`/requests?request=${encodeURIComponent(create.data.id)}&requestKind=costCenter`, { replace: true })
    }
  }, [create.isSuccess, create.data, navigate])
  useEffect(() => {
    if (create.isError) errorRef.current?.focus()
  }, [create.isError, create.error])
  useEffect(() => {
    if (invalidSubmission) formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
  }, [invalidSubmission])

  function update(field: FormField, value: string) {
    setValues((current) => ({ ...current, [field]: value }))
    setErrors((current) => ({ ...current, [field]: undefined }))
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (create.isPending || !policy.data?.creationRequestsEnabled) return
    const nextErrors: FormErrors = {}
    if (!values.name.trim()) nextErrors.name = 'Enter a name for the cost center.'
    if (!values.justification.trim()) nextErrors.justification = 'Explain why you need this cost center.'
    if (values.suggestedGroupObjectId.trim() && !groupIdPattern.test(values.suggestedGroupObjectId.trim())) {
      nextErrors.suggestedGroupObjectId = 'Enter a group object ID in GUID format, or leave this blank.'
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length) {
      setInvalidSubmission((attempt) => attempt + 1)
      return
    }
    create.mutate({
      name: values.name.trim(),
      code: values.code.trim() || undefined,
      description: values.description.trim() || undefined,
      justification: values.justification.trim(),
      suggestedGroupObjectId: values.suggestedGroupObjectId.trim() || undefined,
      idempotencyKey,
    })
  }

  return <>
    <UnsavedChanges dirty={dirty} title="Discard this cost-center request?" />
    <div className="workspace-back">
      <Button appearance="subtle" icon={<ArrowLeftRegular />} onClick={() => navigate('/cost-centers')}>Back to My cost centers</Button>
    </div>
    <PageHeader title="Request a cost center" description="Propose a charging and access boundary for your team. An administrator reviews the request and configures its Entra group and resource access." />
    {policy.isPending && <Loading label="Checking cost-center settings" />}
    {policy.isError && <><ErrorState error={policy.error} /><Button onClick={() => void policy.refetch()}>Retry cost-center settings</Button></>}
    {policy.data && !policy.data.creationRequestsEnabled && <MessageBar intent="info"><MessageBarBody>
      Your administrator has turned off new cost-center creation requests. Existing requests are unchanged.
    </MessageBarBody></MessageBar>}
    {policy.data?.creationRequestsEnabled && <form ref={formRef} onSubmit={submit} className="cost-center-request-form" noValidate aria-label="Request a cost center">
      {create.isError && <div ref={errorRef} tabIndex={-1}>
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Request not confirmed</MessageBarTitle>
            {Object.values(errors).some(Boolean) ? 'Check the highlighted fields and submit again.' : create.error.message}
            {' '}Your draft is preserved. Retry this form to use the same submission ID.
          </MessageBarBody>
        </MessageBar>
      </div>}
      <section className="request-form-section" aria-labelledby="cost-center-details">
        <h2 id="cost-center-details">Cost-center details</h2>
        <p>Choose a clear name and explain the work it will support. A code is optional; the administrator can choose one during review.</p>
        <div className="request-form-fields">
          <Field label="Name" required validationMessage={errors.name} validationState={errors.name ? 'error' : 'none'}>
            <Input autoFocus maxLength={120} value={values.name} onChange={(_, data) => update('name', data.value)} disabled={create.isPending} />
          </Field>
          <Field label="Code (optional)" validationMessage={errors.code} validationState={errors.code ? 'error' : 'none'}>
            <Input maxLength={64} value={values.code} onChange={(_, data) => update('code', data.value)} disabled={create.isPending} />
          </Field>
          <Field label="Description (optional)" validationMessage={errors.description} validationState={errors.description ? 'error' : 'none'}>
            <Textarea maxLength={1000} resize="vertical" value={values.description} onChange={(_, data) => update('description', data.value)} disabled={create.isPending} />
          </Field>
          <Field label="Purpose / justification" required validationMessage={errors.justification} validationState={errors.justification ? 'error' : 'none'}>
            <Textarea maxLength={4000} resize="vertical" value={values.justification} onChange={(_, data) => update('justification', data.value)} disabled={create.isPending} />
          </Field>
        </div>
      </section>
      <section className="request-form-section" aria-labelledby="cost-center-group">
        <h2 id="cost-center-group">Suggest an existing Entra group</h2>
        <p>If you already know the group object ID, you can suggest it. This does not connect the group, change its membership, or grant access. The administrator may use a different group.</p>
        <Field label="Suggested group object ID (optional)" validationMessage={errors.suggestedGroupObjectId} validationState={errors.suggestedGroupObjectId ? 'error' : 'none'}>
          <Input value={values.suggestedGroupObjectId} onChange={(_, data) => update('suggestedGroupObjectId', data.value)} disabled={create.isPending} />
        </Field>
      </section>
      <div className="request-submit">
        <p>Submitting does not add you to the group. An administrator must explicitly choose whether to request your membership. Approval creates one cost center; group setup and gateway access may still be pending.</p>
        <div className="request-actions">
          <Button type="submit" appearance="primary" disabled={create.isPending}>{create.isPending ? 'Submitting request…' : 'Submit request'}</Button>
          <Button type="button" onClick={() => navigate('/cost-centers')} disabled={create.isPending}>Cancel</Button>
        </div>
      </div>
    </form>}
  </>
}
