import { Badge, Button, Text } from '@fluentui/react-components'
import type { AccessRequest, RequestFormSnapshotView } from '../types'
import { ErrorState, Loading } from './AsyncState'
import { useRecordedRequestForm } from './useRecordedRequestForm'

export function RequestFormAnswers({ form }: { form: RequestFormSnapshotView }) {
  const answers = new Map(form.answers.map(answer => [answer.questionId, answer.value]))
  return <section className="recorded-request-form" aria-label="Submitted governance answers">
    <h3>Submitted answers <Badge appearance="outline">Recorded form v{form.revision}</Badge></h3>
    <Text block>Historical evidence, unchanged by later form publication. Answers confer no authority.</Text>
    <dl>{form.questions.map(question => {
      const value = answers.get(question.id)
      const text = value === undefined ? 'Not provided' : question.kind === 'acknowledgment'
        ? value ? 'Acknowledged (self-reported)' : 'Not acknowledged'
        : question.kind === 'singleChoice' ? question.options.find(option => option.id === value)?.label ?? 'Recorded choice unavailable'
        : String(value)
      return <div key={question.id}><dt>{question.label}{question.required ? ' (required)' : ' (optional)'}</dt>
        <dd>{text}</dd></div>
    })}</dl>
    <Text>Acknowledgments are not proof of permission or data safety. Charge references are informational, not cost-center eligibility.</Text>
  </section>
}

export function RecordedRequestForm({ request }: { request: AccessRequest }) {
  const { hasForm, form, detail } = useRecordedRequestForm(request)
  if (!hasForm) return null
  if (form) return <RequestFormAnswers form={form} />
  if (detail.isError) return <><ErrorState error={detail.error} /><Button onClick={() => void detail.refetch()}>Retry recorded answers</Button></>
  if (detail.isSuccess) return <Text role="alert">Recorded answers are unavailable. Do not substitute today's form.</Text>
  return <Loading label="Loading recorded governance answers" />
}
