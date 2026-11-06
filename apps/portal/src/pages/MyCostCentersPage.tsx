import { Badge, Button, Field, Input, MessageBar, MessageBarBody, Tab, TabList } from '@fluentui/react-components'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { DetailPanel } from '../components/DetailPanel'
import { PageHeader } from '../components/PageHeader'
import { membershipOutcome } from '../cost-center-membership'
import { costCenterDirectoryKey, costCenterPolicyKey, membershipRequestsKey, myCostCentersKey } from '../request-queries'
import type { MembershipCostCenter } from '../types'

const membershipLabels: Record<MembershipCostCenter['membershipState'], string> = {
  member: 'Member',
  notMember: 'Not a member',
  unknown: 'Membership unavailable',
  exempt: 'Built-in charging option',
}

export function MyCostCentersPage() {
  const api = usePortalApi()
  const [params, setParams] = useSearchParams()
  const discover = params.get('view') === 'discover'
  const query = params.get('q') ?? ''
  const [draftSearch, setDraftSearch] = useState(query)
  const selectedId = params.get('center')
  const policy = useQuery({ queryKey: costCenterPolicyKey, queryFn: api.getCostCenterSelfServiceSettings })
  const requests = useQuery({ queryKey: membershipRequestsKey, queryFn: api.listCostCenterMembershipRequests })
  const centers = useInfiniteQuery({
    queryKey: [...(discover ? costCenterDirectoryKey : myCostCentersKey), query],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => (discover ? api.searchCostCenters : api.listMyCostCenters)({ q: query, cursor: pageParam }),
    getNextPageParam: (page) => page.nextCursor ?? undefined,
  })
  const rows = centers.data?.pages.flatMap((page) => page.items) ?? []
  const listedSelection = rows.find((item) => item.id === selectedId)
  const detail = useQuery({
    queryKey: [...costCenterDirectoryKey, 'detail', selectedId],
    queryFn: () => {
      if (!selectedId) throw new Error('No cost center selected')
      return api.getDiscoverableCostCenter(selectedId)
    },
    enabled: Boolean(selectedId && !listedSelection),
    retry: false,
  })
  const selected = listedSelection ?? detail.data
  const membershipError = centers.data?.pages.find((page) => page.membershipError)?.membershipError
  function updateParams(change: Record<string, string | null>) {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(change)) {
      if (value === null) next.delete(key)
      else next.set(key, value)
    }
    setParams(next)
  }
  function search(event: FormEvent) {
    event.preventDefault()
    updateParams({ q: draftSearch.trim() || null, center: null })
  }
  function activeRequest(centerId: string) {
    return requests.data?.find((request) => request.costCenter.id === centerId &&
      (request.state === 'pending' || (request.state === 'approved' && request.membershipState !== 'succeeded')))
  }
  function joinAction(center: MembershipCostCenter) {
    const request = activeRequest(center.id)
    if (request) return <Link to={`/requests?request=${encodeURIComponent(request.id)}&requestKind=costCenterMembership`}>
      {request.state === 'pending' ? 'View pending join request' : 'View membership progress'}
    </Link>
    if (center.membershipState !== 'notMember' || !policy.data?.membershipRequestsEnabled || !requests.isSuccess) return null
    return <Link className="workspace-primary-link" to={`/cost-centers/${encodeURIComponent(center.id)}/join`}>Request to join</Link>
  }
  return <>
    <PageHeader title="My cost centers"
      description="Your charging and membership boundaries. Membership and permission to use a resource are separate."
      actions={policy.data?.creationRequestsEnabled
        ? <Link className="workspace-primary-link" to="/cost-centers/new">Request a cost center</Link>
        : <Button disabled>Request a cost center</Button>} />
    {policy.isError && <div className="request-load-error"><ErrorState error={policy.error} />
      <Button onClick={() => void policy.refetch()}>Retry cost-center settings</Button></div>}
    {policy.data && (!policy.data.creationRequestsEnabled || !policy.data.membershipRequestsEnabled) && <MessageBar intent="info">
      <MessageBarBody>
        {!policy.data.creationRequestsEnabled && 'Your administrator has turned off new cost-center creation requests. '}
        {!policy.data.membershipRequestsEnabled && 'Your administrator has turned off new membership requests. '}
        Existing memberships and requests are unchanged.
      </MessageBarBody>
    </MessageBar>}
    <TabList selectedValue={discover ? 'discover' : 'mine'} onTabSelect={(_, data) => {
      setDraftSearch('')
      updateParams({ view: data.value === 'discover' ? 'discover' : null, q: null, center: null })
    }} aria-label="Cost-center views">
      <Tab value="mine">My memberships</Tab>
      <Tab value="discover">Find cost centers</Tab>
    </TabList>
    <form onSubmit={search} className="cost-center-search">
      <Field label={discover ? 'Search published cost centers' : 'Search my cost centers'}>
        <Input value={draftSearch} onChange={(_, data) => setDraftSearch(data.value)} maxLength={120} />
      </Field>
      <Button type="submit">Search</Button>
    </form>
    {discover && <p className="connection-note">Only cost centers published by an administrator appear here. Requesting to join does not add you to the group.</p>}
    {membershipError && <MessageBar intent="warning"><MessageBarBody>
      {membershipError} Membership is not assumed from your grants or old tokens.
      {' '}<Button onClick={() => void centers.refetch()}>Retry membership verification</Button>
    </MessageBarBody></MessageBar>}
    {requests.isError && <div className="request-load-error"><ErrorState error={requests.error} />
      <Button onClick={() => void requests.refetch()}>Retry membership requests</Button></div>}
    {centers.isPending && <Loading label="Loading cost centers" />}
    {centers.isError && <div className="request-load-error"><ErrorState error={centers.error} />
      <Button onClick={() => void centers.refetch()}>Retry cost centers</Button></div>}
    {centers.isSuccess && !rows.length && <EmptyState title={query ? 'No cost centers match your search' : discover ? 'No published cost centers' : 'No memberships found'}>
      {discover ? 'An administrator must publish a supported cost center before you can find and request to join it.'
        : 'Find a published cost center or request a new one. General is a charging option, not a group membership.'}
    </EmptyState>}
    <ul className="cost-center-list" aria-label={discover ? 'Published cost centers' : 'My cost centers'}>
      {rows.map((center) => {
        const request = activeRequest(center.id)
        return <li key={center.id}>
          <div className="cost-center-row">
            <div>
              <Button appearance="transparent" className="cost-center-title" onClick={() => updateParams({ center: center.id })}>{center.name}</Button>
              <p>{center.code}{center.publicPurpose ? ` - ${center.publicPurpose}` : ''}</p>
            </div>
            <div className="cost-center-row-actions">
              <Badge appearance="outline">{membershipLabels[center.membershipState]}</Badge>
              {joinAction(center)}
            </div>
          </div>
          {request && <p className="connection-note">{request.state === 'pending' ? 'Join request pending administrator review.' : membershipOutcome(request)}</p>}
        </li>
      })}
    </ul>
    {centers.hasNextPage && <Button disabled={centers.isFetchingNextPage} onClick={() => void centers.fetchNextPage()}>
      {centers.isFetchingNextPage ? 'Loading more cost centers' : 'Load more cost centers'}
    </Button>}
    {selectedId && <DetailPanel title={selected?.name ?? 'Cost center'} onClose={() => updateParams({ center: null })}>
      {!selected && detail.isPending && <Loading label="Loading cost-center details" />}
      {!selected && detail.isError && <><ErrorState error={detail.error} />
        <Button onClick={() => void detail.refetch()}>Retry cost-center details</Button></>}
      {selected && <>
        <Badge appearance="outline">{membershipLabels[selected.membershipState]}</Badge>
        {selected.publicPurpose && <p>{selected.publicPurpose}</p>}
        <dl className="metadata-list"><div><dt>Code</dt><dd>{selected.code}</dd></div>
          <div><dt>Current sign-in eligibility</dt><dd>{selected.tokenEligible ? 'Eligible to select this charging option' : 'Not proven by your current token'}</dd></div></dl>
        <p>Membership can unlock Automatic offerings only after their grants are applied and a fresh token includes the group.
          Request-only resources still need approval. Center budgets, pooled quotas, and key restrictions remain unchanged.</p>
        {selected.membershipState === 'member' && !selected.tokenEligible && <MessageBar intent="info"><MessageBarBody>
          The directory confirms membership, but your current sign-in token does not prove it.
          Directory propagation and a new sign-in token may be needed. Group-claim overage is not bypassed.
        </MessageBarBody></MessageBar>}
        {selected.builtIn && <p>General is available under its existing resource rules. There is no Entra group to join.</p>}
        <div className="cost-center-detail-actions">{joinAction(selected)}
          <Link to={`/access?costCenter=${encodeURIComponent(selected.id)}`}>View my resource access</Link>
          <Link to={`/catalog?costCenter=${encodeURIComponent(selected.id)}`}>Browse resource offerings</Link></div>
      </>}
    </DetailPanel>}
  </>
}
