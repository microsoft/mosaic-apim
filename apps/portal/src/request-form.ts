import type { RequestFormAnswer, RequestQuestionView } from './types'
import { ApiError } from './api'

export type RequestAnswerDraft = Record<string, RequestFormAnswer['value']>
export const customAnswerMaxLength = 200
export const requestFormWarning = 'Do not include secrets, credentials, personal data or sensitive datasets. Describe code and intended usage at a high level.'

const normalizeText = (value: string) => value.replace(/\r\n?/g, '\n').trim()
export const answerLength = (value: string) => Array.from(normalizeText(value)).length
export const isStructuredChoice = (question: RequestQuestionView) =>
  question.kind === 'multipleChoice' || question.kind === 'singleChoice' && question.allowCustomAnswer === true

export function requestFormErrorMessage(error: unknown): string {
  if (error instanceof ApiError && Array.isArray(error.body?.detail)) {
    const messages = error.body.detail.flatMap(item =>
      typeof item === 'object' && item !== null && 'msg' in item && typeof item.msg === 'string'
        ? [item.msg] : [])
    if (messages.length) return messages.join(' ')
  }
  return error instanceof Error ? error.message : 'The request could not be confirmed. Retry or contact an administrator.'
}

export function requestAnswerErrors(questions: RequestQuestionView[], values: RequestAnswerDraft) {
  let errors: Record<string, string> = {}
  for (const question of questions) {
    const value = Object.hasOwn(values, question.id) ? values[question.id] : undefined
    if (typeof value === 'object' && isStructuredChoice(question)) {
      if (value.customText !== undefined && !value.customText.trim())
        errors = { ...errors, [question.id]: 'Enter your custom answer or deselect Other.' }
      else if (value.customText !== undefined && answerLength(value.customText) > customAnswerMaxLength)
        errors = { ...errors, [question.id]: `Use at most ${customAnswerMaxLength} characters for Other.` }
      else if (question.required && !value.optionIds.length && value.customText === undefined)
        errors = { ...errors, [question.id]: 'Choose at least one answer.' }
    } else if (question.required && (value === undefined || value === '' ||
      typeof value === 'string' && !value.trim() || question.kind === 'acknowledgment' && value !== true)) {
      errors = { ...errors, [question.id]: question.kind === 'acknowledgment' ? 'Acknowledge this statement.'
        : isStructuredChoice(question) ? 'Choose at least one answer.' : 'Answer this required question.' }
    } else if (typeof value === 'string' && question.maxLength && answerLength(value) > question.maxLength) {
      errors = { ...errors, [question.id]: `Use at most ${question.maxLength} characters.` }
    }
  }
  return errors
}

export function requestAnswers(questions: RequestQuestionView[], values: RequestAnswerDraft): RequestFormAnswer[] {
  return questions.flatMap<RequestFormAnswer>(question => {
    const value = Object.hasOwn(values, question.id) ? values[question.id] : undefined
    if (typeof value === 'object') {
      if (!value.optionIds.length && value.customText === undefined) return []
      return [{ questionId: question.id, value: {
        optionIds: value.optionIds,
        ...(value.customText !== undefined && { customText: normalizeText(value.customText) }),
      } }]
    }
    return value === undefined || typeof value === 'string' && !value.trim()
      ? [] : [{ questionId: question.id, value }]
  })
}

export function compatibleRequestAnswers(
  before: RequestQuestionView[], after: RequestQuestionView[], values: RequestAnswerDraft,
): RequestAnswerDraft {
  const previous = new Map(before.map(question => [question.id, question]))
  let kept: RequestAnswerDraft = {}
  for (const question of after) {
    const old = previous.get(question.id)
    const value = Object.hasOwn(values, question.id) ? values[question.id] : undefined
    if (!old || old.kind !== question.kind || value === undefined) continue
    if (question.kind === 'acknowledgment' && (old.label !== question.label || old.helpText !== question.helpText)) continue
    if (question.kind === 'singleChoice' || question.kind === 'multipleChoice') {
      if (old.label !== question.label || old.helpText !== question.helpText || typeof value === 'boolean') continue
      const { optionIds, customText } = typeof value === 'string' ? { optionIds: [value], customText: undefined } : value
      const sameOptions = optionIds.every(id => {
        const option = old.options.find(item => item.id === id)
        return option && question.options.some(item => item.id === id && item.label === option.label)
      })
      if (!sameOptions || customText !== undefined && (!question.allowCustomAnswer ||
        !customText.trim() || answerLength(customText) > customAnswerMaxLength)) continue
      if (isStructuredChoice(question)) kept = { ...kept, [question.id]: { optionIds, customText } }
      else if (optionIds.length === 1) kept = { ...kept, [question.id]: optionIds[0] }
    } else if (!(typeof value === 'string' && question.maxLength && answerLength(value) > question.maxLength)) {
      kept = { ...kept, [question.id]: value }
    }
  }
  return kept
}
