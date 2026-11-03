import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, MessageBar, MessageBarBody, Text, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { MEMBERSHIP_KEY_WARNING, MEMBERSHIP_PROPAGATION_WARNING, supportedDirectoryMember, workloadDirectoryMember } from '../cost-centers'
import { useCostCenterCapabilities } from '../cost-center-setup'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { CostCenter, CostCenterDirectMember, CostCenterDirectoryOperationView, DirectoryObject } from '../types'
import { ErrorState } from './AsyncState'
import { CostCenterCapabilityNotice } from './CostCenterSetupForm'
import { CostCenterDirectoryPicker } from './CostCenterDirectoryPicker'
import { CostCenterDirectoryOperations } from './CostCenterDirectoryOperations'
import { IdentityAvatar } from './IdentityAvatar'
import styles from './CostCenterSetup.module.css'

interface MemberIntent { objectId: string; memberKind: DirectoryObject['kind']; key: string }

export function CostCenterDirectoryMembers({ costCenter, onSetup }: { costCenter: CostCenter; onSetup: () => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const capabilities = useCostCenterCapabilities()
  const [cursors, setCursors] = useState<Array<string | undefined>>([undefined])
  const cursor = cursors[cursors.length - 1]
  const [selection, setSelection] = useState<DirectoryObject | null>(null)
  const addIntent = useRef<MemberIntent | null>(null)
  const [removeIntent, setRemoveIntent] = useState<{ member: CostCenterDirectMember; key: string; recovering: boolean } | null>(null)
  const [lastOperation, setLastOperation] = useState<CostCenterDirectoryOperationView | null>(null)
  const removeKeys = useRef(new Map<string, { key: string; operationId?: string }>())
  const operations = useQuery({
    queryKey: ['cost-centers', costCenter.id, 'directory-operations'],
    queryFn: () => api.listCostCenterDirectoryOperations(costCenter.id),
    enabled: costCenter.setupState !== 'exempt',
    retry: false,
  })
  const ready = costCenter.setupState === 'ready' && costCenter.entraBinding?.verificationState === 'verified'
  const members = useQuery({
    queryKey: ['cost-centers', costCenter.id, 'directory-members', { cursor }],
    queryFn: () => api.listCostCenterDirectoryMembers(costCenter.id, { pageSize: 100, ...(cursor ? { cursor } : {}) }),
    enabled: ready,
    retry: false,
    refetchOnWindowFocus: false,
  })
  useEffect(() => {
    for (const [objectId, intent] of removeKeys.current) {
      if (operations.data?.some((operation) => operation.id === intent.operationId
        && operation.state === 'succeeded' && operation.directoryUpdated)) removeKeys.current.delete(objectId)
    }
  }, [operations.data])
  async function record(operation: CostCenterDirectoryOperationView) {
    setLastOperation(operation)
    queryClient.setQueryData<CostCenterDirectoryOperationView[]>(
      ['cost-centers', costCenter.id, 'directory-operations'],
      (current) => [operation, ...(current ?? []).filter((item) => item.id !== operation.id)],
    )
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenter.id, 'directory-operations'] }),
      queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenter.id] }),
      queryClient.invalidateQueries({ queryKey: ['cost-center-requests'] }),
      ...(operation.state === 'succeeded' && operation.directoryUpdated
        ? [queryClient.invalidateQueries({ queryKey: ['entitlements'] })] : []),
    ])
  }
  const add = useMutation({
    mutationFn: (intent: MemberIntent) => api.addCostCenterDirectoryMember(costCenter.id, intent.objectId, intent.memberKind, intent.key),
    retry: false,
    onSuccess: async (operation) => {
      addIntent.current = null
      setSelection(null)
      await record(operation)
    },
  })
  const remove = useMutation({
    mutationFn: (intent: NonNullable<typeof removeIntent>) => {
      const objectId = intent.member.objectId.toLowerCase()
      removeKeys.current.set(objectId, { ...removeKeys.current.get(objectId), key: intent.key })
      return api.removeCostCenterDirectoryMember(costCenter.id, intent.member.objectId, intent.key)
    },
    retry: false,
    onSuccess: async (operation, intent) => {
      if (operation.state === 'succeeded' && operation.directoryUpdated) removeKeys.current.delete(intent.member.objectId.toLowerCase())
      else removeKeys.current.set(intent.member.objectId.toLowerCase(), { key: intent.key, operationId: operation.id })
      setRemoveIntent(null)
      await record(operation)
    },
  })
  const permissionFor = (kind: DirectoryObject['kind'] | null) => Boolean(kind && supportedDirectoryMember(kind)
    && (workloadDirectoryMember(kind) ? capabilities.data?.canAddServicePrincipals : capabilities.data?.canAddUsers))
  const frozenAdd = add.isPending || (add.isError && Boolean(addIntent.current))
  const historyReady = operations.isSuccess && !operations.isFetching
  const observedLastOperation = lastOperation
    ? operations.data?.find((operation) => operation.id === lastOperation.id) ?? lastOperation : null
  function interruptedRemoval(objectId: string) {
    const intent = removeKeys.current.get(objectId.toLowerCase())
    return intent?.operationId && operations.data?.some((operation) => operation.id === intent.operationId
      && operation.state === 'succeeded' && operation.directoryUpdated) ? undefined : intent
  }
  const uncertainFor = (objectId: string) => operations.data?.some((operation) =>
    operation.memberObjectId?.toLowerCase() === objectId.toLowerCase() && (operation.state === 'pending' || operation.state === 'unknown'))
  function addSelected() {
    if (!selection || !ready || !permissionFor(selection.kind) || (!addIntent.current && (!historyReady || uncertainFor(selection.objectId)))) return
    const intent = addIntent.current ?? { objectId: selection.objectId, memberKind: selection.kind, key: crypto.randomUUID() }
    addIntent.current = intent
    add.mutate(intent)
  }
  function refreshFirstPage() {
    setCursors([undefined])
    void queryClient.invalidateQueries({ queryKey: ['cost-centers', costCenter.id, 'directory-members'] })
  }
  if (costCenter.setupState === 'exempt') return <section className={styles.stack}>
    <Title3 as="h2">Members</Title3>
    <Text>General is exempt from dedicated Entra group setup by its stable identity. Its legacy eligibility rules remain unchanged. This workspace does not offer a local shadow roster.</Text>
    <Link to={`/entitlements?costCenter=${encodeURIComponent(costCenter.id)}`}>Review individual grants and keys</Link>
  </section>
  return <div className={styles.stack}>
    <section className={styles.stack} aria-label="Entra group members">
      <Title3 as="h2">Members</Title3>
      {!ready ? <MessageBar intent="warning"><MessageBarBody>
        Setup required. No local member list is used as authorization.
        {' '}<Button onClick={onSetup}>Finish group setup</Button>
      </MessageBarBody></MessageBar> : <>
        <Text>Actual direct members of the verified Entra group, not MOSAIC principal records. Nested membership is not flattened or removable through this editor.</Text>
        <Text className={styles.technical}>Group: {costCenter.entraBinding!.groupObjectId}</Text>
        {capabilities.isError && <ErrorState title="Membership capabilities unavailable" error={capabilities.error} />}
        {!historyReady && <MessageBar intent={operations.isError ? 'warning' : 'info'}><MessageBarBody>
          {operations.isError ? 'Directory operation history is unavailable. New membership writes are disabled until it can be refreshed.'
            : 'Checking durable operation history before enabling new membership writes…'}
          {operations.isError && <> <Button onClick={() => void operations.refetch()}>Refresh operation history before editing</Button></>}
        </MessageBarBody></MessageBar>}
        <MessageBar intent="warning"><MessageBarBody>{members.data?.previewNotice ?? capabilities.data?.memberListingNotice
          ?? 'Listing uses Microsoft Graph beta to include service principals omitted by v1.0. Beta is a preview and is not supported for production applications.'}</MessageBarBody></MessageBar>
        <details>
          <summary>Membership-write capabilities</summary>
          <div className={styles.stack}>
            {capabilities.isPending && <Text role="status">Checking membership capabilities…</Text>}
            {capabilities.data && <CostCenterCapabilityNotice capabilities={capabilities.data} />}
            <Button onClick={() => void capabilities.refetch()} disabled={capabilities.isFetching}>Refresh membership capabilities</Button>
          </div>
        </details>
        {capabilities.isSuccess && !capabilities.data.canAddUsers && !capabilities.data.canAddServicePrincipals && !capabilities.data.canRemoveMembers && <MessageBar intent="info"><MessageBarBody>
          Membership is read-only with the observed permissions. The verified group can still support runtime token access; missing write consent does not unconfigure the center.
        </MessageBarBody></MessageBar>}
        {observedLastOperation && <MessageBar intent={observedLastOperation.state === 'succeeded' && observedLastOperation.directoryUpdated ? 'info' : 'warning'}><MessageBarBody>
          {observedLastOperation.state === 'succeeded' && observedLastOperation.directoryUpdated
            ? 'Directory updated. Propagation and token refresh remain pending; this does not confirm runtime access or key revocation.'
            : `Directory change not confirmed: ${observedLastOperation.state}. Inspect its recorded operation; do not assume success or repeat an uncertain write.`}
          {' '}This describes the recorded operation, not a fresh verification of current membership. Operation ID: {observedLastOperation.id}
        </MessageBarBody></MessageBar>}
        <div className={styles.actions}>
          <Button onClick={refreshFirstPage} disabled={members.isFetching}>Refresh members from first page</Button>
          <Text size={200}>Page {cursors.length} · up to 100 direct members. Paging replaces the visible page; refresh restarts it. Directory changes can shift page boundaries.</Text>
        </div>
        {members.isPending && <Text role="status">Loading direct directory members…</Text>}
        {members.isError && <div className={styles.stack}><ErrorState title="Directory member list unavailable" error={members.error} />
          <Button disabled={members.isFetching} onClick={() => void members.refetch()}>Retry this member page</Button></div>}
        {members.isSuccess && <ul className={`${styles.results} ${styles.members}`} aria-label="Direct Entra members">
          {members.data.members.length === 0 && <li>No direct members on this page. Add a person or workload identity explicitly.</li>}
          {members.data.members.map((member) => {
            const supported = supportedDirectoryMember(member.kind)
            const removalAllowed = historyReady && supported && capabilities.data?.canRemoveMembers && permissionFor(member.kind) && !uncertainFor(member.objectId)
            const interrupted = interruptedRemoval(member.objectId)
            return <li key={member.objectId} className={styles.member}>
              <div className={styles.memberIdentity}>
                {member.kind && <IdentityAvatar name={member.displayName || 'Unnamed directory member'} kind={member.kind} size={32} />}
                <div>
                  <Text weight="semibold">{member.displayName || 'Unnamed directory member'}</Text>
                  <Text>{member.kind ? PRINCIPAL_KIND_LABELS[member.kind] : member.odataType} · Direct member</Text>
                  {member.detail && <Text>{member.detail}</Text>}
                  <details><summary>Member technical identity</summary>
                    <Text className={styles.technical}>Object ID: {member.objectId}</Text>
                    {member.appId && <Text block className={styles.technical}>Application ID: {member.appId}</Text>}
                  </details>
                  {!supported && <Text>Unsupported member type. Nested groups and unknown directory types are read-only; manage their relationship in Entra.</Text>}
                  {supported && (!capabilities.data?.canRemoveMembers || !permissionFor(member.kind)) && <Text>Removal unavailable with the observed user/workload permissions. Group-specific delegation may also restrict writes.</Text>}
                  {uncertainFor(member.objectId) && <Text>A previous write for this member is pending or unknown. Inspect its durable operation and recover without another write.</Text>}
                </div>
              </div>
              <Button disabled={!removalAllowed || remove.isPending} onClick={() => {
                remove.reset()
                const original = interruptedRemoval(member.objectId)
                setRemoveIntent({ member, key: original?.key ?? crypto.randomUUID(), recovering: Boolean(original) })
              }} aria-label={interrupted ? `Retrieve prior removal for ${member.displayName || 'directory member'}` : `Remove ${member.displayName || 'directory member'}`}>
                {interrupted ? 'Retrieve prior removal' : 'Remove'}
              </Button>
            </li>
          })}
        </ul>}
        <div className={styles.actions}>
          <Button disabled={cursors.length === 1 || members.isFetching} onClick={() => setCursors((current) => current.slice(0, -1))}>Previous member page</Button>
          <Button disabled={!members.data?.nextCursor || members.isFetching || members.isError}
            onClick={() => { if (members.data?.nextCursor) setCursors((current) => [...current, members.data!.nextCursor!]) }}>Next member page</Button>
        </div>
        <section className={styles.section} aria-label="Add real directory member">
          <Title3 as="h3">Add directory member</Title3>
          <CostCenterDirectoryPicker selected={selection} disabled={frozenAdd || !capabilities.data?.available || !historyReady}
            onSelect={(value) => { setSelection(value); add.reset() }} />
          {selection && !permissionFor(selection.kind) && <Text>{workloadDirectoryMember(selection.kind)
            ? 'Adding this workload requires separately consented Application.ReadWrite.All and membership-write permission.'
            : 'Adding this person requires separately consented GroupMember.ReadWrite.All.'}</Text>}
          {selection && uncertainFor(selection.objectId) && <Text>A write for this member is already pending or unknown. Use its recorded operation; do not start another membership write.</Text>}
          <Text>This changes the real bound group. It does not create per-person automatic grants, keys, or APIM policy writes. Contact metadata never adds a member.</Text>
          {add.isError && <div className={styles.stack}><ErrorState title="Membership request outcome unavailable" error={add.error} />
            <Text>The write may have reached Entra. Retry unchanged with the same idempotency key to retrieve its recorded operation, or refresh operation history. Do not select another target for this retry.</Text>
            <Text className={styles.technical}>Idempotency key: {addIntent.current?.key}</Text>
          </div>}
          <div><Button appearance="primary" disabled={!selection || !permissionFor(selection.kind) || add.isPending || (!addIntent.current && (!historyReady || Boolean(selection && uncertainFor(selection.objectId))))}
            onClick={addSelected}>{add.isError ? 'Retrieve same member operation' : 'Add to Entra group'}</Button></div>
        </section>
        <section className={styles.section} aria-label="Membership propagation and credentials">
          <Title3 as="h3">After a membership change</Title3>
          <Text>{MEMBERSHIP_PROPAGATION_WARNING}</Text>
          <Text>{MEMBERSHIP_KEY_WARNING}</Text>
          <Text>There is no APIM Graph callback. Model and MCP authorization are independent.</Text>
          <Link to={`/entitlements?costCenter=${encodeURIComponent(costCenter.id)}`}>Review affected grants and individual keys</Link>
        </section>
      </>}
    </section>
    <CostCenterDirectoryOperations costCenterId={costCenter.id} />
    <Dialog open={removeIntent !== null} onOpenChange={(_, data) => { if (!data.open && !remove.isPending && !remove.isError) setRemoveIntent(null) }}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>Remove real Entra membership</DialogTitle>
          <DialogContent className={styles.confirmation}>
            {removeIntent?.recovering ? <Text>Continue only the original removal intent for <strong>{removeIntent.member.displayName || 'this member'}</strong> with its retained idempotency key. It may have reached Entra. Resolve that recorded outcome and refresh actual membership before starting any later removal.</Text>
              : <Text>Remove <strong>{removeIntent?.member.displayName || 'this member'}</strong> from this center&apos;s actual bound group? Other applications using the group may also be affected.</Text>}
            <Text>{MEMBERSHIP_PROPAGATION_WARNING}</Text>
            <Text>{MEMBERSHIP_KEY_WARNING}</Text>
            {remove.isError && <><ErrorState title="Removal outcome unavailable" error={remove.error} /><Text>Retrieve the same recorded operation before another write. The original idempotency key is retained.</Text></>}
          </DialogContent>
          <DialogActions>
            <Button disabled={remove.isPending} onClick={() => { setRemoveIntent(null); remove.reset() }}>Close without another write</Button>
            <Button appearance="primary" disabled={remove.isPending} onClick={() => removeIntent && remove.mutate(removeIntent)}>
              {remove.isError || removeIntent?.recovering ? 'Retrieve same removal operation' : 'Remove from Entra group'}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  </div>
}
