import { Badge, Button } from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { usePortalApi } from '../api'
import type { RequestFormSnapshotView, RequestFormSummary } from '../types'
import { ErrorState, Loading } from './AsyncState'

export function RequestFormAnswers({ form, draft = false, reviewer = false }: { form: RequestFormSnapshotView; draft?: boolean; reviewer?: boolean }) {
  const answers = new Map(form.answers.map(answer => [answer.questionId, answer.value]))
  return <section className="recorded-request-form" aria-label="Submitted governance answers">
    <h3>{draft ? 'Review your answers' : 'Submitted answers'} <Badge appearance="outline">{draft ? 'Form' : 'Recorded form'} v{form.revision}</Badge></h3>
    <p>{draft ? 'Review every answer before submitting. No request has been saved yet.'
      : reviewer ? "These answers were recorded when the requester submitted, not from today's form."
        : "These are the questions and answers recorded when you submitted, not today's form."}</p>
    <dl>{form.questions.map(question => {
      const value = answers.get(question.id)
      const text = value === undefined ? 'Not provided' : question.kind === 'acknowledgment'
        ? value ? 'Acknowledged (self-reported)' : 'Not acknowledged'
        : typeof value === 'object' ? <ul>
          {value.optionIds.map(id => <li key={id}>{question.options.find(option => option.id === id)?.label ?? 'Recorded choice unavailable'}</li>)}
          {value.customText !== undefined && <li><strong>Other:</strong> {value.customText}</li>}
        </ul>
          : question.kind === 'singleChoice' ? question.options.find(option => option.id === value)?.label ?? 'Recorded choice unavailable'
            : String(value)
      return <div key={question.id}><dt>{question.label}{question.required ? ' (required)' : ' (optional)'}</dt>
        <dd>{question.helpText && <p>{question.helpText}</p>}{text}</dd></div>
    })}</dl>
    <p>Acknowledgments are self-reported, not proof of permission or data safety. A charge reference does not change the charged cost center.</p>
  </section>
}

export function SubmittedRequestForm({ requestId, summary }: { requestId: string; summary: RequestFormSummary }) {
  const api = usePortalApi()
  const [open, setOpen] = useState(false)
  const detail = useQuery({
    queryKey: ['portal', 'request-detail', requestId],
    queryFn: () => api.getAccessRequest(requestId),
    enabled: open,
    retry: false,
  })
  return <div>
    <Button aria-expanded={open} onClick={() => setOpen(!open)}>
      {open ? 'Hide' : 'View'} submitted answers (form v{summary.revision})
    </Button>
    {open && detail.isPending && <Loading label="Loading your recorded answers" />}
    {open && detail.isError && <><ErrorState error={detail.error} /><Button onClick={() => void detail.refetch()}>Retry recorded answers</Button></>}
    {open && detail.isSuccess && (detail.data.recordedForm
      ? <RequestFormAnswers form={detail.data.recordedForm} />
      : <p role="alert">Recorded answers are unavailable. Contact an administrator; the current form cannot replace them.</p>)}
  </div>
}
