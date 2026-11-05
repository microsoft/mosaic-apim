import { Button, Field, MessageBar, MessageBarBody, Textarea } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { usePortalApi } from '../api'
import { ErrorState, Loading } from '../components/AsyncState'
import { PageHeader } from '../components/PageHeader'
import { UnsavedChanges } from '../components/UnsavedChanges'
import { costCenterDirectoryKey, costCenterPolicyKey, invalidateRequests } from '../request-queries'

export function RequestCostCenterMembershipPage() {
  const api = usePortalApi()
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [justification, setJustification] = useState('')
  const [validation, setValidation] = useState<string | null>(null)
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const errorRef = useRef<HTMLDivElement>(null)
  const center = useQuery({
    queryKey: [...costCenterDirectoryKey, 'detail', id],
    queryFn: () => {
      if (!id) throw new Error('No cost center selected')
      return api.getDiscoverableCostCenter(id)
    },
    retry: false,
  })
  const policy = useQuery({ queryKey: costCenterPolicyKey, queryFn: api.getCostCenterSelfServiceSettings })
  const create = useMutation({
    mutationFn: () => {
      if (!id) throw new Error('No cost center selected')
      return api.createCostCenterMembershipRequest({ costCenterId: id, justification: justification.trim(), idempotencyKey })
    },
    retry: false,
    onSuccess: () => { void invalidateRequests(queryClient) },
  })
  useEffect(() => {
    if (create.isSuccess) navigate(`/requests?request=${encodeURIComponent(create.data.id)}&requestKind=costCenterMembership`, { replace: true })
  }, [create.isSuccess, create.data, navigate])
  useEffect(() => {
    if (create.isError) errorRef.current?.focus()
  }, [create.isError, create.error])
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!justification.trim()) {
      setValidation('Explain why you need to join this cost center.')
      return
    }
    if (policy.data?.membershipRequestsEnabled && center.data?.membershipState === 'notMember' && !create.isPending) create.mutate()
  }
  const canJoin = policy.data?.membershipRequestsEnabled && center.data?.membershipState === 'notMember' && !center.data.builtIn
  return <>
    <UnsavedChanges dirty={Boolean(justification) && !create.isSuccess} title="Discard this membership request?" />
    <div className="workspace-back"><Button appearance="subtle" onClick={() => navigate('/cost-centers?view=discover')}>Back to My cost centers</Button></div>
    <PageHeader title="Request to join a cost center" description="An administrator reviews your request before adding you to the center's real Entra group." />
    {(center.isPending || policy.isPending) && <Loading label="Checking cost-center membership and policy" />}
    {center.isError && <><ErrorState error={center.error} /><Button onClick={() => void center.refetch()}>Retry cost center</Button></>}
    {policy.isError && <><ErrorState error={policy.error} /><Button onClick={() => void policy.refetch()}>Retry cost-center settings</Button></>}
    {center.data && <h2>{center.data.name} ({center.data.code})</h2>}
    {policy.data && !policy.data.membershipRequestsEnabled && <MessageBar intent="info"><MessageBarBody>
      Your administrator has turned off new membership requests. Existing requests are unchanged.
    </MessageBarBody></MessageBar>}
    {center.data?.membershipState === 'member' && <p>You are already a member. <Link to="/cost-centers">View My cost centers</Link>.</p>}
    {center.data?.builtIn && <p>General is a built-in charging option, not a group you can join.</p>}
    {center.data?.membershipState === 'unknown' && <MessageBar intent="warning"><MessageBarBody>
      Membership could not be verified. Retry the cost-center check before submitting a request.
      {' '}<Button onClick={() => void center.refetch()}>Retry membership verification</Button>
    </MessageBarBody></MessageBar>}
    {canJoin && <form onSubmit={submit} noValidate className="cost-center-request-form request-form-section" aria-label="Request cost-center membership">
      <p>{center.data?.publicPurpose}</p>
      <p>Joining can unlock already-applied Automatic offerings and may affect other applications using the group.
        Request-only offerings still require resource approval. No key is created, and pooled quotas and budgets do not increase.</p>
      <Field label="Purpose / justification" required validationMessage={validation ?? undefined} validationState={validation ? 'error' : 'none'}>
        <Textarea autoFocus required maxLength={4000} resize="vertical" value={justification} disabled={create.isPending}
          onChange={(_, data) => { setJustification(data.value); setValidation(null) }} />
      </Field>
      {create.isError && <div ref={errorRef} tabIndex={-1}><ErrorState error={create.error} />
        <p>Your draft is preserved. Retry uses the same submission ID.</p></div>}
      <div className="request-actions">
        <Button type="submit" appearance="primary" disabled={create.isPending}>{create.isPending ? 'Submitting request' : 'Submit join request'}</Button>
        <Button type="button" disabled={create.isPending} onClick={() => navigate('/cost-centers?view=discover')}>Cancel</Button>
      </div>
    </form>}
  </>
}
