import { Badge, Button, Field, Tab, TabList, Text, Textarea } from '@fluentui/react-components'
import { useMutation, type MutationKey } from '@tanstack/react-query'
import { type ReactNode, type Ref, useEffect, useRef, useState } from 'react'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'
import { completeWorkspaceNavigation } from './WorkspaceLeaveGuard'
import styles from './CatalogSummaryEditor.module.css'

const SUMMARY_LIMIT = 2000

export function CatalogSummaryDetails({
  displayName, summary, onEdit, disabled, editRef,
}: {
  displayName: string
  summary?: string | null
  onEdit: () => void
  disabled?: boolean
  editRef?: Ref<HTMLButtonElement>
}) {
  return (
    <section className={styles.savedSummary} aria-label={`Catalog summary for ${displayName}`}>
      <div className={styles.summaryHeading}>
        <Text weight="semibold">Catalog summary</Text>
        <Button ref={editRef} disabled={disabled} onClick={onEdit}
          aria-label={`Edit catalog summary for ${displayName}`}>Edit catalog summary</Button>
      </div>
      <p className={styles.copy}>{summary?.trim() ? summary : 'No catalog summary yet'}</p>
    </section>
  )
}

interface CatalogSummaryEditorProps {
  displayName: string
  summary?: string | null
  context: ReactNode
  visibilityLabel: string
  availabilityNote?: string
  mutationKey: MutationKey
  onSave: (summary: string | null) => Promise<void>
  onSaved: () => void
  onClose: () => void
}

export function CatalogSummaryEditor({
  displayName, summary, context, visibilityLabel, availabilityNote, mutationKey,
  onSave, onSaved, onClose,
}: CatalogSummaryEditorProps) {
  const baseline = useRef(summary ?? '')
  const [draft, setDraft] = useState(baseline.current)
  const [step, setStep] = useState<'write' | 'review'>('write')
  const title = useRef<HTMLSpanElement>(null)
  const normalized = draft.trim() || null
  const length = Array.from(normalized ?? '').length
  const dirty = draft !== baseline.current
  const problem = length > SUMMARY_LIMIT
    ? `Use ${SUMMARY_LIMIT.toLocaleString()} characters or fewer. Your text has ${length.toLocaleString()}; shorten it before reviewing.`
    : null
  const save = useMutation({
    mutationKey,
    mutationFn: (summary: string | null) => onSave(summary),
    onSuccess: () => completeWorkspaceNavigation(onSaved),
  })

  useEffect(() => { title.current?.focus() }, [step])

  return (
    <WorkspaceEditorFrame
      title={<span ref={title} tabIndex={-1}>{step === 'review' ? 'Review catalog summary' : 'Edit catalog summary'}</span>}
      description="Update portal copy for this exact offering. Access and publishing stay unchanged."
      onBack={onClose} onCancel={onClose} dirty={dirty} pending={save.isPending}
      pendingLabel="Saving catalog summary…"
      actions={step === 'review'
        ? <Button disabled={save.isPending} onClick={() => setStep('write')}>Back to edit</Button>
        : undefined}
      primaryAction={step === 'write'
        ? { label: 'Review summary', onClick: () => setStep('review'), disabled: !dirty || Boolean(problem) }
        : { label: 'Save summary', onClick: () => save.mutate(normalized), disabled: !dirty || Boolean(problem) }}
      error={save.isError ? <>
        <strong>Catalog summary wasn&apos;t saved.</strong>{' '}
        {save.error instanceof Error ? save.error.message : 'An unexpected error occurred.'}
        <p>Your draft is retained. Check the error and try Save summary again, or return to editing.</p>
      </> : undefined}
    >
      <TabList selectedValue={step} aria-label="Catalog summary steps"
        onTabSelect={(_, data) => setStep(data.value === 'review' ? 'review' : 'write')}>
        <Tab value="write" disabled={save.isPending}>1 Write summary</Tab>
        <Tab value="review" disabled={save.isPending || !dirty || Boolean(problem)}>2 Review</Tab>
      </TabList>
      <div className={styles.context}>
        <Text weight="semibold" size={400}>{displayName}</Text>
        {context}
      </div>
      {availabilityNote && <p className={styles.note}>{availabilityNote}</p>}
      {step === 'write' ? (
        <div className={styles.write}>
          <Field label="Catalog summary (optional)"
            hint="Plain text, up to 2,000 characters. Leave empty to remove the summary. Do not include secrets or endpoint details."
            validationState={problem ? 'error' : 'none'} validationMessage={problem ?? undefined}>
            <Textarea value={draft} resize="vertical" rows={8} disabled={save.isPending}
              onChange={(_, data) => { setDraft(data.value); save.reset() }} />
          </Field>
          <Text size={200} className={styles.count}>{length.toLocaleString()} / {SUMMARY_LIMIT.toLocaleString()}</Text>
          <p className={styles.note}>Catalog visibility: {visibilityLabel} — unchanged. Only saved text appears in the portal.</p>
        </div>
      ) : (
        <>
          <section aria-labelledby="summary-changes-heading" className={styles.review}>
            <h3 id="summary-changes-heading">What will change</h3>
            <div className={styles.comparison}>
              <div><h4>Before (saved)</h4><p className={styles.copy}>{baseline.current.trim() ? baseline.current : 'No summary yet'}</p></div>
              <div><h4>After (draft)</h4><p className={styles.copy}>{normalized ?? 'Summary will be removed'}</p></div>
              <div><h4>Unchanged</h4>
                <dl className={styles.unchanged}>
                  <dt>Visibility</dt><dd>{visibilityLabel}</dd>
                  <dt>Gateway publishing</dt><dd>Unchanged</dd>
                  <dt>Grants and access</dt><dd>Unchanged</dd>
                </dl>
              </div>
            </div>
          </section>
          <section className={styles.preview} aria-label="Portal summary preview">
            <div className={styles.summaryHeading}><h3>Portal preview</h3><Badge appearance="tint">Unsaved draft</Badge></div>
            <div className={styles.previewBody}>
              <h3>Access to {displayName}</h3>
              <div className={styles.previewContext}>{context}</div>
              {normalized && <p className={styles.copy}>{normalized}</p>}
              <p className={styles.note}>Summary preview only. Access choices and grants are unchanged.</p>
            </div>
          </section>
          <p className={styles.note}>This reviews catalog copy, not an API Management plan.</p>
        </>
      )}
    </WorkspaceEditorFrame>
  )
}
