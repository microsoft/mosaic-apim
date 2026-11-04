import { Badge, Button, Checkbox, Field, Input, MessageBar, MessageBarBody, Radio, RadioGroup, Switch, Text, Textarea, Title3 } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import { COST_CENTER_SETUP_LABELS, costCenterSetupState } from '../cost-centers'
import { COST_CENTER_CODE_PATTERN, costCenterDraftErrors, costCenterDraftPayload, newCostCenterDraft, useCostCenterCapabilities, type CostCenterDraft } from '../cost-center-setup'
import type { CostCenter, CostCenterGroupCapabilities } from '../types'
import { ErrorState } from './AsyncState'
import { CostCenterDirectoryPicker } from './CostCenterDirectoryPicker'
import { CostCenterResourceAccessFields } from './CostCenterResourceAccess'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import styles from './CostCenterSetup.module.css'

export function CostCenterCapabilityNotice({ capabilities }: { capabilities: CostCenterGroupCapabilities }) {
  return <div className={styles.stack}>
    {capabilities.message && <MessageBar intent={capabilities.available ? 'info' : 'warning'}><MessageBarBody>
      {capabilities.message}{capabilities.errorCode ? ` (${capabilities.errorCode})` : ''}
    </MessageBarBody></MessageBar>}
    <Text>Read-only capability observation, not tenant consent or a guarantee of delegated access to this group. A MOSAIC Admin role does not grant Graph application permissions.</Text>
    <ul className={styles.permissionList}>
      <li>Group lookup: {capabilities.canReadGroups ? 'Available' : 'Unavailable'}</li>
      <li>Create groups: {capabilities.canCreateGroups ? 'Available' : 'Unavailable — optional Group.Create consent'}</li>
      <li>Add people: {capabilities.canAddUsers ? 'Available' : 'Unavailable — optional GroupMember.ReadWrite.All consent'}</li>
      <li>Add workload identities: {capabilities.canAddServicePrincipals ? 'Available' : 'Unavailable — additional Application.ReadWrite.All consent'}</li>
      <li>Remove members: {capabilities.canRemoveMembers ? 'Permission observed; group/type restrictions still apply' : 'Unavailable'}</li>
    </ul>
    <Text className={styles.technical}>Configured directory: {capabilities.directoryTenantId ?? 'Not available'}</Text>
  </div>
}

export function CostCenterSetupFields({ value, onChange, costCenter, disabled = false, suggestedGroup, recorded = false }: {
  value: CostCenterDraft
  onChange: (value: CostCenterDraft) => void
  costCenter?: CostCenter
  disabled?: boolean
  suggestedGroup?: string | null
  recorded?: boolean
}) {
  const api = useMosaicApi()
  const capabilities = useCostCenterCapabilities()
  const suggestion = useQuery({
    queryKey: ['directory-object', suggestedGroup],
    queryFn: () => api.getDirectoryObject(suggestedGroup!),
    enabled: Boolean(suggestedGroup && value.suggestionChoice === 'use'),
    retry: false,
  })
  const exempt = costCenter?.setupState === 'exempt'
  const bound = costCenter?.entraBinding?.verificationState === 'verified'
  const recordedProvisioning = costCenter?.directoryOperations?.some((operation) => operation.kind === 'createGroup')
  const codeInvalid = Boolean(value.code) && !COST_CENTER_CODE_PATTERN.test(value.code)
  const ownersInvalid = costCenterDraftErrors(value).includes('Enter up to 20 valid contact email addresses.')
  const suggestionSelected = value.suggestionChoice === 'use' && suggestion.data?.kind === 'securityGroup'
  const currentGroup = suggestionSelected ? suggestion.data! : value.group
  function change(change: Partial<CostCenterDraft>) {
    onChange({ ...value, ...change })
  }
  return <div className={styles.formContainer}><div className={styles.form}>
    <section className={styles.section} aria-label="Cost center details">
      <Title3 as="h3">Center details</Title3>
      <div className={styles.grid}>
        <Field label="Name" required>
          <Input aria-label="Name" required maxLength={120} disabled={disabled} value={value.name} onChange={(_, data) => change({ name: data.value })} />
        </Field>
        <Field label="Code" required hint="Use 1 to 64 letters, numbers, dots, underscores, or hyphens."
          validationMessage={codeInvalid ? 'Use only A-Z, a-z, 0-9, dot, underscore, or hyphen.' : undefined}>
          <Input aria-label="Code" required maxLength={64} disabled={disabled} value={value.code} onChange={(_, data) => change({ code: data.value })} />
        </Field>
      </div>
      <Field label="Description"><Textarea aria-label="Description" maxLength={1000} disabled={disabled} value={value.description} onChange={(_, data) => change({ description: data.value })} /></Field>
      <Field label="Owners" hint="Contact email addresses, separated by commas or new lines. These do not assign Entra group ownership or membership."
        validationMessage={ownersInvalid ? 'Enter up to 20 valid contact email addresses.' : undefined}>
        <Textarea aria-label="Owners" disabled={disabled} value={value.owners} onChange={(_, data) => change({ owners: data.value })} />
      </Field>
      <Switch label="Keys allowed" checked={value.keysAllowed} disabled={disabled} onChange={(_, data) => change({ keysAllowed: data.checked })} />
      <Text className={styles.prose}>Individual subscription keys remain explicit credentials when the resource enables them. Automatic group access never creates a shared or inherited key. Turning keys off requires reviewed gateway apply.</Text>
    </section>
    <section className={styles.section} aria-label="Entra group setup">
      <Title3 as="h3">Entra group setup</Title3>
      {exempt ? <Text>General is exempt by its stable built-in identity. Its legacy resource and credential rules remain in effect; renaming a custom center to General does not exempt it.</Text>
        : bound ? <div className={styles.stack}>
          <Badge appearance="tint" color="success">Verified dedicated group</Badge>
          <dl className={styles.facts}>
            <div><dt>Directory tenant</dt><dd>{costCenter.entraBinding!.directoryTenantId}</dd></div>
            <div><dt>Group object ID</dt><dd>{costCenter.entraBinding!.groupObjectId}</dd></div>
            <div><dt>Provenance</dt><dd>{costCenter.entraBinding!.provenance}</dd></div>
          </dl>
          <Text>Verified binding is separate from membership-write consent and gateway apply. Rebinding or deleting this Entra group is not offered here.</Text>
        </div> : recordedProvisioning ? <MessageBar intent="warning"><MessageBarBody>
          Group provisioning already has a durable operation. Inspect its state in the saved center before taking further action. A pending or unknown creation must never be resubmitted as a new group.
        </MessageBarBody></MessageBar> : <>
          <Text>Each custom center needs one exclusive, verified Entra security group. No local MOSAIC roster or internal-group fallback is used. You may save incomplete setup.</Text>
          {costCenter?.entraBinding?.verificationError && <MessageBar intent="warning"><MessageBarBody>{costCenter.entraBinding.verificationError}</MessageBarBody></MessageBar>}
          {suggestedGroup && <section className={styles.section} aria-label="Suggested group review">
            <Text weight="semibold">Requester&apos;s suggested group</Text>
            <Text className={styles.technical}>{suggestedGroup}</Text>
            <RadioGroup value={value.suggestionChoice} disabled={disabled} onChange={(_, data) => {
              const use = data.value === 'use'
              change({
                suggestionChoice: use ? 'use' : 'ignore', setupMode: use ? 'attach' : 'later',
                acknowledge: false, group: null,
              })
            }}>
              <Radio value="use" label="Use suggested group after verification" />
              <Radio value="ignore" label="Ignore suggestion and choose setup below" />
            </RadioGroup>
            {value.suggestionChoice === 'use' && suggestion.isPending && <Text role="status">Verifying the suggested identity…</Text>}
            {value.suggestionChoice === 'use' && suggestion.isError && <div className={styles.stack}><ErrorState title="Suggested group lookup failed" error={suggestion.error} />
              <Button type="button" onClick={() => void suggestion.refetch()}>Retry suggested group lookup</Button></div>}
            {value.suggestionChoice === 'use' && suggestion.data && !suggestionSelected && <Text>The suggestion is not an Entra security group. Ignore it to select another setup path.</Text>}
            {suggestionSelected && <Button type="button" disabled={disabled} onClick={() => change({ group: suggestion.data!, setupMode: 'attach' })}>
              Select suggested identity: {suggestion.data!.displayName || 'Entra security group'}
            </Button>}
            {suggestionSelected && <Text>Lookup confirms identity only. Group eligibility and exclusive binding are verified when the reviewed configuration is saved.</Text>}
          </section>}
          <RadioGroup aria-label="Group setup mode" value={value.setupMode} disabled={disabled}
            onChange={(_, data) => change({
              setupMode: data.value as CostCenterDraft['setupMode'], acknowledge: false,
              ...(suggestedGroup && value.suggestionChoice === 'use' && data.value !== 'attach'
                ? { suggestionChoice: 'ignore' as const, group: null } : {}),
            })}>
            <Radio value="later" label="Finish setup later" />
            <Radio value="attach" label="Attach existing group" />
            <Radio value="create" label="Create with MOSAIC" />
          </RadioGroup>
          {value.setupMode === 'later' && <Text>Saved as Setup required. This center cannot authorize custom-center access until its group is verified.</Text>}
          {value.setupMode === 'attach' && <div className={styles.stack}>
            <CostCenterDirectoryPicker group selected={currentGroup} disabled={disabled}
              onSelect={(group) => change({ group, acknowledge: false, ...(suggestedGroup ? { suggestionChoice: 'ignore' as const } : {}) })} />
            <Text>Lookup confirms identity, not all group eligibility. Saving verifies ordinary assigned-membership, security-enabled, mail-disabled, non-role-assignable, cloud-managed group requirements and exclusive binding. Dynamic, mail-enabled, role-assignable, or on-premises-managed groups are unsupported.</Text>
            <Checkbox disabled={disabled} checked={value.acknowledge} label="I acknowledge that membership edits change this real Entra group and can affect other applications using it."
              onChange={(_, data) => change({ acknowledge: Boolean(data.checked), ...(suggestionSelected && !value.group ? { group: suggestion.data! } : {}) })} />
          </div>}
          {value.setupMode === 'create' && <div className={styles.stack}>
            <Field label="New group display name" hint="Blank uses the saved cost-center name.">
              <Input value={value.groupName} disabled={disabled} maxLength={256} onChange={(_, data) => change({ groupName: data.value })} />
            </Field>
            <section className={styles.section} aria-label="Group creation preview">
              <Text weight="semibold">{recorded ? 'Recorded group creation configuration' : 'Creation preview · no directory write yet'}</Text>
              <dl className={styles.facts}>
                <div><dt>Display name</dt><dd>{value.groupName.trim() || value.name.trim() || 'Enter a center name'}</dd></div>
                <div><dt>Group type</dt><dd>Assigned-membership security group · Mail disabled · Not role assignable</dd></div>
                <div><dt>Directory owner</dt><dd>API service principal, not the contact email addresses</dd></div>
                <div><dt>Initial members</dt><dd>None unless explicitly requested during approval; add real directory members after saving</dd></div>
              </dl>
              <Text>{recorded
                ? 'This is the original reviewed intent, not the current directory outcome. Inspect the saved center’s durable operation; never resubmit creation as a new group.'
                : 'The center is saved before creation. Failures retain Setup required; uncertain outcomes require recovery or administrator investigation, never blind creation retry.'}</Text>
            </section>
          </div>}
        </>}
      {!exempt && <details>
        <summary>Directory capabilities and optional permissions</summary>
        <div className={styles.stack}>
          {capabilities.isPending && <Text role="status">Checking directory capabilities without creating a group…</Text>}
          {capabilities.isError && <ErrorState title="Directory capabilities unavailable" error={capabilities.error} />}
          {capabilities.data && <CostCenterCapabilityNotice capabilities={capabilities.data} />}
          <Button type="button" disabled={capabilities.isFetching} onClick={() => void capabilities.refetch()}>Refresh read-only capability check</Button>
        </div>
      </details>}
      {!exempt && !bound && value.setupMode !== 'later' && capabilities.isPending && <Text role="status">Checking directory setup capabilities…</Text>}
      {!exempt && !bound && value.setupMode !== 'later' && capabilities.isError && <div className={styles.stack}>
        <ErrorState title="Directory setup preflight unavailable" error={capabilities.error} />
        <Text>Retry the read-only capability check above, or choose Finish setup later. No directory write has been requested.</Text>
      </div>}
      {!exempt && !bound && value.setupMode === 'attach' && capabilities.isSuccess && (!capabilities.data.available || !capabilities.data.canReadGroups) && <MessageBar intent="warning"><MessageBarBody>
        Directory reads are unavailable with the observed permissions or cloud configuration. Configure directory lookup, or choose Finish setup later.
      </MessageBarBody></MessageBar>}
      {!exempt && !bound && value.setupMode === 'create' && capabilities.isSuccess && (!capabilities.data.canCreateGroups || !capabilities.data.available) && <MessageBar intent="warning"><MessageBarBody>
        Creation is unavailable with the observed permissions or cloud configuration. MOSAIC Admin is not Graph admin consent. Choose Finish setup later or attach a supported group after directory reads are configured.
      </MessageBarBody></MessageBar>}
      {!exempt && !bound && <section className={styles.section} aria-label="Initial membership">
        <Title3 as="h4">Initial membership</Title3>
        <Text>Attaching a group keeps its actual existing membership; creating one starts with no members. After saving, use Members to add people or workload identities explicitly. Request approval has a separate, default-off choice to add its requester.</Text>
      </section>}
    </section>
    <div className={`${styles.section} ${styles.wide}`}>
      {exempt ? <Text>General retains legacy resource availability. Per-resource custom-center definitions are not required for this exempt center.</Text>
        : <CostCenterResourceAccessFields value={value.resourceAccess} onChange={(resourceAccess) => change({ resourceAccess })} costCenter={costCenter} keysAllowed={value.keysAllowed} disabled={disabled} />}
    </div></div>
  </div>
}

export function CostCenterEditor({ costCenter, onCancel, onSaved }: {
  costCenter?: CostCenter
  onCancel: () => void
  onSaved: (id: string) => void
}) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const initial = useRef(newCostCenterDraft(costCenter))
  const [draft, setDraft] = useState(initial.current)
  const [savedCenterId, setSavedCenterId] = useState<string | null>(null)
  const capabilities = useCostCenterCapabilities()
  const exempt = costCenter?.setupState === 'exempt'
  const bound = costCenter?.entraBinding?.verificationState === 'verified' || costCenter?.directoryOperations?.some((item) => item.kind === 'createGroup')
  const errors = costCenterDraftErrors(draft, capabilities.data, { exempt, bound })
  const save = useMutation({
    mutationFn: () => {
      const payload = costCenterDraftPayload(draft)
      if (exempt || bound) delete payload.groupSetup
      return costCenter ? api.updateCostCenter(costCenter.id, payload) : api.createCostCenter(payload)
    },
    retry: false,
    onSuccess: async (saved) => {
      queryClient.setQueryData(['cost-centers', saved.id], saved)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['cost-centers'] }),
        queryClient.invalidateQueries({ queryKey: ['entitlements'] }),
        queryClient.invalidateQueries({ queryKey: ['publications'] }),
        queryClient.invalidateQueries({ queryKey: ['mcp-publications'] }),
        queryClient.invalidateQueries({ queryKey: ['model-pools'] }),
      ])
      completeWorkspaceNavigation(() => onSaved(saved.id))
    },
    onError: (error) => {
      const id = error instanceof ApiError ? error.body?.details?.savedCostCenterId : undefined
      if (typeof id === 'string') {
        setSavedCenterId(id)
        void queryClient.invalidateQueries({ queryKey: ['cost-centers'] })
      }
    },
  })
  const reason = save.error instanceof ApiError ? save.error.body?.details?.reason : undefined
  const message = reason === 'codeInUse' ? 'That code is already used by another cost center. Choose a unique code.' : save.error?.message
  const transportUnknown = (!costCenter || draft.setupMode === 'create') && save.isError && !savedCenterId
    && (!(save.error instanceof ApiError) || save.error.status >= 500)
  return <WorkspaceEditorFrame title={costCenter ? `Edit ${costCenter.name}` : 'New cost center'}
    description="Save center details, verify a dedicated Entra group now or later, and choose exact resource access. Directory membership and reviewed gateway apply remain separate."
    onBack={onCancel} onCancel={onCancel}
    dirty={!savedCenterId && !transportUnknown && JSON.stringify(draft) !== JSON.stringify(initial.current)}
    pending={save.isPending} error={message}
    primaryAction={{
      label: costCenter ? 'Save changes' : 'Create cost center',
      onClick: () => { if (!errors.length && !savedCenterId) save.mutate() },
      disabled: Boolean(errors.length || savedCenterId || transportUnknown),
    }}>
    <div className={styles.stack}>
      {savedCenterId && <MessageBar intent="warning"><MessageBarBody>
        The center was saved, but setup or access synchronization did not finish. Do not create another center or group.
        {' '}<Button onClick={() => completeWorkspaceNavigation(() => onSaved(savedCenterId))}>Inspect saved cost center</Button>
      </MessageBarBody></MessageBar>}
      {transportUnknown && <MessageBar intent="warning"><MessageBarBody>
        {costCenter
          ? 'Group creation outcome is unknown. Do not retry creation. Inspect this center’s durable operation and investigate in Entra if its outcome cannot be recovered.'
          : 'Creation outcome is unknown and no saved ID was returned. Do not retry creation. Inspect the cost-center list and recorded operations, and investigate in Entra if group creation was requested.'}
        {' '}<Button onClick={() => completeWorkspaceNavigation(() => costCenter ? onSaved(costCenter.id) : onCancel())}>Inspect cost-center records</Button>
      </MessageBarBody></MessageBar>}
      {costCenter && <div><Badge appearance="tint" color={costCenterSetupState(costCenter) === 'setupRequired' ? 'warning' : 'informative'}>{COST_CENTER_SETUP_LABELS[costCenterSetupState(costCenter)]}</Badge></div>}
      <CostCenterSetupFields value={draft} onChange={setDraft} costCenter={costCenter} disabled={save.isPending || Boolean(savedCenterId) || transportUnknown} />
    </div>
  </WorkspaceEditorFrame>
}
