import { Badge, Button, Checkbox, Field, Input, Select, Text, Textarea } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLayoutEffect, useRef, useState } from 'react'
import { ApiError, useMosaicApi } from '../api'
import type {
  EntitlementResource, EnvironmentCatalogView, RequestFormDefinition, RequestFormScope,
  RequestQuestion, RequestQuestionKind, RequestQuestionView,
} from '../types'
import { ErrorState, Loading } from './AsyncState'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'

const kindLabels: Record<RequestQuestionKind, string> = {
  shortText: 'Short text', paragraph: 'Paragraph', singleChoice: 'Single choice', multipleChoice: 'Multiple choice',
  acknowledgment: 'Acknowledgment',
}
const resourceKinds: { key: RequestFormScope['resourceKinds'][number]; label: string }[] = [
  { key: 'modelApi', label: 'Model APIs' }, { key: 'poolModel', label: 'Pool models' }, { key: 'mcpServer', label: 'MCP servers' },
]
function isKind(value: string): value is RequestQuestionKind {
  return Object.hasOwn(kindLabels, value)
}
function question(kind: RequestQuestionKind, previous?: RequestQuestion): RequestQuestion {
  const base = {
    id: `question_${crypto.randomUUID()}`, label: previous?.label ?? '', helpText: previous?.helpText ?? null,
    required: previous?.required ?? false, scope: previous?.scope ?? { resourceKinds: [], resources: [], environments: [] },
  }
  if (kind === 'singleChoice' || kind === 'multipleChoice') return {
    ...base, kind, ...(
      previous && (previous.kind === 'singleChoice' || previous.kind === 'multipleChoice')
        ? { options: previous.options, allowCustomAnswer: previous.allowCustomAnswer }
        : { options: [{ id: `option_${crypto.randomUUID()}`, label: '' }, { id: `option_${crypto.randomUUID()}`, label: '' }] }
    ),
  }
  if (kind === 'acknowledgment') return { ...base, kind }
  return { ...base, kind, maxLength: kind === 'shortText' ? 200 : 2000 }
}
function key(resource: EntitlementResource) { return JSON.stringify([resource.kind, resource.scopeId, resource.id]) }

function choiceDescription(item: Pick<RequestQuestionView, 'kind' | 'options' | 'allowCustomAnswer'>) {
  return item.kind === 'singleChoice' || item.kind === 'multipleChoice'
    ? ` · ${item.options.length} predefined choices${item.allowCustomAnswer ? ' + Other (write-in)' : ''}`
    : ''
}

export function RequestFormEditor({ resources, environments }: {
  resources: { resource: EntitlementResource; label: string }[]; environments?: EnvironmentCatalogView
}) {
  const api = useMosaicApi()
  const client = useQueryClient()
  const current = useQuery({ queryKey: ['request-form'], queryFn: api.getRequestForm, retry: false, refetchOnWindowFocus: false })
  const [draft, setDraft] = useState<RequestFormDefinition | null>(null)
  const [baseline, setBaseline] = useState<RequestFormDefinition | null>(null)
  const [revision, setRevision] = useState(0)
  const [step, setStep] = useState(0)
  const [confirmed, setConfirmed] = useState(false)
  const [problems, setProblems] = useState<string[]>([])
  const [attempt, setAttempt] = useState(0)
  const [notice, setNotice] = useState('')
  const [previewResource, setPreviewResource] = useState('')
  const [previewDraft, setPreviewDraft] = useState('')
  const heading = useRef<HTMLHeadingElement>(null)
  const editButton = useRef<HTMLButtonElement>(null)
  const wasEditing = useRef(false)
  const preview = useMutation({
    mutationFn: () => {
      const selected = resources.find(item => key(item.resource) === previewResource)
      if (!draft || !selected) throw new Error('Choose an exact offering to preview.')
      setPreviewDraft(JSON.stringify(draft))
      return api.previewRequestForm(draft, selected.resource)
    },
  })
  const publish = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error('No form draft is open.')
      return api.publishRequestForm({ expectedRevision: revision, definition: draft, enableConfirmed: confirmed })
    },
    retry: false,
    onSuccess: saved => {
      client.setQueryData(['request-form'], saved)
      setNotice(`Published request form v${saved.revision}. ${saved.definition.enabled ? 'Matching new requests record this form for human review.' : 'No new requests will collect this form.'} Existing requests, grants and policy revisions are unchanged.`)
      setDraft(null); setBaseline(null)
    },
  })
  const editing = draft !== null
  const publicationError = publish.error instanceof ApiError && Array.isArray(publish.error.body?.detail)
    ? publish.error.body.detail.map(item => item.msg).filter(Boolean).join(' ') || publish.error.message
    : publish.error?.message
  useLayoutEffect(() => { if (editing) heading.current?.focus() }, [step, editing])
  useLayoutEffect(() => {
    if (wasEditing.current && !editing) editButton.current?.focus()
    wasEditing.current = editing
  }, [editing])
  function edit() {
    if (!current.data) return
    setDraft(structuredClone(current.data.definition)); setBaseline(structuredClone(current.data.definition))
    setRevision(current.data.revision); setStep(0); setProblems([]); setConfirmed(false); publish.reset(); preview.reset()
  }
  function update(index: number, next: RequestQuestion) {
    setDraft(old => {
      if (!old) return old
      const questions = old.questions.map((item, i) => i === index ? next : item)
      const formatVersion = questions.some(item => item.kind === 'multipleChoice' ||
        item.kind === 'singleChoice' && item.allowCustomAnswer) ? 2 : old.formatVersion
      return { ...old, questions, formatVersion }
    })
  }
  function scope(index: number, next: RequestFormScope) {
    if (draft) update(index, { ...draft.questions[index], scope: next })
  }
  function validate() {
    if (!draft) return 0
    const errors: string[] = []
    if (draft.enabled && !draft.questions.length) errors.push('Add a question before enabling the form.')
    draft.questions.forEach((item, index) => {
      if (!item.label.trim()) errors.push(`Question ${index + 1}: enter a label.`)
      if ((item.kind === 'singleChoice' || item.kind === 'multipleChoice') && item.options.some(option => !option.label.trim())) errors.push(`Question ${index + 1}: name every choice.`)
      if ((item.kind === 'shortText' || item.kind === 'paragraph') &&
        (!Number.isInteger(item.maxLength) || item.maxLength < 1 || item.maxLength > (item.kind === 'shortText' ? 200 : 2000))) {
        errors.push(`Question ${index + 1}: choose a positive character limit within its type's bound.`)
      }
    })
    const invalidQuestions = errors.length > 0
    if (draft.questions.reduce((count, item) => count + item.scope.resources.length, 0) > 50) errors.push('Target at most 50 exact offerings across the form.')
    setProblems(errors); setAttempt(value => value + 1)
    return errors.length ? invalidQuestions ? 0 : 1 : null
  }
  function next() {
    const invalidStep = validate()
    if (invalidStep !== null) { setStep(invalidStep); return }
    if (step < 2) setStep(step + 1)
    else if (!publish.isPending && draft && (!draft.enabled || baseline?.enabled || confirmed)) publish.mutate()
  }
  async function reloadKeepingDraft() {
    const refreshed = await current.refetch()
    if (!refreshed.data || refreshed.isError) return
    setRevision(refreshed.data.revision)
    setBaseline(structuredClone(refreshed.data.definition))
    setConfirmed(false); setStep(0); publish.reset()
    setNotice('Current revision reloaded. Your draft is retained; review its questions and applicability before publishing again.')
  }
  if (!draft) return <section className="governance-form-fields" aria-label="Request form configuration">
    <h2>Request form</h2>
    <Text>Collect bounded context for individual portal resource-access requests, not Automatic access or membership requests. Answers never confer authority.</Text>
    {notice && <Text role="status">{notice}</Text>}
    {current.isPending && <Loading label="Loading the active request form" />}
    {current.isError && <><ErrorState error={current.error} /><Button onClick={() => void current.refetch()}>Retry request form</Button></>}
    {current.data && <>
      <Badge appearance="outline">{current.data.definition.enabled ? `Active v${current.data.revision}` : current.data.revision ? `Disabled v${current.data.revision}` : 'Not configured'}</Badge>
      <Text>{current.data.definition.questions.length} questions. No existing request is rewritten by publication.</Text>
      <Button ref={editButton} appearance="primary" onClick={edit}>{current.data.revision ? 'Edit request form' : 'Configure request form'}</Button>
      {current.data.definition.questions.map(item => <section key={item.id} className="governance-question">
        <h3>{item.label}</h3><Text>{kindLabels[item.kind]} · {item.required ? 'Required' : 'Optional'}{'options' in item && choiceDescription(item)}</Text>
      </section>)}
    </>}
  </section>

  const envs = [...(environments?.environments ?? []).map(item => ({ key: item.key, label: item.displayName })), { key: 'unclassified', label: 'Unclassified' }]
  return <WorkspaceEditorFrame title="Edit request form" onBack={() => setDraft(null)} onCancel={() => setDraft(null)}
    description={`Draft v${revision + 1}, based on v${revision}. Publication affects only future matching requests.`}
    dirty={JSON.stringify(draft) !== JSON.stringify(baseline)} pending={publish.isPending}
    error={publicationError} errorKey={publish.submittedAt || attempt}
    validationSummary={problems.length ? <ul>{problems.map(problem => <li key={problem}>{problem}</li>)}</ul> : undefined}
    primaryAction={{ label: step === 0 ? 'Next: Applicability' : step === 1 ? 'Next: Publish' : 'Publish revision',
      onClick: next, disabled: step === 2 && draft.enabled && !baseline?.enabled && !confirmed }}>
    <ol className="governance-steps" aria-label="Form authoring steps">
      {['Questions', 'Applicability', 'Publish'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined}>{index + 1}. {label}</li>)}
    </ol>
    <h3 ref={heading} tabIndex={-1}>{['Define questions', 'Target and preview questions', 'Review publication'][step]}</h3>
    {notice && <Text role="status">{notice}</Text>}
    {publish.error instanceof ApiError && publish.error.status === 409 &&
      <Button disabled={current.isFetching} onClick={() => void reloadKeepingDraft()}>Reload current revision and keep draft</Button>}
    {step === 0 && <div className="governance-form-fields">
      <Checkbox label="Enable this form for matching new requests" checked={draft.enabled}
        onChange={(_, data) => setDraft({ ...draft, enabled: data.checked === true })} />
      <Text>At most 20 questions. No secrets, credentials, personal data, datasets, uploaded files or executable content. Optional justification and the eligible cost-center selector remain separate.</Text>
      {draft.questions.map((item, index) => <section key={item.id} className="governance-question" aria-label={`Question ${index + 1}`}>
        <header><h4>Question {index + 1}</h4><div className="governance-actions">
          <Button aria-label={`Move question ${index + 1} up`} disabled={index === 0} onClick={() => {
            const questions = [...draft.questions]; [questions[index - 1], questions[index]] = [questions[index], questions[index - 1]]
            setDraft({ ...draft, questions })
          }}>Move up</Button>
          <Button aria-label={`Move question ${index + 1} down`} disabled={index === draft.questions.length - 1} onClick={() => {
            const questions = [...draft.questions]; [questions[index + 1], questions[index]] = [questions[index], questions[index + 1]]
            setDraft({ ...draft, questions })
          }}>Move down</Button>
          <Button aria-label={`Remove question ${index + 1}`} onClick={() => setDraft({ ...draft, questions: draft.questions.filter((_, i) => i !== index) })}>Remove</Button>
        </div></header>
        <div className="governance-form-fields">
          <div className="governance-question-grid">
            <Field label={`Question ${index + 1} label`} required><Input value={item.label} maxLength={120} onChange={(_, data) => update(index, { ...item, label: data.value })} /></Field>
            <Field label={`Question ${index + 1} type`} hint="Changing type assigns a new question ID; old submissions keep the original.">
              <Select value={item.kind} onChange={(_, data) => { if (isKind(data.value)) update(index, question(data.value, item)) }}>
                {Object.entries(kindLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </Select>
            </Field>
          </div>
          <Field label={`Question ${index + 1} help text`}><Textarea value={item.helpText ?? ''} maxLength={500}
            onChange={(_, data) => update(index, { ...item, helpText: data.value || null })} /></Field>
          <Checkbox label={`Require question ${index + 1}`} checked={item.required} onChange={(_, data) => update(index, { ...item, required: data.checked === true })} />
          {(item.kind === 'shortText' || item.kind === 'paragraph') && <Field label={`Question ${index + 1} character limit`}>
            <Input type="number" min={1} max={item.kind === 'shortText' ? 200 : 2000} value={String(item.maxLength)}
              onChange={(_, data) => update(index, { ...item, maxLength: Number(data.value) })} />
          </Field>}
          {(item.kind === 'singleChoice' || item.kind === 'multipleChoice') && <>
            <Text>{item.kind === 'singleChoice' ? 'Requesters choose one answer.' : 'Requesters can choose more than one answer.'}</Text>
            {item.options.map((option, optionIndex) => <div className="governance-question-grid" key={option.id}>
              <Field label={`Question ${index + 1} choice ${optionIndex + 1}`} required><Input value={option.label} maxLength={100}
                onChange={(_, data) => update(index, { ...item, options: item.options.map((old, i) => i === optionIndex ? { ...old, label: data.value } : old) })} /></Field>
              <Button disabled={item.options.length <= 2} onClick={() => update(index, { ...item, options: item.options.filter((_, i) => i !== optionIndex) })}>Remove choice {optionIndex + 1}</Button>
            </div>)}
            <Button disabled={item.options.length >= 12} onClick={() => update(index, { ...item, options: [...item.options, { id: `option_${crypto.randomUUID()}`, label: '' }] })}>Add choice</Button>
            <Field hint={item.kind === 'singleChoice'
              ? 'Adds “Other” as an alternative to the predefined choices. Write-ins are limited to 200 characters.'
              : 'Adds “Other” alongside any selected predefined choices. Write-ins are limited to 200 characters.'}>
              <Checkbox label="Allow a custom answer" checked={item.allowCustomAnswer ?? false}
                onChange={(_, data) => update(index, { ...item, allowCustomAnswer: data.checked === true })} />
            </Field>
          </>}
        </div>
      </section>)}
      <Button disabled={draft.questions.length >= 20} onClick={() => setDraft({ ...draft, questions: [...draft.questions, question('paragraph')] })}>Add question</Button>
    </div>}
    {step === 1 && <div className="governance-form-fields">
      <Text>Filters within a dimension are ORed; resource types, exact offerings and environments intersect. No selected filters means All for that dimension, including future matching offerings. Scopes never establish permission.</Text>
      {draft.questions.map((item, index) => <section key={item.id} className="governance-question">
        <h4>{item.label}</h4>
        <fieldset><legend>Resource types ({item.scope.resourceKinds.length ? 'filtered' : 'All'})</legend>
          {resourceKinds.map(kind => <Checkbox key={kind.key} label={kind.label} checked={item.scope.resourceKinds.includes(kind.key)}
            onChange={(_, data) => scope(index, { ...item.scope, resourceKinds: data.checked ? [...item.scope.resourceKinds, kind.key] : item.scope.resourceKinds.filter(value => value !== kind.key) })} />)}
        </fieldset>
        <fieldset><legend>Environments ({item.scope.environments.length ? 'filtered' : 'All, including Unclassified'})</legend>
          {envs.map(env => <Checkbox key={env.key} label={env.label} checked={item.scope.environments.includes(env.key)}
            onChange={(_, data) => scope(index, { ...item.scope, environments: data.checked ? [...item.scope.environments, env.key] : item.scope.environments.filter(value => value !== env.key) })} />)}
          {item.scope.environments.filter(value => !envs.some(env => env.key === value)).map(value => <Text key={value} role="alert">Environment {value} is unavailable. Remove or replace this target before publishing.</Text>)}
          <Button onClick={() => scope(index, { ...item.scope, environments: [] })}>Use all environments</Button>
        </fieldset>
        <Field label="Exact offerings" hint="No selection means all offerings matching the other filters. A pool-model target includes its exact pool.">
          <Select value="" disabled={draft.questions.reduce((count, item) => count + item.scope.resources.length, 0) >= 50} onChange={(_, data) => {
            const selected = resources.find(resource => key(resource.resource) === data.value)
            if (selected) scope(index, { ...item.scope, resources: [...item.scope.resources, selected.resource] })
          }}>
            <option value="">Add an exact offering</option>
            {resources.filter(resource => !item.scope.resources.some(target => key(target) === key(resource.resource)))
              .map(resource => <option key={key(resource.resource)} value={key(resource.resource)}>{resource.label}</option>)}
          </Select>
        </Field>
        {item.scope.resources.map(resource => <div className="governance-actions" key={key(resource)}>
          <Text>{resources.find(option => key(option.resource) === key(resource))?.label ?? 'Target offering unavailable'}</Text>
          <Button onClick={() => scope(index, { ...item.scope, resources: item.scope.resources.filter(target => key(target) !== key(resource)) })}>Remove exact target</Button>
        </div>)}
      </section>)}
      <section><h4>Preview applicable questions</h4>
        <Field label="Preview exact offering"><Select value={previewResource} disabled={preview.isPending} onChange={(_, data) => {
          setPreviewResource(data.value); preview.reset(); setPreviewDraft('')
        }}>
          <option value="">Choose an offering</option>{resources.map(item => <option key={key(item.resource)} value={key(item.resource)}>{item.label}</option>)}
        </Select></Field>
        <Button disabled={!previewResource} disabledFocusable={preview.isPending} onClick={() => { if (!preview.isPending) preview.mutate() }}>Preview questions</Button>
        {preview.isError && <ErrorState context="action" title="Unable to preview form" error={preview.error} announcementKey={preview.submittedAt} />}
        {preview.data && <div><Text>{previewDraft === JSON.stringify(draft) ? 'Current draft preview' : 'Preview is stale; preview this draft again.'}</Text>
          <ul>{preview.data.questions.map(item => <li key={item.id}>{item.label} - {item.required ? 'Required' : 'Optional'} - {kindLabels[item.kind]}{choiceDescription(item)}</li>)}</ul>
          {!preview.data.questions.length && <Text>No questions apply to this offering under this draft. Disabled forms collect no answers.</Text>}
        </div>}
      </section>
    </div>}
    {step === 2 && <div className="governance-form-fields">
      <Text weight="semibold">Active v{revision} → proposed v{revision + 1}</Text>
      <Text>{draft.enabled ? 'Enabled' : 'Disabled'} · {draft.questions.length} questions · {draft.questions.filter(item => item.required).length} required</Text>
      <Text>Existing: {baseline?.questions.length ?? 0} questions, {baseline?.enabled ? 'enabled' : 'disabled'}. Submitted answers remain tied to their original version.</Text>
      {draft.questions.map(item => <section className="governance-question" key={item.id}><h4>{item.label}</h4>
        <Text>{kindLabels[item.kind]}{'options' in item && choiceDescription(item)} · {item.required ? 'Required' : 'Optional'} · {item.scope.resourceKinds.length ? item.scope.resourceKinds.join(', ') : 'All types'} · {item.scope.environments.join(', ') || 'All environments'} · {item.scope.resources.length ? `${item.scope.resources.length} exact offering(s)` : 'All matching offerings'}</Text>
      </section>)}
      <Text>Matching new form-bearing requests require human single/bulk review in Phase 1. Automatic policies do not evaluate these answers. Existing no-form requests and policy revisions are unchanged. Approval creates grant intent only; gateway apply stays separate.</Text>
      <Text>A charge reference is informational. The existing eligible cost-center selector alone binds charging. No answers grant identity, membership, funding eligibility, scope or relaxed limits. No credentials/uploads/datasets.</Text>
      {draft.enabled && !baseline?.enabled && <Checkbox label="Enable this form and require human review for matching new requests"
        checked={confirmed} onChange={(_, data) => setConfirmed(data.checked === true)} />}
    </div>}
    {step > 0 && <div className="governance-actions"><Button disabled={publish.isPending} onClick={() => setStep(step - 1)}>Previous step</Button></div>}
  </WorkspaceEditorFrame>
}
