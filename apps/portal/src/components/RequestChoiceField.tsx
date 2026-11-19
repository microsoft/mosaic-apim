import { Checkbox, Field, Input, Select } from '@fluentui/react-components'
import { useId } from 'react'
import { answerLength, customAnswerMaxLength } from '../request-form'
import type { RequestChoiceAnswer, RequestQuestionView } from '../types'

// The colon is not allowed in stored option IDs.
const other = ':custom'

export function RequestChoiceField({ question, value, error, onChange }: {
  question: RequestQuestionView
  value?: RequestChoiceAnswer
  error?: string
  onChange: (value: RequestChoiceAnswer) => void
}) {
  const descriptionId = useId()
  const errorId = useId()
  const options = value?.optionIds ?? []
  const customText = value?.customText
  const customError = Boolean(error && customText !== undefined && (!customText.trim() || answerLength(customText) > customAnswerMaxLength))
  const choiceError = Boolean(error && !customError)
  const describedBy = choiceError ? `${descriptionId} ${errorId}` : descriptionId
  const multiple = question.kind === 'multipleChoice'
  return <fieldset className="governance-choice">
    <legend>{question.label}{question.required ? ' (required)' : ''}</legend>
    <div id={descriptionId} className="governance-choice-hint">
      {question.helpText && <p>{question.helpText}</p>}
      <p>{multiple ? 'Select all that apply.' : 'Select one answer.'}</p>
    </div>
    {multiple ? <div className="governance-choice-options">
      {question.options.map((option, index) => <Checkbox key={option.id} label={option.label} checked={options.includes(option.id)}
        aria-invalid={index === 0 && choiceError} aria-describedby={describedBy}
        onChange={(_, data) => onChange({
          optionIds: question.options.filter(item => item.id === option.id ? data.checked === true : options.includes(item.id)).map(item => item.id),
          customText,
        })} />)}
      {question.allowCustomAnswer && <Checkbox label="Other (write in)" checked={customText !== undefined}
        aria-describedby={describedBy}
        onChange={(_, data) => onChange({ optionIds: options, customText: data.checked === true ? '' : undefined })} />}
    </div> : <Select aria-label={question.label} value={customText !== undefined ? other : options[0] ?? ''}
      aria-invalid={choiceError} aria-describedby={describedBy}
      onChange={(_, data) => onChange(data.value === other ? { optionIds: [], customText: '' }
        : { optionIds: data.value ? [data.value] : [] })}>
      <option value="">Choose an answer</option>
      {question.options.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
      <option value={other}>Other (write in)</option>
    </Select>}
    {choiceError && <p id={errorId} role="alert" className="governance-choice-error">{error}</p>}
    {customText !== undefined && <Field label={`Custom answer for ${question.label}`} required
      hint={`Maximum ${customAnswerMaxLength} characters. No secrets or datasets.`}
      validationState={customError ? 'error' : 'none'} validationMessage={customError ? error : undefined}>
      <Input value={customText} aria-invalid={customError}
        onChange={(_, data) => onChange({ optionIds: options, customText: data.value })} />
    </Field>}
  </fieldset>
}
