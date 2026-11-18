import { Button, Field, Select, Textarea, Title3 } from '@fluentui/react-components'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useMosaicApi } from '../api'
import type { CostCenter, DirectoryObject, PocRole, PocSelection } from '../types'
import { CostCenterDirectoryPicker } from './CostCenterDirectoryPicker'
import styles from './CostCenterSetup.module.css'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'

export const pocRoles = { technical: 'Technical POC', billing: 'Billing POC' } satisfies Record<PocRole, string>
export type PocDraft = PocSelection & { displayName?: string }

export function PocFields({ assignments, onChange, disabled = false }: {
  assignments: PocDraft[]
  onChange: (items: PocDraft[]) => void
  disabled?: boolean
}) {
  const [role, setRole] = useState<PocRole>('technical')
  const [selected, setSelected] = useState<DirectoryObject | null>(null)
  const canAdd = selected && assignments.length < 20 && !assignments.some(
    item => item.role === role && item.objectId === selected.objectId,
  )
  return <section className={styles.stack} aria-label="Points of contact">
    <Title3 as="h3">Points of contact</Title3>
    <p>Technical and billing contacts can review membership and resource access for this center. An assignment never grants directory membership or gateway access.</p>
    {assignments.length > 0 && <ul aria-label="Contact assignments">
      {assignments.map(item => <li key={`${item.role}:${item.objectId}`}>
        <strong>{pocRoles[item.role]}</strong> · {item.displayName ?? item.objectId}{' '}
        <Button type="button" size="small" disabled={disabled}
          aria-label={`Remove ${pocRoles[item.role]} ${item.displayName ?? item.objectId}`}
          onClick={() => onChange(assignments.filter(value => value !== item))}>Remove</Button>
      </li>)}
    </ul>}
    {!disabled && <details>
      <summary>Add an approved directory identity</summary>
      <div className={styles.stack}>
        <Field label="Contact role"><Select value={role} onChange={(_, data) => setRole(data.value === 'billing' ? 'billing' : 'technical')}>
          <option value="technical">Technical POC</option><option value="billing">Billing POC</option>
        </Select></Field>
        <CostCenterDirectoryPicker peopleOnly selected={selected} onSelect={setSelected} />
        <Button type="button" disabled={!canAdd} onClick={() => {
          if (!selected || !canAdd) return
          onChange([...assignments, { role, objectId: selected.objectId, displayName: selected.displayName ?? undefined }])
          setSelected(null)
        }}>Add contact to draft</Button>
      </div>
    </details>}
  </section>
}

export function pocSelections(items: PocDraft[]): PocSelection[] {
  return items.map(({ role, objectId }) => ({ role, objectId }))
}

export function CostCenterPocEditor({ center, onBack }: { center: CostCenter; onBack: () => void }) {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [initial] = useState(() => ({ assignments: center.pocs ?? [], revision: center.pocRevision }))
  const [assignments, setAssignments] = useState<PocDraft[]>(initial.assignments)
  const [reason, setReason] = useState('')
  const changed = JSON.stringify(pocSelections(assignments)) !== JSON.stringify(pocSelections(initial.assignments))
  const save = useMutation({
    mutationFn: () => api.assignCostCenterPocs(center.id, {
      assignments: pocSelections(assignments), revision: initial.revision!, reason: reason.trim(),
    }),
    retry: false,
    onSuccess: saved => {
      queryClient.setQueryData(['cost-centers', center.id], saved)
      void queryClient.invalidateQueries({ queryKey: ['cost-centers'] })
      completeWorkspaceNavigation(onBack)
    },
  })
  return <WorkspaceEditorFrame title={`Points of contact: ${center.name}`}
    description="Only approved identities receive review authority. Removing an assignment ends that authority immediately."
    onBack={onBack} onCancel={onBack} dirty={changed || Boolean(reason.trim())} pending={save.isPending}
    error={save.error?.message} primaryAction={{
      label: 'Save assignments', onClick: () => save.mutate(),
      disabled: !changed || !reason.trim() || !initial.revision || save.isPending,
    }}>
    <PocFields assignments={assignments} onChange={setAssignments} disabled={save.isPending} />
    <Field label="Reason for changes" required>
      <Textarea value={reason} onChange={(_, data) => setReason(data.value)} maxLength={2000} disabled={save.isPending} />
    </Field>
  </WorkspaceEditorFrame>
}
